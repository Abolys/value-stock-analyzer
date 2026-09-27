"""Sector routing for Financials and REITs (CLAUDE.md Rule 5).

Tickers in SECTOR_ADJUSTED_SECTORS get "Sector-adjusted" treatment; the
treatment is chosen by matching the yfinance industry string against
SUBSECTOR_RULES by prefix. An unmatched industry gets the default treatment
and is logged so new labels get noticed.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel

import config
from data.provider import InfoResult

log = logging.getLogger(__name__)


class SectorRoute(BaseModel):
    sector: str | None
    industry: str | None
    sector_adjusted: bool
    subsector: str | None = None  # bank | reit | insurer | other_financial
    matched_rule: str | None = None
    unmatched_industry: bool = False

    @property
    def label(self) -> str:
        return f"Sector-adjusted ({self.subsector})" if self.sector_adjusted else "Standard"


def route(info: InfoResult) -> SectorRoute:
    sector, industry = info.get("sector"), info.get("industry")
    if sector not in config.SECTOR_ADJUSTED_SECTORS:
        return SectorRoute(sector=sector, industry=industry, sector_adjusted=False)
    for prefix, subsector in config.SUBSECTOR_RULES:
        if industry and industry.startswith(prefix):
            return SectorRoute(sector=sector, industry=industry, sector_adjusted=True,
                               subsector=subsector, matched_rule=prefix)
    log.warning("Unmatched financial/real-estate industry %r (sector %r) for %s; using default treatment",
                industry, sector, info.ticker)
    return SectorRoute(sector=sector, industry=industry, sector_adjusted=True,
                       subsector=config.SUBSECTOR_DEFAULT, unmatched_industry=True)
