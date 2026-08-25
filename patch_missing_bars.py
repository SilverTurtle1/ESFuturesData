"""
One-off patch script: backfills two confirmed holes in the raw 1m contract data
that were silently flattened into fake zero-volume bars by the master-build's
reindex/ffill step. Run this once from the project root (same venv as main.py),
then re-run main.py (or ask Claude to rebuild the master/summary files).

Confirmed gaps (verified against the live API - real trades exist in both):
  - ESU5: 2025-09-08 ~15:36 PT -> 2025-09-09 ~09:57 PT  (~1,102 missing minutes)
  - ESZ5: 2025-11-27 ~18:45 PT -> 2025-11-28 ~05:36 PT  (~652 missing minutes)

NOTE: ESH5 2025-01-09 was investigated too but is NOT patched here -- the API
has no data after ~06:29 PT that day, which lines up with the National Day of
Mourning for President Carter (NYSE/bond markets closed, CME equity futures
went dark). That's a genuine closure, not a data hole, same as Good Friday /
Christmas / New Year's -- nothing to backfill.
"""
import time
import requests
import pandas as pd
from local_settings import polygon as _local_settings

api_key = _local_settings["api_key"]

URL = "https://api.massive.com/futures/v1/aggs/{ticker}"

# (ticker, gte, lte) -- date-level bounds with a 1-day buffer on each side of the
# confirmed gap; dedup on merge makes the extra buffer harmless.
PATCHES = [
    ("ESU5", "2025-09-07", "2025-09-10"),
    ("ESZ5", "2025-11-26", "2025-11-29"),
]


def fetch_range(ticker, start, end):
    all_bars = []
    params = {
        "resolution": "1min",
        "limit": 50000,
        "sort": "window_start.asc",
        "window_start.gte": start,
        "window_start.lte": end,
        "apiKey": api_key,
    }
    url = URL.format(ticker=ticker)
    while True:
        resp = requests.get(url, params=params)
        if resp.status_code != 200:
            print(f"  ERROR {resp.status_code}: {resp.text[:300]}")
            break
        data = resp.json()
        bars = data.get("results", [])
        all_bars.extend(bars)
        print(f"  {ticker}: +{len(bars)} bars (total {len(all_bars)})")
        next_url = data.get("next_url")
        if not next_url:
            break
        url, params = next_url, {"apiKey": api_key}
        time.sleep(0.5)
    return all_bars


def patch_csv(ticker, start, end):
    print(f"\n=== Patching {ticker}: {start} to {end} ===")
    bars = fetch_range(ticker, start, end)
    if not bars:
        print("  No bars returned -- nothing to patch.")
        return

    rows = [{
        "Timestamp": pd.to_datetime(b["window_start"], unit="ns", utc=True),
        "Open": float(b["open"]), "High": float(b["high"]),
        "Low": float(b["low"]), "Close": float(b["close"]),
        "Volume": float(b["volume"]),
    } for b in bars]
    df_new = pd.DataFrame(rows)

    csv_path = f"{ticker}_raw_1m_bars.csv"
    df_old = pd.read_csv(csv_path)
    df_old["Timestamp"] = pd.to_datetime(df_old["Timestamp"], utc=True)

    before = len(df_old)
    combined = pd.concat([df_old, df_new])
    combined = combined.drop_duplicates(subset="Timestamp", keep="last").sort_values("Timestamp")
    combined.to_csv(csv_path, index=False)
    print(f"  {ticker}: rows before={before}, after={len(combined)}, net new={len(combined) - before}")


if __name__ == "__main__":
    for ticker, start, end in PATCHES:
        patch_csv(ticker, start, end)
    print("\nDone. Next: re-run main.py (or ask Claude) to rebuild the continuous master + daily summaries.")
