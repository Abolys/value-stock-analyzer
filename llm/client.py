"""Anthropic API client for the LLM lenses (CLAUDE.md Rule 4).

- Model from ANTHROPIC_MODEL; never hard-coded.
- Lowest temperature the model supports: LLM_TEMPERATURE is sent only to models
  that accept sampling parameters (LLM_NO_SAMPLING_MODEL_PREFIXES reject it
  with a 400, so it is omitted there; the response cache then keeps scores stable).
- JSON matching a pydantic schema via output_config.format; every response is
  validated against the model plus a lens-specific check (the evidence rule).
  On failure: one retry with the validation error fed back, then
  "Insufficient data - <error>".
- Responses are cached on disk by (ticker, lens, payload hash, prompt version).
- Every call is logged with input and output tokens and estimated cost;
  cache hits are logged at zero cost.
- Backend (LLM_BACKEND, default "auto"): the free tier (llm/free_api.py,
  FREE_LLM_API_KEY) first, logged at a billed cost of $0 with backend "free"; a failed
  free-tier call (bad auth, rate limit, outage) falls back to the Anthropic API when
  ANTHROPIC_API_KEY is set. The app enables that fallback only for the owner; a shared
  viewer's client is built with paid_fallback=False, so it stays on the free tier and a
  failed call shows the lens as not available (nothing is billed). Else the Anthropic API;
  else the Claude Code CLI on the
  user's subscription (llm/claude_code.py), logged with a billed cost of $0 and its
  list-price estimate. None available → "Insufficient data - LLM not configured", no call.

The `api` object is injectable (anything with `.messages.create(**params)`),
so tests mock it and never reach the network.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Callable

import anthropic
from pydantic import BaseModel, ValidationError

import config
from llm import claude_code, free_api
from llm.cache import LLMCache, payload_hash
from storage.llm_store import LLMCallRecord, log_call

log = logging.getLogger(__name__)

INSUFFICIENT = "Insufficient data"
NOT_CONFIGURED = (f"{INSUFFICIENT} - LLM not configured (no FREE_LLM_API_KEY, no ANTHROPIC_API_KEY "
                  "and no Claude Code CLI found; see LLM_BACKEND / CLAUDE_CODE_CLI)")
API = "api"

Validator = Callable[[BaseModel], list[str]]


class LLMResult(BaseModel):
    ok: bool
    status: str = "ok"
    data: dict[str, Any] | None = None
    cache_hit: bool = False
    attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0  # billed API cost ($0 on the Claude Code subscription backend)
    list_price_cost: float = 0.0
    errors: list[str] = []


def cost_of(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens * config.LLM_PRICE_PER_MTOK_IN + output_tokens * config.LLM_PRICE_PER_MTOK_OUT) \
        / config.TOKENS_PER_MTOK


def supports_temperature(model: str) -> bool:
    return not any(model.startswith(p) for p in config.LLM_NO_SAMPLING_MODEL_PREFIXES)


def output_schema(schema: type[BaseModel]) -> dict[str, Any]:
    return {"type": "json_schema", "schema": anthropic.transform_schema(schema)}


def choose_backend(api_key: str | None = None, backend: str | None = None,
                   free_key: str | None = None) -> tuple[Any, str]:
    """(api object or None, backend name) per LLM_BACKEND: the free tier with FREE_LLM_API_KEY,
    else the Anthropic API with a key, else the Claude Code CLI. The free tier's API fallback
    is decided in LLMClient, so it stays out of the forced-backend choices."""
    backend = backend or config.LLM_BACKEND
    if backend not in config.LLM_BACKENDS:
        raise ValueError(f"LLM_BACKEND must be one of {config.LLM_BACKENDS}, got {backend!r}")
    fkey = free_key if free_key is not None else os.getenv("FREE_LLM_API_KEY", config.FREE_LLM_API_KEY)
    if backend in ("auto", free_api.BACKEND) and fkey:
        return free_api.FreeAPI(fkey), free_api.BACKEND
    key = api_key if api_key is not None else os.getenv("ANTHROPIC_API_KEY", config.ANTHROPIC_API_KEY)
    if backend in ("auto", API) and key:
        return anthropic.Anthropic(api_key=key, timeout=config.LLM_TIMEOUT_SECONDS), API
    if backend in ("auto", claude_code.BACKEND):
        cli = claude_code.find_cli()
        if cli:
            return claude_code.ClaudeCodeAPI(cli), claude_code.BACKEND
    return None, "none"


class LLMClient:
    def __init__(self, api: Any = None, model: str | None = None, cache: LLMCache | None = None,
                 db_path: Any = None, api_key: str | None = None, analysis_id: int | None = None,
                 record: Callable[[LLMCallRecord], None] | None = None, backend: str | None = None,
                 paid_fallback: bool | None = None):
        if api is None:
            api, self.backend = choose_backend(api_key, backend)
        else:
            self.backend = getattr(api, "backend_name", API)
        self.api = api
        self.model = model or (config.FREE_LLM_MODEL if self.backend == free_api.BACKEND
                               else config.ANTHROPIC_MODEL)
        # When the primary is the free tier and the Anthropic API is also configured, a failed
        # free-tier call (bad auth, rate limit, outage) is retried on the API, which is billed.
        # paid_fallback=False (a shared viewer's session) keeps it on the free tier only: a
        # failed call fails the analysis instead of silently spending the owner's API key.
        self._fallback: tuple[Any, str] | None = None
        if self.backend == free_api.BACKEND and paid_fallback is not False:
            key = os.getenv("ANTHROPIC_API_KEY", config.ANTHROPIC_API_KEY)
            if key:
                self._fallback = (anthropic.Anthropic(api_key=key, timeout=config.LLM_TIMEOUT_SECONDS), API)
        self.cache = cache if cache is not None else LLMCache()
        self.db_path = db_path
        self.analysis_id = analysis_id
        self._record = record
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return self.api is not None

    # ------------------------------------------------------------------
    def request_params(self, system: str, messages: list[dict[str, Any]], schema: type[BaseModel]) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": config.LLM_MAX_TOKENS,
            "system": system,
            "messages": messages,
            "output_config": {"format": output_schema(schema)},
        }
        if config.LLM_EFFORT:  # None for models without effort support (e.g. Haiku 4.5)
            params["output_config"]["effort"] = config.LLM_EFFORT
        if supports_temperature(self.model):
            params["temperature"] = config.LLM_TEMPERATURE
        return params

    def _log(self, rec: LLMCallRecord) -> None:
        rec.analysis_id = self.analysis_id
        with self._lock:
            if self._record is not None:
                self._record(rec)
            else:
                log_call(rec, self.db_path)

    # ------------------------------------------------------------------
    def _backends(self) -> list[tuple[Any, str, str]]:
        """(api, backend name, model) in priority order: the configured primary plus, when it
        is the free tier and an API key is set, the billed Anthropic API fallback."""
        out = [(self.api, self.backend, self.model)]
        if self._fallback is not None:
            out.append((self._fallback[0], self._fallback[1], config.ANTHROPIC_MODEL))
        return out

    def run(self, *, ticker: str, lens: str, prompt_version: str, system: str, user: str,
            schema: type[BaseModel], cache_key: str, validate: Validator | None = None) -> LLMResult:
        """One structured call with cache, validation, one retry and cost logging.

        When the primary backend is the free tier and it fails with a transport error
        (bad auth, rate limit, outage, timeout), the call is retried on the Anthropic API
        fallback, which is billed. A schema/evidence rejection retries on the same backend;
        the paid fallback is only for transport failures.

        cache_key: the canonical input (payload JSON plus any data-block text, or
        an accession number) that identifies this request for the cache.
        """
        key_hash = payload_hash(self.model, cache_key)
        cached = self.cache.get(ticker, lens, key_hash, prompt_version)
        if cached is not None:
            data = json.loads(cached)
            self._log(LLMCallRecord(ticker=ticker, lens=lens, model=self.model, prompt_version=prompt_version,
                                    cache_hit=True, outcome="ok (cache hit)", backend=self.backend))
            return LLMResult(ok=True, data=data, cache_hit=True)
        if not self.configured:
            return LLMResult(ok=False, status=NOT_CONFIGURED)

        result = LLMResult(ok=False)
        transport_error = False
        for api, name, model in self._backends():
            messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
            for attempt in range(1, config.LLM_VALIDATION_RETRIES + 2):
                result.attempts = attempt
                params = self.request_params(system, messages, schema)
                params["model"] = model
                try:
                    resp = api.messages.create(**params)
                except (anthropic.APIError, claude_code.ClaudeCodeError,
                        free_api.FreeTierError) as exc:  # the SDK already retried 429/5xx
                    msg = f"API error: {type(exc).__name__}: {getattr(exc, 'message', exc)}"
                    if self._fallback is not None:
                        msg += " (falling back to the Anthropic API)"
                    self._log(LLMCallRecord(ticker=ticker, lens=lens, model=model, prompt_version=prompt_version,
                                            attempt=attempt, outcome=f"error: {msg}"[:500], backend=name))
                    result.errors.append(msg)
                    result.status = f"{INSUFFICIENT} - {msg}"
                    transport_error = True
                    break  # to the next backend, if any
                in_tok = int(getattr(resp.usage, "input_tokens", 0) or 0)
                out_tok = int(getattr(resp.usage, "output_tokens", 0) or 0)
                list_price = getattr(resp, "list_price_cost", None)
                list_price = cost_of(in_tok, out_tok) if list_price is None else float(list_price)
                billed = getattr(resp, "billed_cost", None)
                call_cost = list_price if billed is None else float(billed)
                result.input_tokens += in_tok
                result.output_tokens += out_tok
                result.cost += call_cost
                result.list_price_cost += list_price
                text, problems = self._check(resp, schema, validate)
                outcome = "ok" if not problems else "invalid: " + "; ".join(problems)
                self._log(LLMCallRecord(ticker=ticker, lens=lens, model=model, prompt_version=prompt_version,
                                        input_tokens=in_tok, output_tokens=out_tok, cost=call_cost, attempt=attempt,
                                        outcome=outcome[:500], backend=name, list_price_cost=list_price))
                if not problems:
                    parsed = schema.model_validate_json(text)
                    result.ok, result.status, result.data = True, "ok", parsed.model_dump(mode="json")
                    self.cache.put(ticker, lens, key_hash, prompt_version, model, json.dumps(result.data))
                    return result
                result.errors.extend(problems)
                log.info("%s %s attempt %d rejected: %s", ticker, lens, attempt, problems)
                if getattr(resp, "stop_reason", None) == "refusal":
                    break
                messages = [*messages, {"role": "assistant", "content": text or "(no text)"},
                            {"role": "user", "content": "Your previous answer was rejected: " + "; ".join(problems)
                                                        + ". Answer again, following every rule and the JSON schema."}]
        if not transport_error:
            result.status = f"{INSUFFICIENT} - response rejected after {result.attempts} attempt(s): " \
                            + "; ".join(result.errors[-3:])
        return result

    @staticmethod
    def _check(resp: Any, schema: type[BaseModel], validate: Validator | None) -> tuple[str, list[str]]:
        stop = getattr(resp, "stop_reason", None)
        if stop == "refusal":
            details = getattr(resp, "stop_details", None)
            return "", [f"model refused ({getattr(details, 'category', None) or 'no category'})"]
        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "")
        if stop == "max_tokens":
            return text, ["response truncated at max_tokens"]
        try:
            parsed = schema.model_validate_json(text)
        except ValidationError as exc:
            return text, [f"schema validation failed: {exc.errors()[0]['msg']} at "
                          f"{'.'.join(str(x) for x in exc.errors()[0]['loc']) or 'root'}"]
        return text, (validate(parsed) if validate else [])


# --------------------------------------------------------------------------
# Evidence rule (SPEC "Business Moat" / "Devil's Advocate")
# --------------------------------------------------------------------------
def payload_fields(payload: dict[str, Any], prefix: str = "") -> set[str]:
    """Every field path in the payload: top-level keys, dotted nested paths and nested leaf names."""
    out: set[str] = set()
    for k, v in payload.items():
        path = f"{prefix}{k}"
        out.add(path)
        if prefix:
            out.add(k)
        if isinstance(v, dict):
            out |= payload_fields(v, f"{path}.")
    return out


def evidence_problems(evidence: list[Any], payload: dict[str, Any]) -> list[str]:
    fields = payload_fields(payload)
    cited = {e.field.strip() for e in evidence if e.field.strip() in fields}
    unknown = [e.field for e in evidence if e.field.strip() not in fields]
    problems = []
    if len(cited) < config.LLM_MIN_EVIDENCE_FACTS:
        problems.append(f"cites {len(cited)} payload fact(s); at least {config.LLM_MIN_EVIDENCE_FACTS} required"
                        + (f" (not in the payload: {', '.join(unknown[:5])})" if unknown else ""))
    return problems
