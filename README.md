# research

## screen.py — multi-sector stock screen (2-year hold horizon)

Pulls fundamentals and price history from Yahoo Finance via `yfinance`, scores every
ticker against a sector-specific weighting, and prints a ranked table per sector plus a
diversified overall Top 5.

### Setup

```bash
pip install -r requirements.txt   # just yfinance (which pulls in pandas/numpy)
```

### Running

```bash
python screen.py                              # full screen
python screen.py --components                 # also show per-component sub-scores
python screen.py --csv "runs/$(date +%F).csv" # save a dated snapshot to diff later
python screen.py --sectors Energy Biotech     # subset by sector
python screen.py --tickers AMD NVDA           # subset by ticker
```

Nothing is cached between runs, so re-running every week or two just re-fetches. To
pull once and re-score many times (e.g. while tuning the weights), use:

```bash
python screen.py --dump-json raw.json   # fetch + save
python screen.py --from-json raw.json   # re-score offline, no network
```

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
* Yahoo's quarterly statements usually cover 5–8 quarters. With 8, revenue growth is the
  average of the last 4 quarter-over-year-ago comparisons; with fewer, the script falls
  back to TTM vs. prior fiscal year, then to annual-over-annual, and reports which basis
  it used in the CSV (`rev_growth_basis`).
* Yahoo data is free and occasionally wrong or stale. Spot-check anything before acting
  on it.

This is a quantitative screen, not investment advice. Past performance does not predict
future returns.
