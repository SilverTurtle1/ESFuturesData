"""
Reusable triple-barrier trade evaluator: given an entry (time, price,
direction), walks forward on 1m bars to see whether a profit target or stop
is touched first. This is the objective-matching scorer for "20+ pts,
<=7 pt drawdown" entries -- apply it to any candidate signal set to get an
apples-to-apples leaderboard.

Same-bar ambiguity (both target and stop within one bar's H/L range) is
resolved conservatively in favor of the stop, since OHLC bars don't tell us
the intrabar sequencing.
"""
import pandas as pd
import numpy as np

BASE = "/sessions/vibrant-awesome-edison/mnt/Shared Data--ESFuturesData"
PT = "America/Los_Angeles"


def load_master():
    df = pd.read_csv(f"{BASE}/ES_continuous_master_1m.csv")
    df['Timestamp'] = pd.to_datetime(df['Timestamp'], utc=True).dt.tz_convert(PT)
    df = df.set_index('Timestamp').sort_index()
    return df


def triple_barrier(master, entries, target=20.0, stop=7.0, max_bars=240):
    """
    entries: DataFrame with columns entry_time (tz-aware Timestamp), entry_price, direction (1=long,-1=short)
    Returns entries with added: outcome (win/loss/timeout), bars_to_resolution, R (in units of stop risk)
    """
    idx = master.index
    highs, lows, closes = master['High'].values, master['Low'].values, master['Close'].values
    n = len(master)

    outcomes, r_mults, bars_to_res = [], [], []
    entry_times = pd.DatetimeIndex(entries['entry_time'])
    if entry_times.tz is None:
        entry_times = entry_times.tz_localize(idx.tz)
    else:
        entry_times = entry_times.tz_convert(idx.tz)
    positions = idx.searchsorted(entry_times)

    for pos, price, direction in zip(positions, entries['entry_price'].values, entries['direction'].values):
        if pos >= n:
            outcomes.append('no_data'); r_mults.append(np.nan); bars_to_res.append(np.nan)
            continue
        end = min(pos + max_bars, n)
        h = highs[pos:end]
        l = lows[pos:end]

        if direction == 1:
            target_price, stop_price = price + target, price - stop
            hit_t = h >= target_price
            hit_s = l <= stop_price
        else:
            target_price, stop_price = price - target, price + stop
            hit_t = l <= target_price
            hit_s = h >= stop_price

        t_i = np.argmax(hit_t) if hit_t.any() else None
        s_i = np.argmax(hit_s) if hit_s.any() else None

        if t_i is None and s_i is None:
            exit_price = closes[end - 1]
            r = (exit_price - price) * direction / stop
            outcomes.append('timeout'); r_mults.append(r); bars_to_res.append(end - pos)
        elif s_i is None or (t_i is not None and t_i < s_i):
            outcomes.append('win'); r_mults.append(target / stop); bars_to_res.append(t_i + 1)
        elif t_i is None or (s_i is not None and s_i < t_i):
            outcomes.append('loss'); r_mults.append(-1.0); bars_to_res.append(s_i + 1)
        else:  # same bar -- conservative: stop first
            outcomes.append('loss'); r_mults.append(-1.0); bars_to_res.append(s_i + 1)

    out = entries.copy()
    out['outcome'] = outcomes
    out['R'] = r_mults
    out['bars_to_resolution'] = bars_to_res
    return out


def leaderboard_row(label, scored):
    n = len(scored)
    win = (scored['outcome'] == 'win').mean()
    loss = (scored['outcome'] == 'loss').mean()
    timeout = (scored['outcome'] == 'timeout').mean()
    expectancy = scored['R'].mean()
    return {'setup': label, 'n': n, 'win_rate': win, 'loss_rate': loss,
            'timeout_rate': timeout, 'expectancy_R': expectancy}
