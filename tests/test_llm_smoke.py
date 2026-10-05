"""Live smoke test for the LLM backend (CLAUDE.md Rule 4): one tiny call to the
configured backend — the Anthropic API when ANTHROPIC_API_KEY is set, otherwise the
Claude Code CLI — to verify key, model and network before relying on the lenses.

    pytest -m live tests/test_llm_smoke.py

Excluded from the default run (pytest.ini: -m "not live"). Costs a fraction of a
cent; the call is logged to a throwaway database so it never pollutes the real
monthly spend.
"""

import pytest
from pydantic import BaseModel

from llm.cache import LLMCache
from llm.client import LLMClient


class _Pong(BaseModel):
    word: str


@pytest.mark.live
def test_one_call_reaches_the_backend(tmp_path):
    llm = LLMClient(cache=LLMCache(tmp_path / "smoke_cache.db"), db_path=tmp_path / "smoke.db")
    if not llm.configured:
        pytest.skip("no LLM backend: set ANTHROPIC_API_KEY or install the Claude Code CLI")
    result = llm.run(ticker="SMOKE", lens="smoke", prompt_version="smoke-v1",
                     system="You answer with JSON matching the schema and nothing else.",
                     user='Reply with exactly "pong" as the value of word.',
                     schema=_Pong, cache_key="smoke-v1")
    assert result.ok and result.status == "ok"
    assert result.data is not None and result.data["word"].lower() == "pong"
    assert not result.cache_hit and result.attempts >= 1 and result.input_tokens > 0
    assert result.cost >= 0
