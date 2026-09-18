import sqlite3
import sys
import os
import json

base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if base_dir not in sys.path:
    sys.path.insert(0, base_dir)

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

print("==================================================")
print("1. OPERATIONAL_V16.DB - POSITIONS")
print("==================================================")
conn_op = sqlite3.connect("data/operational_v16.db")
conn_op.row_factory = sqlite3.Row
positions = conn_op.execute("SELECT * FROM positions WHERE account_no = '20201549311' ORDER BY rowid ASC").fetchall()
print(f"Total positions in operational_v16: {len(positions)}")
pos_pnl_sum = 0.0
for p in positions:
    pnl = float(p["pnl"] or 0)
    pos_pnl_sum += pnl
    print(f"PosID={p['position_id']} | Sym={p['iem_cd']}({p['name']}) | Status={p['status']} | Qty={p['qty']} | Entry={p['entry_price']} | Exit={p['exit_price']} | PnL={pnl:+,.0f} | EntryTime={p['entry_time']} | ExitTime={p['exit_time']}")
print(f"Sum of PnL in positions: {pos_pnl_sum:+,.0f}")

print("\n==================================================")
print("2. OPERATIONAL_V16.DB - FILLS (SELL & BUY)")
print("==================================================")
fills = conn_op.execute("SELECT * FROM fills WHERE account_no = '20201549311' ORDER BY timestamp ASC").fetchall()
print(f"Total fills in operational_v16: {len(fills)}")
buy_fills = [f for f in fills if f["side"] == "BUY"]
sell_fills = [f for f in fills if f["side"] == "SELL"]
print(f"BUY fills: {len(buy_fills)}, SELL fills: {len(sell_fills)}")
total_buy_val = sum(f["filled_qty"] * f["filled_price"] for f in buy_fills)
total_sell_val = sum(f["filled_qty"] * f["filled_price"] for f in sell_fills)
print(f"Total BUY fills value: {total_buy_val:,.0f} KRW")
print(f"Total SELL fills value: {total_sell_val:,.0f} KRW")
print(f"Gross Fill PnL (SELL - BUY): {total_sell_val - total_buy_val:+,.0f} KRW")

print("\n==================================================")
print("3. TRADE_HISTORY_V7.DB - TRADES")
print("==================================================")
conn_v7 = sqlite3.connect("data/trade_history_v7.db")
conn_v7.row_factory = sqlite3.Row
trades = conn_v7.execute("SELECT * FROM trades WHERE account_no = '20201549311' AND exit_time LIKE '2026-09-15%' ORDER BY exit_time ASC").fetchall()
print(f"Total today trades in trade_history_v7: {len(trades)}")
v7_pnl_all = sum(float(t["pnl"]) for t in trades)
v7_pnl_no_rec = sum(float(t["pnl"]) for t in trades if not str(t["setup_name"]).startswith("RESTART_RECOVERY"))
v7_pnl_rec_only = sum(float(t["pnl"]) for t in trades if str(t["setup_name"]).startswith("RESTART_RECOVERY"))
print(f"All today trades PnL: {v7_pnl_all:+,.0f} KRW")
print(f"Trades WITHOUT RESTART_RECOVERY PnL: {v7_pnl_no_rec:+,.0f} KRW (Count: {len([t for t in trades if not str(t['setup_name']).startswith('RESTART_RECOVERY')])})")
print(f"RESTART_RECOVERY ONLY PnL: {v7_pnl_rec_only:+,.0f} KRW (Count: {len([t for t in trades if str(t['setup_name']).startswith('RESTART_RECOVERY')])})")

print("\n==================================================")
print("4. BROKER REAL BALANCE & EXECUTIONS (NamuClient)")
print("==================================================")
try:
    from namu_client import NamuClient
    client = NamuClient(mode="live", act_no="20201549311")
    bal = client.get_balance()
    print(f"Broker Cash: {bal.get('cash'):,}")
    print(f"Broker Total Equity: {bal.get('total_equity'):,}")
    print(f"Broker Total Profit: {bal.get('total_profit'):,}")
    print(f"Broker Holdings Count: {len(bal.get('holdings', []))}")
    for h in bal.get("holdings", []):
        print(f"  Holding: {h.get('stock_code')}({h.get('stock_name')}) Qty={h.get('qty')} BuyP={h.get('buy_price')} NowP={h.get('now_price')} PnL={h.get('profit_amount')}")
except Exception as e:
    print(f"Error connecting to broker: {e}")
