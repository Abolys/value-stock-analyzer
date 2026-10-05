"""Free-tier LLM backend (CLAUDE.md Rule 4): the OpenAI-compatible adapter's request and
response shapes, and LLMClient's free-tier → Anthropic API fallback. Offline: the HTTP layer
and the Anthropic API are mocked, so no test reaches a real provider.

Free-tier calls log at $0 with backend "free"; a fallback call is billed and logged with
backend "api". A schema/evidence rejection retries on the same free-tier backend — the paid
fallback is only for transport failures (bad auth, rate limit, outage, timeout).
"""

from __future__ import annotations

import json

import pytest
import requests

import config
from llm import free_api
from llm.client import LLMClient, choose_backend
from llm.schemas import MoatResponse
from storage import llm_store
from tests.llm_fakes import MOAT_OK, FakeAPI


class _Resp:
    def __init__(self, payload=None, status_code=200, text=""):
        self.payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        return self.payload


def _payload(text, prompt_tokens=10, completion_tokens=5, finish="stop"):
    return {"choices": [{"message": {"content": text}, "finish_reason": finish}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}}


class _Session:
    """Records posts; answers in order from its response list."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if not self.responses:
            raise RuntimeError("unexpected free-tier call")
        return self.responses.pop(0)


def _params(**over):
    p = {"model": config.FREE_LLM_MODEL, "max_tokens": 100, "system": "You are a moat analyst.",
         "temperature": 0.0, "messages": [{"role": "user", "content": "score it"}],
         "output_config": {"format": {"type": "json_schema", "schema": {"title": "MoatResponse",
                                                                        "type": "object"}}}}
    p.update(over)
    return p


def test_adapter_request_shape_and_response():
    sess = _Session([_Resp(_payload(json.dumps(MOAT_OK)))])
    api = free_api.FreeAPI("sk-free", session=sess)
    resp = api.messages.create(**_params())
    url, kw = sess.posts[0]
    assert url.endswith("/chat/completions")
    assert kw["headers"]["Authorization"] == "Bearer sk-free"
    body = kw["json"]
    assert body["model"] == config.FREE_LLM_MODEL
    assert body["max_tokens"] == 100
    assert body["temperature"] == 0.0
    assert body["messages"][0]["role"] == "system"
    assert "MoatResponse" in body["messages"][0]["content"]  # the schema is in the prompt
    assert "JSON schema" in body["messages"][0]["content"]
    assert body["messages"][1] == {"role": "user", "content": "score it"}
    assert resp.content[0].type == "text"
    assert json.loads(resp.content[0].text) == MOAT_OK
    assert (resp.usage.input_tokens, resp.usage.output_tokens) == (10, 5)
    assert resp.stop_reason == "end_turn"
    assert resp.billed_cost == 0.0 and resp.list_price_cost == 0.0  # free: nothing is billed


def test_length_finish_maps_to_max_tokens():
    sess = _Session([_Resp(_payload("partial", finish="length"))])
    api = free_api.FreeAPI("sk-free", session=sess)
    resp = api.messages.create(**_params())
    assert resp.stop_reason == "max_tokens"


def test_transport_failures_raise_free_tier_error():
    for bad in (_Resp(status_code=429, text="rate limited"),
                _Resp(payload={"choices": []})):
        api = free_api.FreeAPI("sk-free", session=_Session([bad]))
        with pytest.raises(free_api.FreeTierError):
            api.messages.create(**_params())

    class _Broken:
        @staticmethod
        def post(*_args, **_kwargs):
            raise requests.ConnectionError("connection refused")

    api = free_api.FreeAPI("sk-free", session=_Broken())
    with pytest.raises(free_api.FreeTierError) as exc:
        api.messages.create(**_params())
    assert "ConnectionError" in str(exc.value)


def test_choose_backend_order(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant")
    api, backend = choose_backend(backend="auto", free_key="sk-free", api_key="sk-ant")
    assert backend == "free" and isinstance(api, free_api.FreeAPI)
    api, backend = choose_backend(backend="api", free_key="sk-free", api_key="sk-ant")
    assert backend == "api"  # forced backends stay forced
    api, backend = choose_backend(backend="free", free_key="sk-free", api_key="sk-ant")
    assert backend == "free"
    api, backend = choose_backend(backend="auto", free_key="", api_key="sk-ant")
    assert backend == "api"
    monkeypatch.setattr("llm.claude_code.find_cli", lambda: None)
    api, backend = choose_backend(backend="auto", free_key="", api_key="")
    assert (api, backend) == (None, "none")
    api, backend = choose_backend(backend="none", free_key="sk-free", api_key="sk-ant")
    assert (api, backend) == (None, "none")
    with pytest.raises(ValueError):
        choose_backend(backend="gemini", free_key="", api_key="")


def test_free_failure_falls_back_to_the_api(tmp_path, monkeypatch):
    monkeypatch.setenv("FREE_LLM_API_KEY", "sk-free")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setattr(config, "LLM_BACKEND", "auto")
    llm = LLMClient(db_path=tmp_path / "runs.db")
    assert llm.backend == "free" and llm.model == config.FREE_LLM_MODEL
    assert llm._fallback is not None
    # primary free tier: a rate limit on the call
    llm.api = free_api.FreeAPI("sk-free",
                               session=_Session([_Resp(status_code=429, text="rate limited")]))
    fake = FakeAPI({"MoatResponse": MOAT_OK})
    llm._fallback = (fake, "api")  # stands in for the billed Anthropic API
    result = llm.run(ticker="T", lens="moat", prompt_version="moat-v1", system="s", user="u",
                     schema=MoatResponse, cache_key="k")
    assert result.ok and result.status == "ok"
    assert result.data["score"] == 6.0
    assert result.cost > 0  # the fallback call is billed at Anthropic list price
    assert fake.requests and fake.requests[0]["model"] == config.ANTHROPIC_MODEL
    recs = llm_store.calls(path=tmp_path / "runs.db")
    assert [r.backend for r in recs] == ["free", "api"]
    assert "429" in recs[0].outcome and "falling back" in recs[0].outcome
    assert recs[0].cost == 0.0 and recs[1].cost > 0
    assert recs[1].model == config.ANTHROPIC_MODEL
    # the answer is cached: a repeat is a $0 cache hit, no further provider calls
    again = llm.run(ticker="T", lens="moat", prompt_version="moat-v1", system="s", user="u",
                    schema=MoatResponse, cache_key="k")
    assert again.cache_hit and again.cost == 0.0
    assert len(fake.requests) == 1


def test_free_success_costs_nothing_and_skips_the_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("FREE_LLM_API_KEY", "sk-free")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setattr(config, "LLM_BACKEND", "auto")
    llm = LLMClient(db_path=tmp_path / "runs.db")
    llm.api = free_api.FreeAPI("sk-free", session=_Session([_Resp(_payload(json.dumps(MOAT_OK)))]))
    fake = FakeAPI({"MoatResponse": MOAT_OK})
    llm._fallback = (fake, "api")
    result = llm.run(ticker="T", lens="moat", prompt_version="moat-v1", system="s", user="u",
                     schema=MoatResponse, cache_key="k")
    assert result.ok and result.cost == 0.0
    assert fake.requests == []
    recs = llm_store.calls(path=tmp_path / "runs.db")
    assert [r.backend for r in recs] == ["free"] and recs[0].cost == 0.0


def test_no_fallback_without_an_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("FREE_LLM_API_KEY", "sk-free")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(config, "LLM_BACKEND", "auto")
    llm = LLMClient(db_path=tmp_path / "runs.db")
    assert llm.backend == "free" and llm._fallback is None
    llm.api = free_api.FreeAPI("sk-free",
                               session=_Session([_Resp(status_code=429, text="rate limited")]))
    result = llm.run(ticker="T", lens="moat", prompt_version="moat-v1", system="s", user="u",
                     schema=MoatResponse, cache_key="k")
    assert not result.ok
    assert "Insufficient data" in result.status and "429" in result.status
    assert "falling back" not in result.status
    recs = llm_store.calls(path=tmp_path / "runs.db")
    assert len(recs) == 1 and recs[0].backend == "free"


def test_schema_rejection_retries_on_the_free_backend_not_the_api(tmp_path, monkeypatch):
    monkeypatch.setenv("FREE_LLM_API_KEY", "sk-free")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setattr(config, "LLM_BACKEND", "auto")
    llm = LLMClient(db_path=tmp_path / "runs.db")
    # the free model gets the schema wrong on the first try, then answers correctly
    llm.api = free_api.FreeAPI("sk-free",
                               session=_Session([_Resp(_payload("not json")),
                                                _Resp(_payload(json.dumps(MOAT_OK)))]) )
    fake = FakeAPI({"MoatResponse": MOAT_OK})
    llm._fallback = (fake, "api")
    result = llm.run(ticker="T", lens="moat", prompt_version="moat-v1", system="s", user="u",
                     schema=MoatResponse, cache_key="k")
    assert result.ok and result.cost == 0.0 and result.attempts == 2
    assert fake.requests == []  # the paid fallback is only for transport failures
    recs = llm_store.calls(path=tmp_path / "runs.db")
    assert [r.backend for r in recs] == ["free", "free"]
    assert "invalid" in recs[0].outcome and recs[1].outcome == "ok"


def test_forced_free_without_a_key_is_not_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("FREE_LLM_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setattr(config, "LLM_BACKEND", "free")
    llm = LLMClient(db_path=tmp_path / "runs.db")
    assert not llm.configured
    result = llm.run(ticker="T", lens="moat", prompt_version="moat-v1", system="s", user="u",
                     schema=MoatResponse, cache_key="k")
    assert not result.ok and "LLM not configured" in result.status

