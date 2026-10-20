# ESFuturesData

Pulls, stitches, and maintains a continuous 1-minute ES (E-mini S&P 500) futures
series from the Massive (formerly Polygon.io) API, then builds daily
session-based reports (value area, POC, session highs/lows) on top of it.

## How it fits together

- **`engine.py`** (`FuturesIntradayScanner`) walks the CME quarterly contract
  calendar, fetches each contract's 1-minute bars (caching per-contract to
  `<TICKER>_raw_1m_bars.csv` and only requesting the missing date delta on
  each run), welds them into one continuous series, repairs volume/session
  gaps, and writes the result to `output/ES_continuous_master_1m.csv`. The
  series rolls to the next contract at the first Globex session after its
  volume overtakes the outgoing contract's, never before the CME roll date
  (the Thursday 8 days before expiration) and no later than expiration.
- **`main.py`** orchestrates a full run: refresh data via `engine.py` → sync
  the master series into the shared `market_data` Postgres database
  (`db_sync.py`) → generate the two daily reports (`ES_daily_value_area.csv`,
  `ES_daily_comprehensive_summary.csv`). A day is only included in the reports
  once its RTH session has actually closed — in-progress sessions are dropped,
  not shown partially filled.
- **`indicators.py`** / `volume_profile.py` hold the shared indicator and
  volume-profile calculations the reports and research scripts build on.
- Everything else (`candle_signal_backtest.py`, `level_interactions.py`,
  `triple_barrier.py`, `weekly_value_area.py`, `halfback_*.py`,
  `premarket_lime_*.py`, `ib_breakout_test.py`, `b_engulfs_a_test.py`,
  `stacked_confirmation_test.py`, `run_leaderboard.py`) are standalone
  research/backtest scripts built on top of the master dataset.
- `Main_OLD.py` / `Rest_Test.py` are earlier, now-unused exploratory scripts
  against the `polygon` client package — kept for reference.

## Shared infrastructure

- **`output/`** is a symlink into a Google Drive–synced folder
  (`Shared Data/ESFuturesData`), so the generated CSVs are visible to anyone
  the folder is shared with, without any extra upload step.
- **`market_data`** is a Postgres database shared with other projects (see
  [MarketDataDB](https://github.com/SilverTurtle1/MarketDataDB) for the
  schema) — this project writes ES futures bars into it under
  `asset_type='futures'`, alongside
  [EquityScanner](https://github.com/SilverTurtle1/EquityScanner)'s equity
  bars under `asset_type='equity'`.

## Setup

```bash
python3 -m venv .venv
.venv/bin/python3 -m pip install pandas numpy requests psycopg2-binary pandas-ta-classic
cp local_settings.example.py local_settings.py   # drop in your Massive API key
cp db_settings.example.py db_settings.py          # drop in your Postgres password
```

`config.json` holds the non-secret settings (date range, session-hour
boundaries, resample timeframe).

## Running

```bash
.venv/bin/python3 main.py
```
