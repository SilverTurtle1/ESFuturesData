import os
import json
import time
import logging
import requests
import pandas as pd
from datetime import date, datetime, timedelta

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

PT = "America/Los_Angeles"
QUARTER_CODES = {3: "H", 6: "M", 9: "U", 12: "Z"}
ROLL_DAYS_BEFORE_EXPIRATION = 8
SESSION_OPEN_HOUR_PT = 15
SESSION_LAST_BAR_PT = (13, 59)
SEAM_PAD_DAYS = 2
ROLL_DATA_GAP_DAYS = 7


class FuturesIntradayScanner:

    def __init__(self):
        self.config_filename = "config.json"

        # 1. Load configuration parameters
        self.api_key, self.start_date, self.end_date = self._load_configurations()

        # 2. Map out the execution timeline sequence of contracts
        self.contract_schedule = self._build_contract_schedule(self.start_date, self.end_date)

        # --- PERSISTENT TRACKER ACROSS ALL TICKERS ---
        self.global_request_count = 0

        logging.info(f"Engine Timeline Constructed. Sequence of active contracts required: "
                     f"{[c['ticker'] for c in self.contract_schedule]}")

    def _load_configurations(self):
        """Extracts the historical date range from config.json.
        The API key comes from local_settings.py (gitignored) instead, so it never lands in version control."""
        from local_settings import polygon as _local_settings
        api_key = _local_settings["api_key"]

        if not os.path.exists(self.config_filename):
            logging.warning("config.json not found! Instantiating template file.")
            defaults = {
                "start_date": "2026-01-02",
                "end_date": "2026-07-04"
            }
            with open(self.config_filename, "w") as f:
                json.dump(defaults, f, indent=4)
            return api_key, defaults["start_date"], defaults["end_date"]

        with open(self.config_filename, "r") as f:
            config = json.load(f)

        return api_key, config.get("start_date"), config.get("end_date")

    @staticmethod
    def _third_friday(year: int, month: int) -> date:
        first = date(year, month, 1)
        return first + timedelta(days=(4 - first.weekday()) % 7 + 14)

    @staticmethod
    def _session_open(trade_date: date) -> pd.Timestamp:
        """Open of a trade date's Globex session: 15:00 PT on the previous calendar day."""
        day = trade_date - timedelta(days=1)
        return pd.Timestamp(year=day.year, month=day.month, day=day.day, hour=SESSION_OPEN_HOUR_PT, tz=PT)

    @staticmethod
    def _range_bounds(start_str: str, end_str: str):
        """[start, end) of the configured range: midnight PT of start_date to midnight PT after end_date."""
        return pd.Timestamp(start_str, tz=PT), pd.Timestamp(end_str, tz=PT) + pd.DateOffset(days=1)

    def _build_contract_schedule(self, start_str: str, end_str: str) -> list:
        """
        Returns every contract that could be the front month at some point in [start_date, end_date].
        The series may leave a contract no earlier than its CME roll date (the Thursday
        ROLL_DAYS_BEFORE_EXPIRATION days before the third-Friday expiration) and no later than the open
        of its expiration-day session; the exact moment is driven by volume (see _find_roll_exit), so
        each contract's window is filled in by _resolve_windows once its bars are fetched. Each contract
        is fetched from SEAM_PAD_DAYS before the earliest it could be entered through its expiration,
        so both sides of every possible switch are available.
        """
        start_ts, end_ts = self._range_bounds(start_str, end_str)
        start_date = start_ts.date()
        end_date = pd.Timestamp(end_str).date()

        contracts = []
        for year in range(start_ts.year - 1, end_ts.year + 2):
            for month, code in QUARTER_CODES.items():
                expiration = self._third_friday(year, month)
                cme_roll_date = expiration - timedelta(days=ROLL_DAYS_BEFORE_EXPIRATION)
                contracts.append({
                    "ticker": f"ES{code}{str(year)[-1]}",
                    "expiration": expiration,
                    "cme_roll_date": cme_roll_date,
                    "earliest_exit": self._session_open(cme_roll_date),
                    "latest_exit": self._session_open(expiration),
                })

        schedule = []
        for prev, cur in zip(contracts, contracts[1:]):
            if cur["latest_exit"] <= start_ts or prev["earliest_exit"] >= end_ts:
                continue

            fetch_start = max((prev["earliest_exit"] - timedelta(days=SEAM_PAD_DAYS)).date(), start_date)
            fetch_end = min(cur["expiration"], end_date)
            schedule.append({
                **cur,
                "start": fetch_start.strftime("%Y-%m-%d"),
                "end": fetch_end.strftime("%Y-%m-%d"),
            })

        return schedule

    @staticmethod
    def _session_volumes(df: pd.DataFrame) -> pd.Series:
        """Total volume per Globex trade date (bars at or after 15:00 PT belong to the next day)."""
        trade_dates = (df.index + pd.to_timedelta((df.index.hour >= 15).astype(int), unit="D")).date
        return df["Volume"].groupby(trade_dates).sum()

    @staticmethod
    def _session_last_bar(trade_date: date) -> pd.Timestamp:
        """Start of the last 1-minute bar of a trade date's session (the 14:00 PT maintenance halt follows)."""
        return pd.Timestamp(year=trade_date.year, month=trade_date.month, day=trade_date.day,
                            hour=SESSION_LAST_BAR_PT[0], minute=SESSION_LAST_BAR_PT[1], tz=PT)

    def _find_roll_exit(self, out_info: dict, in_info: dict, out_df: pd.DataFrame, in_df: pd.DataFrame):
        """
        Finds when the series leaves the outgoing contract: the open of the first Globex session, on or
        after the CME roll date, in which the incoming contract traded more volume than the outgoing one.
        That whole session is built from the incoming contract, which matches how continuous charts label
        the roll day, so its value area is in the same prices as the chart it is plotted on. Judging a
        session needs its full volume, so a roll is only decided once the session is complete. That is
        fine for a batch run the morning after, but it is hindsight for any live intraday use: the
        crossover day's bars stay on the outgoing contract until the session ends, then switch. If no
        crossover appears before the expiration-day session, switches at that session's open and warns.
        Returns None when the available data ends before a decision can be made (e.g. the config range
        ends mid-roll).
        """
        label = f"Roll {out_info['ticker']} -> {in_info['ticker']}"
        out_vol, in_vol = self._session_volumes(out_df), self._session_volumes(in_df)
        trade_dates = sorted(set(out_vol.index) | set(in_vol.index))
        data_end = out_df.index.max()

        for trade_date in trade_dates:
            if trade_date < out_info["cme_roll_date"] or trade_date >= out_info["expiration"]:
                continue

            if data_end < self._session_last_bar(trade_date):
                logging.info(f"{label}: no volume crossover yet within the available data; staying on "
                             f"{out_info['ticker']}.")
                return None

            if in_vol.get(trade_date, 0.0) > out_vol.get(trade_date, 0.0):
                exit_ts = self._session_open(trade_date)
                days_after_cme = (trade_date - out_info["cme_roll_date"]).days
                logging.info(f"{label}: {in_info['ticker']} out-traded {out_info['ticker']} on {trade_date} "
                             f"({in_vol.get(trade_date, 0.0):,.0f} vs {out_vol.get(trade_date, 0.0):,.0f}), "
                             f"{days_after_cme} calendar days after the CME roll date {out_info['cme_roll_date']}. "
                             f"Switching at {exit_ts}, the open of that session, so {in_info['ticker']} owns "
                             f"it from the start.")
                return exit_ts

        exit_ts = out_info["latest_exit"]
        if exit_ts > data_end:
            logging.info(f"{label}: no volume crossover yet within the available data; staying on "
                         f"{out_info['ticker']}.")
            return None

        logging.warning(f"{label}: {in_info['ticker']} never out-traded {out_info['ticker']} between the CME "
                        f"roll date {out_info['cme_roll_date']} and expiration {out_info['expiration']}; forcing "
                        f"the switch at {exit_ts}.")
        return exit_ts

    def _resolve_windows(self, frames: dict):
        """
        Sets each scheduled contract's exact front-month window (window_start inclusive, window_end
        exclusive) from the data-driven switch times, clipped to the configured date range. The windows
        tile the range exactly, so stitching needs no de-duplication to pick a winner at a seam.
        """
        start_ts, end_ts = self._range_bounds(self.start_date, self.end_date)
        schedule = self.contract_schedule

        exits = []
        for out_info, in_info in zip(schedule, schedule[1:]):
            out_df, in_df = frames.get(out_info["ticker"]), frames.get(in_info["ticker"])
            if out_df is None or in_df is None:
                exit_ts = out_info["earliest_exit"]
                logging.warning(f"Roll {out_info['ticker']} -> {in_info['ticker']}: missing bars for one side; "
                                f"falling back to the CME roll date switch at {exit_ts}.")
            else:
                exit_ts = self._find_roll_exit(out_info, in_info, out_df, in_df)
                gap = pd.Timedelta(days=ROLL_DATA_GAP_DAYS)
                if exit_ts is None and in_df.index.max() > out_df.index.max() + gap:
                    raise RuntimeError(
                        f"Roll {out_info['ticker']} -> {in_info['ticker']} can't be decided: {out_info['ticker']} "
                        f"bars end {out_df.index.max()} but {in_info['ticker']} runs to {in_df.index.max()}. "
                        f"The {out_info['ticker']} cache looks incomplete; re-run to extend it, or delete "
                        f"{out_info['ticker']}_raw_1m_bars.csv to rebuild it. Refusing to write a truncated series.")
            exits.append(end_ts if exit_ts is None else exit_ts)
        exits.append(end_ts)

        window_start = start_ts
        for info, exit_ts in zip(schedule, exits):
            window_end = min(exit_ts, end_ts)
            info["window_start"], info["window_end"] = window_start, window_end
            window_start = max(window_end, window_start)

    def _request_bars(self, ticker: str, api_start: str, api_end: str) -> list:
        """Pages through the Massive futures aggregates endpoint for one date range, newest bars first."""
        # INCLUSIVE EXTENSION TO CAPTURE ABSOLUTE END OF LAST SESSION
        api_end_inclusive = (pd.to_datetime(api_end) + timedelta(days=1)).strftime("%Y-%m-%d")

        logging.info(f"Querying Massive API for {ticker} ({api_start} to {api_end_inclusive})...")
        url = f"https://api.massive.com/futures/v1/aggs/{ticker}"
        all_bars = []
        batch_limit = 3000

        params = {
            "resolution": "1min",
            "limit": batch_limit,
            "sort": "window_start.desc",
            "window_start.gte": api_start,
            "window_start.lte": api_end_inclusive,
            "apiKey": self.api_key,
        }

        for i in range(100):
            request_num = i + 1
            # Increment the persistent cross-ticker counter
            self.global_request_count += 1

            logging.info(
                f"Firing API Request {request_num} (Global: {self.global_request_count}) for {ticker} delta patch...")

            response = requests.get(url, params=params)
            if response.status_code != 200:
                logging.error(f"API Error on request {request_num}: {response.text}")
                break

            res_data = response.json()
            bars = res_data.get("data", res_data.get("results", []))
            if not bars:
                break

            all_bars.extend(bars)
            if len(bars) < batch_limit:
                logging.info(f"Reached absolute edge of available historical data for {ticker}.")
                break

            params["window_start.lt"] = bars[-1]["window_start"]

            # USE GLOBAL COUNTER TO PRESERVE COOL-DOWNS ACROSS TICKERS
            if self.global_request_count % 5 == 0:
                logging.warning(
                    "Free Tier Throttle Guard active (Global Cap). Pausing 60s to safely reset clocks...")
                time.sleep(60)
            else:
                time.sleep(2)

        return all_bars

    def _fetch_single_contract_bars(self, contract_info: dict) -> pd.DataFrame:
        """Handles fetching, caching, and extending data for a single specific contract."""
        ticker = contract_info["ticker"]
        req_start = contract_info["start"]
        req_end = contract_info["end"]
        csv_filename = f"{ticker}_raw_1m_bars.csv"

        target_start_date = pd.to_datetime(req_start).date()
        target_end_date = pd.to_datetime(req_end).date()

        df_cache = pd.DataFrame()
        cache_exists = os.path.exists(csv_filename)

        if cache_exists:
            try:
                df_cache = pd.read_csv(csv_filename)
                df_cache["Timestamp"] = pd.to_datetime(df_cache["Timestamp"], utc=True)
                df_cache.set_index("Timestamp", inplace=True)
                df_cache.index = df_cache.index.tz_convert("America/Los_Angeles")

                cache_start_date = df_cache.index.min().date()
                cache_end_date = df_cache.index.max().date()

                # Check for cache hit with market padding
                if (cache_start_date <= target_start_date + timedelta(days=1)) and (
                        cache_end_date >= target_end_date - timedelta(days=1)):
                    # Slice exact required subset from cache
                    return df_cache[
                        (df_cache.index.date >= target_start_date) & (df_cache.index.date <= target_end_date)]

                logging.info(
                    f"Cache Extension for {ticker}: File holds ({cache_start_date} to {cache_end_date}). Adjusting...")
            except Exception as e:
                logging.warning(f"Error reading cache for {ticker}: {e}. Rebuilding...")
                cache_exists = False

        # Determine exact missing API ranges: the head and/or the tail of the cache can be short
        if cache_exists and not df_cache.empty:
            api_ranges = []
            if target_start_date < cache_start_date:
                api_ranges.append((req_start, (cache_start_date + timedelta(days=1)).strftime("%Y-%m-%d")))
            if target_end_date > cache_end_date:
                api_ranges.append(((cache_end_date - timedelta(days=1)).strftime("%Y-%m-%d"), req_end))
        else:
            api_ranges = [(req_start, req_end)]

        try:
            all_bars = []
            for n, (api_start, api_end) in enumerate(api_ranges):
                if n:
                    time.sleep(2)
                all_bars.extend(self._request_bars(ticker, api_start, api_end))

            if not all_bars and df_cache.empty:
                return pd.DataFrame()

            parsed_rows = [
                {
                    "Timestamp": pd.to_datetime(item["window_start"], unit="ns", utc=True),
                    "Open": float(item["open"]),
                    "High": float(item["high"]),
                    "Low": float(item["low"]),
                    "Close": float(item["close"]),
                    "Volume": float(item["volume"]),
                }
                for item in all_bars
            ]

            df_new = pd.DataFrame(parsed_rows)
            if not df_new.empty:
                df_new.set_index("Timestamp", inplace=True)
                df_new = df_new.tz_convert("America/Los_Angeles")

            if cache_exists and not df_cache.empty:
                df_combined = pd.concat([df_cache, df_new])
            else:
                df_combined = df_new

            df_combined = df_combined[~df_combined.index.duplicated(keep='first')].sort_index()
            df_combined.reset_index().to_csv(csv_filename, index=False)

            # --- METHOD EXIT THROTTLE SEAL ---
            if self.global_request_count % 5 == 0:
                logging.warning(
                    "Loop broken at throttle threshold. Forcing mandatory 60s cooldown before next contract sequence...")
                time.sleep(60)

            return df_combined[
                (df_combined.index.date >= target_start_date) & (df_combined.index.date <= target_end_date)]

        except Exception as e:
            logging.error(f"Failed pulling network block for {ticker}: {e}", exc_info=True)
            return df_cache

    def process_and_backtest(self) -> pd.DataFrame:
        """Gathers data across all required quarterly contracts, welds them together seamlessly, and saves the master timeline."""
        frames = {}

        # Pull each contract sequentially, from before its earliest possible entry through its expiration
        for contract_info in self.contract_schedule:
            logging.info(f"Processing sequence slice for active contract: {contract_info['ticker']}")
            df_segment = self._fetch_single_contract_bars(contract_info)
            if not df_segment.empty:
                frames[contract_info["ticker"]] = df_segment

        if not frames:
            logging.error("No valid transactional blocks could be sourced.")
            return pd.DataFrame()

        # Work out where each roll happens from the fetched volumes, then trim every contract to its
        # exact front-month window so the switch time is explicit rather than a side effect of which
        # duplicate bars survive de-duplication
        self._resolve_windows(frames)
        master_raw_segments = []
        for info in self.contract_schedule:
            df = frames.get(info["ticker"])
            if df is None:
                continue
            trimmed = df[(df.index >= info["window_start"]) & (df.index < info["window_end"])]
            if not trimmed.empty:
                master_raw_segments.append(trimmed)

        # Stitch all high-volume contract blocks together into one continuous line
        raw_df = pd.concat(master_raw_segments)
        raw_df = raw_df[~raw_df.index.duplicated(keep='first')].sort_index()

        # --- REPAIR VOLUME GAPS ACROSS THE ENTIRE WELDED INDEX ---
        logging.info("Repairing volume gaps across active market hours...")
        full_time_idx = pd.date_range(
            start=raw_df.index.min(),
            end=raw_df.index.max(),
            freq="1min",
            tz="America/Los_Angeles"
        )

        raw_df = raw_df.reindex(full_time_idx)

        # --- FIX: EXPLICITLY CONVERT TIMEZONE BEFORE EXTRACTING PROPERTIES ---
        localized_index = raw_df.index.tz_convert("America/Los_Angeles")
        day_of_week = localized_index.dayofweek  # 0=Monday, 4=Friday, 5=Saturday, 6=Sunday
        hour = localized_index.hour

        # 1. Flag the true daily 14:00 to 15:00 Pacific maintenance window
        is_maintenance = (hour == 14)

        # 2. Flag Weekends (Friday 14:00 PT to Sunday 15:00 PT)
        is_weekend = (
                (day_of_week == 5) |  # All day Saturday
                ((day_of_week == 4) & (hour >= 14)) |  # Friday after 14:00 PT
                ((day_of_week == 6) & (hour < 15))  # Sunday before 15:00 PT
        )

        # Drop closure periods completely from our tracking index matrix
        raw_df = raw_df[~(is_maintenance | is_weekend)]

        # --- NOW FILL TRUE INTRADAY DATA HOLES SAFELY ---
        raw_df[["Open", "High", "Low", "Close"]] = raw_df[["Open", "High", "Low", "Close"]].ffill()
        raw_df["Volume"] = raw_df["Volume"].fillna(0.0)

        # SAVE REPAIRED MASTER TIMELINE TO DISK
        master_csv_name = "output/ES_continuous_master_1m.csv"
        logging.info(f"Disk Persistence: Saving clean, gapless active market timeline to **{master_csv_name}**...")
        raw_df.reset_index().rename(columns={"index": "Timestamp"}).to_csv(master_csv_name, index=False)

        return raw_df
