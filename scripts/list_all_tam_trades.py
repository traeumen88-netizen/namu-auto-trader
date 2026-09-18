import os
import sys

base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if base_dir not in sys.path:
    sys.path.insert(0, base_dir)

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from core.trade_accounting import TradeAccountingManager

tam = TradeAccountingManager()
trades = tam.get_completed_trades(trading_mode="live", account_no="20201549311")
print(f"Total trades: {len(trades)}")
for i, t in enumerate(trades, 1):
    print(f"{i:02d} | {t['symbol']} | {t['exit_time']} | Qty={t['qty']} | PnL={t['net_pnl']:+,d}")
