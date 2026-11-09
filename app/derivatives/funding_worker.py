"""Asentamiento local automático de financiación pública para posiciones AUTO PAPER.

No consulta pagos privados ni envía órdenes. Los fallos conservan la cobertura
pendiente: nunca se contabiliza cero por falta de histórico verificable.
"""

from __future__ import annotations

import time
from uuid import uuid4

from app.core.local_observer import LocalObserver
from app.derivatives.funding import FundingHistoryError, FundingRecord, HOUR_NS
from app.derivatives.paper_math import PaperPerpError
from app.derivatives.paper_store import PaperPerpStore
from app.marketdata.adapters.grvt import GrvtAdapter, GrvtHistoryError


# El cierre manual acepta cotizaciones de hasta 15 s. Se asienta el histórico
# de una posición abierta sólo hasta 15 s antes de cualquier posible
# cierre con cotización aún válida; el tramo final se concilia al cerrar.
QUOTE_FRESHNESS_NS = 15_000_000_000


def _ceil_second(nanoseconds: int) -> int:
    return (nanoseconds + 999_999_999) // 1_000_000_000


class AutoFunding:
    """Usa exactamente el motor de financiación y el libro PAPER manual."""

    def __init__(self, paper: PaperPerpStore, *, adapter_factory=GrvtAdapter,
                 clock_ns=time.time_ns):
        self.paper = paper
        self.adapter_factory = adapter_factory
        self.clock_ns = clock_ns

    def candidates(self) -> list[dict]:
        return self.paper.auto_funding_candidates()

    @staticmethod
    def due_at(row: dict) -> int:
        if row["closed_at_ns"] is not None:
            return 0  # El cierre requiere conciliación final cuanto antes.
        checked = row["funding_checked_through_ns"]
        return (_ceil_second(checked + HOUR_NS + QUOTE_FRESHNESS_NS) if checked is not None
                else _ceil_second(row["opened_at_ns"] + QUOTE_FRESHNESS_NS + 1))

    def poll(self, position_id: str) -> dict:
        candidate = next((row for row in self.candidates() if row["id"] == position_id), None)
        if candidate is None:
            return {"poll_result": "COMPLETE_OR_NOT_AUTO"}
        now_ns = self.clock_ns()
        if self.due_at(candidate) > now_ns // 1_000_000_000:
            return {"poll_result": "WAIT"}
        through_ns = (candidate["closed_at_ns"] if candidate["closed_at_ns"] is not None
                      else now_ns - QUOTE_FRESHNESS_NS)
        if through_ns <= candidate["opened_at_ns"]:
            return {"poll_result": "WAIT"}
        raw = self.adapter_factory().get_funding_history(candidate["symbol"],
                                                          candidate["opened_at_ns"], through_ns)
        if not isinstance(raw, list) or not raw:
            raise FundingHistoryError("Histórico público de financiación no disponible")
        records = [FundingRecord.from_payload(row, candidate["instrument"]) for row in raw]
        request_id = str(uuid4())
        digest = self.paper.digest({"action": "AUTO_FUNDING_PAPER", "position_id": position_id,
                                    "account_id": candidate["account_id"], "through_ns": through_ns,
                                    "request_id": request_id})
        result = self.paper.settle_funding(candidate["account_id"], position_id,
                                           request_id, digest, records, through_ns)
        return {"poll_result": "BOOKED" if result["new_settlements"] else "VERIFIED_NO_NEW",
                "new_settlements": result["new_settlements"],
                "new_payment": result["new_payment"], "through_ns": through_ns}


class AutoFundingWorker(LocalObserver):
    def __init__(self, funding: AutoFunding, *, retry_seconds: float = 300.0,
                 idle_seconds: float = 60.0):
        self.funding = funding
        super().__init__(name="orbis-paper-grvt-funding",
                         items=funding.candidates, due_at=funding.due_at,
                         run_item=funding.poll, clock_ns=funding.clock_ns,
                         retry_exceptions=(FundingHistoryError, GrvtHistoryError, PaperPerpError),
                         execution="AUTO_PAPER_PUBLIC_FUNDING_NO_REAL_PAYMENTS",
                         retry_seconds=retry_seconds, idle_seconds=idle_seconds)
