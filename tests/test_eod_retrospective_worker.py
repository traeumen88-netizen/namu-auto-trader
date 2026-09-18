"""Unit and Integration Tests for EOD Retrospective Worker Pipeline
(tests/test_eod_retrospective_worker.py)
"""

import os
import shutil
import pytest
import tempfile
import asyncio
from datetime import datetime

from database.persistence import ExperienceDB, ExperienceRecord
from ml.model_registry import ModelRegistry
from ml.trainer import ChallengerTrainer
from ml.evaluator import ShadowEvaluator
from ml.eod_worker import EODRetrospectiveWorker


@pytest.fixture
def temp_env():
    tmp_dir = tempfile.mkdtemp()
    exp_db_path = os.path.join(tmp_dir, "test_exp_memory.db")
    reg_path = os.path.join(tmp_dir, "test_registry.json")
    audit_path = os.path.join(tmp_dir, "test_audit.jsonl")
    models_dir = os.path.join(tmp_dir, "models")

    exp_db = ExperienceDB(db_path=exp_db_path)
    registry = ModelRegistry(registry_file=reg_path, audit_log_file=audit_path)

    yield {
        "tmp_dir": tmp_dir,
        "exp_db": exp_db,
        "registry": registry,
        "models_dir": models_dir
    }

    shutil.rmtree(tmp_dir, ignore_errors=True)


def test_experience_db_lifecycle(temp_env):
    db: ExperienceDB = temp_env["exp_db"]

    # 1. Update labels with synthetic records
    records = [
        ExperienceRecord(
            event_id="TEST_EVT_1",
            iem_cd="005930",
            decision="NO_TRADE",
            decision_time="2026-09-10T10:00:00",
            name="삼성전자",
            entry_price=70000.0,
            raw_features={"price": 70000.0, "score": 85.0},
            label="MISSED_WINNER",
            future_return=0.06
        ),
        ExperienceRecord(
            event_id="TEST_EVT_2",
            iem_cd="000660",
            decision="NO_TRADE",
            decision_time="2026-09-10T10:05:00",
            name="SK하이닉스",
            entry_price=160000.0,
            raw_features={"price": 160000.0, "score": 45.0},
            label="CORRECT_REJECT",
            future_return=-0.02
        ),
        ExperienceRecord(
            event_id="TEST_EVT_3",
            iem_cd="035420",
            decision="BUY",
            decision_time="2026-09-10T10:10:00",
            name="NAVER",
            entry_price=190000.0,
            raw_features={"price": 190000.0, "score": 92.0},
            label="WINNING_TRADE",
            future_return=0.04
        ),
        ExperienceRecord(
            event_id="TEST_EVT_4",
            iem_cd="035720",
            decision="BUY",
            decision_time="2026-09-10T10:15:00",
            name="카카오",
            entry_price=45000.0,
            raw_features={"price": 45000.0, "score": 65.0},
            label="FALSE_SIGNAL",
            future_return=-0.03
        ),
    ]

    db.update_labels(records)

    # 2. Retrieve labeled data
    all_data = db.get_all_labeled_data()
    assert len(all_data) >= 4

    labels = {d["label"] for d in all_data}
    assert "MISSED_WINNER" in labels
    assert "CORRECT_REJECT" in labels
    assert "WINNING_TRADE" in labels
    assert "FALSE_SIGNAL" in labels

    # Check targets (wins should be target=1, losses target=0)
    for d in all_data:
        if d["label"] in ("MISSED_WINNER", "WINNING_TRADE"):
            assert d["target"] == 1
        elif d["label"] in ("CORRECT_REJECT", "FALSE_SIGNAL"):
            assert d["target"] == 0


def test_shadow_evaluator_and_promotion_condition(temp_env):
    db: ExperienceDB = temp_env["exp_db"]
    evaluator = ShadowEvaluator(db)

    # Case A: Challenger outperformed Champion and expectancy > 0.15%
    evaluator.set_mock_pnls(champion_pnl=0.20, challenger_pnl=0.45)
    champ = evaluator.get_champion_realized_pnl()
    chall = evaluator.get_challenger_simulated_pnl()
    assert chall > champ and chall > 0.15

    # Case B: Challenger failed expectancy threshold <= 0.15%
    evaluator.set_mock_pnls(champion_pnl=0.05, challenger_pnl=0.12)
    champ = evaluator.get_champion_realized_pnl()
    chall = evaluator.get_challenger_simulated_pnl()
    assert not (chall > champ and chall > 0.15)

    # Case C: Challenger underperformed Champion
    evaluator.set_mock_pnls(champion_pnl=0.80, challenger_pnl=0.50)
    champ = evaluator.get_champion_realized_pnl()
    chall = evaluator.get_challenger_simulated_pnl()
    assert not (chall > champ and chall > 0.15)


def test_model_registry_shadow_and_promotion(temp_env):
    registry: ModelRegistry = temp_env["registry"]
    initial_champ = registry.get_champion().model_id

    # 1. Register new shadow model
    challenger_ver = "CHALLENGER_20260910_TEST"
    registry.register_as_shadow(challenger_ver)

    assert registry.challenger_id == challenger_ver
    chall_meta = registry.get_challenger()
    assert chall_meta is not None
    assert chall_meta.model_id == challenger_ver
    assert chall_meta.role == "CHALLENGER"

    # 2. Promote challenger to champion
    success = registry.promote_challenger_to_champion(challenger_ver)
    assert success is True
    assert registry.champion_id == challenger_ver
    assert registry.previous_champion_id == initial_champ
    assert registry.challenger_id is None

    promoted_meta = registry.get_champion()
    assert promoted_meta.role == "CHAMPION"
    assert promoted_meta.promotion_status == "PROMOTED"


def test_challenger_trainer_purged_cv(temp_env):
    trainer = ChallengerTrainer(model_dir=temp_env["models_dir"])

    synthetic_dataset = [
        {
            "features": {"price": 50000 + i * 200, "score": 60 + i, "vwap": 50000},
            "target": 1 if i % 3 == 0 else 0,
            "timestamp": f"2026-09-10T10:{i:02d}:00"
        }
        for i in range(30)
    ]

    version = asyncio.run(trainer.train_with_purged_cv(synthetic_dataset))
    assert version.startswith("CHALLENGER_")
    artifact_path = os.path.join(temp_env["models_dir"], f"{version}.pkl")
    assert os.path.exists(artifact_path)


def test_eod_retrospective_worker_full_pipeline_promoted(temp_env):
    db: ExperienceDB = temp_env["exp_db"]
    registry: ModelRegistry = temp_env["registry"]

    # Populate some experience
    records = [
        ExperienceRecord(
            event_id=f"EVT_EOD_{i}",
            iem_cd="005930",
            decision="NO_TRADE" if i % 2 == 0 else "BUY",
            decision_time=f"2026-09-10T11:{i:02d}:00",
            raw_features={"price": 70000 + i*10, "score": 75}
        )
        for i in range(10)
    ]
    db.update_labels(records)

    worker = EODRetrospectiveWorker(exp_db=db, registry=registry)
    worker.trainer.model_dir = temp_env["models_dir"]

    # Force promotion conditions: challenger (0.50%) > champion (0.10%) and > 0.15%
    worker.shadow_eval.set_mock_pnls(champion_pnl=0.10, challenger_pnl=0.50)

    # Register an active challenger first to be tested
    active_challenger = "CHALLENGER_YESTERDAY"
    registry.register_as_shadow(active_challenger)

    # Run batch
    result = asyncio.run(worker.run_daily_batch())

    assert result["status"] == "SUCCESS"
    assert result["promotion_approved"] is True
    assert result["champion_after"] == active_challenger
    assert result["new_challenger"].startswith("CHALLENGER_")

    # The new challenger for tomorrow should now be registered as shadow
    assert registry.challenger_id == result["new_challenger"]


def test_eod_retrospective_worker_full_pipeline_rejected(temp_env):
    db: ExperienceDB = temp_env["exp_db"]
    registry: ModelRegistry = temp_env["registry"]

    worker = EODRetrospectiveWorker(exp_db=db, registry=registry)
    worker.trainer.model_dir = temp_env["models_dir"]

    initial_champion = registry.champion_id

    # Force reject condition: challenger (0.10%) <= 0.15% threshold
    worker.shadow_eval.set_mock_pnls(champion_pnl=0.20, challenger_pnl=0.10)

    result = asyncio.run(worker.run_daily_batch())

    assert result["status"] == "SUCCESS"
    assert result["promotion_approved"] is False
    assert result["champion_after"] == initial_champion
    assert result["new_challenger"].startswith("CHALLENGER_")
    assert registry.champion_id == initial_champion
