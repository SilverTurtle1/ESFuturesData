import logging
import pandas as pd
import psycopg2
from psycopg2.extras import execute_values
from db_settings import db

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def sync_master_csv_to_postgres(master_csv_path: str, symbol: str = "ES", asset_type: str = "futures",
                                 timeframe: str = "1m"):
    """Upserts the continuous master 1m bars into the shared market_data Postgres database
    so other projects can query the same history without touching the CSV directly."""
    df = pd.read_csv(master_csv_path)
    df["Timestamp"] = pd.to_datetime(df["Timestamp"], utc=True)

    conn = psycopg2.connect(**db)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO symbols (symbol, asset_type, description)
                VALUES (%s, %s, %s)
                ON CONFLICT (symbol, asset_type) DO NOTHING
                RETURNING id
                """,
                (symbol, asset_type, "Continuous ES futures contract, stitched from CME quarterly contracts"),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "SELECT id FROM symbols WHERE symbol = %s AND asset_type = %s",
                    (symbol, asset_type),
                )
                row = cur.fetchone()
            symbol_id = row[0]

            rows = [
                (symbol_id, timeframe, ts.to_pydatetime(), o, h, l, c, v)
                for ts, o, h, l, c, v in zip(
                    df["Timestamp"], df["Open"], df["High"], df["Low"], df["Close"], df["Volume"]
                )
            ]

            execute_values(
                cur,
                """
                INSERT INTO ohlcv_bars (symbol_id, timeframe, ts, open, high, low, close, volume)
                VALUES %s
                ON CONFLICT (symbol_id, timeframe, ts) DO UPDATE SET
                    open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close,
                    volume = EXCLUDED.volume
                """,
                rows,
                page_size=5000,
            )
        conn.commit()
        logging.info(f"Postgres sync: upserted {len(rows)} {timeframe} bars for {symbol} into market_data.")
    finally:
        conn.close()
