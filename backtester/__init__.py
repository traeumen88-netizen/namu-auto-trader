"""Realistic Backtester Package for Korean Equity Auto-Trader (NH Namu)
Provides 100% LIVE Parity Shared Decision Core, Execution Simulator,
PIT/Leakage Verification, Walk-Forward Analysis, and Capital Scenario Evaluation.
"""

from backtester.execution_simulator import (
    RealisticExecutionSimulator, SimulatedOrder, ExecutionResult, OrderFillStatus
)
from backtester.shared_decision_core import SharedDecisionCore, PipelineDecision
from backtester.leakage_verifier import LookaheadLeakageVerifier, LeakageError
from backtester.realistic_engine import RealisticBacktestEngine
from backtester.walk_forward import WalkForwardEngine

__all__ = [
    "RealisticExecutionSimulator",
    "SimulatedOrder",
    "ExecutionResult",
    "OrderFillStatus",
    "SharedDecisionCore",
    "PipelineDecision",
    "LookaheadLeakageVerifier",
    "LeakageError",
    "RealisticBacktestEngine",
    "WalkForwardEngine"
]
