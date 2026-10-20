# ESFuturesData

See `README.md` for what this project does and how it's structured. This file
is operating context for working in this repo specifically — things that
aren't obvious from reading the code.

## Sibling projects

This is one of three related repos sharing infrastructure:

- [EquityScanner](https://github.com/SilverTurtle1/EquityScanner) — equity
  candle scanner. Same Massive API account as this project.
- [MarketDataDB](https://github.com/SilverTurtle1/MarketDataDB) — schema for
  the shared `market_data` Postgres database both this project and
  EquityScanner write to.

**The Massive API rate limit (5 calls/min, free tier) is shared** across
whatever uses the key — this project doesn't get its own independent budget
if EquityScanner happens to be fetching at the same time. Also: the free tier
has no same-day data — a day's bars only become queryable roughly the
following morning, not intraday as the session happens.

## Running scripts safely

`main.py` (via `engine.py`) and `patch_missing_bars.py` hit the live Massive
API — real network calls against the shared rate-limit budget. `Main_OLD.py`
and `Rest_Test.py` are earlier, unused exploratory scripts against the old
`polygon` client package; they also make real API calls if run and are kept
only for reference, not as something to run.

The research/backtest scripts (`candle_signal_backtest.py`,
`level_interactions.py`, `triple_barrier.py`, `weekly_value_area.py`,
`halfback_*.py`, `premarket_lime_*.py`, `ib_breakout_test.py`,
`b_engulfs_a_test.py`, `stacked_confirmation_test.py`, `run_leaderboard.py`)
read local CSVs only — no network/rate-limit cost — but most lack an
`if __name__ == "__main__":` guard and run their full analysis immediately
on `import`, not just when executed directly. Read the code rather than
importing one of these to "check" it; same lesson learned the hard way in
EquityScanner (see that repo's CLAUDE.md), just without the live-API cost
here.

## Credentials

`local_settings.py` (Massive API key) and `db_settings.py` (Postgres
password) are both gitignored and not present in a fresh clone — copy from
their `.example.py` counterparts. `engine.py`'s `_load_configurations()` and
`patch_missing_bars.py` are the only places that read `local_settings.py`
directly — keep it that way rather than adding new direct reads elsewhere.

## Things that look like bugs but aren't

- **Contract rolls follow volume, not the calendar.** `engine.py` switches to
  the next contract at the open (15:00 PT) of the first Globex session after
  a full session where its volume exceeded the outgoing contract's. The CME
  roll date (Thursday 8 days before expiration) is only the *earliest* allowed
  switch, because on this data the incoming contract holds just 3-10% of volume
  on that date. Each outgoing contract is therefore fetched through its
  expiration, and the cap is the expiration-day session (forced switch plus a
  warning if no crossover appeared). Switch times depend only on sessions
  before the switch, so an already-made roll never changes when new data
  arrives; a roll still in progress stays on the outgoing contract until the
  crossover shows up.
- **Daily reports rebuild from scratch every run.** `generate_daily_reports()`
  re-reads the entire master CSV and recomputes every day's stats on each
  call — there's no incremental/append path. Run time scales with how much
  history `config.json`'s `start_date` covers.
- **The most recent day is dropped from the report, not shown partial.**
  A trade date's row only appears once the master dataset's latest timestamp
  reaches that day's RTH session close (13:00 PT) — an in-progress session
  is excluded entirely rather than reported with partial/misleading
  high-low-volume numbers.
- **`output/ES_continuous_master_1m.csv` is overwritten, not appended, on
  every run** — it always reflects exactly whatever `config.json`'s
  `start_date`/`end_date` window currently specifies. Narrowing the config
  dates narrows the master file too (older data isn't lost — it's still in
  the per-contract `*_raw_1m_bars.csv` caches — just excluded until the range
  is widened again).
