#!/usr/bin/env python3
"""
screen.py - multi-sector quantitative stock screen for a ~2-year hold horizon.

Pulls fundamentals and price history from Yahoo Finance (via yfinance), scores
every ticker against a sector-specific weighting, and prints a ranked table per
sector plus a diversified "Top 5".

Usage
-----
    pip install yfinance
    python screen.py                       # full screen, human-readable tables
    python screen.py --csv run.csv         # also write a flat CSV of every metric
    python screen.py --dump-json raw.json  # save the fetched metrics
    python screen.py --from-json raw.json  # re-score offline from a saved fetch
    python screen.py --sectors Energy Biotech
    python screen.py --tickers AMD NVDA

Re-running: nothing is stateful. Run it every week or two; use --csv with a
dated filename (e.g. --csv "runs/$(date +%F).csv") if you want to diff how the
rankings shift over time.

Notes on the data
-----------------
* Price returns are true price returns (split-adjusted, NOT dividend-adjusted),
  so dividends are not double-counted against the dividend-yield component.
* Momentum is scored on 12-month return in excess of the sector ETF. The
  6-month return is reported for context but is not scored.
* Every metric is optional. If a metric is missing, its component drops out of
  the score and the remaining weights are renormalized; the DATA column shows
  what fraction of the sector's weight was actually available.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
import warnings
from datetime import datetime

warnings.filterwarnings("ignore")

# --------------------------------------------------------------------------
# Configuration - edit freely, everything downstream is driven off these.
# --------------------------------------------------------------------------

SECTORS: dict[str, dict] = {
    "Semiconductor/Tech": {
        "etf": "SOXX",
        "tickers": ["AMD", "MRVL", "AVGO", "NVDA", "QCOM", "TSM"],
    },
    "Energy": {
        "etf": "XLE",
        "tickers": ["SHEL", "CVX", "XOM", "COP"],
    },
    "HVAC/Home Equipment": {
        "etf": "XHB",
        "tickers": ["TT", "CARR", "REZI", "LII"],
    },
    "Biotech": {
        "etf": "XBI",
        "tickers": ["AMGN", "VRTX", "REGN"],
    },
}

# Component weights per sector (must sum to 100 within each sector).
WEIGHTS: dict[str, dict[str, float]] = {
    "Semiconductor/Tech": {
        "rev_growth": 35.0,
        "gross_margin": 25.0,
        "momentum": 25.0,
        "debt_equity": 15.0,
    },
    "Energy": {
        "fcf_yield": 35.0,
        "dividend": 30.0,
        "debt_equity": 20.0,
        "momentum": 15.0,
    },
    "HVAC/Home Equipment": {
        "rev_growth": 30.0,
        "gross_margin": 25.0,
        "momentum": 25.0,
        "debt_equity": 20.0,
    },
    "Biotech": {
        "rev_growth": 25.0,
        "fcf_yield": 25.0,
        "debt_equity": 20.0,
        "momentum": 30.0,
    },
}

# Normalization endpoints: (value scoring 0, value scoring 100).
# The upper anchors are the ones pinned by the screen's spec; the lower anchors
# set how harshly a laggard is treated. Tune these, not the code below.
BANDS = {
    "semi_rev_growth": (-20.0, 30.0),      # 30%+ YoY revenue growth = 100
    "semi_gm_level": (20.0, 45.0),         # 45%+ gross margin = 100
    "gm_slope": (-2.0, 2.0),               # pp of margin per quarter
    "hvac_rev_growth": (-10.0, 20.0),
    "hvac_gm_level": (15.0, 40.0),
    "bio_rev_growth": (-10.0, 25.0),
    "energy_fcf_yield": (0.0, 15.0),       # FCF / market cap, %
    "bio_fcf_yield": (0.0, 10.0),
    "energy_div_yield": (0.0, 6.0),
    "div_coverage": (1.0, 2.5),            # FCF / dividends paid
    "momentum_excess": (-30.0, 30.0),      # pp vs sector ETF over 12m
    "semi_momentum_excess": (-40.0, 40.0), # semis are higher beta
}

# A score built on less than this share of its sector's weight is flagged.
MIN_COVERAGE = 0.50

DISCLAIMER = (
    "This is a quantitative screen, not investment advice. "
    "Past performance does not predict future returns."
)

# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _finite(x):
    """Return x as a float, or None if it is missing/NaN/inf."""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def lin(x, lo, hi):
    """Linearly map x from [lo, hi] onto [0, 100], clipped at both ends."""
    x = _finite(x)
    if x is None or hi == lo:
        return None
    return max(0.0, min(100.0, (x - lo) / (hi - lo) * 100.0))


def debt_equity_score(de):
    """Lower leverage scores higher. D/E of 1.0 scores 40; 2.0 and above, 0."""
    de = _finite(de)
    if de is None:
        return None
    if de < 0:          # negative book equity
        return 0.0
    if de <= 1.0:
        return 100.0 - 60.0 * de
    if de <= 2.0:
        return 40.0 - 40.0 * (de - 1.0)
    return 0.0


def slope_per_period(values):
    """Least-squares slope of a series against its index (oldest -> newest)."""
    vals = [_finite(v) for v in values]
    vals = [v for v in vals if v is not None]
    n = len(vals)
    if n < 2:
        return None
    mean_x = (n - 1) / 2.0
    mean_y = sum(vals) / n
    num = sum((i - mean_x) * (v - mean_y) for i, v in enumerate(vals))
    den = sum((i - mean_x) ** 2 for i in range(n))
    return num / den if den else None


# --------------------------------------------------------------------------
# Data fetching (yfinance). Everything here is best-effort: any field that
# cannot be resolved comes back as None and is handled downstream.
# --------------------------------------------------------------------------

# Yahoo's row labels drift between tickers and filings, so each metric is
# looked up through a list of candidates.
ROW_ALIASES = {
    "revenue": ["Total Revenue", "OperatingRevenue", "Operating Revenue", "Revenue"],
    "gross_profit": ["Gross Profit"],
    "cost_of_revenue": ["Cost Of Revenue", "Cost of Revenue", "Reconciled Cost Of Revenue"],
    "total_debt": ["Total Debt", "TotalDebt"],
    "long_term_debt": ["Long Term Debt", "Long Term Debt And Capital Lease Obligation"],
    "current_debt": ["Current Debt", "Current Debt And Capital Lease Obligation"],
    "equity": [
        "Stockholders Equity",
        "Total Stockholders Equity",
        "Common Stock Equity",
        "Total Equity Gross Minority Interest",
    ],
    "fcf": ["Free Cash Flow"],
    "ocf": ["Operating Cash Flow", "Total Cash From Operating Activities"],
    "capex": ["Capital Expenditure", "Capital Expenditures"],
    "dividends_paid": ["Cash Dividends Paid", "Common Stock Dividend Paid", "Dividends Paid"],
}


def _row(df, key):
    """Pull one row out of a yfinance statement, newest column first."""
    if df is None or getattr(df, "empty", True):
        return None
    index_map = {str(i).strip().lower().replace(" ", ""): i for i in df.index}
    for name in ROW_ALIASES[key]:
        probe = name.strip().lower().replace(" ", "")
        if probe in index_map:
            series = df.loc[index_map[probe]]
            if hasattr(series, "columns"):      # duplicated row label
                series = series.iloc[0]
            series = series.dropna()
            try:
                series = series.sort_index(ascending=False)
            except Exception:
                pass
            return series
    return None


def _vals(series, n=None):
    """Row values as plain floats, newest first."""
    if series is None:
        return []
    out = [_finite(v) for v in list(series.values)]
    out = [v for v in out if v is not None]
    return out[:n] if n else out


def _quarter_labels(series, n=None):
    if series is None:
        return []
    labels = [str(getattr(c, "date", lambda: c)()) for c in series.index]
    return labels[:n] if n else labels


def price_return(closes, months):
    """Percent price return over the trailing `months`, or None if too short."""
    import pandas as pd

    if closes is None or len(closes) < 2:
        return None
    end_ts = closes.index[-1]
    target = end_ts - pd.DateOffset(months=months)
    earlier = closes.index[closes.index <= target]
    if len(earlier) == 0:
        return None
    start_ts = earlier[-1]
    # Guard against a gappy history silently anchoring on a much older bar.
    if (target - start_ts).days > 20:
        return None
    start, end = _finite(closes.loc[start_ts]), _finite(closes.iloc[-1])
    if not start or end is None:
        return None
    return (end / start - 1.0) * 100.0


def fetch_close_series(ticker, period="2y"):
    """Split-adjusted closing prices (not dividend-adjusted)."""
    import yfinance as yf

    hist = yf.Ticker(ticker).history(period=period, auto_adjust=False)
    if hist is None or hist.empty or "Close" not in hist.columns:
        return None
    closes = hist["Close"].dropna()
    return closes if len(closes) else None


def fetch_etf_returns(etfs, delay=0.0):
    """{etf: {'ret_6m': x, 'ret_12m': y}} for the sector benchmarks."""
    out = {}
    for etf in etfs:
        try:
            closes = fetch_close_series(etf)
            out[etf] = {
                "ret_6m": price_return(closes, 6),
                "ret_12m": price_return(closes, 12),
            }
            if out[etf]["ret_12m"] is None:
                print(f"  ! warning: no 12-month price history for benchmark {etf}",
                      file=sys.stderr)
        except Exception as exc:
            out[etf] = {"ret_6m": None, "ret_12m": None}
            print(f"  ! warning: could not fetch benchmark {etf}: {exc}", file=sys.stderr)
        if delay:
            time.sleep(delay)
    return out


def fetch_ticker(ticker, sector, etf, etf_rets, delay=0.0):
    """Collect every raw metric for one ticker. Never raises."""
    import yfinance as yf

    m = {
        "ticker": ticker,
        "sector": sector,
        "etf": etf,
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "warnings": [],
        "failed": False,
    }
    for key in (
        "price", "market_cap", "rev_yoy_avg", "rev_yoy_latest", "rev_yoy_quarters",
        "rev_growth_basis", "gross_margin", "gross_margin_slope", "gross_margin_quarters",
        "debt_equity", "debt_equity_basis", "fcf_ttm", "fcf_basis", "fcf_yield",
        "div_yield", "div_coverage", "ret_6m", "ret_12m", "etf_ret_6m", "etf_ret_12m",
        "excess_12m", "forward_pe", "peg",
    ):
        m[key] = None

    try:
        tk = yf.Ticker(ticker)
    except Exception as exc:
        m["failed"] = True
        m["warnings"].append(f"could not open ticker: {exc}")
        return m

    def guarded(label, fn, default=None):
        try:
            return fn()
        except Exception as exc:
            m["warnings"].append(f"{label} unavailable ({type(exc).__name__})")
            return default

    info = guarded("info", lambda: tk.info, {}) or {}
    fast = guarded("fast_info", lambda: tk.fast_info, None)

    # ---- price, market cap ------------------------------------------------
    closes = guarded("price history", lambda: fetch_close_series(ticker))
    if closes is not None:
        m["price"] = _finite(closes.iloc[-1])
        m["ret_6m"] = price_return(closes, 6)
        m["ret_12m"] = price_return(closes, 12)
    else:
        m["warnings"].append("no price history")

    mc = None
    for get in (lambda: fast["market_cap"], lambda: info.get("marketCap")):
        try:
            mc = _finite(get())
        except Exception:
            mc = None
        if mc:
            break
    m["market_cap"] = mc

    bench = etf_rets.get(etf, {})
    m["etf_ret_6m"], m["etf_ret_12m"] = bench.get("ret_6m"), bench.get("ret_12m")
    if m["ret_12m"] is not None and m["etf_ret_12m"] is not None:
        m["excess_12m"] = m["ret_12m"] - m["etf_ret_12m"]

    # ---- statements -------------------------------------------------------
    qis = guarded("quarterly income statement", lambda: tk.quarterly_income_stmt)
    ais = guarded("annual income statement", lambda: tk.income_stmt)
    qbs = guarded("quarterly balance sheet", lambda: tk.quarterly_balance_sheet)
    qcf = guarded("quarterly cash flow", lambda: tk.quarterly_cashflow)
    acf = guarded("annual cash flow", lambda: tk.cashflow)

    # ---- revenue growth YoY over the last 4 quarters ----------------------
    q_rev = _vals(_row(qis, "revenue"))
    q_rev_labels = _quarter_labels(_row(qis, "revenue"))
    yoy = []
    if len(q_rev) >= 5:
        # Compare each of the last 4 quarters with the same quarter a year back.
        for i in range(min(4, len(q_rev) - 4)):
            prior = q_rev[i + 4]
            if prior:
                yoy.append({
                    "quarter": q_rev_labels[i] if i < len(q_rev_labels) else None,
                    "yoy_pct": (q_rev[i] / prior - 1.0) * 100.0,
                })
    if yoy:
        m["rev_yoy_quarters"] = yoy
        m["rev_yoy_latest"] = yoy[0]["yoy_pct"]
        m["rev_yoy_avg"] = sum(q["yoy_pct"] for q in yoy) / len(yoy)
        m["rev_growth_basis"] = f"{len(yoy)}q YoY avg"
    else:
        # Not enough quarterly history for a YoY comparison - fall back to TTM
        # vs. the prior-year annual figure, then to annual-over-annual.
        a_rev = _vals(_row(ais, "revenue"))
        if len(q_rev) >= 4 and a_rev:
            ttm = sum(q_rev[:4])
            if a_rev[0]:
                m["rev_yoy_avg"] = m["rev_yoy_latest"] = (ttm / a_rev[0] - 1.0) * 100.0
                m["rev_growth_basis"] = "TTM vs prior FY"
        elif len(a_rev) >= 2 and a_rev[1]:
            m["rev_yoy_avg"] = m["rev_yoy_latest"] = (a_rev[0] / a_rev[1] - 1.0) * 100.0
            m["rev_growth_basis"] = "annual YoY"
        if m["rev_yoy_avg"] is None:
            m["warnings"].append("revenue growth unavailable")

    # ---- gross margin level and trend -------------------------------------
    q_gp = _vals(_row(qis, "gross_profit"))
    if not q_gp:
        q_cor = _vals(_row(qis, "cost_of_revenue"))
        if q_cor and q_rev:
            q_gp = [r - c for r, c in zip(q_rev, q_cor)]
    margins = []
    for rev, gp in zip(q_rev[:4], q_gp[:4]):
        if rev:
            margins.append(gp / rev * 100.0)
    if margins:
        m["gross_margin"] = margins[0]
        m["gross_margin_quarters"] = [
            {"quarter": q_rev_labels[i] if i < len(q_rev_labels) else None,
             "gross_margin_pct": v}
            for i, v in enumerate(margins)
        ]
        # margins is newest-first; reverse so the slope reads pp per quarter.
        m["gross_margin_slope"] = slope_per_period(list(reversed(margins)))
    else:
        m["warnings"].append("gross margin unavailable")

    # ---- debt / equity ----------------------------------------------------
    debt = _vals(_row(qbs, "total_debt"), 1)
    if not debt:
        lt = _vals(_row(qbs, "long_term_debt"), 1)
        cur = _vals(_row(qbs, "current_debt"), 1)
        if lt or cur:
            debt = [(lt[0] if lt else 0.0) + (cur[0] if cur else 0.0)]
    equity = _vals(_row(qbs, "equity"), 1)
    if debt and equity and equity[0]:
        m["debt_equity"] = debt[0] / equity[0]
        m["debt_equity_basis"] = "latest quarterly balance sheet"
    else:
        de_info = _finite(info.get("debtToEquity"))
        if de_info is not None:
            m["debt_equity"] = de_info / 100.0   # Yahoo reports this as a percent
            m["debt_equity_basis"] = "info.debtToEquity"
        else:
            m["warnings"].append("debt/equity unavailable")
    if equity and equity[0] is not None and equity[0] < 0:
        m["warnings"].append("negative book equity")

    # ---- free cash flow (TTM) ---------------------------------------------
    def fcf_from(df, n):
        vals = _vals(_row(df, "fcf"), n)
        if len(vals) == n:
            return sum(vals)
        ocf = _vals(_row(df, "ocf"), n)
        capex = _vals(_row(df, "capex"), n)
        if len(ocf) == n and len(capex) == n:
            # Yahoo signs capital expenditure negative.
            return sum(o - abs(c) for o, c in zip(ocf, capex))
        return None

    fcf = fcf_from(qcf, 4)
    if fcf is not None:
        m["fcf_ttm"], m["fcf_basis"] = fcf, "sum of last 4 quarters"
    else:
        fcf = fcf_from(acf, 1)
        if fcf is not None:
            m["fcf_ttm"], m["fcf_basis"] = fcf, "most recent fiscal year"
        else:
            fcf = _finite(info.get("freeCashflow"))
            if fcf is not None:
                m["fcf_ttm"], m["fcf_basis"] = fcf, "info.freeCashflow"
            else:
                m["warnings"].append("free cash flow unavailable")
    if m["fcf_ttm"] is not None and mc:
        m["fcf_yield"] = m["fcf_ttm"] / mc * 100.0

    # ---- dividend yield and coverage --------------------------------------
    # Derived from the actual dividend history rather than info['dividendYield'],
    # whose units have changed between yfinance releases.
    div_ttm_ps = None
    divs = guarded("dividend history", lambda: tk.dividends)
    if divs is not None and len(divs):
        try:
            import pandas as pd

            cutoff = pd.Timestamp.now(tz=divs.index.tz) - pd.DateOffset(months=12)
            div_ttm_ps = _finite(divs[divs.index >= cutoff].sum())
        except Exception:
            div_ttm_ps = None
    if div_ttm_ps is not None and m["price"]:
        m["div_yield"] = div_ttm_ps / m["price"] * 100.0
    else:
        raw = _finite(info.get("dividendYield"))
        if raw is not None:
            # Older releases return a fraction, newer ones a percent.
            m["div_yield"] = raw if raw > 1.0 else raw * 100.0

    paid = _vals(_row(qcf, "dividends_paid"), 4)
    total_paid = abs(sum(paid)) if len(paid) == 4 else None
    if total_paid is None:
        paid_a = _vals(_row(acf, "dividends_paid"), 1)
        total_paid = abs(paid_a[0]) if paid_a else None
    if total_paid is None and div_ttm_ps:
        shares = _finite(info.get("sharesOutstanding"))
        if shares:
            total_paid = div_ttm_ps * shares
    if m["fcf_ttm"] is not None and total_paid:
        m["div_coverage"] = m["fcf_ttm"] / total_paid

    # ---- valuation --------------------------------------------------------
    m["forward_pe"] = _finite(info.get("forwardPE"))
    m["peg"] = _finite(info.get("trailingPegRatio")) or _finite(info.get("pegRatio"))

    core = [m["rev_yoy_avg"], m["gross_margin"], m["debt_equity"],
            m["fcf_ttm"], m["ret_12m"]]
    if all(v is None for v in core):
        m["failed"] = True
        m["warnings"].append("no usable data returned")

    if delay:
        time.sleep(delay)
    return m


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def _blend(primary, secondary, w_primary):
    """Weighted blend of two 0-100 sub-scores.

    The primary drives the component; if only the secondary is missing it is
    treated as neutral (50) rather than dropping the whole component.
    """
    if primary is None:
        return None
    if secondary is None:
        secondary = 50.0
    return w_primary * primary + (1.0 - w_primary) * secondary


def components_semi(m):
    return {
        "rev_growth": lin(m.get("rev_yoy_avg"), *BANDS["semi_rev_growth"]),
        # Level does most of the work; the 4-quarter trend tilts it.
        "gross_margin": _blend(
            lin(m.get("gross_margin"), *BANDS["semi_gm_level"]),
            lin(m.get("gross_margin_slope"), *BANDS["gm_slope"]),
            0.7,
        ),
        "momentum": lin(m.get("excess_12m"), *BANDS["semi_momentum_excess"]),
        "debt_equity": debt_equity_score(m.get("debt_equity")),
    }


def components_energy(m):
    return {
        "fcf_yield": lin(m.get("fcf_yield"), *BANDS["energy_fcf_yield"]),
        # Yield is the component; coverage by free cash flow modulates it.
        "dividend": _blend(
            lin(m.get("div_yield"), *BANDS["energy_div_yield"]),
            lin(m.get("div_coverage"), *BANDS["div_coverage"]),
            0.7,
        ),
        "debt_equity": debt_equity_score(m.get("debt_equity")),
        "momentum": lin(m.get("excess_12m"), *BANDS["momentum_excess"]),
    }


def components_hvac(m):
    return {
        "rev_growth": lin(m.get("rev_yoy_avg"), *BANDS["hvac_rev_growth"]),
        # Spec weights the trend here, so the slope leads and level tilts.
        "gross_margin": _blend(
            lin(m.get("gross_margin_slope"), *BANDS["gm_slope"]),
            lin(m.get("gross_margin"), *BANDS["hvac_gm_level"]),
            0.7,
        ),
        "momentum": lin(m.get("excess_12m"), *BANDS["momentum_excess"]),
        "debt_equity": debt_equity_score(m.get("debt_equity")),
    }


def components_biotech(m):
    return {
        "rev_growth": lin(m.get("rev_yoy_avg"), *BANDS["bio_rev_growth"]),
        "fcf_yield": lin(m.get("fcf_yield"), *BANDS["bio_fcf_yield"]),
        "debt_equity": debt_equity_score(m.get("debt_equity")),
        "momentum": lin(m.get("excess_12m"), *BANDS["momentum_excess"]),
    }


COMPONENT_BUILDERS = {
    "Semiconductor/Tech": components_semi,
    "Energy": components_energy,
    "HVAC/Home Equipment": components_hvac,
    "Biotech": components_biotech,
}


def score_ticker(m):
    """Attach score / coverage / per-component detail to a metrics dict."""
    sector = m.get("sector")
    builder = COMPONENT_BUILDERS.get(sector)
    m["score"] = None
    m["coverage"] = 0.0
    m["components"] = {}
    if builder is None or m.get("failed"):
        return m

    weights = WEIGHTS[sector]
    comps = builder(m)
    m["components"] = {k: comps.get(k) for k in weights}

    available = {k: v for k, v in comps.items() if v is not None and weights.get(k)}
    total_weight = sum(weights[k] for k in available)
    if total_weight <= 0:
        m["warnings"].append("no scoreable metrics")
        return m

    m["score"] = sum(available[k] * weights[k] for k in available) / total_weight
    m["coverage"] = total_weight / sum(weights.values())
    missing = [k for k in weights if comps.get(k) is None]
    if missing:
        m["warnings"].append("scored without: " + ", ".join(sorted(missing)))
    return m


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------

NA = "n/a"
TOP5_WIDTH = 84


def f_num(v, dec=1, suffix=""):
    v = _finite(v)
    return NA if v is None else f"{v:,.{dec}f}{suffix}"


def f_signed(v, dec=1, suffix=""):
    v = _finite(v)
    return NA if v is None else f"{v:+,.{dec}f}{suffix}"


def f_money(v):
    v = _finite(v)
    if v is None:
        return NA
    a = abs(v)
    if a >= 1e12:
        return f"{v / 1e12:,.2f}T"
    if a >= 1e9:
        return f"{v / 1e9:,.2f}B"
    if a >= 1e6:
        return f"{v / 1e6:,.1f}M"
    return f"{v:,.0f}"


COLUMNS = [
    ("#", 3, "<", lambda m, i: str(i)),
    ("TICKER", 6, "<", lambda m, i: m["ticker"]),
    ("SCORE", 6, ">", lambda m, i: f_num(m.get("score"), 1)),
    ("DATA", 5, ">", lambda m, i: f"{m.get('coverage', 0) * 100:.0f}%"),
    ("RevYoY", 8, ">", lambda m, i: f_signed(m.get("rev_yoy_avg"), 1, "%")),
    ("GrMgn", 7, ">", lambda m, i: f_num(m.get("gross_margin"), 1, "%")),
    ("GMtrend", 8, ">", lambda m, i: f_signed(m.get("gross_margin_slope"), 2)),
    ("D/E", 6, ">", lambda m, i: f_num(m.get("debt_equity"), 2)),
    ("FCF(TTM)", 9, ">", lambda m, i: f_money(m.get("fcf_ttm"))),
    ("FCFyld", 7, ">", lambda m, i: f_signed(m.get("fcf_yield"), 1, "%")),
    ("DivYld", 7, ">", lambda m, i: f_num(m.get("div_yield"), 2, "%")),
    ("DivCov", 7, ">", lambda m, i: f_num(m.get("div_coverage"), 1, "x")),
    ("6mRet", 8, ">", lambda m, i: f_signed(m.get("ret_6m"), 1, "%")),
    ("12mRet", 8, ">", lambda m, i: f_signed(m.get("ret_12m"), 1, "%")),
    ("vsETF", 8, ">", lambda m, i: f_signed(m.get("excess_12m"), 1, "%")),
    ("FwdPE", 7, ">", lambda m, i: f_num(m.get("forward_pe"), 1)),
    ("PEG", 6, ">", lambda m, i: f_num(m.get("peg"), 2)),
]

TABLE_WIDTH = sum(w for _, w, _, _ in COLUMNS) + len(COLUMNS) - 1


def render_row(cells):
    """cells: (text, width, align) triples, align being '<' or '>'."""
    return " ".join(format(str(t)[:w], f"{a}{w}") for t, w, a in cells).rstrip()


def print_table(rows):
    print(render_row([(name, w, a) for name, w, a, _ in COLUMNS]))
    print("-" * TABLE_WIDTH)
    for rank, m in enumerate(rows, start=1):
        print(render_row([(fn(m, rank), w, a) for _, w, a, fn in COLUMNS]))


def print_sector(sector, metrics, show_components=False):
    etf = SECTORS.get(sector, {}).get("etf", "?")
    scored = sorted(
        [m for m in metrics if m.get("score") is not None],
        key=lambda m: m["score"], reverse=True,
    )
    unscored = [m for m in metrics if m.get("score") is None]

    print()
    print("=" * TABLE_WIDTH)
    print(f"{sector}  (benchmark: {etf})")
    weights = WEIGHTS.get(sector, {})
    print("weights: " + ", ".join(f"{k} {int(v)}%" for k, v in weights.items()))
    print("=" * TABLE_WIDTH)

    if scored:
        print_table(scored)
        if show_components:
            print()
            print("component scores (0-100):")
            for m in scored:
                parts = ", ".join(
                    f"{k}={NA if m['components'].get(k) is None else format(m['components'][k], '.0f')}"
                    for k in weights
                )
                print(f"  {m['ticker']:<6} {parts}")
    else:
        print("  no ticker in this sector could be scored")

    flagged = [m for m in scored if m.get("coverage", 1) < MIN_COVERAGE]
    if flagged:
        print()
        for m in flagged:
            print(f"  ! {m['ticker']}: scored on only {m['coverage'] * 100:.0f}% "
                  f"of sector weight - treat with caution")
    for m in unscored:
        why = "; ".join(m.get("warnings") or ["unknown reason"])
        print(f"  ! {m['ticker']}: not ranked ({why})")


def print_top5(all_metrics):
    scored = [m for m in all_metrics if m.get("score") is not None]
    if not scored:
        print("\nNo tickers could be scored - nothing to rank.")
        return

    picks, reasons = [], {}
    for sector in SECTORS:
        in_sector = [m for m in scored if m.get("sector") == sector]
        if not in_sector:
            continue
        best = max(in_sector, key=lambda m: m["score"])
        picks.append(best)
        reasons[best["ticker"]] = f"top of {sector}"

    chosen = {m["ticker"] for m in picks}
    rest = sorted([m for m in scored if m["ticker"] not in chosen],
                  key=lambda m: m["score"], reverse=True)
    for m in rest[: max(0, 5 - len(picks))]:
        picks.append(m)
        reasons[m["ticker"]] = f"highest remaining score ({m['sector']})"

    picks.sort(key=lambda m: m["score"], reverse=True)

    print()
    print("=" * TOP5_WIDTH)
    print("OVERALL TOP 5  (one leader per sector, plus the best remaining score)")
    print("=" * TOP5_WIDTH)
    cols = [("#", 3, "<"), ("TICKER", 6, "<"), ("SECTOR", 20, "<"),
            ("SCORE", 6, ">"), ("WHY", 45, "<")]
    print(render_row([(n, w, a) for n, w, a in cols]))
    print("-" * TOP5_WIDTH)
    for i, m in enumerate(picks[:5], start=1):
        values = [str(i), m["ticker"], m.get("sector", ""),
                  f_num(m.get("score"), 1), reasons.get(m["ticker"], "")]
        print(render_row([(v, w, a) for v, (_, w, a) in zip(values, cols)]))


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

CSV_FIELDS = [
    "ticker", "sector", "etf", "score", "coverage", "rev_yoy_avg", "rev_yoy_latest",
    "rev_growth_basis", "gross_margin", "gross_margin_slope", "debt_equity",
    "debt_equity_basis", "fcf_ttm", "fcf_basis", "fcf_yield", "div_yield",
    "div_coverage", "ret_6m", "ret_12m", "etf_ret_12m", "excess_12m", "forward_pe",
    "peg", "market_cap", "price", "fetched_at", "warnings",
]


def write_csv(path, metrics):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for m in sorted(metrics, key=lambda m: (m.get("sector") or "",
                                                -(m.get("score") or -1))):
            row = {}
            for k in CSV_FIELDS:
                v = m.get(k)
                row[k] = round(v, 4) if isinstance(v, float) else v
            row["warnings"] = "; ".join(m.get("warnings") or [])
            writer.writerow(row)
    print(f"\nWrote {path}")


def write_json(path, metrics):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(metrics, fh, indent=2, default=str)
    print(f"Wrote {path}")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def build_worklist(sectors_filter, tickers_filter):
    work = []
    wanted = {t.upper() for t in tickers_filter} if tickers_filter else None
    for sector, cfg in SECTORS.items():
        if sectors_filter and sector.lower() not in {s.lower() for s in sectors_filter}:
            continue
        for ticker in cfg["tickers"]:
            if wanted and ticker.upper() not in wanted:
                continue
            work.append((ticker, sector, cfg["etf"]))
    return work


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Rank stocks across four sectors for a ~2-year hold horizon.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Re-run any time; nothing is cached between runs unless you use "
               "--dump-json / --from-json.",
    )
    ap.add_argument("--csv", metavar="PATH", help="write all metrics to a CSV file")
    ap.add_argument("--dump-json", metavar="PATH",
                    help="save the fetched metrics as JSON")
    ap.add_argument("--from-json", metavar="PATH",
                    help="re-score from a saved JSON fetch instead of hitting the network")
    ap.add_argument("--sectors", nargs="+", metavar="NAME",
                    help="only screen these sectors (exact names from SECTORS)")
    ap.add_argument("--tickers", nargs="+", metavar="SYM",
                    help="only screen these tickers")
    ap.add_argument("--delay", type=float, default=0.3, metavar="SEC",
                    help="pause between requests, to stay under Yahoo's rate limits "
                         "(default: 0.3)")
    ap.add_argument("--components", action="store_true",
                    help="also print the per-component sub-scores")
    args = ap.parse_args(argv)

    work = build_worklist(args.sectors, args.tickers)
    if not work:
        print("No tickers selected - check --sectors / --tickers.", file=sys.stderr)
        return 2

    if args.from_json:
        with open(args.from_json, encoding="utf-8") as fh:
            metrics = json.load(fh)
        selected = {t for t, _, _ in work}
        metrics = [m for m in metrics if m.get("ticker") in selected]
        print(f"Re-scoring {len(metrics)} ticker(s) from {args.from_json} "
              "(no network access).")
    else:
        try:
            import yfinance  # noqa: F401
        except ImportError:
            print("yfinance is not installed. Run:  pip install yfinance",
                  file=sys.stderr)
            return 1

        etfs = sorted({etf for _, _, etf in work})
        print(f"Screening {len(work)} tickers across "
              f"{len({s for _, s, _ in work})} sectors "
              f"({datetime.now():%Y-%m-%d %H:%M}).")
        print(f"Fetching benchmarks: {', '.join(etfs)}")
        etf_rets = fetch_etf_returns(etfs, delay=args.delay)

        metrics = []
        for ticker, sector, etf in work:
            print(f"  fetching {ticker} ...", end=" ", flush=True)
            try:
                m = fetch_ticker(ticker, sector, etf, etf_rets, delay=args.delay)
            except Exception as exc:   # belt and braces; fetch_ticker guards too
                m = {"ticker": ticker, "sector": sector, "etf": etf, "failed": True,
                     "warnings": [f"unhandled error: {type(exc).__name__}: {exc}"]}
            metrics.append(m)
            if m.get("failed"):
                print("FAILED")
                print(f"  ! warning: {ticker} could not be fetched - "
                      f"{'; '.join(m.get('warnings') or ['unknown reason'])}",
                      file=sys.stderr)
            else:
                print("ok")

    for m in metrics:
        m.setdefault("warnings", [])
        score_ticker(m)

    for sector in SECTORS:
        in_sector = [m for m in metrics if m.get("sector") == sector]
        if in_sector:
            print_sector(sector, in_sector, show_components=args.components)

    print_top5(metrics)

    failed = [m["ticker"] for m in metrics if m.get("failed")]
    if failed:
        print(f"\nCould not evaluate: {', '.join(failed)}")

    print()
    print(DISCLAIMER)

    if args.csv:
        write_csv(args.csv, metrics)
    if args.dump_json:
        write_json(args.dump_json, metrics)
    return 0


if __name__ == "__main__":
    sys.exit(main())
