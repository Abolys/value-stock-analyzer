"""LLM calibration (SPEC "Calibration"). Live: calls the configured LLM backend — the
Anthropic API when ANTHROPIC_API_KEY is set (billed), otherwise the Claude Code CLI on
the Claude subscription.

    pytest -m live tests/test_calibration.py

Scores the golden tickers with the current Moat and Devil's Advocate prompts
and saves tests/calibration/<moat version>__<da version>.json. Every payload is
built from the saved offline fixtures (tests/fixtures, EDGAR replay, the
fixture capture date as "today"), never live data, so only the prompt can
differ between calibration runs. A fresh response cache is used so the API is
really called. When a prompt version changes, the scores are compared with the
previous version's file and the test fails on any ticker whose score moved by
more than CALIBRATION_MAX_SHIFT, listing each shift. A deliberate re-rating is
accepted by committing the new file.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

import config
from analysis.devils_advocate import devils_advocate_lens
from analysis.inputs import load_inputs
from analysis.macro import macro_lens
from analysis.moat import moat_lens
from analysis.quant import quant_lens
from data.fixture_provider import captured_on, fixture_edgar, fixture_provider
from llm import prompts
from llm.cache import LLMCache
from llm.client import LLMClient
from screening.engine import ScreenContext

CALIBRATION_DIR = Path(__file__).resolve().parent / "calibration"
REAL_RUNS_DB = config.ROOT / "storage" / "runs.db"  # calibration spend counts toward the monthly total
LENSES = ("moat", "devils_advocate")


def version_key() -> str:
    return f"{prompts.PROMPT_VERSIONS['moat']}__{prompts.PROMPT_VERSIONS['devils_advocate']}"


def score_ticker(ticker: str, llm: LLMClient, db_path: Path) -> dict:
    ctx = ScreenContext(provider=fixture_provider(), db_path=db_path, today=captured_on(ticker), save_snapshots=False)
    x = load_inputs(ctx, ticker, edgar=fixture_edgar())
    quant, macro = quant_lens(x), macro_lens(x)
    moat = moat_lens(x, llm)
    da = devils_advocate_lens(x, quant, macro, moat, llm)
    return {
        "fixture_captured_on": captured_on(ticker).isoformat(),
        "moat": {"score": moat.score, "status": moat.status, "sector_threat": moat.sector_threat,
                 "evidence": [e.field for e in moat.evidence]},
        "devils_advocate": {"score": da.score, "status": da.status, "impairment_type": da.impairment_type,
                            "leadership_status": da.leadership_status, "evidence": [e.field for e in da.evidence]},
        "cost": round(moat.cost + da.cost, 6),
        "list_price_cost": round(moat.list_price_cost + da.list_price_cost, 6),
    }


def previous_file(current: Path) -> Path | None:
    others = []
    for p in CALIBRATION_DIR.glob("*.json"):
        if p == current:
            continue
        created = json.loads(p.read_text()).get("created_at", "")
        others.append((created, p))
    return max(others)[1] if others else None


def shifts(previous: dict, current: dict) -> list[str]:
    out = []
    for ticker, cur in current["tickers"].items():
        prev = previous["tickers"].get(ticker)
        if prev is None:
            continue
        for lens in LENSES:
            a, b = prev[lens]["score"], cur[lens]["score"]
            if a is None or b is None:
                if a != b:
                    out.append(f"{ticker} {lens}: {a} → {b} (a score appeared or disappeared)")
                continue
            if abs(b - a) > config.CALIBRATION_MAX_SHIFT:
                out.append(f"{ticker} {lens}: {a:.1f} → {b:.1f} (shift {b - a:+.1f} > {config.CALIBRATION_MAX_SHIFT})")
    return out


@pytest.mark.live
def test_calibration(tmp_path):
    llm = LLMClient(cache=LLMCache(tmp_path / "calibration_cache.db"), db_path=REAL_RUNS_DB)
    if not llm.configured:
        pytest.skip("no LLM backend: set ANTHROPIC_API_KEY or install the Claude Code CLI")
    results = {t: score_ticker(t, llm, tmp_path / "runs.db") for t in config.GOLDEN_TICKERS}
    failed = {t: {k: r[k]["status"] for k in LENSES if r[k]["status"] != "ok"} for t, r in results.items()}
    failed = {t: v for t, v in failed.items() if v}
    current = {"prompt_versions": dict(prompts.PROMPT_VERSIONS), "model": llm.model, "backend": llm.backend,
               "created_at": datetime.now().isoformat(timespec="seconds"),
               "total_cost": round(sum(r["cost"] for r in results.values()), 6), "tickers": results}
    CALIBRATION_DIR.mkdir(exist_ok=True)
    path = CALIBRATION_DIR / f"{version_key()}.json"
    prev_path = previous_file(path)
    path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
    assert not failed, f"LLM lenses without a validated score: {failed}"
    if prev_path is not None:
        moved = shifts(json.loads(prev_path.read_text()), current)
        assert not moved, (f"Scores moved more than CALIBRATION_MAX_SHIFT vs {prev_path.name}:\n" + "\n".join(moved)
                           + "\nCommit the new file to accept a deliberate re-rating.")


def test_calibration_builds_payloads_from_fixtures_only(tmp_path, monkeypatch):
    """Offline guard: calibration payloads come from fixtures (the socket block would fail any live fetch)."""
    from tests.llm_fakes import FakeAPI

    api = FakeAPI()
    llm = LLMClient(api=api, cache=LLMCache(tmp_path / "c.db"), db_path=tmp_path / "r.db")
    out = score_ticker("LULU", llm, tmp_path / "runs.db")
    assert out["fixture_captured_on"] == captured_on("LULU").isoformat()
    da_payload = json.loads(api.requests[1]["messages"][0]["content"].split("<payload>\n")[1].split("\n</payload>")[0])
    assert da_payload["fundamentals_as_of"] <= captured_on("LULU").isoformat()
    assert out["moat"]["score"] is not None and out["devils_advocate"]["score"] is not None


def test_shift_detection():
    prev = {"tickers": {"LULU": {"moat": {"score": 6.0}, "devils_advocate": {"score": 5.0}}}}
    cur = {"tickers": {"LULU": {"moat": {"score": 8.5}, "devils_advocate": {"score": 6.9}}}}
    moved = shifts(prev, cur)
    assert len(moved) == 1 and moved[0].startswith("LULU moat: 6.0 → 8.5")
