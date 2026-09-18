import sqlite3
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

conn = sqlite3.connect("data/trade_history_v7.db")
conn.row_factory = sqlite3.Row

rows = conn.execute("SELECT * FROM trades WHERE account_no = '20201549311' AND LOWER(trading_mode) = 'live' AND SUBSTR(exit_time, 12, 5) BETWEEN '08:30' AND '18:10'").fetchall()
print(f"Valid market hours trades: {len(rows)}")
today_rows = [r for r in rows if r["exit_time"].startswith("2026-09-15")]
print(f"Today market hours trades: {len(today_rows)}")
pnl_sum = sum(r["pnl"] for r in today_rows)
print(f"Today PnL sum: {pnl_sum:+,.0f}")
