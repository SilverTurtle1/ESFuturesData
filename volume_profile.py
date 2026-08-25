import pandas as pd
import numpy as np


def calculate_daily_value_area(
        master_csv_path: str,
        value_area_pct: float = 0.682,
        rth_only: bool = True,  # True for 06:30-13:00 PT, False for Full Globex
        include_volume: bool = False,  # TOGGLE: Include Total_Volume in output (Default: False)
        include_poc: bool = False,  # TOGGLE: Include POC in output (Default: False)
        output_csv_path: str = "ES_daily_value_area_rth.csv"
) -> pd.DataFrame:
    """
    Calculates Daily Value Area levels (VAH, VAL, and optionally POC & Volume) from 1-minute ES data.

    Parameters:
        master_csv_path (str): Path to master 1m continuous CSV file.
        value_area_pct (float): Target volume percentage (default 0.682 for 68.2% / 1-sigma).
        rth_only (bool): Restrict calculation strictly to Regular Trading Hours (06:30 - 13:00 PT).
        include_volume (bool): Include Total_Volume column in exported CSV/DataFrame (default False).
        include_poc (bool): Include POC column in exported CSV/DataFrame (default False).
        output_csv_path (str): Path to write the resulting CSV.
    """
    df = pd.read_csv(master_csv_path)
    df['Timestamp'] = pd.to_datetime(df['Timestamp'], utc=True)
    df.set_index('Timestamp', inplace=True)
    df.index = df.index.tz_convert("America/Los_Angeles")

    # 1. Strip zero-volume rows
    df = df[df['Volume'] > 0].copy()

    # --- 2. SESSION FILTERING (RTH vs. Full Globex) ---
    if rth_only:
        # Filter strictly for 06:30 AM PT to 01:00 PM PT (13:00 PT)
        rth_mask = (
                ((df.index.hour > 6) | ((df.index.hour == 6) & (df.index.minute >= 30))) &
                (df.index.hour < 13)
        )
        df = df[rth_mask].copy()
        df['Session_Date'] = df.index.date
    else:
        # Full 24-Hour Globex Session (15:00 PT cutoff)
        session_dt = df.index + pd.to_timedelta((df.index.hour >= 15).astype(int), unit='D')
        df['Session_Date'] = session_dt.date
        # Filter out Saturday ghost sessions
        df = df[df['Session_Date'].apply(lambda d: d.weekday() != 5)]

    daily_results = []

    # 3. Process each trading session
    for date, group in df.groupby('Session_Date'):
        if group.empty:
            continue

        total_session_vol = group['Volume'].sum()
        if total_session_vol == 0:
            continue

        target_va_vol = total_session_vol * value_area_pct
        profile = {}

        # Integer Tick Allocation
        for _, row in group.iterrows():
            low_tick = int(round(row['Low'] * 4))
            high_tick = int(round(row['High'] * 4))
            vol = row['Volume']

            num_ticks = (high_tick - low_tick) + 1
            vol_per_tick = vol / num_ticks

            for t in range(low_tick, high_tick + 1):
                profile[t] = profile.get(t, 0.0) + vol_per_tick

        if not profile:
            continue

        profile_df = pd.DataFrame(list(profile.items()), columns=['Tick', 'Volume']).sort_values('Tick').reset_index(
            drop=True)

        # POC
        poc_idx = profile_df['Volume'].idxmax()
        poc_tick = profile_df.loc[poc_idx, 'Tick']
        poc_vol = profile_df.loc[poc_idx, 'Volume']

        current_va_vol = poc_vol
        va_indices = {poc_idx}

        up_idx = poc_idx + 1
        down_idx = poc_idx - 1
        max_idx = len(profile_df) - 1

        # Standard CME 2-Tick Expansion Algorithm
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

        daily_results.append({
            'Date': date,
            'Total_Volume': round(total_session_vol, 2),
            'POC': poc_price,
            'VAH': vah_price,
            'VAL': val_price
        })

    result_df = pd.DataFrame(daily_results)

    # --- 4. DYNAMIC COLUMN FILTERING ---
    output_cols = ['Date']
    if include_volume:
        output_cols.append('Total_Volume')
    if include_poc:
        output_cols.append('POC')
    output_cols.extend(['VAH', 'VAL'])

    result_df = result_df[output_cols]

    if output_csv_path:
        result_df.to_csv(output_csv_path, index=False)

    return result_df