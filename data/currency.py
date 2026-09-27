"""Currency handling (CLAUDE.md Rule 5).

If yfinance `financialCurrency` differs from the trading `currency`, financials
are converted to the trading currency with a yfinance FX pair before any ratio
against price or market cap. This applies to stage-1 `info` fields as well as
statements. Only monetary and per-share fields are converted (field_map kinds);
market cap is already in the trading currency and is never touched.
"""

from __future__ import annotations

from datetime import date

from data import field_map as fm
from data.provider import DataProvider, InfoResult, ProviderError, Statement
from data.values import Datum, NA_INCOMPLETE, OK

CONVERTIBLE_KINDS = ("monetary", "per_share")


def detect_mismatch(info: InfoResult) -> tuple[str, str] | None:
    """(reporting currency, trading currency) when they differ, else None."""
    fin = info.get("financial_currency")
    trade = info.get("currency")
    if not fin or not trade or fin == trade:
        return None
    return fin, trade


def fx_pair(from_ccy: str, to_ccy: str) -> str:
    return f"{from_ccy}{to_ccy}=X"


def fx_rate(provider: DataProvider, from_ccy: str, to_ccy: str) -> Datum:
    """Latest FX close (units of to_ccy per 1 from_ccy) from the yfinance pair."""
    pair = fx_pair(from_ccy, to_ccy)
    try:
        s = provider.get_price_history(pair, adjusted=False).dropna()
    except ProviderError as exc:
        return Datum.missing(f"N/A - FX rate {pair} unavailable ({exc})")
    if s.empty or s.iloc[-1] <= 0:
        return Datum.missing(f"N/A - FX rate {pair} unavailable (no data)")
    return Datum(value=float(s.iloc[-1]), period_end=s.index[-1].date(), period_label=f"FX {pair}",
                 provider=s.attrs.get("provider", provider.name), currency=to_ccy,
                 notes=[pair])


def conversion_note(from_ccy: str, to_ccy: str, fx: Datum) -> str:
    return (f"converted {from_ccy}→{to_ccy} at {fx.value:.4f} "
            f"({fx_pair(from_ccy, to_ccy)}, {fx.period_end.isoformat() if fx.period_end else 'n.d.'})")


def info_datums(info: InfoResult, fx: Datum | None = None, period_end: date | None = None) -> dict[str, Datum]:
    """Numeric info fields as Datums in the trading currency.

    `fx` must be supplied when detect_mismatch(info) is not None; without a
    usable rate, convertible fields become N/A with the reason (never left in
    the wrong currency).
    """
    mismatch = detect_mismatch(info)
    trade_ccy = info.get("currency")
    out: dict[str, Datum] = {}
    for fs in fm.fields_for("info"):
        if fs.kind in ("text", "table", "date"):
            continue
        value, status = info.get(fs.canonical), info.status(fs.canonical)
        label = "info field (provider's latest)"
        if status != OK or value is None:
            out[fs.canonical] = Datum.missing(status if status != OK else NA_INCOMPLETE,
                                              provider=info.provider, period_label=label)
            continue
        d = Datum(value=float(value), period_end=period_end, period_label=label, provider=info.provider,
                  currency=trade_ccy if fs.kind in ("monetary", "per_share", "trading_monetary") else None)
        if mismatch and fs.kind in CONVERTIBLE_KINDS:
            src, dst = mismatch
            if fx is None or not fx.ok:
                reason = fx.status if fx is not None else f"N/A - FX rate {fx_pair(src, dst)} not fetched"
                d = Datum.missing(reason, provider=info.provider, period_label=label)
            else:
                d.value = d.value * fx.value
                d.notes.append(conversion_note(src, dst, fx))
        out[fs.canonical] = d
    return out


def convert_statement(stmt: Statement, from_ccy: str, to_ccy: str, fx: Datum) -> Statement:
    """A copy of the statement with monetary and per-share rows in to_ccy.

    Uses the latest FX rate for every period and says so in the notes.
    """
    if not fx.ok:
        raise ValueError(f"cannot convert without a valid FX rate: {fx.status}")
    new_values: dict[str, dict] = {}
    for canonical, periods in stmt.values.items():
        kind = fm.spec(canonical).kind
        if kind in CONVERTIBLE_KINDS:
            new_values[canonical] = {d: v * fx.value for d, v in periods.items()}
        else:
            new_values[canonical] = dict(periods)
    note = conversion_note(from_ccy, to_ccy, fx) + "; latest rate applied to all periods"
    return stmt.model_copy(update={"values": new_values, "currency": to_ccy, "notes": [*stmt.notes, note]})


def to_trading_currency(provider: DataProvider, info: InfoResult, stmt: Statement) -> Statement:
    """Convert a statement when the ticker reports in a different currency."""
    mismatch = detect_mismatch(info)
    if not mismatch:
        return stmt.model_copy(update={"currency": info.get("currency")})
    fx = fx_rate(provider, *mismatch)
    if not fx.ok:
        return stmt.model_copy(update={"values": {}, "notes": [*stmt.notes, fx.status]})
    return convert_statement(stmt, mismatch[0], mismatch[1], fx)
