# -*- coding: utf-8 -*-
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, ".")
from web_dashboard import DashboardStateManager

mgr_live = DashboardStateManager(default_mode='live')
state_live = mgr_live.get_full_state()
print("=== LIVE POSITIONS ===")
for p in state_live.get('positions', []):
    print(f"[{p['symbol']}] {p['name']} | 평단: {p['entry_price']:,}원 | 현재가: {p['current_price']:,}원 | 손절가: {p['stop_price']:,}원 | 1차매도기준가: {p['target_1r']:,}원 (도달: {p['target_1r_hit']}) | 2차매도기준가: {p['target_2r']:,}원 (도달: {p['target_2r_hit']})")

mgr_mock = DashboardStateManager(default_mode='mock')
state_mock = mgr_mock.get_full_state()
print("\n=== MOCK POSITIONS ===")
for p in state_mock.get('positions', []):
    print(f"[{p['symbol']}] {p['name']} | 평단: {p['entry_price']:,}원 | 현재가: {p['current_price']:,}원 | 손절가: {p['stop_price']:,}원 | 1차매도기준가: {p['target_1r']:,}원 (도달: {p['target_1r_hit']}) | 2차매도기준가: {p['target_2r']:,}원 (도달: {p['target_2r_hit']})")
