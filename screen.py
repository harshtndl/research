#!/usr/bin/env python3
"""
screen.py - multi-sector quantitative stock screen for a ~2-year hold horizon.

Pulls fundamentals and price history from Financial Modeling Prep (FMP), scores
every ticker against a sector-specific weighting, and prints a ranked table per
sector plus a diversified "Top 5".

Usage
-----
    pip install -r requirements.txt        # just requests
    export FMP_API_KEY=...                 # free key: 250 requests/day
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
* FMP's free tier allows 250 requests/day. One screen of the default universe
  costs about 110, and --max-requests caps what a run may spend.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import json
import math
import os
import sys
import time
from datetime import date, datetime

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
# Data fetching (Financial Modeling Prep). Everything here is best-effort: any
# field that cannot be resolved comes back as None and is handled downstream.
# --------------------------------------------------------------------------

FMP_BASE_URL = os.environ.get(
    "FMP_BASE_URL", "https://financialmodelingprep.com/api/v3"
).rstrip("/")

# FMP's public "demo" key answers for a handful of large caps (AAPL and
# friends) and 403s on everything else - enough to smoke-test the plumbing,
# not enough to run the screen. A free key allows 250 requests/day.
DEMO_API_KEY = "demo"

# Free tier: 250 requests/day. The screen spends one request per endpoint per
# ticker, plus one price history per benchmark ETF, plus the odd annual-period
# fallback when a quarterly statement is too short.
DEFAULT_REQUEST_BUDGET = 250
REQUESTS_PER_TICKER = 5          # income, ratios, cash flow, prices, quote
ESTIMATES_REQUEST = 1            # analyst estimates, for forward P/E

# Statement history to pull: 8 quarters is what the YoY revenue comparison and
# the 4-quarter margin trend need.
QUARTERS = 8

# Years of daily closes to pull for the momentum window.
PRICE_YEARS = 2

# FMP spells the same quantity differently across endpoints and API revisions,
# so each metric is looked up through a list of candidates rather than one
# hard-coded field name.
FIELD_ALIASES = {
    "revenue": ["revenue"],
    "gross_profit": ["grossProfit"],
    "cost_of_revenue": ["costOfRevenue"],
    # /ratios says grossProfitMargin, /income-statement says grossProfitRatio.
    "gross_margin_ratio": ["grossProfitMargin", "grossProfitRatio"],
    "debt_equity": ["debtEquityRatio", "debtToEquityRatio"],
    "fcf": ["freeCashFlow"],
    "ocf": ["operatingCashFlow", "netCashProvidedByOperatingActivities"],
    "capex": ["capitalExpenditure"],
    "dividends_paid": ["dividendsPaid", "commonDividendsPaid", "netDividendsPaid"],
    "dividend_yield": ["dividendYield"],
    "peg": ["priceEarningsToGrowthRatio", "priceToEarningsGrowthRatio"],
    "price": ["price", "previousClose"],
    "market_cap": ["marketCap", "mktCap"],
    "shares": ["sharesOutstanding", "weightedAverageShsOut"],
    "eps_estimate": ["estimatedEpsAvg", "epsAvg"],
}


class FMPError(RuntimeError):
    """Any FMP request that did not produce usable JSON."""


class FMPAuthError(FMPError):
    """401/403 - key missing, invalid, or the endpoint needs a paid plan."""


class FMPRateLimit(FMPError):
    """429 - burst or daily quota exhausted on FMP's side."""


class FMPBudgetExhausted(FMPError):
    """The local --max-requests budget is spent. Nothing was sent."""


# Errors that mean "stop the run", not "skip this metric".
FATAL_FMP_ERRORS = (FMPRateLimit, FMPBudgetExhausted)


class FMPClient:
    """Thin REST client: paces requests, retries, and caps the daily spend.

    Every response is cached for the life of the run, so re-reading a
    statement (or a benchmark's price history) is free.
    """

    def __init__(self, api_key, delay=0.3, budget=DEFAULT_REQUEST_BUDGET,
                 timeout=20.0, retries=3, base_url=FMP_BASE_URL):
        import requests

        self.api_key = api_key or DEMO_API_KEY
        self.delay = max(0.0, delay)
        self.budget = budget
        self.timeout = timeout
        self.retries = max(1, retries)
        self.base_url = base_url.rstrip("/")
        self.requests_made = 0
        self.skip_estimates = False
        self.session = requests.Session()
        self._cache = {}
        self._last_call = 0.0

    # -- plumbing ----------------------------------------------------------

    def remaining(self):
        return None if self.budget is None else max(0, self.budget - self.requests_made)

    def _pace(self):
        """Keep at least `delay` seconds between calls actually leaving here."""
        if not self.delay:
            return
        wait = self.delay - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)

    def get(self, path, **params):
        """GET one endpoint and return parsed JSON, memoized per run."""
        key = (path, tuple(sorted(params.items())))
        if key not in self._cache:
            self._cache[key] = self._request(path, params)
        return self._cache[key]

    def _request(self, path, params):
        import requests

        if self.budget is not None and self.requests_made >= self.budget:
            raise FMPBudgetExhausted(
                f"local request budget of {self.budget} is spent "
                "(raise it with --max-requests)"
            )

        url = f"{self.base_url}/{path.lstrip('/')}"
        query = dict(params)
        query["apikey"] = self.api_key
        backoff = 2.0            # network hiccups and 5xx
        rate_backoff = 5.0       # 429s deserve a longer pause
        last_error = None

        for attempt in range(self.retries):
            self._pace()
            try:
                resp = self.session.get(url, params=query, timeout=self.timeout)
            except requests.RequestException as exc:
                self._last_call = time.monotonic()
                last_error = FMPError(f"{type(exc).__name__}: {exc}")
                if attempt + 1 < self.retries:
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                raise last_error
            self._last_call = time.monotonic()
            self.requests_made += 1     # a rejected call still costs quota

            if resp.status_code == 429:
                # Could be the per-second burst limit, which waiting clears, or
                # the daily cap, which it does not. Back off a couple of times
                # before giving up on the run.
                last_error = FMPRateLimit("FMP returned 429 (rate limit)")
                if attempt + 1 < self.retries:
                    time.sleep(rate_backoff)
                    rate_backoff *= 2
                    continue
                raise last_error
            if resp.status_code in (401, 403):
                raise FMPAuthError(
                    f"HTTP {resp.status_code}: key rejected, or this endpoint is "
                    "not on the plan"
                )
            if resp.status_code >= 500:
                last_error = FMPError(f"HTTP {resp.status_code} from FMP")
                if attempt + 1 < self.retries:
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                raise last_error
            if not resp.ok:
                raise FMPError(f"HTTP {resp.status_code} for {path}")

            try:
                data = resp.json()
            except ValueError:
                raise FMPError(f"non-JSON response for {path}")

            # FMP reports some failures with HTTP 200 and an error payload.
            if isinstance(data, dict):
                message = data.get("Error Message") or data.get("error")
                if message:
                    text = str(message)
                    if "limit" in text.lower():
                        raise FMPRateLimit(text[:200])
                    raise FMPError(text[:200])
            return data

        raise last_error or FMPError(f"no response for {path}")


# --------------------------------------------------------------------------
# Endpoint wrappers. Each returns a plain list of records, newest first, or an
# empty list if FMP answered with something unexpected.
# --------------------------------------------------------------------------


def _records(payload):
    return [r for r in payload if isinstance(r, dict)] if isinstance(payload, list) else []


def fetch_income_statement(client, ticker, period="quarter", limit=QUARTERS):
    return _records(client.get(f"income-statement/{ticker}", period=period, limit=limit))


def fetch_ratios(client, ticker, period="quarter", limit=QUARTERS):
    return _records(client.get(f"ratios/{ticker}", period=period, limit=limit))


def fetch_cash_flow(client, ticker, period="quarter", limit=QUARTERS):
    return _records(
        client.get(f"cash-flow-statement/{ticker}", period=period, limit=limit)
    )


def fetch_quote(client, ticker):
    rows = _records(client.get(f"quote/{ticker}"))
    return rows[0] if rows else {}


def fetch_analyst_estimates(client, ticker, limit=6):
    return _records(
        client.get(f"analyst-estimates/{ticker}", period="annual", limit=limit)
    )


# --------------------------------------------------------------------------
# Record helpers
# --------------------------------------------------------------------------


def _field(record, key, default=None):
    """First finite value for `key` among its FMP field aliases."""
    if not isinstance(record, dict):
        return default
    for name in FIELD_ALIASES[key]:
        if name in record:
            value = _finite(record[name])
            if value is not None:
                return value
    return default


def _series(records, key, n=None):
    """Values of `key` across statement records, newest first."""
    out = []
    for record in records or []:
        value = _field(record, key)
        if value is not None:
            out.append(value)
    return out[:n] if n else out


def _period_labels(records, n=None):
    labels = [
        (record.get("date") if isinstance(record, dict) else None)
        for record in records or []
    ]
    return labels[:n] if n else labels


def _parse_date(text):
    try:
        return datetime.strptime(str(text)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _shift_months(day, months):
    """`day` moved back by whole calendar months, clamped to month length."""
    month_index = day.month - 1 - months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last))


def price_return(closes, months):
    """Percent price return over the trailing `months`, or None if too short.

    `closes` is a list of (date, close) pairs, oldest first.
    """
    if not closes or len(closes) < 2:
        return None
    end_day, end = closes[-1]
    target = _shift_months(end_day, months)
    earlier = [pair for pair in closes if pair[0] <= target]
    if not earlier:
        return None
    start_day, start = earlier[-1]
    # Guard against a gappy history silently anchoring on a much older bar.
    if (target - start_day).days > 20:
        return None
    start, end = _finite(start), _finite(end)
    if not start or end is None:
        return None
    return (end / start - 1.0) * 100.0


def fetch_close_series(client, ticker, years=PRICE_YEARS):
    """Split-adjusted closing prices as (date, close), oldest first.

    FMP's `close` is adjusted for splits but not for dividends, which is what
    the screen wants: dividends are scored separately and must not be counted
    twice. (`adjClose` is the total-return series - deliberately not used.)
    """
    today = datetime.now().date()
    payload = client.get(
        f"historical-price-full/{ticker}",
        **{"from": _shift_months(today, 12 * years + 1).isoformat(),
           "to": today.isoformat()},
    )

    rows = None
    if isinstance(payload, dict):
        rows = payload.get("historical")
        if not rows:
            # Batch shape: {"historicalStockList": [{"symbol": .., "historical": ..}]}
            for entry in payload.get("historicalStockList") or []:
                if str(entry.get("symbol", "")).upper() == ticker.upper():
                    rows = entry.get("historical")
                    break
    elif isinstance(payload, list):
        rows = payload

    closes = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        day, close = _parse_date(row.get("date")), _finite(row.get("close"))
        if day is not None and close is not None:
            closes.append((day, close))
    closes.sort(key=lambda pair: pair[0])       # FMP returns newest first
    return closes or None


def fetch_etf_returns(etfs, client):
    """{etf: {'ret_6m': x, 'ret_12m': y}} for the sector benchmarks.

    Doubles as the run's preflight: if every benchmark comes back rejected the
    key cannot screen anything, so the caller is told once rather than watching
    every ticker fail in turn.
    """
    out = {}
    rejected = 0
    for etf in etfs:
        try:
            closes = fetch_close_series(client, etf)
            out[etf] = {
                "ret_6m": price_return(closes, 6),
                "ret_12m": price_return(closes, 12),
            }
            if out[etf]["ret_12m"] is None:
                print(f"  ! warning: no 12-month price history for benchmark {etf}",
                      file=sys.stderr)
            continue
        except FATAL_FMP_ERRORS:
            raise
        except FMPAuthError as exc:
            rejected += 1
            reason = exc
        except Exception as exc:
            reason = exc
        out[etf] = {"ret_6m": None, "ret_12m": None}
        print(f"  ! warning: could not fetch benchmark {etf}: {reason}", file=sys.stderr)
    if etfs and rejected == len(etfs):
        raise FMPAuthError("every benchmark request was rejected")
    return out


def forward_pe_from_estimates(estimates, price):
    """price / consensus EPS for the nearest fiscal year that has not closed."""
    price = _finite(price)
    if not price or not estimates:
        return None
    today = datetime.now().date()
    dated = [(_parse_date(e.get("date")), e) for e in estimates]
    dated = [(d, e) for d, e in dated if d is not None]
    if not dated:
        return None
    # Sort on the date alone; two records sharing one would make Python try to
    # order the dicts behind them.
    future = sorted((pair for pair in dated if pair[0] >= today), key=lambda p: p[0])
    candidate = future[0][1] if future else max(dated, key=lambda p: p[0])[1]
    eps = _field(candidate, "eps_estimate")
    if eps is None or eps <= 0:
        return None
    return price / eps


def fetch_ticker(ticker, sector, etf, etf_rets, client, want_estimates=True):
    """Collect every raw metric for one ticker. Never raises, except on the
    fatal client errors that mean the rest of the run is pointless."""
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

    def guarded(label, fn, default=None):
        try:
            return fn()
        except FATAL_FMP_ERRORS:
            raise
        except FMPAuthError as exc:
            m["warnings"].append(f"{label} unavailable ({exc})")
            return default
        except Exception as exc:
            m["warnings"].append(f"{label} unavailable ({type(exc).__name__})")
            return default

    # ---- quote: price, market cap, shares ---------------------------------
    quote = guarded("quote", lambda: fetch_quote(client, ticker), {}) or {}
    m["price"] = _field(quote, "price")
    m["market_cap"] = _field(quote, "market_cap")
    shares = _field(quote, "shares")

    # ---- price history ----------------------------------------------------
    closes = guarded("price history", lambda: fetch_close_series(client, ticker))
    if closes:
        if m["price"] is None:
            m["price"] = closes[-1][1]
        m["ret_6m"] = price_return(closes, 6)
        m["ret_12m"] = price_return(closes, 12)
    else:
        m["warnings"].append("no price history")

    mc = m["market_cap"]
    if mc is None and shares and m["price"]:
        mc = m["market_cap"] = shares * m["price"]

    bench = etf_rets.get(etf, {})
    m["etf_ret_6m"], m["etf_ret_12m"] = bench.get("ret_6m"), bench.get("ret_12m")
    if m["ret_12m"] is not None and m["etf_ret_12m"] is not None:
        m["excess_12m"] = m["ret_12m"] - m["etf_ret_12m"]

    # ---- statements -------------------------------------------------------
    qis = guarded("quarterly income statement",
                  lambda: fetch_income_statement(client, ticker), [])
    qratios = guarded("quarterly ratios", lambda: fetch_ratios(client, ticker), [])
    qcf = guarded("quarterly cash flow", lambda: fetch_cash_flow(client, ticker), [])

    # Annual statements cost another request each, so they are only pulled if
    # a quarterly series turns out to be too short to work with.
    annual = {}

    def annual_records(kind, fetch):
        if kind not in annual:
            annual[kind] = guarded(f"annual {kind}", fetch, []) or []
        return annual[kind]

    # ---- revenue growth YoY over the last 4 quarters ----------------------
    q_rev = _series(qis, "revenue")
    q_rev_labels = _period_labels(qis)
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
        a_rev = _series(
            annual_records(
                "income statement",
                lambda: fetch_income_statement(client, ticker, period="annual", limit=2),
            ),
            "revenue",
        )
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
    # FMP reports margins as fractions on /ratios; fall back to the income
    # statement (gross profit, or revenue less cost of revenue) if they are
    # missing for this ticker.
    margins = [r * 100.0 for r in _series(qratios, "gross_margin_ratio", 4)]
    margin_labels = _period_labels(qratios, 4)
    if not margins:
        q_gp = _series(qis, "gross_profit")
        if not q_gp:
            q_cor = _series(qis, "cost_of_revenue")
            if q_cor and q_rev:
                q_gp = [r - c for r, c in zip(q_rev, q_cor)]
        margins = [gp / rev * 100.0 for rev, gp in zip(q_rev[:4], q_gp[:4]) if rev]
        margin_labels = q_rev_labels[:4]
    if margins:
        m["gross_margin"] = margins[0]
        m["gross_margin_quarters"] = [
            {"quarter": margin_labels[i] if i < len(margin_labels) else None,
             "gross_margin_pct": v}
            for i, v in enumerate(margins)
        ]
        # margins is newest-first; reverse so the slope reads pp per quarter.
        m["gross_margin_slope"] = slope_per_period(list(reversed(margins)))
    else:
        m["warnings"].append("gross margin unavailable")

    # ---- debt / equity ----------------------------------------------------
    # FMP's debtEquityRatio is total debt over total stockholders' equity,
    # which is the basis debt_equity_score's kink points assume.
    de = _series(qratios, "debt_equity", 1)
    if de:
        m["debt_equity"] = de[0]
        m["debt_equity_basis"] = "FMP ratios, latest quarter"
    else:
        de_annual = _series(
            annual_records(
                "ratios",
                lambda: fetch_ratios(client, ticker, period="annual", limit=1),
            ),
            "debt_equity",
            1,
        )
        if de_annual:
            m["debt_equity"] = de_annual[0]
            m["debt_equity_basis"] = "FMP ratios, latest fiscal year"
        else:
            m["warnings"].append("debt/equity unavailable")
    if m["debt_equity"] is not None and m["debt_equity"] < 0:
        m["warnings"].append("negative book equity")

    # ---- free cash flow (TTM) ---------------------------------------------
    def fcf_from(records, n):
        vals = _series(records, "fcf", n)
        if len(vals) == n:
            return sum(vals)
        ocf = _series(records, "ocf", n)
        capex = _series(records, "capex", n)
        if len(ocf) == n and len(capex) == n:
            # FMP signs capital expenditure negative.
            return sum(o - abs(c) for o, c in zip(ocf, capex))
        return None

    fcf = fcf_from(qcf, 4)
    if fcf is not None:
        m["fcf_ttm"], m["fcf_basis"] = fcf, "sum of last 4 quarters"
    else:
        acf = annual_records(
            "cash flow",
            lambda: fetch_cash_flow(client, ticker, period="annual", limit=1),
        )
        fcf = fcf_from(acf, 1)
        if fcf is not None:
            m["fcf_ttm"], m["fcf_basis"] = fcf, "most recent fiscal year"
        else:
            m["warnings"].append("free cash flow unavailable")
    if m["fcf_ttm"] is not None and mc:
        m["fcf_yield"] = m["fcf_ttm"] / mc * 100.0

    # ---- dividend yield and coverage --------------------------------------
    # Cash actually paid out over the trailing four quarters, which gives both
    # the yield (against market cap) and the free-cash-flow coverage.
    paid = _series(qcf, "dividends_paid", 4)
    total_paid = abs(sum(paid)) if len(paid) == 4 else None
    if total_paid is None:
        paid_annual = _series(
            annual_records(
                "cash flow",
                lambda: fetch_cash_flow(client, ticker, period="annual", limit=1),
            ),
            "dividends_paid",
            1,
        )
        total_paid = abs(paid_annual[0]) if paid_annual else None
    if total_paid is not None and mc:
        # A reported zero is a real 0% yield, not a missing one.
        m["div_yield"] = total_paid / mc * 100.0
    else:
        # Fall back to the reported ratio. On quarterly records it is a
        # quarterly yield, so four of them make the annual figure.
        quarterly_yields = _series(qratios, "dividend_yield", 4)
        if len(quarterly_yields) == 4:
            m["div_yield"] = sum(quarterly_yields) * 100.0
    if m["fcf_ttm"] is not None and total_paid:
        m["div_coverage"] = m["fcf_ttm"] / total_paid

    # ---- valuation --------------------------------------------------------
    # /quote carries a trailing P/E only, so the forward figure is priced off
    # the analyst consensus for the current fiscal year. One extra request per
    # ticker; --no-estimates turns it off, and a paywalled endpoint disables it
    # for the rest of the run rather than failing every ticker.
    if want_estimates and not client.skip_estimates:
        try:
            m["forward_pe"] = forward_pe_from_estimates(
                fetch_analyst_estimates(client, ticker), m["price"]
            )
        except FATAL_FMP_ERRORS:
            raise
        except FMPAuthError:
            client.skip_estimates = True
            print("  ! warning: analyst estimates are not available on this API "
                  "key - forward P/E will be blank", file=sys.stderr)
        except Exception as exc:
            m["warnings"].append(f"forward P/E unavailable ({type(exc).__name__})")
    peg = _series(qratios, "peg", 1)
    m["peg"] = peg[0] if peg else None

    core = [m["rev_yoy_avg"], m["gross_margin"], m["debt_equity"],
            m["fcf_ttm"], m["ret_12m"]]
    if all(v is None for v in core):
        m["failed"] = True
        m["warnings"].append("no usable data returned")

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
    ap.add_argument("--api-key", metavar="KEY",
                    default=os.environ.get("FMP_API_KEY", DEMO_API_KEY),
                    help="Financial Modeling Prep API key "
                         "(default: $FMP_API_KEY, else FMP's 'demo' key)")
    ap.add_argument("--delay", type=float, default=0.3, metavar="SEC",
                    help="pause between FMP requests, to stay under the burst "
                         "limit (default: 0.3)")
    ap.add_argument("--max-requests", type=int, default=DEFAULT_REQUEST_BUDGET,
                    metavar="N",
                    help=f"stop the run after this many FMP requests, to protect "
                         f"the daily quota (default: {DEFAULT_REQUEST_BUDGET}; "
                         "0 for no cap)")
    ap.add_argument("--no-estimates", action="store_true",
                    help="skip the analyst-estimates call (saves one request per "
                         "ticker; leaves forward P/E blank)")
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
            import requests  # noqa: F401
        except ImportError:
            print("requests is not installed. Run:  pip install -r requirements.txt",
                  file=sys.stderr)
            return 1

        args.api_key = args.api_key or DEMO_API_KEY
        if args.api_key == DEMO_API_KEY:
            print("  ! warning: using FMP's shared 'demo' key, which only answers "
                  "for a few sample symbols.\n"
                  "    Get a free key (250 requests/day) at "
                  "https://site.financialmodelingprep.com/developer/docs and pass "
                  "it with\n    --api-key, or set FMP_API_KEY.", file=sys.stderr)

        etfs = sorted({etf for _, _, etf in work})
        per_ticker = REQUESTS_PER_TICKER + (0 if args.no_estimates else ESTIMATES_REQUEST)
        estimated = len(work) * per_ticker + len(etfs)
        budget = args.max_requests if args.max_requests > 0 else None
        client = FMPClient(args.api_key, delay=args.delay, budget=budget)

        print(f"Screening {len(work)} tickers across "
              f"{len({s for _, s, _ in work})} sectors "
              f"({datetime.now():%Y-%m-%d %H:%M}).")
        print(f"Budgeting ~{estimated} FMP requests"
              + (f" of {budget} allowed." if budget else "."))
        if budget and estimated > budget:
            print(f"  ! warning: this run needs about {estimated} requests but is "
                  f"capped at {budget}; later tickers will be skipped.",
                  file=sys.stderr)
        print(f"Fetching benchmarks: {', '.join(etfs)}")
        try:
            etf_rets = fetch_etf_returns(etfs, client)
        except FMPAuthError as exc:
            print(f"FMP rejected this key: {exc}. "
                  + ("The 'demo' key cannot screen this universe - get a free key "
                     "at https://site.financialmodelingprep.com/developer/docs."
                     if args.api_key == DEMO_API_KEY else
                     "Check FMP_API_KEY / --api-key and the plan it is on."),
                  file=sys.stderr)
            return 1

        metrics = []
        stopped = None
        for ticker, sector, etf in work:
            if stopped:
                metrics.append({"ticker": ticker, "sector": sector, "etf": etf,
                                "failed": True, "warnings": [f"not fetched: {stopped}"]})
                continue
            print(f"  fetching {ticker} ...", end=" ", flush=True)
            try:
                m = fetch_ticker(ticker, sector, etf, etf_rets, client,
                                 want_estimates=not args.no_estimates)
            except FATAL_FMP_ERRORS as exc:
                # Out of quota - keep whatever was fetched and score that.
                stopped = str(exc)
                print("STOPPED")
                print(f"  ! warning: stopping fetch after "
                      f"{client.requests_made} requests - {exc}", file=sys.stderr)
                m = {"ticker": ticker, "sector": sector, "etf": etf, "failed": True,
                     "warnings": [f"not fetched: {stopped}"]}
            except Exception as exc:   # belt and braces; fetch_ticker guards too
                m = {"ticker": ticker, "sector": sector, "etf": etf, "failed": True,
                     "warnings": [f"unhandled error: {type(exc).__name__}: {exc}"]}
            metrics.append(m)
            if m.get("failed"):
                if not stopped:
                    print("FAILED")
                print(f"  ! warning: {ticker} could not be fetched - "
                      f"{'; '.join(m.get('warnings') or ['unknown reason'])}",
                      file=sys.stderr)
            else:
                print("ok")

        print(f"Used {client.requests_made} FMP request(s)"
              + (f"; {client.remaining()} left in this run's budget." if budget else "."))

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
