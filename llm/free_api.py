"""Free-tier LLM backend: an OpenAI-compatible chat-completions endpoint (CLAUDE.md Rule 4).

`FreeAPI` exposes the same `.messages.create(**params)` call the Anthropic SDK does, so
LLMClient's cache, validation, retry, evidence rule and logging are unchanged. Groq's free
dev tier is the default (FREE_LLM_BASE_URL / FREE_LLM_MODEL); any OpenAI-compatible endpoint
(Gemini's OpenAI-compatible API, OpenRouter, ...) works by pointing the two at it.

The JSON schema goes in the system prompt instead of a provider-native structured-output
field, so every endpoint behaves the same; the app's validation plus the one retry handle
the rest. Calls are free, so the billed cost logged is $0 (backend "free"). A
transport-level failure (bad auth, rate limit, outage, timeout, no answer) raises
FreeTierError; LLMClient catches it and, when the Anthropic API is configured, retries the
call there.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import requests

import config
from llm import claude_code  # strip_text_format: make the schema plain JSON Schema for the prompt

BACKEND = "free"


class FreeTierError(Exception):
    """The free-tier endpoint failed: bad auth, rate limit, outage, timeout, or no answer."""


class FreeAPI:
    """One OpenAI-compatible chat-completions endpoint behind the Anthropic-style surface."""

    backend_name = BACKEND

    def __init__(self, api_key: str, base_url: str | None = None, model: str | None = None,
                 timeout: float | None = None, session=None):
        self.api_key = api_key
        self.base_url = (base_url or config.FREE_LLM_BASE_URL).rstrip("/")
        self.model = model or config.FREE_LLM_MODEL
        self.timeout = timeout or config.FREE_LLM_TIMEOUT_SECONDS
        self.messages = self
        self._session = session

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        s = self._session or requests
        try:
            resp = s.post(f"{self.base_url}/chat/completions",
                          headers={"Authorization": f"Bearer {self.api_key}",
                                   "Content-Type": "application/json"},
                          json=body, timeout=self.timeout)
        except requests.RequestException as exc:
            raise FreeTierError(f"free-tier request failed: {type(exc).__name__}: {exc}") from exc
        if resp.status_code != 200:
            raise FreeTierError(f"free-tier HTTP {resp.status_code}: {getattr(resp, 'text', '')[:300]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise FreeTierError(f"free-tier response is not JSON: {exc}") from exc

    def create(self, **params: Any) -> SimpleNamespace:
        """The Anthropic-style params → one chat-completions call → an Anthropic-shaped result."""
        schema = claude_code.strip_text_format(params["output_config"]["format"]["schema"])
        system = (params["system"]
                  + "\n\nRespond with a JSON object only, matching this JSON schema:\n"
                  + json.dumps(schema, sort_keys=True))
        body: dict[str, Any] = {
            "model": params["model"],
            "max_tokens": params["max_tokens"],
            "messages": [{"role": "system", "content": system}, *params["messages"]],
        }
        if "temperature" in params:
            body["temperature"] = params["temperature"]
        data = self._post(body)
        try:
            choice = data["choices"][0]
            text = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise FreeTierError(f"free-tier response has no answer: {str(data)[:300]}") from exc
        usage = data.get("usage") or {}
        finish = choice.get("finish_reason") or ""
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text or "")],
            usage=SimpleNamespace(input_tokens=int(usage.get("prompt_tokens") or 0),
                                  output_tokens=int(usage.get("completion_tokens") or 0)),
            stop_reason="max_tokens" if finish == "length" else "end_turn",
            stop_details=None,
            backend=BACKEND,
            billed_cost=0.0,    # nothing is billed on the free tier (LLMClient reads these)
            list_price_cost=0.0,
        )
