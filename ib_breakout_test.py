"""
Tests an Initial Balance (IB) breakout. IB is the combined A+B period range
(06:30-07:30 PT, the first hour of RTH). The signal is whether the first
30-minute period after IB closes (07:30-08:00 PT) closes beyond the IB high
(bullish) or the IB low (bearish), a sign the range is expanding.

Entries are taken at 08:00 PT, the close of that period, and scored with the
triple-barrier tester (20-point target / 7-point stop, and 10 / 7).
"""
import sys
sys.path.insert(0, '.')
import pandas as pd
import numpy as np
from triple_barrier import load_master, triple_barrier, leaderboard_row, BASE, PT

def main():
    master = load_master()
    globex_dt = master.index + pd.to_timedelta((master.index.hour >= 15).astype(int), unit='D')
    master['Trade_Date'] = globex_dt.date

    hour, minute = master.index.hour, master.index.minute
    tmin = hour * 60 + minute
    is_ib = (tmin >= 6*60+30) & (tmin < 7*60+30)          # A+B combined = IB
    is_c = (tmin >= 7*60+30) & (tmin < 8*60)              # first 30m after IB
    is_rth = (tmin >= 6*60+30) & (tmin < 13*60)

    ib = master[is_ib].groupby('Trade_Date').agg(IB_High=('High','max'), IB_Low=('Low','min'))
    c_bar = master[is_c].groupby('Trade_Date').agg(C_Close=('Close','last'))
    rth = master[is_rth].groupby('Trade_Date').agg(RTH_Open=('Open','first'), RTH_Close=('Close','last'))

    day = ib.join(c_bar, how='inner').join(rth, how='inner')
    day['ib_break_up'] = day['C_Close'] > day['IB_High']
    day['ib_break_down'] = day['C_Close'] < day['IB_Low']
    n_up, n_down = day['ib_break_up'].sum(), day['ib_break_down'].sum()
    print(f"Total days: {len(day)}, IB break up (C closes > IB High): {n_up} ({n_up/len(day)*100:.1f}%), "
          f"IB break down (C closes < IB Low): {n_down} ({n_down/len(day)*100:.1f}%)")

    # --- 1. Is the IB-break confirmation itself tradeable, entered at 08:00 PT (end of period C)? ---
    entries = []
    for d, r in day[day['ib_break_up'] | day['ib_break_down']].iterrows():
        direction = 1 if r['ib_break_up'] else -1
        c_end = master[(master['Trade_Date'] == d).values & is_c]
        if c_end.empty:
            continue
        entry_time = c_end.index[-1] + pd.Timedelta(minutes=1)
        entries.append({'entry_time': entry_time, 'entry_price': r['C_Close'], 'direction': direction})
    ib_entries = pd.DataFrame(entries)
    print(f"\nIB-break confirmed entries: {len(ib_entries)}")

    results = []
    for target, stop in [(20.0, 7.0), (10.0, 7.0)]:
        sc = triple_barrier(master, ib_entries, target=target, stop=stop)
        results.append(leaderboard_row(f'IB-break confirmation (standalone) [{int(target)}/{int(stop)}]', sc))

    res = pd.DataFrame(results)
    pd.set_option('display.width', 200)
    pd.set_option('display.float_format', lambda x: f'{x:.3f}')
    print(res.to_string(index=False))
    day.to_csv(f"{BASE}/ES_ib_break_days.csv")


if __name__ == "__main__":
    main()
