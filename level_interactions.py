"""
Reusable level-interaction engine.

For a fixed catalog of key levels (Asia/London/Premarket H-L, A-period
halfback, prior-day RTH H/L, prior-day VAH/VAL, prior-week VAH/VAL), this:

  1. Builds a no-lookahead "level validity" table -- each level value is only
     usable starting the moment it was actually knowable in real time.
  2. Scans the 1m master series for the first touch of each live level.
  3. Classifies each touch as REJECT / BREAKOUT / CHOP based on forward price
     action, and records forward returns/MFE/MAE at several horizons.

Output: ES_level_touch_events.csv (one row per touch) -- query/filter this
for any future "how does price react at level X" question instead of writing
a new one-off script.
"""
import pandas as pd
import numpy as np

BASE = "/sessions/vibrant-awesome-edison/mnt/Shared Data--ESFuturesData"
PT = "America/Los_Angeles"

BREAKOUT_THRESH = 1.5      # pts beyond level to count as a breakout close
REJECT_THRESH = 1.5        # pts back on origin side to count as a rejection close
CONFIRM_MIN = 10           # minutes breakout must hold to be confirmed
HORIZON_MIN = 60           # how far forward to search for resolution
FWD_HORIZONS = [15, 30, 60, 120]   # minutes for forward-return snapshots
NO_RETOUCH_BUFFER = 0.5    # must be this far from level to arm a fresh touch


def load_master():
    df = pd.read_csv(f"{BASE}/ES_continuous_master_1m.csv")
    df['Timestamp'] = pd.to_datetime(df['Timestamp'], utc=True).dt.tz_convert(PT)
    df = df.set_index('Timestamp').sort_index()
    globex_dt = df.index + pd.to_timedelta((df.index.hour >= 15).astype(int), unit='D')
    df['Trade_Date'] = globex_dt.date
    hour = df.index.hour
    minute = df.index.minute
    tmin = hour * 60 + minute
    def in_window(sh, sm, eh, em):
        st, et = sh * 60 + sm, eh * 60 + em
        return (tmin >= st) & (tmin < et) if st < et else (tmin >= st) | (tmin < et)
    df['session'] = 'other'
    df.loc[in_window(15, 0, 0, 0), 'session'] = 'asia'
    df.loc[in_window(0, 0, 5, 0), 'session'] = 'london'
    df.loc[in_window(5, 0, 6, 30), 'session'] = 'premarket'
    df.loc[in_window(6, 30, 13, 0), 'session'] = 'rth'
    return df


def build_levels_long(summary_path, weekly_path):
    s = pd.read_csv(summary_path)
    s['Date'] = pd.to_datetime(s['Date'])
    s = s.sort_values('Date').reset_index(drop=True)
    s['Prior_VAH'] = s['VAH'].shift(1)
    s['Prior_VAL'] = s['VAL'].shift(1)

    w = pd.read_csv(weekly_path)
    w['week_start'] = pd.to_datetime(w['week_start'])
    w = w.sort_values('week_start').reset_index(drop=True)
    w['Prior_WVAH'] = w['WVAH'].shift(1)
    w['Prior_WVAL'] = w['WVAL'].shift(1)
    s['week_start'] = (s['Date'] - pd.to_timedelta(s['Date'].dt.weekday, unit='D'))
    s = s.merge(w[['week_start', 'Prior_WVAH', 'Prior_WVAL']], on='week_start', how='left')

    def mk_ts(date, h, m):
        return date + pd.Timedelta(hours=h, minutes=m)

    rows = []
    for _, r in s.iterrows():
        d = r['Date']
        prev_d = d - pd.Timedelta(days=1)
        rth_close = mk_ts(d, 13, 0)
        specs = [
            ('Asia_High',  r['Asia_High'],  mk_ts(d, 0, 0),   rth_close),
            ('Asia_Low',   r['Asia_Low'],   mk_ts(d, 0, 0),   rth_close),
            ('London_High',r['London_High'],mk_ts(d, 5, 0),   rth_close),
            ('London_Low', r['London_Low'], mk_ts(d, 5, 0),   rth_close),
            ('PM_High',    r['Premarket_High'], mk_ts(d, 6, 30), rth_close),
            ('PM_Low',     r['Premarket_Low'],  mk_ts(d, 6, 30), rth_close),
            ('A_Halfback', r['A_Period_Halfback'], mk_ts(d, 7, 0), rth_close),
            ('Prior_RTH_High', r['Prior_RTH_High'], mk_ts(prev_d, 13, 0), rth_close),
            ('Prior_RTH_Low',  r['Prior_RTH_Low'],  mk_ts(prev_d, 13, 0), rth_close),
            ('Prior_VAH',  r['Prior_VAH'],  mk_ts(prev_d, 13, 0), rth_close),
            ('Prior_VAL',  r['Prior_VAL'],  mk_ts(prev_d, 13, 0), rth_close),
            ('Prior_WVAH', r['Prior_WVAH'], mk_ts(d, 0, 0),   rth_close),
            ('Prior_WVAL', r['Prior_WVAL'], mk_ts(d, 0, 0),   rth_close),
        ]
        for name, val, vf, vu in specs:
            if pd.notna(val):
                rows.append({'Date': d, 'level_name': name, 'level_value': val,
                             'valid_from': vf, 'valid_until': vu})
    lv = pd.DataFrame(rows)
    lv['valid_from'] = lv['valid_from'].dt.tz_localize(PT)
    lv['valid_until'] = lv['valid_until'].dt.tz_localize(PT)
    return lv


def classify_touches(df, levels):
    events = []
    touch_counter = {}

    for _, lvrow in levels.iterrows():
        name, level, vf, vu, date = (lvrow['level_name'], lvrow['level_value'],
                                      lvrow['valid_from'], lvrow['valid_until'], lvrow['Date'])
        window = df.loc[vf:vu]
        if window.empty or len(window) < 2:
            continue

        highs, lows, closes = window['High'].values, window['Low'].values, window['Close'].values
        idx = window.index
        armed = True  # can a fresh touch be flagged right now
        n = len(window)
        i = 1
        while i < n:
            dist_prev_close = closes[i-1] - level
            if not armed:
                if abs(dist_prev_close) > NO_RETOUCH_BUFFER:
                    armed = True
                i += 1
                continue

            touches_now = (lows[i] <= level <= highs[i])
            if touches_now and abs(dist_prev_close) > NO_RETOUCH_BUFFER:
                approach_from = 'above' if dist_prev_close > 0 else 'below'
                touch_ts = idx[i]
                key = (name, date)
                touch_counter[key] = touch_counter.get(key, 0) + 1
                tnum = touch_counter[key]

                # look forward for resolution
                fwd_end = min(i + HORIZON_MIN, n - 1)
                outcome = 'chop'
                resolved_at = None
                resolved_idx = None
                for j in range(i, fwd_end + 1):
                    c = closes[j]
                    if approach_from == 'below':
                        if c >= level + BREAKOUT_THRESH:
                            confirm_end = min(j + CONFIRM_MIN, n - 1)
                            if confirm_end > j and np.min(closes[j:confirm_end+1]) >= level - REJECT_THRESH * 0.2:
                                outcome, resolved_at, resolved_idx = 'breakout', idx[j], j
                                break
                        if c <= level - REJECT_THRESH:
                            outcome, resolved_at, resolved_idx = 'reject', idx[j], j
                            break
                    else:
                        if c <= level - BREAKOUT_THRESH:
                            confirm_end = min(j + CONFIRM_MIN, n - 1)
                            if confirm_end > j and np.max(closes[j:confirm_end+1]) <= level + REJECT_THRESH * 0.2:
                                outcome, resolved_at, resolved_idx = 'breakout', idx[j], j
                                break
                        if c >= level + REJECT_THRESH:
                            outcome, resolved_at, resolved_idx = 'reject', idx[j], j
                            break

                # touch-anchored forward closes (descriptive only -- these overlap the
                # resolution window and should NOT be read as a tradeable entry signal)
                fwd = {}
                for h in FWD_HORIZONS:
                    j = min(i + h, n - 1)
                    fwd[f'close_{h}m'] = closes[j]
                mfe_window = closes[i:fwd_end+1]
                if approach_from == 'below':
                    mfe = np.max(mfe_window) - level
                    mae = level - np.min(mfe_window)
                else:
                    mfe = level - np.min(mfe_window)
                    mae = np.max(mfe_window) - level

                # entry-anchored forward closes -- the actually-tradeable signal:
                # "enter the instant the reject/breakout is CONFIRMED, hold N minutes
                # from that confirmation bar" (NaN if never resolved within horizon)
                entry_fwd = {}
                entry_price = np.nan
                if resolved_idx is not None:
                    entry_price = closes[resolved_idx]
                    for h in FWD_HORIZONS:
                        j = min(resolved_idx + h, n - 1)
                        entry_fwd[f'entry_close_{h}m'] = closes[j]
                else:
                    for h in FWD_HORIZONS:
                        entry_fwd[f'entry_close_{h}m'] = np.nan

                events.append({
                    'level_name': name, 'Date': date, 'level_value': level,
                    'touch_time': touch_ts, 'touch_num_of_day': tnum,
                    'session': window['session'].iloc[i], 'approach_from': approach_from,
                    'outcome': outcome, 'resolved_at': resolved_at,
                    'price_at_touch': closes[i-1],
                    'entry_price': entry_price,
                    'mfe': mfe, 'mae': mae,
                    **fwd, **entry_fwd,
                })
                armed = False
            i += 1
    return pd.DataFrame(events)


if __name__ == "__main__":
    print("Loading 1m master data...")
    df = load_master()
    print(f"  {len(df)} bars, {df['Trade_Date'].nunique()} trading days")

    print("Building no-lookahead level table...")
    levels = build_levels_long(f"{BASE}/ES_daily_comprehensive_summary.csv", f"{BASE}/ES_weekly_value_area.csv")
    print(f"  {len(levels)} level-day instances across {levels['level_name'].nunique()} level types")

    print("Scanning for touches and classifying outcomes (this may take a few minutes)...")
    events = classify_touches(df, levels)
    print(f"  {len(events)} touch events found")

    out_path = f"{BASE}/ES_level_touch_events.csv"
    events.to_csv(out_path, index=False)
    print(f"Saved to {out_path}")

    print("\n=== Quick summary by level_name x outcome ===")
    print(events.groupby(['level_name', 'outcome']).size().unstack(fill_value=0))
