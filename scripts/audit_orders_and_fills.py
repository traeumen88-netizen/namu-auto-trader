import sqlite3
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

conn = sqlite3.connect("data/operational_v16.db")
conn.row_factory = sqlite3.Row

print("=== ORDERS ON 2026-09-15 ===")
orders = conn.execute("SELECT * FROM orders WHERE account_no = '20201549311' AND created_at LIKE '2026-09-15%' ORDER BY created_at ASC").fetchall()
print(f"Total orders: {len(orders)}")
for o in orders:
    print(f"ID={o['client_order_id']} | BrokerNo={o['broker_order_no']} | Sym={o['iem_cd']} | Side={o['side']} | Qty={o['qty']} | Price={o['price']} | Status={o['status']} | Created={o['created_at']} | Filled={o['filled_at']}")

print("\n=== FILLS ON 2026-09-15 ===")
fills = conn.execute("SELECT * FROM fills WHERE account_no = '20201549311' AND timestamp LIKE '2026-09-15%' ORDER BY timestamp ASC").fetchall()
print(f"Total fills: {len(fills)}")
for f in fills:
    print(f"FillID={f['fill_id']} | OrderID={f['client_order_id']} | Sym={f['iem_cd']} | Side={f['side']} | Qty={f['filled_qty']} | Price={f['filled_price']} | Time={f['timestamp']}")
