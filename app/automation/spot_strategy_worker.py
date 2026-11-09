"""Observador local de estrategias spot PAPER; no utiliza credenciales."""

from app.automation.spot_strategy_paper import SpotStrategyPaper, SpotStrategyUnavailable, SpotPaperLocked
from app.core.local_observer import LocalObserver


class SpotStrategyWorker(LocalObserver):
    def __init__(self, paper: SpotStrategyPaper, *, retry_seconds: float = 5.0,
                 idle_seconds: float = 60.0):
        self.paper = paper
        super().__init__(name="orbis-paper-spot-strategies", items=paper.scheduled_sessions,
                         due_at=lambda row: row["due_at"],
                         run_item=paper.observe, clock_ns=paper.clock_ns,
                         retry_exceptions=(SpotStrategyUnavailable, SpotPaperLocked),
                         execution="SPOT_PAPER_APPROXIMATION_NO_REAL_ORDERS",
                         retry_seconds=retry_seconds, idle_seconds=idle_seconds)
