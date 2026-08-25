import os
import json
import time
import logging
import requests
import pandas as pd
from datetime import datetime, timedelta
from indicators import calculate_custom_tv_indicator

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


class FuturesIntradayScanner:

    def __init__(self):
        self.config_filename = "config.json"

        # 1. Load configuration parameters
        self.api_key, self.start_date, self.end_date, self.timeframe = self._load_configurations()

        # 2. Map out the execution timeline sequence of contracts
        self.contract_schedule = self._build_contract_schedule(self.start_date, self.end_date)

        # --- PERSISTENT TRACKER ACROSS ALL TICKERS ---
        self.global_request_count = 0

        logging.info(f"Engine Timeline Constructed. Sequence of active contracts required: "
                     f"{[c['ticker'] for c in self.contract_schedule]}")

    def _load_configurations(self):
        """Extracts historical boundaries and analysis timeframes from config.json.
        The API key comes from local_settings.py (gitignored) instead, so it never lands in version control."""
        from local_settings import polygon as _local_settings
        api_key = _local_settings["api_key"]

        if not os.path.exists(self.config_filename):
            logging.warning("config.json not found! Instantiating template file.")
            defaults = {
                "start_date": "2026-01-02",
                "end_date": "2026-07-04",
                "analysis_timeframe": "30min"
            }
            with open(self.config_filename, "w") as f:
                json.dump(defaults, f, indent=4)
            return api_key, defaults["start_date"], defaults["end_date"], defaults["analysis_timeframe"]

        with open(self.config_filename, "r") as f:
            config = json.load(f)

        return api_key, config.get("start_date"), config.get("end_date"), config.get("analysis_timeframe", "30min")

    def _get_contract_for_date(self, dt: datetime) -> str:
        """Returns the specific active CME contract code for any given calendar date."""
        month = dt.month
        day = dt.day
        year_last_digit = str(dt.year)[-1]

        # Standard operational rollover thresholds (around the 12th of expiration months)
        if month in [1, 2]:
            contract_code = "H"
        elif month == 3:
            contract_code = "M" if day > 12 else "H"
        elif month in [4, 5]:
            contract_code = "M"
        elif month == 6:
            contract_code = "U" if day > 12 else "M"
        elif month in [7, 8]:
            contract_code = "U"
        elif month == 9:
            contract_code = "Z" if day > 12 else "U"
        elif month in [10, 11]:
            contract_code = "Z"
        elif month == 12:
            if day > 12:
                contract_code = "H"
                year_last_digit = str(dt.year + 1)[-1]
            else:
                contract_code = "Z"

        return f"ES{contract_code}{year_last_digit}"

    def _build_contract_schedule(self, start_str: str, end_str: str) -> list:
        """
        Generates a chronological list of required contracts with an intentional
        2-day overlap on slice boundaries to prevent session gaps caused by
        futures market evening openings.
        """
        start_dt = pd.to_datetime(start_str)
        end_dt = pd.to_datetime(end_str)

        schedule = []
        current_dt = start_dt

        while current_dt <= end_dt:
            ticker = self._get_contract_for_date(current_dt)
            seg_start = current_dt

            while current_dt <= end_dt and self._get_contract_for_date(current_dt) == ticker:
                current_dt += timedelta(days=1)
            seg_end = min(current_dt - timedelta(days=1), end_dt)

            # ADD A 2-DAY SEAM PADDING TO PREVENT EVENING SESSION GAPS
            padded_start = max(seg_start - timedelta(days=2), start_dt)
            padded_end = min(seg_end + timedelta(days=2), end_dt)

            schedule.append({
                "ticker": ticker,
                "start": padded_start.strftime("%Y-%m-%d"),
                "end": padded_end.strftime("%Y-%m-%d")
            })

        return schedule

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

        # Determine exact missing API targets
        api_start = req_start
        api_end = req_end
        if cache_exists and not df_cache.empty:
            if target_start_date < cache_start_date:
                api_end = (cache_start_date + timedelta(days=1)).strftime("%Y-%m-%d")
            elif target_end_date > cache_end_date:
                api_start = (cache_end_date - timedelta(days=1)).strftime("%Y-%m-%d")

        # INCLUSIVE EXTENSION TO CAPTURE ABSOLUTE END OF LAST SESSION
        api_end_inclusive = (pd.to_datetime(api_end) + timedelta(days=1)).strftime("%Y-%m-%d")

        logging.info(f"Querying Massive API for {ticker} ({api_start} to {api_end_inclusive})...")
        try:
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
        """Gathers data across all required quarterly contracts, welds them together seamlessly, and runs indicators."""
        master_raw_segments = []

        # Pull each contract sequentially for its specific high-volume slice window
        for contract_info in self.contract_schedule:
            logging.info(f"Processing sequence slice for active contract: {contract_info['ticker']}")
            df_segment = self._fetch_single_contract_bars(contract_info)
            if not df_segment.empty:
                master_raw_segments.append(df_segment)

        if not master_raw_segments:
            logging.error("No valid transactional blocks could be sourced.")
            return pd.DataFrame()

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

        # RESAMPLE INTO TIMEFRAME
        logging.info(f"Dynamically resampling collective dataset to custom timeframe: {self.timeframe}")
        resampled_df = (
            raw_df.resample(self.timeframe)
            .agg({
                "Open": "first",
                "High": "max",
                "Low": "min",
                "Close": "last",
                "Volume": "sum",
            })
            .dropna()
        )

        req_start_tz = pd.to_datetime(self.start_date).tz_localize("America/Los_Angeles")
        req_end_tz = pd.to_datetime(self.end_date).tz_localize("America/Los_Angeles").replace(hour=23, minute=59,
                                                                                              second=59)
        analysis_df = resampled_df[(resampled_df.index >= req_start_tz) & (resampled_df.index <= req_end_tz)]

        if len(analysis_df) < 30:
            logging.warning(f"Insufficient candles ({len(analysis_df)}) generated at the {self.timeframe} level.")
            return pd.DataFrame()

        logging.info("Applying JM -TRM calculations to collective timeline...")
        processed_df = calculate_custom_tv_indicator(analysis_df)

        events_df = processed_df[
            (processed_df['Failed_Breakdown'] == True) |
            (processed_df['Failed_Breakout'] == True) |
            (processed_df['Signal_Breakout'] == True) |
            (processed_df['Signal_Level_Loss'] == True)
            ]

        logging.info(f"Analysis complete. Found {len(events_df)} anomalies on the {self.timeframe} timeframe.")
        return events_df[
            ['Open', 'High', 'Low', 'Close', 'clr', 'Failed_Breakdown', 'Failed_Breakout', 'Signal_Breakout',
             'Signal_Level_Loss']]