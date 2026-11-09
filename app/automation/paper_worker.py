"""Observador local de DCA PAPER, sin credenciales ni órdenes reales."""

from __future__ import annotations

from app.automation.paper_dca import PaperDca, PaperDcaUnavailable
from app.core.local_observer import LocalObserver


class PaperDcaWorker(LocalObserver):
    def __init__(self, paper: PaperDca, *, retry_seconds: float = 5.0,
                 idle_seconds: float = 60.0):
        self.paper = paper
        super().__init__(name="orbis-paper-dca",
                         items=lambda: [row for row in paper.sessions() if row["status"] == "ACTIVE"],
                         due_at=lambda row: row["next_exec_t"], run_item=paper.poll,
                         clock_ns=paper.clock_ns, retry_exceptions=(PaperDcaUnavailable,),
                         execution="NO_EXCHANGE_ORDERS", retry_seconds=retry_seconds,
                         idle_seconds=idle_seconds)
