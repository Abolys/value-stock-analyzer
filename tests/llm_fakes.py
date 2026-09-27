"""A fake Anthropic API for offline tests: records every request and answers by
the requested output schema. No test ever reaches the real API."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from typing import Any, Callable

MOAT_OK = {"sector_threat": "private-label erosion", "threat_reasoning": "apparel brands compete on brand heat",
           "advantages": ["brand"], "pricing_power": "moderate", "score": 6.0,
           "evidence": [{"field": "gross_margin_ttm", "value": "58%", "why": "high margins"},
                        {"field": "sector", "value": "Consumer Cyclical", "why": "discretionary demand"}],
           "strongest_bull_point": "brand premium", "biggest_risk": "competition", "rationale": "Moderate moat."}
DA_OK = {"weakest_valuation_assumption": "stage-1 growth", "weakest_moat_point": "brand heat",
         "accounting_red_flags": "none obvious", "implied_growth_view": "fair", "implied_growth_reasoning": "ok",
         "insider_activity": "no buying", "dividend_risk": "no dividend", "asset_floor": "thin",
         "leadership_turnover": "coverage partial; unknown", "leadership_status": "unknown",
         "impairment_type": "cyclical", "impairment_reasoning": "demand cycle", "data_freshness": "current",
         "bull_case_requirements": ["margins recover"], "score": 5.0,
         "evidence": [{"field": "dcf_implied_upside", "value": "+10%", "why": "modest upside"},
                      {"field": "piotroski", "value": "5 / 9", "why": "average quality"}],
         "rationale": "Bull case holds if margins recover."}
DEPARTURE_OK = {"departure": True, "role": "CEO", "person": "Peter Grant", "effective_date": "2026-06-30",
                "explanation": "The CEO will step down."}
DEFAULTS = {"MoatResponse": MOAT_OK, "DevilsAdvocateResponse": DA_OK, "DepartureResponse": DEPARTURE_OK}


def response(data: dict | str, input_tokens: int = 1000, output_tokens: int = 200, stop_reason: str = "end_turn"):
    text = data if isinstance(data, str) else json.dumps(data)
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)],
                           usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
                           stop_reason=stop_reason, stop_details=None)


def schema_title(params: dict[str, Any]) -> str:
    return params["output_config"]["format"]["schema"].get("title", "")


class FakeAPI:
    """`responder(params) -> response` or a dict schema-title → answer (dict) or list of answers (per call)."""

    def __init__(self, answers: dict[str, Any] | Callable | None = None, delay: Callable | None = None):
        self.requests: list[dict[str, Any]] = []
        self.messages = self
        self._answers = answers if answers is not None else {}
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()
        self.delay = delay

    def create(self, **params):
        with self._lock:
            self.requests.append(params)
        if self.delay:
            self.delay(params)
        if callable(self._answers):
            return self._answers(params)
        title = schema_title(params)
        ans = self._answers.get(title, DEFAULTS.get(title))
        if isinstance(ans, list):
            with self._lock:
                i = self._counts.get(title, 0)
                self._counts[title] = i + 1
            ans = ans[min(i, len(ans) - 1)]
        return ans if isinstance(ans, SimpleNamespace) else response(ans)

    def titles(self) -> list[str]:
        return [schema_title(p) for p in self.requests]
