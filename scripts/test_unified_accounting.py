import sys
import os

base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if base_dir not in sys.path:
    sys.path.insert(0, base_dir)

from core.trade_accounting import TradeAccountingManager

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

tam = TradeAccountingManager()
trades = tam.get_completed_trades(trading_mode="live", account_no="20201549311", date_str="2026-09-15")
print(f"Completed trades count: {len(trades)}")
total_pnl = sum(t["net_pnl"] for t in trades)
print(f"Total net PnL: {total_pnl:+,.0f} KRW")

wins = [t for t in trades if t["net_pnl"] > 0]
losses = [t for t in trades if t["net_pnl"] <= 0]
win_amt = sum(t["net_pnl"] for t in wins)
loss_amt = sum(t["net_pnl"] for t in losses)
win_rate = len(wins) / len(trades) * 100 if trades else 0.0

print(f"Wins: {len(wins)} ({win_amt:+,.0f} KRW)")
print(f"Losses: {len(losses)} ({loss_amt:+,.0f} KRW)")
print(f"Win Rate: {win_rate:.1f}%")
print(f"Profit Factor: {abs(win_amt / loss_amt):.2f}" if loss_amt != 0 else "N/A")

for i, t in enumerate(trades, 1):
    print(f"[{i:02d}] {t['symbol']:<8} ({t['symbol_name']}) | Qty={t['qty']:>4} | BuyP={t['entry_price']:>9,f} | ExitP={t['exit_price']:>9,f} | PnL={t['net_pnl']:>9,f} | Reason={t['exit_reason']}")
