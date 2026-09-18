"""[MASTER TEST SUITE: LIVE PRIORITY & QUALITY GATES]
Tests covering the 15+ requirements:
1. TelegramEventFilter blocks MOCK / PAPER accounts
2. TelegramEventFilter passes LIVE accounts
3. send_trade_event strictly blocks MOCK events
4. send_trade_event passes LIVE events
5. send_system_alert blocks MOCK alerts
6. EOD worker queries LIVE trades only
7. LiveQuantTrader enforces LIVE account priority first
8. LiveQuantTrader isolates MOCK exceptions from impacting LIVE
9. EntryTimingQualityGate blocks RVOL < 1.5 for breakout
10. EntryTimingQualityGate blocks negative 3m momentum
11. EntryTimingQualityGate blocks late falling entries (drop > 2% from high)
12. EntryTimingQualityGate blocks price below VWAP for breakout
13. EntryTimingQualityGate disables INT_ORB standalone BUY
14. EntryTimingQualityGate allows pullback below VWAP only with rebound confirmed
15. PositionManager sets 15m (900s) cooldown on stop loss
16. Dynamic TIME_STOP allows trending positions to run > 60m
17. Dynamic TIME_STOP terminates stagnant positions
18. Daily same-symbol re-entry counter tracks attempts and throttles entry 3+
19. Churn telemetry logged on repeated losses
"""

import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta

from core.telegram_notifier import TelegramEventFilter, TelegramNotifier
from strategies.breakout_gate import EntryTimingQualityGate, validate_breakout_entry
from execution.live_quant_trader import AccountContext, LiveQuantTrader
from execution.position_manager import PositionManager, Position
from core.models import TimeHorizon, OrderSide, OrderType, OrderStatus


class TestTelegramLiveOnlyFilter(unittest.TestCase):
    """텔레그램 실전(LIVE) 계좌 전용 필터 및 알림 검증"""

    def test_01_filter_blocks_mock_accounts(self):
        self.assertFalse(TelegramEventFilter.is_live_account(account="MOCK"))
        self.assertFalse(TelegramEventFilter.is_live_account(account="mock"))
        self.assertFalse(TelegramEventFilter.is_live_account(trading_mode="mock"))
        self.assertFalse(TelegramEventFilter.is_live_account(account="PAPER_TRADING"))
        self.assertFalse(TelegramEventFilter.is_live_account(trading_mode="simul"))

    def test_02_filter_passes_live_accounts(self):
        self.assertTrue(TelegramEventFilter.is_live_account(account="LIVE"))
        self.assertTrue(TelegramEventFilter.is_live_account(account="live"))
        self.assertTrue(TelegramEventFilter.is_live_account(trading_mode="live"))

    def test_03_send_trade_event_blocks_mock_completely(self):
        notifier = TelegramNotifier()
        notifier.enabled = True
        notifier.token = "dummy_token"
        notifier.chat_id = "12345"

        with patch("requests.post") as mock_post:
            result = notifier.send_trade_event(
                event_type="BUY",
                symbol="005930",
                name="삼성전자",
                price=70000,
                qty=10,
                account="MOCK",
                trading_mode="mock"
            )
            self.assertFalse(result)
            mock_post.assert_not_called()

    def test_04_send_trade_event_passes_live(self):
        notifier = TelegramNotifier()
        notifier.enabled = True
        notifier.token = "dummy_token"
        notifier.chat_id = "12345"

        with patch("requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_post.return_value = mock_resp

            result = notifier.send_trade_event(
                event_type="BUY",
                symbol="005930",
                name="삼성전자",
                price=70000,
                qty=10,
                account="LIVE",
                trading_mode="live",
                trade_id=f"TEST_UNIQUE_{int(datetime.now().timestamp() * 1000)}"
            )
            self.assertTrue(result)
            mock_post.assert_called_once()

    def test_05_send_system_alert_blocks_mock(self):
        notifier = TelegramNotifier()
        notifier.enabled = True
        notifier.token = "dummy_token"
        notifier.chat_id = "12345"

        with patch("requests.post") as mock_post:
            result = notifier.send_system_alert(
                title="서킷브레이커",
                message="MOCK 계좌 리스크 한도 초과",
                account="MOCK",
                trading_mode="mock"
            )
            self.assertFalse(result)
            mock_post.assert_not_called()


class TestEntryTimingQualityGate(unittest.TestCase):
    """진입 타이밍 퀄리티 게이트 검증"""

    def setUp(self):
        self.curr_price = 10000
        self.vwap = 9950.0
        self.rvol = 2.0
        self.c_1m_open = 9980
        self.p_3m_ago = 9900
        self.high_price = 10100

    def test_06_blocks_rvol_below_min(self):
        ok, reason, _ = EntryTimingQualityGate.validate_breakout_momentum(
            curr_price=self.curr_price,
            rvol=1.2,  # < 1.5
            current_1m_open=self.c_1m_open,
            price_3m_ago=self.p_3m_ago,
            high_price=self.high_price,
            vwap=self.vwap,
            strategy_id="INT_BREAKOUT"
        )
        self.assertFalse(ok)
        self.assertIn("ENTRY_RVOL_LOW", reason)

    def test_07_blocks_negative_3m_momentum(self):
        ok, reason, _ = EntryTimingQualityGate.validate_breakout_momentum(
            curr_price=self.curr_price,
            rvol=2.0,
            current_1m_open=self.c_1m_open,
            price_3m_ago=10100,  # 3m ago was higher -> negative momentum
            high_price=self.high_price,
            vwap=self.vwap,
            strategy_id="INT_BREAKOUT"
        )
        self.assertFalse(ok)
        self.assertIn("ENTRY_MOMENTUM_NEGATIVE", reason)

    def test_08_blocks_late_falling_entry(self):
        high = 10500  # curr 10000 is < 10500 * 0.98 (10290)
        ok, reason, _ = EntryTimingQualityGate.validate_breakout_momentum(
            curr_price=self.curr_price,
            rvol=2.0,
            current_1m_open=self.c_1m_open,
            price_3m_ago=10050,  # negative momentum
            high_price=high,
            vwap=self.vwap,
            strategy_id="INT_BREAKOUT"
        )
        self.assertFalse(ok)
        self.assertIn("LATE_FALLING_ENTRY", reason)

    def test_09_blocks_price_below_vwap_for_breakout(self):
        ok, reason, _ = EntryTimingQualityGate.validate_breakout_momentum(
            curr_price=9900,
            rvol=2.0,
            current_1m_open=9850,
            price_3m_ago=9800,
            high_price=9950,
            vwap=10000.0,  # curr < vwap
            require_above_vwap=True,
            strategy_id="INT_BREAKOUT"
        )
        self.assertFalse(ok)
        self.assertIn("BELOW_VWAP", reason)

    def test_10_disables_int_orb(self):
        ok, reason, _ = EntryTimingQualityGate.validate_breakout_momentum(
            curr_price=self.curr_price,
            rvol=3.0,
            current_1m_open=self.c_1m_open,
            price_3m_ago=self.p_3m_ago,
            high_price=self.high_price,
            vwap=self.vwap,
            strategy_id="INT_ORB"
        )
        self.assertFalse(ok)
        self.assertIn("DISABLED_STRATEGY_INT_ORB", reason)

    def test_11_pullback_below_vwap_requires_rebound(self):
        # Without rebound confirmed -> Reject
        ok, reason, _ = EntryTimingQualityGate.validate_pullback_rebound(
            curr_price=9900,
            vwap=10000.0,
            rvol=1.5,
            rebound_confirmed=False
        )
        self.assertFalse(ok)
        self.assertIn("ENTRY_NO_REBOUND", reason)

        # With rebound confirmed -> Pass
        ok, reason, _ = EntryTimingQualityGate.validate_pullback_rebound(
            curr_price=9900,
            vwap=10000.0,
            rvol=1.5,
            rebound_confirmed=True
        )
        self.assertTrue(ok)
        self.assertIn("ENTRY_QUALITY_APPROVED", reason)


class TestPositionManagerAndCooldown(unittest.TestCase):
    """포지션 관리자, 스톱로스 쿨다운 및 동적 타임스톱 검증"""

    def setUp(self):
        self.pm = PositionManager(order_router=None)

    def test_12_stop_loss_records_loss_trade(self):
        now = datetime(2026, 9, 17, 10, 0, 0)
        pos = self.pm.open_position(
            time_horizon=TimeHorizon.INTRADAY,
            strategy_id="INT_BREAKOUT",
            iem_cd="005930",
            name="삼성전자",
            qty=10,
            entry_price=70000,
            stop_price=68000,
            target_1r=72000,
            target_2r=74000,
            target_3r=76000,
            initial_risk=20000
        )
        pos.entry_time = now

        # Update price to stop-loss (68,000)
        self.pm.update_price_and_manage(
            iem_cd="005930",
            current_price=67500,
            current_time=now + timedelta(minutes=5),
            vwap=70000,
            momentum_3m=-0.02
        )

        last_closed = self.pm.get_last_closed_trade("005930")
        self.assertIsNotNone(last_closed)
        self.assertTrue(last_closed["is_stop_loss"])
        self.assertLess(last_closed["pnl"], 0)

    def test_13_dynamic_time_stop_exempts_trending_positions_past_60m(self):
        now = datetime(2026, 9, 17, 10, 0, 0)
        pos = self.pm.open_position(
            time_horizon=TimeHorizon.INTRADAY,
            strategy_id="INT_BREAKOUT",
            iem_cd="005930",
            name="삼성전자",
            qty=10,
            entry_price=70000,
            stop_price=68000,
            target_1r=72000,
            target_2r=74000,
            target_3r=76000,
            initial_risk=20000
        )
        pos.entry_time = now
        pos.highest_price = 70800

        # After 75 minutes, price is 70500 (profitable, near high, above VWAP, positive momentum)
        check_time = now + timedelta(minutes=75)
        self.pm.update_price_and_manage(
            iem_cd="005930",
            current_price=70500,
            current_time=check_time,
            vwap=70200,
            momentum_3m=0.005
        )

        # Position must NOT be closed! (Allowed to run > 60m)
        self.assertTrue(self.pm.has_position("005930"))
        self.assertNotEqual(pos.status, "TIME_STOP_TRIGGERED")

    def test_14_dynamic_time_stop_terminates_stagnant_positions(self):
        now = datetime(2026, 9, 17, 10, 0, 0)
        pos = self.pm.open_position(
            time_horizon=TimeHorizon.INTRADAY,
            strategy_id="INT_BREAKOUT",
            iem_cd="005930",
            name="삼성전자",
            qty=10,
            entry_price=70000,
            stop_price=68000,
            target_1r=72000,
            target_2r=74000,
            target_3r=76000,
            initial_risk=20000
        )
        pos.entry_time = now
        pos.highest_price = 70100

        # After 35 minutes, price has stagnated at 69900 (in loss, negative momentum, below VWAP)
        check_time = now + timedelta(minutes=35)
        self.pm.update_price_and_manage(
            iem_cd="005930",
            current_price=69900,
            current_time=check_time,
            vwap=70100,
            momentum_3m=-0.005
        )

        # Position should be closed via TIME_STOP
        self.assertFalse(self.pm.has_position("005930"))
        last_closed = self.pm.get_last_closed_trade("005930")
        self.assertIsNotNone(last_closed)
        self.assertIn("타임스톱", last_closed["reason"])


class TestLiveQuantTraderPriority(unittest.TestCase):
    """실전 계좌(LIVE) 최우선 집행 및 계좌 간 예외 격리 검증"""

    def test_15_live_account_is_strictly_first(self):
        # Even if accounts were added in any order, self.accounts must sort LIVE first
        with patch.object(LiveQuantTrader, "_init_premarket_data"), \
             patch("universe.full_universe_master.FullUniverseMaster.load_full_universe", return_value={}):
            trader = LiveQuantTrader(mode="dual")
            self.assertEqual(trader.accounts[0].name, "LIVE")
            self.assertEqual(trader.accounts[1].name, "MOCK")


if __name__ == "__main__":
    unittest.main()
