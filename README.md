# research

## screen.py — multi-sector stock screen (2-year hold horizon)

Pulls fundamentals and price history from [Financial Modeling Prep](https://site.financialmodelingprep.com/developer/docs)
(FMP), scores every ticker against a sector-specific weighting, and prints a ranked
table per sector plus a diversified overall Top 5.

### Setup

```bash
pip install -r requirements.txt   # just requests
export FMP_API_KEY=your_key_here  # free tier: 250 requests/day
```

An API key is required. FMP's shared `demo` key (the default when `FMP_API_KEY` is
unset) only answers for a handful of sample symbols, so the screen stops with a pointer
to the signup page rather than failing every ticker one at a time. A free key covers a
full run several times over — see [Rate limits](#rate-limits).

### Running

```bash
python screen.py                              # full screen
python screen.py --components                 # also show per-component sub-scores
python screen.py --csv "runs/$(date +%F).csv" # save a dated snapshot to diff later
python screen.py --sectors Energy Biotech     # subset by sector
python screen.py --tickers AMD NVDA           # subset by ticker
python screen.py --api-key KEY                # instead of $FMP_API_KEY
python screen.py --no-estimates               # skip forward P/E, save a request/ticker
```

Nothing is cached between runs, so re-running every week or two just re-fetches. To
pull once and re-score many times (e.g. while tuning the weights), use:

```bash
python screen.py --dump-json raw.json   # fetch + save
python screen.py --from-json raw.json   # re-score offline, no network
```

### Rate limits

FMP's free tier allows 250 requests/day. Each ticker costs five requests — income
statement, ratios, cash-flow statement, price history, quote — plus one for the analyst
estimates behind forward P/E, and each benchmark ETF costs one price history. The
default universe of 17 tickers and 4 ETFs therefore runs at about **106 requests**, so a
free key is good for two full screens a day.

* `--max-requests N` caps what a single run may spend (default 250, `0` to disable).
  When the cap is hit the run stops fetching, reports which tickers were skipped, and
  scores what it already has.
* `--delay SEC` paces requests (default 0.3s) to stay under the burst limit. A 429 is
  retried with exponential backoff before the run gives up.
* `--no-estimates` drops the forward-P/E request, taking a full run to ~89 requests.
* Responses are cached for the life of a run, and annual-period statements are only
  fetched when a quarterly one turns out to be too short.

Every response is fetched once, so `--dump-json` + `--from-json` is the cheap way to
re-tune weights: pull the data once, then re-score offline as often as you like without
spending any quota.

### Endpoints used

| Metric | Endpoint |
| --- | --- |
| Revenue growth YoY | `/api/v3/income-statement/{ticker}?period=quarter` |
| Gross margin (level + trend), debt/equity, PEG | `/api/v3/ratios/{ticker}?period=quarter` |
| Free cash flow, dividends paid | `/api/v3/cash-flow-statement/{ticker}?period=quarter` |
| 6mo/12mo price returns | `/api/v3/historical-price-full/{ticker}` |
| Price, market cap, shares | `/api/v3/quote/{ticker}` |
| Forward P/E | `/api/v3/analyst-estimates/{ticker}` |

`FMP_BASE_URL` overrides the API root if you need to point at a different revision of
the API.

### Universe

| Sector | Tickers | Benchmark |
| --- | --- | --- |
| Semiconductor/Tech | AMD, MRVL, AVGO, NVDA, QCOM, TSM | SOXX |
| Energy | SHEL, CVX, XOM, COP | XLE |
| HVAC/Home Equipment | TT, CARR, REZI, LII | XHB |
| Biotech | AMGN, VRTX, REGN | XBI |

### Scoring

Each sector scores 0–100 on its own weighting:

| Component | Semis | Energy | HVAC | Biotech |
| --- | --- | --- | --- | --- |
| Revenue growth YoY | 35% | — | 30% | 25% |
| Gross margin (level + trend) | 25% | — | 25% | — |
| Free cash flow yield | — | 35% | — | 25% |
| Dividend yield + coverage | — | 30% | — | — |
| Debt/equity | 15% | 20% | 20% | 20% |
| Price momentum vs sector ETF | 25% | 15% | 25% | 30% |

Raw metrics are mapped onto 0–100 through the linear bands in `BANDS`; the anchors that
the screen pins are 30%+ revenue growth and a 45%+ gross margin scoring 100 for semis.
Leverage is scored on a kinked curve — D/E of 0 scores 100, 1.0 scores 40, and 2.0 or
worse (including negative book equity) scores 0. Momentum is the 12-month return in
excess of the sector ETF; the 6-month return is reported but not scored.

`SECTORS`, `WEIGHTS`, and `BANDS` at the top of the script are the only things you need
to touch to re-tune the screen or change the universe.

### Missing data

Every metric is optional. If one is unavailable the component drops out and the
remaining weights are renormalized — the `DATA` column shows what share of the sector's
weight actually went into the score, and anything under 50% is flagged. A ticker that
fails to fetch entirely is reported as a warning and listed under "Could not evaluate"
rather than crashing the run.

### Caveats

* Price returns are split-adjusted but **not** dividend-adjusted, so dividends are not
  double-counted against the dividend-yield component.
* FMP's quarterly statements usually cover 5–8 quarters. With 8, revenue growth is the
  average of the last 4 quarter-over-year-ago comparisons; with fewer, the script falls
  back to TTM vs. prior fiscal year, then to annual-over-annual, and reports which basis
  it used in the CSV (`rev_growth_basis`).
* Debt/equity comes from FMP's `debtEquityRatio` (total debt over stockholders' equity)
  rather than being recomputed from the balance sheet; `debt_equity_basis` in the CSV
  records which period it came from. It feeds the leverage score directly, so it is
  worth spot-checking against a filing the first time you run the screen.
* Dividend yield is trailing cash actually paid (from the cash-flow statement) over
  market cap, which is also what the free-cash-flow coverage ratio divides by. FMP's
  reported `dividendYield` is only used if that line is missing.
* `/quote` carries a trailing P/E only, so forward P/E is priced off the analyst
  consensus EPS for the nearest fiscal year that has not closed. It is reported for
  context and is not scored; neither is PEG, which is taken as FMP reports it. If
  analyst estimates are not on your plan the column is simply blank.
* Price returns use FMP's `close`, which is split-adjusted but not dividend-adjusted
  (`adjClose` is the total-return series and is deliberately not used).
* FMP's free data is occasionally wrong or stale. Spot-check anything before acting
  on it.

This is a quantitative screen, not investment advice. Past performance does not predict
future returns.
