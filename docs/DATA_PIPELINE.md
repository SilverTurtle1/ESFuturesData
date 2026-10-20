# ES Futures Data: Pipeline & Methodology

This document explains how the project turns individual CME E-mini S&P 500 (ES) futures contracts into one continuous, analysis-ready 1-minute price series, and how the daily value-area levels used by my TradingView Market Profile indicator are calculated from it. It is the foundation for every analysis and backtest in this repo.

## Why this exists

Futures contracts expire. ES trades in quarterly contracts (March, June, September, December), and each one is only the actively traded "front month" for about three months. Any analysis that spans more than a quarter needs a single continuous series, and how that series is built (when to roll, how to handle the seam, what to do with gaps) directly affects every level and statistic computed downstream. Commercial continuous series make these choices for you, often invisibly. This project makes them explicitly.

## Pipeline overview

```
Massive API (1-min bars, per contract)
        │  delta fetch + per-contract cache (<TICKER>_raw_1m_bars.csv)
        ▼
Contract schedule → roll detection → trim → stitch → 1-min grid repair
        │
        ▼
output/ES_continuous_master_1m.csv ──► Postgres (market_data, shared)
        │
        ▼
Daily reports (closed sessions only)
   ├─ ES_daily_value_area.csv           (VAH / VAL, optional POC & volume)
   └─ ES_daily_comprehensive_summary.csv (session highs/lows, A-period, prior RTH)
        │
        ▼
TradingView Market Profile indicator · research & backtest scripts
```

`main.py` runs the whole sequence: refresh data (`engine.py`), sync to Postgres (`db_sync.py`), and rebuild the daily reports.

## 1. Data source and constraints

Bars come from the Massive (formerly Polygon.io) futures aggregates endpoint at 1-minute resolution, per individual contract (for example `ESZ5`). The free tier shapes several design decisions:

- **Rate limit of 5 calls per minute, shared across projects.** The engine keeps a global request counter across all contracts and pauses 60 seconds after every fifth call, so a multi-contract refresh never trips the limit, even when it spans several tickers.
- **No same-day data.** A session's bars become available roughly the following morning, so the pipeline is a daily batch process, not a live feed.
- **Incremental fetching.** Each contract's bars are cached locally. On every run the engine compares the cache's date range with what the schedule requires and requests only the missing delta, then merges, de-duplicates, and rewrites the cache.

## 2. Contract roll and stitching

**Where volume actually moves.** ES expires on the third Friday of each quarter month, and the CME roll date is the Thursday eight calendar days before expiration. On real data, liquidity moves later than that date. Across all seven rolls from March 2025 to September 2026, the incoming contract held only 3–10% of the day's volume on roll Thursday, and no more than 25% the following day (the Friday of roll week). Rolling on the calendar would build several days of every roll week from a thinly traded contract.

**Roll rule.** The CME roll date is therefore treated as the earliest allowed switch, and the series rolls when volume crosses over. The crossover session is the first Globex session, on or after the CME roll date, in which the incoming contract traded more volume than the outgoing one. A session here is a complete Globex trade date, from 15:00 PT the previous day to 15:00 PT, counting all hours rather than RTH only, so overnight trading counts toward the comparison. The new contract owns that whole session, from its 15:00 PT open (Sunday evening for a Monday crossover). In all seven rolls the crossover session was Monday's of expiration week, four calendar days after the CME date, so Monday is the first session the new contract owns. If no crossover appears before the expiration-day session, the engine switches at that session's open and logs a warning.

This follows how continuous charts label the roll day: on a chart such as TradingView's ES1!, the crossover day itself plots from the new contract. Switching only after the crossover session would have built that one day from the old contract, and its value area would reach the next day's chart as prior-day VAH and VAL off by the calendar spread (about 50 to 68 points here). Choosing a session's contract from that same session's volume is deliberate. It looks ahead within the day, but the daily batch runs the next morning, when the full session is already known, so the daily levels are unaffected. It matters only for intraday work on the crossover day itself, where the contract choice depends on volume from later that day. It changes which contract's real prices are used, never the prices themselves.

**Switching at a session boundary.** Switches always happen at 15:00 PT, the open of a Globex session, which is the start of the crossover session. Every session, including the overnight Asia and London segments, is therefore built from a single contract, and no day's highs, lows, or volume profile ever mixes two contracts' prices.

**Seam padding and trimming.** Each contract is fetched from two days before the earliest it could be entered (the CME roll date) through its own expiration, so both contracts' volumes are available for every candidate session. Once the switch times are known, each contract is trimmed to its exact front-month window before stitching, which makes the switch explicit instead of a side effect of de-duplication.

**Roll log and safeguards.** Every run logs each roll: the crossover session, both contracts' volumes, and how many calendar days after the CME date it falls. A roll is decided once the crossover session is complete, and it never changes after that, because the decision uses only that session and the sessions before it. While a roll is still in progress, the series stays on the outgoing contract until the crossover session finishes. If a roll can't be decided but the incoming contract's data runs well past where the outgoing one ends, which means the outgoing cache is incomplete, the run stops with an error rather than write a truncated series.

**Unadjusted prices (deliberate).** At each roll, the new contract trades at a different price from the old one (the calendar spread). Many continuous series remove that jump by back-adjusting history, but back-adjusted prices never actually traded. Because this data exists to produce price levels that must line up with live charts, the series keeps real traded prices. The trade-off is an artificial jump at each roll seam. The calendar spread between the two contracts was between about 50 and 68 points across these seven rolls, measured at the last minute both traded, and because the switch lands at Sunday's open, the jump visible in the series also includes whatever the market did over the weekend. This matters for return calculations that cross a seam but not for intraday levels.

## 3. Gap handling

The stitched series is reindexed onto a complete 1-minute grid, then known market closures are removed: the daily 14:00–15:00 PT maintenance halt, and the weekend from Friday 14:00 PT to Sunday 15:00 PT. Remaining holes are filled by carrying the last price forward, with volume set to zero, so the master file is gapless in time.

Filled bars carry zero volume by design, and every downstream report strips zero-volume rows before calculating. Synthetic bars therefore keep the time index continuous but never contribute to a high, a low, or a volume profile.

Gap-filling can also hide real data loss, so suspicious holes are investigated against the source:

- **Two genuine API gaps** (in September and November 2025, about 1,100 and 650 missing minutes) were confirmed against the live API and backfilled with `patch_missing_bars.py`.
- **A third gap was left alone deliberately.** The halt after 06:29 PT on January 9, 2025 coincides with the national day of mourning for President Carter, when CME equity futures closed early. That is a real closure, not missing data.

## 4. Sessions and trade dates

All timestamps are stored in UTC and processed in Pacific time, so daylight-saving shifts are handled by the timezone library rather than by hand. Each bar is assigned to a Globex trade date: anything at or after 15:00 PT belongs to the next day's session.

| Session    | Pacific time   |
|------------|----------------|
| Asia       | 15:00 – 00:00  |
| London     | 00:00 – 05:00  |
| Premarket  | 05:00 – 06:30  |
| A-period   | 06:30 – 07:00  |
| RTH        | 06:30 – 13:00  |

Session boundaries live in `config.json`, not in code.

## 5. Value area calculation

Daily value areas are computed from the regular-trading-hours (RTH) volume profile.

1. **Build the volume profile.** True volume-at-price requires tick data. With 1-minute bars, each bar's volume is spread evenly across every 0.25-point tick between its low and high. This approximation sharpens as bar size shrinks, and 1-minute bars are fine-grained relative to a full session's range.
2. **Find the point of control (POC).** This is the tick with the most volume.
3. **Expand to the value area.** Starting from the POC, the algorithm compares the combined volume of the next two ticks above with the next two below, adds the larger side, and repeats until the area holds the target share of session volume. This is the standard Market Profile expansion method.
4. **Use a 68.2% target.** The conventional value is 70%. This project uses 68.2%, the share of a normal distribution within one standard deviation of the mean, which makes the value area a literal one-sigma range.
5. **Report closed sessions only.** A day appears in the reports only after its RTH session has closed, so a partially formed profile is never published as if it were final.

The daily output (`ES_daily_value_area.csv`) supplies the prior-day VAH and VAL levels plotted by the TradingView Market Profile indicator.

## 6. Outputs

- **`output/ES_continuous_master_1m.csv`:** The gapless continuous series, rebuilt in full on every run from the cached contract files, for the date window in `config.json`.
- **`ES_daily_value_area.csv`:** Date, VAH, VAL, and optionally POC and RTH volume.
- **`ES_daily_comprehensive_summary.csv`:** Value area plus Asia, London, and premarket highs and lows, A-period high, low, and halfback, and current and prior RTH high and low.
- **Postgres (`market_data`):** The master series is upserted into a shared database (schema in the MarketDataDB repo), so other projects can query the same history.

The `output/` folder syncs to a shared Google Drive folder, so the reports are available without a separate upload step.

## 7. Research built on this data

The master series and daily levels feed a set of research scripts. Two are reusable frameworks rather than one-off tests:

- **`level_interactions.py`** catalogs key levels (session highs and lows, A-period halfback, prior-day RTH high and low, prior-day and prior-week value areas). It builds a no-lookahead validity table, so each level is only used from the moment it was actually knowable. It then classifies every first touch as a rejection, a breakout, or chop, and records forward returns and maximum favorable and adverse excursion at several horizons.
- **`triple_barrier.py`** scores any set of candidate entries by walking forward on 1-minute bars to see whether the profit target or the stop is hit first. When a single bar spans both, the outcome is resolved conservatively as a loss, because OHLC bars don't reveal the order of moves within the bar.

## 8. Known limitations and next steps

- **Volume-at-price approximation.** The even-distribution method is an approximation of true volume at price. A tick-data source would allow an exact profile.
- **Duplicated value-area logic.** The calculation exists in both `main.py` and `volume_profile.py`. Consolidating it into one function would prevent the two from drifting apart.
- **Hard-coded data paths.** Several research scripts point to an absolute data path instead of the `output/` folder, which makes them less portable.
