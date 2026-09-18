import sqlite3
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

conn = sqlite3.connect("data/operational_v16.db")
conn.row_factory = sqlite3.Row

# Get all fills today for live account
fills = conn.execute("SELECT * FROM fills WHERE account_no = '20201549311' AND timestamp LIKE '2026-09-15%' ORDER BY timestamp ASC").fetchall()

print("=== ROUND TRIP RECONSTRUCTION FOR 2026-09-15 ===")

# Group by symbol
symbols = []
for f in fills:
    if f["iem_cd"] not in symbols:
        symbols.append(f["iem_cd"])

total_buy_cost_all = 0.0
total_sell_amt_all = 0.0
total_pnl_all = 0.0

roundtrips = []

for sym in symbols:
    sym_buys = [f for f in fills if f["iem_cd"] == sym and f["side"] == "BUY"]
    sym_sells = [f for f in fills if f["iem_cd"] == sym and f["side"] == "SELL"]
    
    tot_buy_qty = sum(f["filled_qty"] for f in sym_buys)
    tot_sell_qty = sum(f["filled_qty"] for f in sym_sells)
    
    tot_buy_val = sum(f["filled_qty"] * f["filled_price"] for f in sym_buys)
    tot_sell_val = sum(f["filled_qty"] * f["filled_price"] for f in sym_sells)
    
    avg_buy_p = (tot_buy_val / tot_buy_qty) if tot_buy_qty > 0 else 0.0
    avg_sell_p = (tot_sell_val / tot_sell_qty) if tot_sell_qty > 0 else 0.0
    
    # Check if this stock was entered yesterday (e.g. 490470 entered 2026-09-14)
    if tot_buy_qty == 0 and tot_sell_qty > 0:
        # Check positions table for entry price
        pos = conn.execute("SELECT * FROM positions WHERE iem_cd = ? AND account_no = '20201549311' AND exit_time LIKE '2026-09-15%'", (sym,)).fetchone()
        if pos:
            tot_buy_qty = tot_sell_qty
            avg_buy_p = float(pos["entry_price"])
            tot_buy_val = tot_buy_qty * avg_buy_p

    # Closed qty
    closed_qty = min(tot_buy_qty, tot_sell_qty)
    closed_buy_val = closed_qty * avg_buy_p
    closed_sell_val = closed_qty * avg_sell_p
    
    # Fees & Taxes estimation:
    # Broker commission ~ 0.015% each side
    # Securities transaction tax ~ 0.18% on SELL
    fees = (closed_buy_val + closed_sell_val) * 0.00015
    taxes = closed_sell_val * 0.0018
    net_pnl = closed_sell_val - closed_buy_val # without fee/tax or with
    
    roundtrips.append({
        "symbol": sym,
        "buy_qty": tot_buy_qty,
        "avg_buy_price": avg_buy_p,
        "sell_qty": tot_sell_qty,
        "avg_sell_price": avg_sell_p,
        "closed_qty": closed_qty,
        "closed_buy_val": closed_buy_val,
        "closed_sell_val": closed_sell_val,
        "gross_pnl": closed_sell_val - closed_buy_val,
        "fees": fees,
        "taxes": taxes,
        "net_pnl": (closed_sell_val - closed_buy_val) - fees - taxes,
        "open_qty": tot_buy_qty - closed_qty
    })

print(f"{'Symbol':<8} | {'Buy Qty':<7} | {'Avg Buy P':<10} | {'Sell Qty':<8} | {'Avg Sell P':<10} | {'Buy Cost':<10} | {'Sell Amount':<11} | {'Gross PnL':<10} | {'Net PnL':<10} | {'Open Qty'}")
print("-" * 115)

tot_closed_buy = 0
tot_closed_sell = 0
tot_gross_pnl = 0
tot_net_pnl = 0

for rt in roundtrips:
    tot_closed_buy += rt["closed_buy_val"]
    tot_closed_sell += rt["closed_sell_val"]
    tot_gross_pnl += rt["gross_pnl"]
    tot_net_pnl += rt["net_pnl"]
    print(f"{rt['symbol']:<8} | {rt['buy_qty']:<7} | {rt['avg_buy_price']:<10,.1f} | {rt['sell_qty']:<8} | {rt['avg_sell_price']:<10,.1f} | {rt['closed_buy_val']:<10,.0f} | {rt['closed_sell_val']:<11,.0f} | {rt['gross_pnl']:+10,.0f} | {rt['net_pnl']:+10,.0f} | {rt['open_qty']}")

print("-" * 115)
print(f"TOTALS   | {'':<7} | {'':<10} | {'':<8} | {'':<10} | {tot_closed_buy:<10,.0f} | {tot_closed_sell:<11,.0f} | {tot_gross_pnl:+10,.0f} | {tot_net_pnl:+10,.0f}")
