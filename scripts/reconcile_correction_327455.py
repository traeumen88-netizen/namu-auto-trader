"""[Item 10] 아주IB투자(027360) 주문 327455 오체결 정정 및 감사 이력 기록 스크립트
- 오체결로 잘못 차감된 내부 포지션 수량을 브로커 실보유 수량(3주)으로 복원
- 조기 FILLED 처리된 주문 상태를 PENDING으로 복원
- 감사 추적성 유지를 위해 기존 데이터를 임의 삭제하지 않고 정정 이력(reconciliation_logs) 영구 기록
"""

import sqlite3
import json
import os
import time
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OP_DB_PATH = os.path.join(BASE_DIR, "data", "operational_v16.db")
TRADE_DB_PATH = os.path.join(BASE_DIR, "data", "trade_history_v7.db")


def reconcile():
    print("=" * 60)
    print("아주IB투자(027360) 주문 327455 정합성 보정 시작")
    print("=" * 60)

    # 1. operational_v16.db 보정
    if os.path.exists(OP_DB_PATH):
        conn = sqlite3.connect(OP_DB_PATH)
        c = conn.cursor()

        # 1-1. 주문 상태 복원 (FILLED -> PENDING)
        cid = "RESTART_RECOVERY_EXIT_027360_SELL_20260918092005629_7940"
        c.execute("SELECT status, broker_order_no, qty, price FROM orders WHERE client_order_id=?", (cid,))
        order_row = c.fetchone()
        if order_row:
            print(f"[주문 정정 전] client_order_id={cid}, status={order_row[0]}, broker_order_no={order_row[1]}")
            c.execute(
                "UPDATE orders SET status='PENDING', filled_at=NULL WHERE client_order_id=?",
                (cid,)
            )
            print(f"[주문 정정 후] client_order_id={cid} -> status='PENDING', filled_at=NULL")
        else:
            print(f"[주문 정정 경고] 주문 {cid} 미발견")

        # 1-2. 포지션 수량 복원 (1주 -> 3주)
        pos_id = "POS_027360_1789609454195"
        c.execute("SELECT qty, status FROM positions WHERE position_id=?", (pos_id,))
        pos_row = c.fetchone()
        if pos_row:
            print(f"[포지션 정정 전] position_id={pos_id}, qty={pos_row[0]}, status={pos_row[1]}")
            c.execute(
                "UPDATE positions SET qty=3 WHERE position_id=?",
                (pos_id,)
            )
            print(f"[포지션 정정 후] position_id={pos_id} -> qty=3 (브로커 실보유 3주와 일치)")
        else:
            print(f"[포지션 정정 경고] 포지션 {pos_id} 미발견")

        # 1-3. reconciliation_logs 정정 이력 삽입
        log_id = f"RECON_CORRECTION_327455_{int(time.time())}"
        now_iso = datetime.now().isoformat()
        diff_detected = 1
        details = json.dumps({
            "issue": "ORDER_ACK_TREATED_AS_FILLED",
            "symbol": "027360",
            "broker_order_no": "327455",
            "client_order_id": cid,
            "ground_truth": {
                "broker_holding_qty": 3,
                "broker_pending_sell_qty": 2,
                "broker_psbl_qty": 1,
                "broker_fill_qty": 0,
                "broker_unfilled_qty": 2,
                "broker_order_price": 4020
            },
            "prior_state": {
                "order_status": "FILLED",
                "internal_pos_qty": 1
            },
            "corrected_state": {
                "order_status": "PENDING",
                "internal_pos_qty": 3,
                "pending_exit_qty": 2,
                "available_qty": 1
            },
            "remedy": "Execution Integrity patch prevents ACK-as-FILLED. Reverted premature fill in DB and restored internal holding to 3."
        }, ensure_ascii=False)
        action_taken = "REVERT_FALSE_FILL_RESTORE_POSITION_QTY_TO_3_AND_ORDER_STATUS_TO_PENDING"

        c.execute(
            "INSERT INTO reconciliation_logs (log_id, timestamp, diff_detected, details, action_taken) VALUES (?, ?, ?, ?, ?)",
            (log_id, now_iso, diff_detected, details, action_taken)
        )
        print(f"[감사 로그 기록 완료] {log_id}: {action_taken}")

        conn.commit()
        conn.close()

    # 2. trade_history_v7.db 감사 보존 처리
    if os.path.exists(TRADE_DB_PATH):
        conn = sqlite3.connect(TRADE_DB_PATH)
        c = conn.cursor()
        trade_id = "T_027360_1789690807_027360_SCALE_OUT"
        c.execute("SELECT trade_id, bad_trade_category, raw_features FROM trades WHERE trade_id=?", (trade_id,))
        t_row = c.fetchone()
        if t_row:
            print(f"[트레이드 기록 보존 전] trade_id={trade_id}, category={t_row[1]}")
            c.execute(
                "UPDATE trades SET bad_trade_category='RECONCILED_FALSE_FILL', exit_reason='RECONCILED: Order 327455 unfilled on KRX' WHERE trade_id=?",
                (trade_id,)
            )
            print(f"[트레이드 기록 보존 후] trade_id={trade_id} -> bad_trade_category='RECONCILED_FALSE_FILL'")
        conn.commit()
        conn.close()

    print("=" * 60)
    print("아주IB투자(027360) 정합성 보정 완료")
    print("=" * 60)


if __name__ == "__main__":
    reconcile()
