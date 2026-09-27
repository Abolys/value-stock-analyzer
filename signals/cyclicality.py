"""Cyclical exposure (SPEC "Score mapping — Macro"): INDUSTRY_CYCLICALITY_OVERRIDES
take precedence over SECTOR_CYCLICALITY, both keyed on yfinance's own names.
A sector or industry in neither table scores CYCLICALITY_DEFAULT_SCORE and is
logged so the tables can be extended."""

from __future__ import annotations

import logging

from pydantic import BaseModel

import config

log = logging.getLogger(__name__)


class Cyclicality(BaseModel):
    score: float
    source: str  # "industry override" | "sector" | "default"
    detail: str


def cyclicality(sector: str | None, industry: str | None) -> Cyclicality:
    if industry and industry in config.INDUSTRY_CYCLICALITY_OVERRIDES:
        s = config.INDUSTRY_CYCLICALITY_OVERRIDES[industry]
        return Cyclicality(score=s, source="industry override", detail=f"industry {industry!r} → {s}")
    if sector and sector in config.SECTOR_CYCLICALITY:
        s = config.SECTOR_CYCLICALITY[sector]
        return Cyclicality(score=s, source="sector", detail=f"sector {sector!r} → {s}")
    log.warning("Cyclicality: sector %r / industry %r not in SECTOR_CYCLICALITY or INDUSTRY_CYCLICALITY_OVERRIDES; "
                "default %s used", sector, industry, config.CYCLICALITY_DEFAULT_SCORE)
    return Cyclicality(score=config.CYCLICALITY_DEFAULT_SCORE, source="default",
                       detail=f"sector {sector!r} / industry {industry!r} not in the tables; "
                              f"default {config.CYCLICALITY_DEFAULT_SCORE} (logged)")
