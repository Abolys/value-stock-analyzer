"""Value-trap scores (SPEC "Value-trap, valuation and ownership signals").

- Piotroski F-score: Piotroski (2000), "Value Investing: The Use of Historical
  Financial Statement Information to Separate Winners from Losers", Journal of
  Accounting Research 38, nine binary signals. Simplification: ROA uses
  end-of-year total assets (the paper uses beginning-of-year), so only two
  fiscal years are needed.
- Altman Z''-score: Altman (1995/2002) non-manufacturer / emerging-market model,
  Z'' = 6.56 X1 + 3.26 X2 + 6.72 X3 + 1.05 X4 (config.ALTMAN_COEFFICIENTS),
  zones from config.ALTMAN_ZONES (< 1.10 distress, 1.10-2.60 grey, > 2.60 safe). Computed on the latest quarter's balance sheet
  and TTM EBIT (Rule 3b).
- Beneish M-score: Beneish (1999), Financial Analysts Journal 55(5), the
  8-variable model; coefficients in config.BENEISH_COEFFICIENTS. AQI uses
  1 − (current assets + net PP&E) / total assets, as in the paper.

Financials and REITs get n/m on all three. Every ratio goes through
data.ratios.safe_ratio (Rule 2b).
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

import config
from data.fundamentals import Fundamentals
from data.ratios import combine, first_unusable, safe_ratio, sum_datums
from data.values import Datum, OK, nm

NM_FINANCIALS = "not meaningful for financials and REITs"
INSUFFICIENT = "Insufficient data"
BENEISH_CAVEAT = ("The Beneish M-score is probabilistic and has real false positives; "
                  "a flag is a prompt to read the filings, not a finding.")


class Check(BaseModel):
    name: str
    outcome: str  # "pass" | "fail" | status string (N/A / n/m) when unavailable
    detail: str = ""

    @property
    def available(self) -> bool:
        return self.outcome in ("pass", "fail")


class PiotroskiResult(BaseModel):
    score: int | None = None
    available: int = 0
    checks: list[Check] = Field(default_factory=list)
    status: str = OK
    years: list[date] = Field(default_factory=list)

    @property
    def display(self) -> str:
        if self.status != OK:
            return self.status
        return f"{self.score} / 9 ({self.available} checks available)"


class AltmanResult(BaseModel):
    z: Datum = Field(default_factory=Datum.missing)
    zone: str | None = None  # "distress" | "grey" | "safe"
    components: dict[str, Datum] = Field(default_factory=dict)

    @property
    def status(self) -> str:
        return self.z.status

    @property
    def display(self) -> str:
        return f"{self.z.value:.2f} ({self.zone})" if self.z.ok else self.z.status


class BeneishResult(BaseModel):
    m: Datum = Field(default_factory=Datum.missing)
    variables: dict[str, Datum] = Field(default_factory=dict)
    flag: bool = False
    caveat: str = BENEISH_CAVEAT

    @property
    def status(self) -> str:
        return self.m.status

    @property
    def display(self) -> str:
        if not self.m.ok:
            return self.m.status
        return f"{self.m.value:.2f}" + (" — possible earnings manipulation" if self.flag else "")


def two_fiscal_years(f: Fundamentals) -> tuple[date, date] | None:
    ends = f.fiscal_year_ends()
    return (ends[0], ends[1]) if len(ends) >= 2 else None


# --------------------------------------------------------------------------
# Piotroski
# --------------------------------------------------------------------------
def _gross_margin(f: Fundamentals, d: date) -> Datum:
    rev = f.fy("total_revenue", d)
    gp = f.fy("gross_profit", d)
    if not gp.ok:
        gp = sum_datums({"revenue": rev, "cost of revenue": f.fy("cost_of_revenue", d)}, "gross profit",
                        signs={"cost of revenue": -1})
    return safe_ratio(gp, rev, name="gross margin", nonpositive_reason="revenue ≤ 0")


def _compare(name: str, cur: Datum, prev: Datum, better: str, detail_fmt: str = "{:.4g}") -> Check:
    bad = first_unusable(cur, prev)
    if bad is not None:
        return Check(name=name, outcome=bad.status)
    ok = cur.value > prev.value if better == "higher" else cur.value < prev.value
    return Check(name=name, outcome="pass" if ok else "fail",
                 detail=f"{detail_fmt.format(prev.value)} → {detail_fmt.format(cur.value)}")


def _positive(name: str, d: Datum) -> Check:
    if not d.ok:
        return Check(name=name, outcome=d.status)
    return Check(name=name, outcome="pass" if d.value > 0 else "fail", detail=f"{d.value:,.4g}")


def piotroski(f: Fundamentals, sector_adjusted: bool = False) -> PiotroskiResult:
    if sector_adjusted:
        return PiotroskiResult(status=nm(NM_FINANCIALS))
    years = two_fiscal_years(f)
    if years is None:
        return PiotroskiResult(status=f"{INSUFFICIENT} - needs two consecutive fiscal years")
    t, p = years

    def roa(d):
        return safe_ratio(f.fy("net_income", d), f.fy("total_assets", d), name="ROA",
                          nonpositive_reason="total assets ≤ 0")

    def lev(d):
        return safe_ratio(f.fy("long_term_debt", d), f.fy("total_assets", d), name="LTD/assets",
                          nonpositive_reason="total assets ≤ 0")

    def cur_ratio(d):
        return safe_ratio(f.fy("current_assets", d), f.fy("current_liabilities", d), name="current ratio",
                          nonpositive_reason="current liabilities ≤ 0")

    def turnover(d):
        return safe_ratio(f.fy("total_revenue", d), f.fy("total_assets", d), name="asset turnover",
                          nonpositive_reason="total assets ≤ 0")

    ni, cfo = f.fy("net_income", t), f.fy("operating_cash_flow", t)
    accrual = (Check(name="operating cash flow > net income", outcome=first_unusable(ni, cfo).status)
               if first_unusable(ni, cfo) else
               Check(name="operating cash flow > net income", outcome="pass" if cfo.value > ni.value else "fail",
                     detail=f"CFO {cfo.value:,.4g} vs NI {ni.value:,.4g}"))
    sh_t, sh_p = f.fy("diluted_shares", t), f.fy("diluted_shares", p)
    shares = (Check(name="no new shares issued", outcome=first_unusable(sh_t, sh_p).status)
              if first_unusable(sh_t, sh_p) else
              Check(name="no new shares issued", outcome="pass" if sh_t.value <= sh_p.value else "fail",
                    detail=f"{sh_p.value:,.0f} → {sh_t.value:,.0f} (split-restated diluted shares)"))
    checks = [
        _positive("ROA > 0", roa(t)),
        _positive("operating cash flow > 0", cfo),
        _compare("ROA improved", roa(t), roa(p), "higher"),
        accrual,
        _compare("long-term debt / assets fell", lev(t), lev(p), "lower"),
        _compare("current ratio rose", cur_ratio(t), cur_ratio(p), "higher"),
        shares,
        _compare("gross margin rose", _gross_margin(f, t), _gross_margin(f, p), "higher"),
        _compare("asset turnover rose", turnover(t), turnover(p), "higher"),
    ]
    available = sum(c.available for c in checks)
    score = sum(c.outcome == "pass" for c in checks)
    res = PiotroskiResult(score=score, available=available, checks=checks, years=[t, p])
    if available < config.PIOTROSKI_MIN_CHECKS:
        res.score = None
        res.status = (f"{INSUFFICIENT} - {available} of 9 checks available "
                      f"(needs {config.PIOTROSKI_MIN_CHECKS})")
    return res


# --------------------------------------------------------------------------
# Altman Z''
# --------------------------------------------------------------------------
def altman_zone(z: float) -> str:
    if z < config.ALTMAN_ZONES["distress_below"]:
        return "distress"
    if z > config.ALTMAN_ZONES["safe_above"]:
        return "safe"
    return "grey"


def altman_z2(f: Fundamentals, sector_adjusted: bool = False) -> AltmanResult:
    if sector_adjusted:
        return AltmanResult(z=Datum.missing(nm(NM_FINANCIALS)))
    ta = f.bal("total_assets")
    wc = f.bal("working_capital")
    if not wc.ok:
        wc = sum_datums({"current assets": f.bal("current_assets"),
                         "current liabilities": f.bal("current_liabilities")},
                        "working capital", signs={"current liabilities": -1})
    ebit = f.ttm("ebit")
    if not ebit.ok:
        op = f.ttm("operating_income")
        if op.ok:
            ebit = op.model_copy(update={"notes": [*op.notes, "EBIT not reported; operating income used"]})
    ta_reason = "total assets ≤ 0"
    comps = {
        "X1 working capital / assets": safe_ratio(wc, ta, name="X1", nonpositive_reason=ta_reason),
        "X2 retained earnings / assets": safe_ratio(f.bal("retained_earnings"), ta, name="X2",
                                                    nonpositive_reason=ta_reason),
        "X3 EBIT / assets": safe_ratio(ebit, ta, name="X3", nonpositive_reason=ta_reason),
        "X4 book equity / liabilities": safe_ratio(f.bal("stockholders_equity"), f.bal("total_liabilities"),
                                                   name="X4", nonpositive_reason="total liabilities ≤ 0"),
    }
    bad = first_unusable(*comps.values())
    if bad is not None:
        return AltmanResult(z=Datum.missing(f"{INSUFFICIENT} - {bad.status}"), components=comps)
    c = config.ALTMAN_COEFFICIENTS
    z = sum(c[k] * v.value for k, v in zip(("X1", "X2", "X3", "X4"), comps.values()))
    return AltmanResult(z=combine(z, comps, label="Altman Z''"), zone=altman_zone(z), components=comps)


# --------------------------------------------------------------------------
# Beneish M-score
# --------------------------------------------------------------------------
def _depreciation(f: Fundamentals, d: date) -> Datum:
    dep = f.fy("depreciation_amortization", d)
    return dep if dep.ok else f.fy("depreciation_cf", d)


def _index(name: str, cur: Datum, prev: Datum, reason: str) -> Datum:
    return safe_ratio(cur, prev, name=name, nonpositive_reason=reason, num_name="year t", den_name="year t-1")


def beneish(f: Fundamentals, sector_adjusted: bool = False) -> BeneishResult:
    if sector_adjusted:
        return BeneishResult(m=Datum.missing(nm(NM_FINANCIALS)))
    years = two_fiscal_years(f)
    if years is None:
        return BeneishResult(m=Datum.missing(f"{INSUFFICIENT} - needs two consecutive fiscal years"))
    t, p = years
    sales = {d: f.fy("total_revenue", d) for d in years}
    ta = {d: f.fy("total_assets", d) for d in years}
    rev_reason, ta_reason = "revenue ≤ 0", "total assets ≤ 0"

    def per_sales(field, d):
        return safe_ratio(f.fy(field, d), sales[d], name=f"{field}/sales", nonpositive_reason=rev_reason)

    def soft_assets(d):
        hard = sum_datums({"current assets": f.fy("current_assets", d), "net PP&E": f.fy("net_ppe", d)}, "hard")
        share = safe_ratio(hard, ta[d], name="hard assets share", nonpositive_reason=ta_reason)
        return share if not share.ok else share.model_copy(update={"value": 1 - share.value})

    def dep_rate(d):
        dep = _depreciation(f, d)
        base = sum_datums({"depreciation": dep, "net PP&E": f.fy("net_ppe", d)}, "dep + PP&E")
        return safe_ratio(dep, base, name="depreciation rate", nonpositive_reason="depreciation + PP&E ≤ 0")

    def leverage(d):
        debt = sum_datums({"current liabilities": f.fy("current_liabilities", d),
                           "long-term debt": f.fy("long_term_debt", d)}, "CL + LTD")
        return safe_ratio(debt, ta[d], name="leverage", nonpositive_reason=ta_reason)

    ni_cont = f.fy("net_income_continuing", t)
    if not ni_cont.ok:
        ni_cont = f.fy("net_income", t)
    accruals = sum_datums({"net income": ni_cont, "CFO": f.fy("operating_cash_flow", t)}, "accruals",
                          signs={"CFO": -1})
    variables = {
        "DSRI": _index("DSRI", per_sales("receivables", t), per_sales("receivables", p), "receivables/sales ≤ 0"),
        "GMI": _index("GMI", _gross_margin(f, p), _gross_margin(f, t), "gross margin ≤ 0"),
        "AQI": _index("AQI", soft_assets(t), soft_assets(p), "soft-asset share ≤ 0"),
        "SGI": _index("SGI", sales[t], sales[p], rev_reason),
        "DEPI": _index("DEPI", dep_rate(p), dep_rate(t), "depreciation rate ≤ 0"),
        "SGAI": _index("SGAI", per_sales("sga", t), per_sales("sga", p), "SG&A/sales ≤ 0"),
        "TATA": safe_ratio(accruals, ta[t], name="TATA", nonpositive_reason=ta_reason),
        "LVGI": _index("LVGI", leverage(t), leverage(p), "leverage ≤ 0"),
    }
    missing = [k for k, v in variables.items() if not v.ok]
    if missing:
        reasons = "; ".join(f"{k}: {variables[k].status}" for k in missing)
        return BeneishResult(m=Datum.missing(f"{INSUFFICIENT} - {reasons}"), variables=variables)
    c = config.BENEISH_COEFFICIENTS
    m = c["intercept"] + sum(c[k] * v.value for k, v in variables.items())
    return BeneishResult(m=combine(m, variables, label="Beneish M-score"), variables=variables,
                         flag=m > config.BENEISH_THRESHOLD)
