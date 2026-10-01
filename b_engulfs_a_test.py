"""
Tests the market-profile "B period engulfs A period" concept: B period
(07:00-07:30 PT, immediately following A period 06:30-07:00 PT) makes both a
higher high AND a lower low than A period -- classic early range-expansion
signal associated with trend/non-balance days.

Two questions:
 1. Descriptively: do engulf days show bigger/trendier RTH sessions?
 2. Practically: does "B engulfed A" act as a regime filter that improves
    the already-validated level-breakout focus list (from earlier) when
    applied ON TOP of it?
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
    is_a = (tmin >= 6*60+30) & (tmin < 7*60)
    is_b = (tmin >= 7*60) & (tmin < 7*60+30)
    is_rth = (tmin >= 6*60+30) & (tmin < 13*60)

    a_per = master[is_a].groupby('Trade_Date').agg(A_High=('High','max'), A_Low=('Low','min'))
    b_per = master[is_b].groupby('Trade_Date').agg(B_High=('High','max'), B_Low=('Low','min'))
    rth = master[is_rth].groupby('Trade_Date').agg(RTH_Open=('Open','first'), RTH_High=('High','max'),
                                                     RTH_Low=('Low','min'), RTH_Close=('Close','last'))

    day = a_per.join(b_per, how='inner').join(rth, how='inner')
    day['engulf'] = (day['B_High'] > day['A_High']) & (day['B_Low'] < day['A_Low'])
    day['rth_range'] = day['RTH_High'] - day['RTH_Low']
    day['rth_net'] = (day['RTH_Close'] - day['RTH_Open']).abs()
    day['trendiness'] = day['rth_net'] / day['rth_range']  # closer to 1 = trend day, closer to 0 = balance/rotation day

    print(f"Total days: {len(day)}, B-engulfs-A days: {day['engulf'].sum()} ({day['engulf'].mean()*100:.1f}%)")
    print()
    print("=== Descriptive: RTH character on engulf vs non-engulf days ===")
    g = day.groupby('engulf').agg(
        n=('rth_range','size'),
        avg_rth_range=('rth_range','mean'), med_rth_range=('rth_range','median'),
        avg_trendiness=('trendiness','mean'), med_trendiness=('trendiness','median'),
    )
    print(g)

    day.to_csv(f"{BASE}/ES_b_engulfs_a_days.csv")


if __name__ == "__main__":
    main()
