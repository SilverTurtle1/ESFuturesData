import os
import json
import logging
import pandas as pd
import numpy as np

from engine import FuturesIntradayScanner

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def load_session_configs(config_path: str = "config.json") -> dict:
    """Loads custom session time boundaries from config.json with defaults if missing."""
    defaults = {
        "asia": {"start_hour": 15, "start_min": 0, "end_hour": 0, "end_min": 0},
        "london": {"start_hour": 0, "start_min": 0, "end_hour": 5, "end_min": 0},
        "premarket": {"start_hour": 5, "start_min": 0, "end_hour": 6, "end_min": 30},
        "a_period": {"start_hour": 6, "start_min": 30, "end_hour": 7, "end_min": 0},
        "rth": {"start_hour": 6, "start_min": 30, "end_hour": 13, "end_min": 0}
    }

    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                cfg = json.load(f)
                return cfg.get("session_hours", defaults)
        except Exception as e:
            logging.warning(f"Error reading {config_path}: {e}. Falling back to default session settings.")

    return defaults


def generate_daily_reports(
        master_csv_path: str = "output/ES_continuous_master_1m.csv",
        config_path: str = "config.json",
        value_area_pct: float = 0.682,
        va_only_csv: str = "output/ES_daily_value_area.csv",
        comprehensive_csv: str = "output/ES_daily_comprehensive_summary.csv",
        include_poc: bool = False,
        include_volume: bool = False
):
    """
    Parses the 1m master dataset once and generates TWO reports:
    1. A trimmed Value Area file (Date, VAH, VAL, and optionally POC/Vol)
    2. A comprehensive session summary file (A Period Halfback, Highs/Lows for Asia, London, PM, Prior RTH, RTH)
    """
    logging.info(f"Loading session configurations from {config_path}...")
    sessions = load_session_configs(config_path)

    logging.info(f"Loading master dataset from {master_csv_path}...")
    df = pd.read_csv(master_csv_path)
    df['Timestamp'] = pd.to_datetime(df['Timestamp'], utc=True)
    df.set_index('Timestamp', inplace=True)
    df.index = df.index.tz_convert("America/Los_Angeles")

    # Strip zero-volume rows
    df = df[df['Volume'] > 0].copy()

    # --- GLOBEX SESSION ASSIGNMENT ---
    globex_dt = df.index + pd.to_timedelta((df.index.hour >= 15).astype(int), unit='D')
    df['Trade_Date'] = globex_dt.date

    # Filter out Saturday ghost sessions
    df = df[df['Trade_Date'].apply(lambda d: d.weekday() != 5)].copy()

    # --- DEFINE INTRADAY SUB-SESSION MASKS ---
    hour = df.index.hour
    minute = df.index.minute
    total_mins = hour * 60 + minute

    def in_window(start_h, start_m, end_h, end_m):
        start_t = start_h * 60 + start_m
        end_t = end_h * 60 + end_m
        if start_t < end_t:
            return (total_mins >= start_t) & (total_mins < end_t)
        else:
            return (total_mins >= start_t) | (total_mins < end_t)

    cfg_asia = sessions["asia"]
    cfg_lon = sessions["london"]
    cfg_pm = sessions["premarket"]
    cfg_aperiod = sessions["a_period"]
    cfg_rth = sessions["rth"]

    df['is_asia'] = in_window(cfg_asia["start_hour"], cfg_asia["start_min"], cfg_asia["end_hour"], cfg_asia["end_min"])
    df['is_london'] = in_window(cfg_lon["start_hour"], cfg_lon["start_min"], cfg_lon["end_hour"], cfg_lon["end_min"])
    df['is_premarket'] = in_window(cfg_pm["start_hour"], cfg_pm["start_min"], cfg_pm["end_hour"], cfg_pm["end_min"])
    df['is_aperiod'] = in_window(cfg_aperiod["start_hour"], cfg_aperiod["start_min"], cfg_aperiod["end_hour"],
                                 cfg_aperiod["end_min"])
    df['is_rth'] = in_window(cfg_rth["start_hour"], cfg_rth["start_min"], cfg_rth["end_hour"], cfg_rth["end_min"])

    daily_results = []
    dataset_max_ts = df.index.max()

    logging.info("Building daily profiles and extracting session boundaries...")

    for trade_date, group in df.groupby('Trade_Date'):
        if group.empty:
            continue

        rth_end_boundary = pd.Timestamp(
            year=trade_date.year, month=trade_date.month, day=trade_date.day,
            hour=cfg_rth["end_hour"], minute=cfg_rth["end_min"],
            tz="America/Los_Angeles"
        )
        rth_session_closed = dataset_max_ts >= rth_end_boundary

        asia_data = group[group['is_asia']]
        london_data = group[group['is_london']]
        pm_data = group[group['is_premarket']]
        aperiod_data = group[group['is_aperiod']]
        rth_data = group[group['is_rth']]

        asia_high = asia_data['High'].max() if not asia_data.empty else np.nan
        asia_low = asia_data['Low'].min() if not asia_data.empty else np.nan

        london_high = london_data['High'].max() if not london_data.empty else np.nan
        london_low = london_data['Low'].min() if not london_data.empty else np.nan

        pm_high = pm_data['High'].max() if not pm_data.empty else np.nan
        pm_low = pm_data['Low'].min() if not pm_data.empty else np.nan

        # --- A PERIOD CALCULATIONS ---
        if not aperiod_data.empty:
            a_high = aperiod_data['High'].max()
            a_low = aperiod_data['Low'].min()
            a_halfback = (a_high + a_low) / 2.0
        else:
            a_high = a_low = a_halfback = np.nan

        # --- RTH PROFILE & VALUE AREA CALCULATIONS ---
        if not rth_data.empty and rth_session_closed:
            rth_high = rth_data['High'].max()
            rth_low = rth_data['Low'].min()
            rth_volume = rth_data['Volume'].sum()

            target_va_vol = rth_volume * value_area_pct
            profile = {}

            for _, row in rth_data.iterrows():
                low_tick = int(round(row['Low'] * 4))
                high_tick = int(round(row['High'] * 4))
                vol = row['Volume']
                num_ticks = (high_tick - low_tick) + 1
                vol_per_tick = vol / num_ticks
                for t in range(low_tick, high_tick + 1):
                    profile[t] = profile.get(t, 0.0) + vol_per_tick

            if profile:
                profile_df = pd.DataFrame(list(profile.items()), columns=['Tick', 'Volume']).sort_values(
                    'Tick').reset_index(drop=True)

                poc_idx = profile_df['Volume'].idxmax()
                poc_tick = profile_df.loc[poc_idx, 'Tick']
                poc_vol = profile_df.loc[poc_idx, 'Volume']

                current_va_vol = poc_vol
                va_indices = {poc_idx}

                up_idx = poc_idx + 1
                down_idx = poc_idx - 1
                max_idx = len(profile_df) - 1

                while current_va_vol < target_va_vol and (up_idx <= max_idx or down_idx >= 0):
                    vol_up = 0.0
                    if up_idx <= max_idx:
                        vol_up += profile_df.loc[up_idx, 'Volume']
                        if up_idx + 1 <= max_idx:
                            vol_up += profile_df.loc[up_idx + 1, 'Volume']

                    vol_down = 0.0
                    if down_idx >= 0:
                        vol_down += profile_df.loc[down_idx, 'Volume']
                        if down_idx - 1 >= 0:
                            vol_down += profile_df.loc[down_idx - 1, 'Volume']

                    if (vol_up > vol_down or down_idx < 0) and up_idx <= max_idx:
                        current_va_vol += profile_df.loc[up_idx, 'Volume']
                        va_indices.add(up_idx)
                        up_idx += 1
                        if up_idx <= max_idx and current_va_vol < target_va_vol:
                            current_va_vol += profile_df.loc[up_idx, 'Volume']
                            va_indices.add(up_idx)
                            up_idx += 1
                    elif down_idx >= 0:
                        current_va_vol += profile_df.loc[down_idx, 'Volume']
                        va_indices.add(down_idx)
                        down_idx -= 1
                        if down_idx >= 0 and current_va_vol < target_va_vol:
                            current_va_vol += profile_df.loc[down_idx, 'Volume']
                            va_indices.add(down_idx)
                            down_idx -= 1
                    else:
                        break

                va_ticks = profile_df.loc[list(va_indices), 'Tick']
                poc_price = poc_tick / 4.0
                vah_price = va_ticks.max() / 4.0
                val_price = va_ticks.min() / 4.0
            else:
                poc_price = vah_price = val_price = np.nan
        else:
            rth_high = rth_low = rth_volume = np.nan
            poc_price = vah_price = val_price = np.nan

        daily_results.append({
            'Date': trade_date,
            'POC': poc_price,
            'VAH': vah_price,
            'VAL': val_price,
            'A_Period_Halfback': a_halfback,
            'A_Period_High': a_high,
            'A_Period_Low': a_low,
            'Asia_High': asia_high,
            'Asia_Low': asia_low,
            'London_High': london_high,
            'London_Low': london_low,
            'Premarket_High': pm_high,
            'Premarket_Low': pm_low,
            'RTH_High': rth_high,
            'RTH_Low': rth_low,
            'RTH_Volume': round(rth_volume, 2) if not np.isnan(rth_volume) else np.nan
        })

    master_df = pd.DataFrame(daily_results)
    master_df['Prior_RTH_High'] = master_df['RTH_High'].shift(1)
    master_df['Prior_RTH_Low'] = master_df['RTH_Low'].shift(1)

    # RTH is the last session to close each Globex day, so a NaN RTH_High
    # means that day's session is still in progress -- drop it from the report.
    master_df = master_df[master_df['RTH_High'].notna()].reset_index(drop=True)

    # --- 1. EXPORT TRIMMED VALUE AREA FILE ---
    va_cols = ['Date']
    if include_poc:
        va_cols.append('POC')
    va_cols.extend(['VAH', 'VAL'])
    if include_volume:
        va_cols.append('RTH_Volume')

    df_va = master_df[va_cols]
    if va_only_csv:
        df_va.to_csv(va_only_csv, index=False)
        logging.info(f"Trimmed VA summary saved to **{va_only_csv}**")

    # --- 2. EXPORT COMPREHENSIVE SESSION FILE ---
    comp_cols = ['Date']
    if include_poc:
        comp_cols.append('POC')
    comp_cols.extend(['VAH', 'VAL'])
    comp_cols.extend([
        'A_Period_Halfback',
        'A_Period_High', 'A_Period_Low',
        'Asia_High', 'Asia_Low',
        'London_High', 'London_Low',
        'Premarket_High', 'Premarket_Low',
        'Prior_RTH_High', 'Prior_RTH_Low',
        'RTH_High', 'RTH_Low'
    ])
    if include_volume:
        comp_cols.append('RTH_Volume')

    df_comp = master_df[comp_cols]
    if comprehensive_csv:
        df_comp.to_csv(comprehensive_csv, index=False)
        logging.info(f"Comprehensive session summary saved to **{comprehensive_csv}**")

    return df_va, df_comp


if __name__ == "__main__":
    logging.info("Refreshing ES futures data from source before generating reports...")
    FuturesIntradayScanner().process_and_backtest()

    generate_daily_reports()