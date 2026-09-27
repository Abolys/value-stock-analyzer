"""10-year government yield by trading currency (RISK_FREE_SOURCES).

USD → yfinance ^TNX (quoted in percent). CAD → Bank of Canada Valet API series
BD.CDN.10YR.DQ.YLD (verified 2026-09: "Benchmark bond yield, 10-year").
Any other currency → N/A with the reason. Cached with the price TTL.
"""

from __future__ import annotations

from datetime import date
from typing import Callable

import requests

import config
from data.cache import DiskCache
from data.provider import DataProvider, InfoResult, ProviderError
from data.values import Datum

PROVIDER_BOC = "Bank of Canada Valet"


def _scaled(quote: float, source: dict) -> float:
    lo, hi = config.RISK_FREE_QUOTE_RANGE
    if not lo < quote < hi:
        raise ProviderError(f"{source['label']} quote {quote} outside sane range {lo}-{hi}")
    return quote * source["scale"]


def fetch_boc_valet(series: str, http_get: Callable[..., requests.Response] = requests.get) -> tuple[float, date]:
    """Latest observation of a BoC Valet series, in the series' own units."""
    try:
        r = http_get(config.BOC_VALET_URL.format(series=series), timeout=30)
        r.raise_for_status()
        obs = r.json()["observations"]
    except Exception as exc:
        raise ProviderError(f"BoC Valet request failed: {exc}") from exc
    for o in sorted(obs, key=lambda o: o["d"], reverse=True):
        v = (o.get(series) or {}).get("v")
        if v not in (None, ""):
            return float(v), date.fromisoformat(o["d"])
    raise ProviderError(f"BoC Valet returned no observations for {series}")


def risk_free_rate(currency: str | None, provider: DataProvider,
                   cache: DiskCache | None = None,
                   valet_fetch: Callable[[str], tuple[float, date]] = fetch_boc_valet) -> Datum:
    """The 10-year yield (decimal) for a trading currency, or N/A with the reason."""
    source = config.RISK_FREE_SOURCES.get(currency or "")
    if source is None:
        return Datum.missing(f"N/A - no risk-free source configured for {currency or 'unknown currency'}")

    def fetch() -> Datum:
        if source["kind"] == "yfinance":
            s = provider.get_price_history(source["symbol"], adjusted=False).dropna()
            if s.empty:
                raise ProviderError(f"no data for {source['symbol']}")
            quote, as_of, prov = float(s.iloc[-1]), s.index[-1].date(), s.attrs.get("provider", provider.name)
        elif source["kind"] == "boc_valet":
            quote, as_of = valet_fetch(source["series"])
            prov = PROVIDER_BOC
        else:
            raise ProviderError(f"unknown risk-free source kind {source['kind']!r}")
        return Datum(value=_scaled(quote, source), period_end=as_of, period_label=source["label"],
                     provider=prov, currency=currency)

    try:
        if cache is not None:
            return cache.fetch(f"risk_free|{currency}", "prices", fetch)
        return fetch()
    except ProviderError as exc:
        return Datum.missing(f"N/A - {source['label']} unavailable ({exc})")


def risk_free_for(info: InfoResult, provider: DataProvider, cache: DiskCache | None = None,
                  valet_fetch: Callable[[str], tuple[float, date]] = fetch_boc_valet) -> Datum:
    """Risk-free rate for a ticker, chosen by its trading currency."""
    return risk_free_rate(info.get("currency"), provider, cache, valet_fetch)
