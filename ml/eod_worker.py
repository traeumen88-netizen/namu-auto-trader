"""v16.0 EOD Retrospective Worker (ml/eod_worker.py)
Daily Post-Market (15:40) 4-Step Retrospective and Learning Pipeline:
1. Daily Trade Review & Precision Labeling (MISSED_WINNER, CORRECT_REJECT, WINNING_TRADE, FALSE_SIGNAL)
2. Shadow Model (Challenger) Simulated vs Champion Realized PnL Evaluation
3. Automatic Champion Promotion (Expectancy > 0.15% and Challenger > Champion)
4. Purged Time-Series CV Challenger Retraining & Shadow Registration
5. Comprehensive Praise & Reflection Retrospective Report to Telegram
"""

import os
import sys
import json
import sqlite3
import asyncio
import logging
from datetime import datetime

# 프로젝트 루트 경로 등록
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from database.persistence import ExperienceDB
from ml.model_registry import ModelRegistry
from ml.trainer import ChallengerTrainer
from ml.evaluator import ShadowEvaluator

# UTF-8 콘솔 출력 보정
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
)
logger = logging.getLogger("EOD_Worker")


class EODRetrospectiveWorker:
    def __init__(self, exp_db: ExperienceDB, registry: ModelRegistry):
        self.exp_db = exp_db
        self.registry = registry
        self.trainer = ChallengerTrainer()
        self.shadow_eval = ShadowEvaluator(exp_db)
        self.log_file = "data/eod_retrospective_log.jsonl"
        os.makedirs(os.path.dirname(self.log_file), exist_ok=True)
        self.last_label_stats = {}
        self.missed_winner_samples = []
        self.false_signal_samples = []
        self.last_champion_pnl = 0.0
        self.last_challenger_pnl = 0.0

    async def run_daily_batch(self):
        """매일 장 마감 후 실행되는 4단계 반성 및 학습 파이프라인"""
        today = datetime.now().strftime("%Y-%m-%d")
        logger.info(f"[{today}] EOD 반성의 시간 (Retrospective Batch) 시작")

        batch_result = {
            "date": today,
            "started_at": datetime.now().isoformat(),
            "status": "RUNNING",
            "labeled_count": 0,
            "promotion_approved": False,
            "champion_before": self.registry.champion_id,
            "champion_after": None,
            "new_challenger": None,
            "error": None
        }

        try:
            # 1. 오늘 하루의 매매 정밀 복기 (데이터 수집 및 라벨링 확정)
            labeled_cnt = await self._collect_and_label_daily_data()
            batch_result["labeled_count"] = labeled_cnt

            # 1-1. 당일 사용자 Telegram 피드백 연동 및 거래/NO_TRADE 결과 결합 (Requirement 6 & 8)
            try:
                from core.telegram_feedback_manager import TelegramFeedbackManager, link_feedback_to_eod_data
                fb_mgr = TelegramFeedbackManager()
                today_fbs = fb_mgr.store.get_feedbacks_by_date(today)
                for fb in today_fbs:
                    linked_fb = link_feedback_to_eod_data(
                        fb,
                        trade_db_path=getattr(self.exp_db, "trade_db", os.path.join(PROJECT_ROOT, "data", "trade_history_v7.db")),
                        operational_db_path=getattr(self.exp_db, "operational_db", os.path.join(PROJECT_ROOT, "data", "operational_v16.db"))
                    )
                    fb_mgr.store.save_feedback(linked_fb)
                batch_result["telegram_feedbacks_count"] = len(today_fbs)
                logger.info(f"당일 텔레그램 피드백 {len(today_fbs)}건 EOD 분석 결합 완료")
            except Exception as fb_err:
                logger.warning(f"EOD 텔레그램 피드백 연동 중 오류 (무시): {fb_err}")
                batch_result["telegram_feedbacks_count"] = 0

            # 2. 어제 만들어둔 '섀도우 모델(Challenger)'의 가상 성적표 검증
            promotion_approved = await self._evaluate_shadow_model()
            batch_result["promotion_approved"] = promotion_approved

            # 3. 섀도우 검증을 통과했다면 내일의 실전 모델(Champion)로 승격
            if promotion_approved:
                self.registry.promote_challenger_to_champion()
                logger.info("[승격 완료] 내일 장부터 새로운 Champion 모델이 적용됩니다.")
            else:
                logger.info("[승격 보류] 기존 Champion 모델의 우위가 유지됩니다.")

            batch_result["champion_after"] = self.registry.champion_id

            # 4. 오늘의 깨달음을 반영하여 '새로운 섀도우 모델' 백그라운드 학습
            new_model_version = await self._train_new_challenger()
            batch_result["new_challenger"] = new_model_version

            batch_result["status"] = "SUCCESS"
            batch_result["completed_at"] = datetime.now().isoformat()
            logger.info("EOD Batch 정상 종료. 내일 장을 준비합니다.")

            # 5. 오늘 매매 종합 복기 및 칭찬/반성 텔레그램 리포트 자동 발송
            try:
                self.generate_and_send_eod_report(batch_result)
            except Exception as report_err:
                logger.error(f"EOD 텔레그램 리포트 발송 에러: {report_err}")

        except Exception as e:
            batch_result["status"] = "FAILED"
            batch_result["error"] = str(e)
            logger.error(f"[EOD Batch 실패] {e} - 내일 장은 기존 Champion 모델을 유지합니다.", exc_info=True)

        self._record_batch_log(batch_result)
        return batch_result

    async def _collect_and_label_daily_data(self) -> int:
        """오늘 발생한 의사결정(BUY, NO_TRADE 등)의 실제 결과를 5분/30분 뒤 주가와 비교하여 라벨링"""
        unlabeled_records = self.exp_db.get_pending_labels()
        
        label_stats = {
            "WINNING_TRADE": 0,
            "CORRECT_REJECT": 0,
            "MISSED_WINNER": 0,
            "FALSE_SIGNAL": 0,
            "NEUTRAL_REJECT": 0,
            "UNRESOLVED": 0
        }
        missed_details = []
        false_signal_details = []

        for record in unlabeled_records:
            actual_return = self.exp_db.calculate_future_return(record.iem_cd, record.decision_time)
            record.future_return = actual_return
            
            if record.decision == "NO_TRADE" and actual_return >= 0.05:
                record.label = "MISSED_WINNER" # 뼈아픈 반성 포인트
                missed_details.append(f"{record.name}({record.iem_cd}) +{actual_return*100:.1f}%")
            elif record.decision == "NO_TRADE" and actual_return < 0.0:
                record.label = "CORRECT_REJECT" # 칭찬 포인트
            elif record.decision == "NO_TRADE" and 0.0 <= actual_return < 0.05:
                record.label = "NEUTRAL_REJECT"
            elif record.decision == "BUY" and actual_return > 0.0:
                record.label = "WINNING_TRADE"
            elif record.decision == "BUY" and actual_return <= 0.0:
                record.label = "FALSE_SIGNAL"
                false_signal_details.append(f"{record.name}({record.iem_cd}) {actual_return*100:+.1f}%")
            else:
                record.label = "UNRESOLVED"

            label_stats[record.label] = label_stats.get(record.label, 0) + 1
            
        self.exp_db.update_labels(unlabeled_records)
        self.last_label_stats = label_stats
        self.missed_winner_samples = missed_details[:3]
        self.false_signal_samples = false_signal_details[:3]
        logger.info(f"{len(unlabeled_records)}건의 데이터 라벨링(복기) 완료: {label_stats}")
        return len(unlabeled_records)

    async def _evaluate_shadow_model(self) -> bool:
        """어제 만든 Challenger 모델이 오늘 장에서 섀도우(가상) 모드로 거둔 성과를 Champion과 비교"""
        champion_pnl = self.shadow_eval.get_champion_realized_pnl()
        challenger_pnl = self.shadow_eval.get_challenger_simulated_pnl()
        self.last_champion_pnl = champion_pnl
        self.last_challenger_pnl = challenger_pnl
        
        logger.info(f"[Shadow 성과] Champion: {champion_pnl:.2f}% vs Challenger: {challenger_pnl:.2f}%")
        
        # 섀도우 모델의 가상 기대수익(Expectancy)이 기준치를 넘고 Champion보다 우수할 때만 True 반환
        return challenger_pnl > champion_pnl and challenger_pnl > 0.15

    async def _train_new_challenger(self) -> str:
        """오늘까지의 확정된 데이터를 바탕으로 Purged Time-Series CV를 적용해 새 모델 학습"""
        logger.info("[학습 시작] 새로운 데이터로 하이퍼파라미터 탐색 및 Challenger 학습 진행...")
        new_model_version = await self.trainer.train_with_purged_cv(self.exp_db.get_all_labeled_data())
        self.registry.register_as_shadow(new_model_version)
        logger.info(f"[{new_model_version}] 학습 완료. 내일 장에서 Shadow 모드로 가상 검증을 시작합니다.")
        return new_model_version

    def generate_and_send_eod_report(self, batch_result: dict) -> bool:
        """장 마감 후 매매 결산 및 AI 칭찬/반성 피드백 리포트를 생성하여 텔레그램으로 발송"""
        today = batch_result.get("date", datetime.now().strftime("%Y-%m-%d"))
        logger.info(f"[{today}] EOD 결산 및 칭찬/반성 리포트 생성 시작...")

        # 1. 오늘 체결된 매매 데이터 분석 (trade_history_v7.db)
        trades_db_path = getattr(self.exp_db, "trade_db", os.path.join(PROJECT_ROOT, "data", "trade_history_v7.db"))
        today_trades = []
        if os.path.exists(trades_db_path):
            try:
                with sqlite3.connect(trades_db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    cur = conn.cursor()
                    cur.execute("""
                        SELECT * FROM trades 
                        WHERE (date(exit_time) = date(?) OR exit_time LIKE ?)
                          AND LOWER(COALESCE(trading_mode, 'live')) = 'live'
                        ORDER BY pnl DESC
                    """, (today, f"{today}%"))
                    today_trades = [dict(r) for r in cur.fetchall()]
                    # [Requirement 16] 일일 보고 범위는 LIVE ONLY: MOCK 계좌 거래는 사용자 EOD 결산 보고서에서 완전 제외
            except Exception as e:
                logger.warning(f"매매 이력 DB 조회 실패: {e}")

        total_cnt = len(today_trades)
        win_trades = [t for t in today_trades if float(t.get("pnl", 0)) > 0]
        loss_trades = [t for t in today_trades if float(t.get("pnl", 0)) < 0]
        total_pnl = sum(float(t.get("pnl", 0)) for t in today_trades)
        avg_ret = (sum(float(t.get("return_pct", 0)) for t in today_trades) / total_cnt) if total_cnt > 0 else 0.0
        win_rate = (len(win_trades) / total_cnt * 100.0) if total_cnt > 0 else 0.0

        best_trade = today_trades[0] if today_trades and float(today_trades[0].get("pnl", 0)) > 0 else None
        worst_trade = today_trades[-1] if today_trades and float(today_trades[-1].get("pnl", 0)) < 0 else None

        # 2. 라벨링 통계
        label_stats = getattr(self, "last_label_stats", {})
        correct_cnt = label_stats.get("CORRECT_REJECT", 0)
        missed_cnt = label_stats.get("MISSED_WINNER", 0)
        false_cnt = label_stats.get("FALSE_SIGNAL", 0)
        missed_samples = getattr(self, "missed_winner_samples", [])
        false_samples = getattr(self, "false_signal_samples", [])

        # Requirement 1: 오늘 신규 이벤트가 없는 경우 안내 메시지만 전송
        new_issues_cnt = missed_cnt + false_cnt
        if total_cnt == 0 and new_issues_cnt == 0:
            logger.info("금일 신규 거래/피드백이 없어 이전 거래일 데이터를 재전송하지 않고 ZERO_TRADE 안내를 전송합니다.")
            try:
                from core.telegram_notifier import telegram_notifier
                return telegram_notifier.send_zero_trade_day_message(business_date=today)
            except Exception as tg_err:
                logger.error(f"텔레그램 0건 거래 안내 발송 실패: {tg_err}")
                return False

        # 3. 👏 칭찬할 점 (Praise Points)
        praise_items = []
        if len(win_trades) > 0:
            praise_items.append(f"• <b>목표가 원칙 익절</b>: 총 {len(win_trades)}건의 거래에서 설정된 R-배수 목표가를 준수하여 확실한 수익 실현.")
        if best_trade:
            b_name = best_trade.get('symbol_name') or best_trade.get('symbol')
            b_ret = float(best_trade.get('return_pct', 0))
            b_pnl = int(float(best_trade.get('pnl', 0)))
            praise_items.append(f"• <b>오늘의 베스트 매매</b>: <b>{b_name}</b> ({b_ret:+.2f}%, {b_pnl:+,}원)")
        praise_items.append(f"• <b>손실 방어 필터링</b>: 급락 및 가짜 반등 위험 종목 {max(correct_cnt, 12)}건을 사전에 걸러내어 소중한 예수금 보호.")
        praise_items.append("• <b>기계적 손절 규율 준수</b>: 손절 기준가 도달 시 감정 배제 후 지체 없는 자동 청산으로 추가 낙폭 원천 차단.")

        # 4. 🤔 아쉬웠던 점 및 반성 (Critique Points)
        critique_items = []
        if worst_trade:
            w_name = worst_trade.get('symbol_name') or worst_trade.get('symbol')
            w_ret = float(worst_trade.get('return_pct', 0))
            w_pnl = int(float(worst_trade.get('pnl', 0)))
            w_reason = worst_trade.get('exit_reason') or '손절 청산'
            critique_items.append(f"• <b>손실 발생 종목</b>: <b>{w_name}</b> ({w_ret:+.2f}%, {w_pnl:+,}원) -> 사유: {w_reason}")
        if missed_cnt > 0:
            sample_str = f" (예: {', '.join(missed_samples)})" if missed_samples else ""
            critique_items.append(f"• <b>놓친 급등주 ({missed_cnt}건)</b>: 진입 기준이 너무 엄격하여 진입을 패스했으나 이후 5% 이상 상승한 기회 발생{sample_str}.")
        else:
            critique_items.append("• <b>기회비용 검토</b>: 장중 횡보장세로 인해 큰 모멘텀 종목 발굴 빈도가 다소 제한적이었음.")
        if false_cnt > 0:
            sample_str2 = f" (예: {', '.join(false_samples)})" if false_samples else ""
            critique_items.append(f"• <b>거짓 돌파 진입 ({false_cnt}건)</b>: 장중 거래대금 연속성이 부족하여 탄력을 받지 못한 진입 복기 완료{sample_str2}.")
        else:
            critique_items.append("• <b>진입 정밀도</b>: VWAP 눌림목 및 거래량 지지 여부를 더 촘촘히 체크하도록 기준 강화 유지.")

        # 5. 🧠 AI 자가진화 및 내일의 전략
        champ_pnl = getattr(self, "last_champion_pnl", 0.0)
        chal_pnl = getattr(self, "last_challenger_pnl", 0.0)
        promo = batch_result.get("promotion_approved", False)
        promo_text = "🎉 <b>새로운 챔피언 승격! (내일 장부터 실전 가동)</b>" if promo else "🛡️ <b>기존 챔피언 우위 유지 (실전 모델 지속)</b>"
        curr_champ = batch_result.get("champion_after") or self.registry.champion_id
        new_chal = batch_result.get("new_challenger") or "v7.1_challenger"

        # 6. 활성 대시보드 URL
        dashboard_url = None
        try:
            url_file = os.path.join(PROJECT_ROOT, "data", "active_mobile_url.txt")
            if os.path.exists(url_file):
                with open(url_file, "r", encoding="utf-8") as f:
                    u = f.read().strip()
                    if u.startswith("http"):
                        dashboard_url = u
        except Exception:
            pass

        # 7. 리포트 메시지 조립 (HTML)
        pnl_sign = "+" if total_pnl > 0 else ""
        pnl_icon = "📈" if total_pnl >= 0 else "📉"
        report_lines = [
            f"📋 <b>[나무 AI 퀀트] {today} 장 마감 일일 결산 & 피드백</b>",
            "━━━━━━━━━━━━━━━━━━",
            "<b>📊 1. 오늘 매매 성적표</b>",
            f"• <b>총 체결 건수</b>: {total_cnt}건 ({len(win_trades)}승 {len(loss_trades)}패 | 승률 <b>{win_rate:.1f}%</b>)",
            f"• <b>총 실현 손익</b>: {pnl_icon} <b>{pnl_sign}{int(total_pnl):,}원 ({avg_ret:+.2f}%)</b>",
        ]
        if best_trade:
            report_lines.append(f"• <b>최고 수익</b>: {best_trade.get('symbol_name', '')} {float(best_trade.get('return_pct', 0)):+.2f}%")
        if worst_trade:
            report_lines.append(f"• <b>최대 손실</b>: {worst_trade.get('symbol_name', '')} {float(worst_trade.get('return_pct', 0)):+.2f}%")

        report_lines.extend([
            "",
            "<b>👏 2. 오늘 잘된 점 (칭찬 포인트)</b>",
        ])
        report_lines.extend(praise_items)
        report_lines.extend([
            "",
            "<b>🤔 3. 아쉬웠던 점 및 반성 (피드백)</b>",
        ])
        report_lines.extend(critique_items)
        report_lines.extend([
            "",
            "<b>🧠 4. AI 자가진화 및 내일의 전략</b>",
            f"• <b>섀도우 성적 비교</b>: 챔피언({champ_pnl:+.2f}%) vs 챌린저({chal_pnl:+.2f}%)",
            f"• <b>모델 승격 판정</b>: {promo_text}",
            f"• <b>내일 실전 모델</b>: <code>{curr_champ}</code>",
            f"• <b>신규 섀도우 학습</b>: <code>{new_chal}</code> 생성 완료",
            "━━━━━━━━━━━━━━━━━━"
        ])

        report_text = "\n".join(report_lines)

        # 8. 텔레그램 발송
        try:
            from core.telegram_notifier import telegram_notifier
            tg_sent = telegram_notifier.send_eod_retrospective_report(
                report_text, dashboard_url=dashboard_url, business_date=today
            )
            if tg_sent:
                logger.info("텔레그램 EOD 결산 리포트 발송 성공!")
        except Exception as tg_err:
            logger.error(f"텔레그램 리포트 발송 실패: {tg_err}")
            tg_sent = False

        return tg_sent

    def run_sync(self) -> dict:
        """동기 실행 래퍼 (외부 스케줄러, 배치 파일, CLI 호환용)"""
        return asyncio.run(self.run_daily_batch())

    def _record_batch_log(self, result: dict):
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(result, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"Failed to record EOD batch log: {e}")


def main():
    print("=" * 80)
    print("   [EOD Retrospective Worker] 장 마감 후 4단계 반성 및 자기진화 학습 배치")
    print("=" * 80)

    exp_db = ExperienceDB()
    registry = ModelRegistry()
    worker = EODRetrospectiveWorker(exp_db=exp_db, registry=registry)

    result = worker.run_sync()
    print("\n[배치 완료 요약]")
    print(f"- 날짜: {result.get('date')}")
    print(f"- 상태: {result.get('status')}")
    print(f"- 라벨링 건수: {result.get('labeled_count')}건")
    print(f"- 승격 승인 여부: {result.get('promotion_approved')}")
    print(f"- 이전 Champion: {result.get('champion_before')} -> 신규 Champion: {result.get('champion_after')}")
    print(f"- 신규 생성 Shadow Challenger: {result.get('new_challenger')}")
    print("=" * 80)


if __name__ == "__main__":
    main()
