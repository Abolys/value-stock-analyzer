"""The shared breakpoint helper and SECTOR_CYCLICALITY against real yfinance names."""

import json
import logging

import pytest

import config
from signals.cyclicality import cyclicality
from signals.mapping import MappingStep, map_linear, map_step, mapped, mapping_line

BP = [(-0.30, 2), (0.0, 5), (0.30, 7.5), (0.60, 10)]  # QUANT_UPSIDE_BREAKPOINTS defaults


@pytest.mark.parametrize("x,expected", [
    (-0.15, 3.5),   # between points: halfway from 2 to 5
    (0.18, 6.5),    # the spec's example: +18% → 6.5
    (0.0, 5.0),     # at a point
    (0.30, 7.5),    # at a point
    (-0.90, 2.0),   # beyond the low end: flat
    (2.00, 10.0),   # beyond the high end: flat
])
def test_linear_interpolation(x, expected):
    assert map_linear(BP, x) == pytest.approx(expected)


def test_step_mapping_for_runway():
    bp = config.RUNWAY_BREAKPOINTS
    assert [map_step(bp, m) for m in (5, 12, 23.9, 24, 35, 36, 120)] == [1, 3, 3, 4, 4, 5, 5]


def test_mapping_clamps_to_score_range():
    assert map_linear([(0, -3), (1, 20)], 0) == config.SCORE_MIN
    assert map_linear([(0, -3), (1, 20)], 1) == config.SCORE_MAX


def test_mapping_line_shows_every_step():
    steps = [mapped("DCF upside", 0.18, BP, "+18%"),
             MappingStep(name="ROIC spread", input_display="+6 pts", output=1.0, kind="adjust")]
    assert mapping_line(steps, 7.5) == "DCF upside +18% → 6.5; ROIC spread +6 pts → +1; score 7.5"


def test_every_fixture_sector_resolves_to_a_real_entry():
    for info_path in sorted(config.FIXTURES_DIR.glob("*/info.json")):
        info = json.loads(info_path.read_text())
        if info.get("quoteType") == "ETF" or "sector" not in info:
            continue
        assert info["sector"] in config.SECTOR_CYCLICALITY, f"{info_path.parent.name}: {info['sector']!r}"
        c = cyclicality(info["sector"], info.get("industry"))
        assert c.source != "default", info_path.parent.name


def test_golden_sectors_are_all_covered():
    for t in config.GOLDEN_TICKERS:
        info = json.loads((config.FIXTURES_DIR / t / "info.json").read_text())
        assert cyclicality(info["sector"], info["industry"]).source in ("sector", "industry override")


def test_industry_override_beats_sector():
    assert config.SECTOR_CYCLICALITY["Industrials"] == 5
    c = cyclicality("Industrials", "Airlines")
    assert c.score == 3 and c.source == "industry override"


def test_unknown_sector_is_logged(caplog):
    with caplog.at_level(logging.WARNING, logger="signals.cyclicality"):
        c = cyclicality("Space Mining", "Asteroids")
    assert c.score == config.CYCLICALITY_DEFAULT_SCORE and c.source == "default"
    assert "Space Mining" in caplog.text
