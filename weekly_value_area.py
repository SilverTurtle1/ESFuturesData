"""
Builds a weekly volume profile (VAH/VAL/POC) from RTH 1m bars, using the same
68.2%-of-volume tick-expansion method as the daily value area calc in main.py.

Output: one row per trading week (Monday's date = week_start), with that
week's own VAH/VAL/POC. Consumers must shift by one week to get a
non-lookahead, forward-testable level (i.e. "prior completed week's value
area" as the level watched during the CURRENT week).
"""
import pandas as pd
import numpy as np
import json
import os


def load_session_configs(config_path="config.json"):
    defaults = {
        "rth": {"start_hour": 6, "start_min": 30, "end_hour": 13, "end_min": 0}
    }
    if os.path.exists(config_path):
        with open(config_path) as f:
            cfg = json.load(f)
            return cfg.get("session_hours", defaults)
    return defaults


def build_weekly_value_area(master_csv_path, value_area_pct=0.682, config_path="config.json"):
    sessions = load_session_configs(config_path)
    cfg_rth = sessions["rth"]

    df = pd.read_csv(master_csv_path)
    df['Timestamp'] = pd.to_datetime(df['Timestamp'], utc=True)
    df.set_index('Timestamp', inplace=True)
    df.index = df.index.tz_convert("America/Los_Angeles")
    df = df[df['Volume'] > 0].copy()

    globex_dt = df.index + pd.to_timedelta((df.index.hour >= 15).astype(int), unit='D')
    df['Trade_Date'] = globex_dt.date
    df = df[df['Trade_Date'].apply(lambda d: d.weekday() != 5)].copy()

    hour, minute = df.index.hour, df.index.minute
    total_mins = hour * 60 + minute
    start_t = cfg_rth["start_hour"] * 60 + cfg_rth["start_min"]
    end_t = cfg_rth["end_hour"] * 60 + cfg_rth["end_min"]
    df['is_rth'] = (total_mins >= start_t) & (total_mins < end_t)

    rth = df[df['is_rth']].copy()
    rth['Trade_Date'] = pd.to_datetime(rth['Trade_Date'])
    # Monday of that trade date's week = week_start
    rth['week_start'] = (rth['Trade_Date'] - pd.to_timedelta(rth['Trade_Date'].dt.weekday, unit='D')).dt.date

    def profile_va(group):
        target_va_vol = group['Volume'].sum() * value_area_pct
        profile = {}
        for _, row in group.iterrows():
            low_tick = int(round(row['Low'] * 4))
            high_tick = int(round(row['High'] * 4))
            vol = row['Volume']
            num_ticks = (high_tick - low_tick) + 1
            vol_per_tick = vol / num_ticks
            for t in range(low_tick, high_tick + 1):
                profile[t] = profile.get(t, 0.0) + vol_per_tick

        if not profile:
            return pd.Series({'WPOC': np.nan, 'WVAH': np.nan, 'WVAL': np.nan})

        profile_df = pd.DataFrame(list(profile.items()), columns=['Tick', 'Volume']).sort_values('Tick').reset_index(drop=True)
        poc_idx = profile_df['Volume'].idxmax()
        poc_tick = profile_df.loc[poc_idx, 'Tick']
        current_va_vol = profile_df.loc[poc_idx, 'Volume']
        va_indices = {poc_idx}
        up_idx, down_idx = poc_idx + 1, poc_idx - 1
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
        return pd.Series({
            'WPOC': poc_tick / 4.0,
            'WVAH': va_ticks.max() / 4.0,
            'WVAL': va_ticks.min() / 4.0,
        })

    weekly = rth.groupby('week_start').apply(profile_va).reset_index()
    weekly['n_rth_days'] = rth.groupby('week_start')['Trade_Date'].nunique().values
    return weekly


if __name__ == "__main__":
    base = "/sessions/vibrant-awesome-edison/mnt/Shared Data--ESFuturesData"
    weekly = build_weekly_value_area(f"{base}/ES_continuous_master_1m.csv")
    weekly.to_csv(f"{base}/ES_weekly_value_area.csv", index=False)
    print(f"Built {len(weekly)} weekly profiles")
    print(weekly.tail(10).to_string())
