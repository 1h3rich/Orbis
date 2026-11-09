"""Replayed LAB reserves alongside one archived Spot PAPER snapshot."""

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.automation.spot_verified_snapshot import spot_snapshot_stamp, verified_spot_snapshot
from app.lab.engine import INTERVALS, LabError
from app.lab.store import content_hash
from app.research.references import ResearchReferenceError, strategy_reference

if TYPE_CHECKING:
    from app.automation.spot_strategy_paper import SpotStrategyPaper


def compare_spot_reserve(paper: "SpotStrategyPaper", session_id: str, validation_id: str) -> dict:
    stamp = spot_snapshot_stamp(paper)
    try:
        snapshot = verified_spot_snapshot(paper, session_id)
        session, definition = snapshot.session, snapshot.definition
        reference = strategy_reference(paper.lab, validation_id)
        seed, seed_rows = paper.lab.dataset(session["dataset_id"])
        dataset, dataset_rows = paper.lab.dataset(str(reference.dataset_id))
        step = INTERVALS[session["timeframe"]]
        if (str(reference.version_id) != session["version_id"]
                or reference.definition_hash != content_hash(definition)
                or reference.direction != "LONG"
                or reference.symbol != session["symbol"] or reference.timeframe != session["timeframe"]
                or reference.simulation_engine != session["engine_version"]
                or not reference.public_data or not seed["verified_public"]
                or reference.source != "mexc-spot-public" or seed["source"] != reference.source
                or seed["hash"] != session["dataset_hash"] or seed["gaps"]
                or dataset["hash"] != reference.dataset_hash or not dataset["verified_public"]
                or dataset["source"] != reference.source or dataset["gaps"]
                or reference.dataset_to_t > seed["end_t"]
                or (reference.dataset_to_t + step) * 10**9 > session["started_at_ns"]):
            raise LabError("La reserva requiere la misma versión, mercado y motor, con velas anteriores a PAPER")
        past = {candle["t"]: candle for candle in dataset_rows}
        overlap = [candle for candle in seed_rows if candle["t"] in past]
        if any(candle != past[candle["t"]] for candle in overlap):
            raise LabError("Las velas comunes de la reserva y PAPER difieren")
        validation = paper.lab.validation(validation_id)
        if content_hash(validation) != reference.artifact_digest:
            raise LabError("La validación cambió durante la comparación")
        windows = validation["windows"] if reference.method == "ROLLING_FIXED_VERSION" else [validation]
        reserves = []
        for window, span in zip(windows, reference.windows, strict=True):
            metrics = window["out_of_sample"]["metrics"]
            reserves.append({"from_t": span.from_t, "end_t": span.to_t + step, "bars": span.bars,
                             "metrics": {"initial_capital": definition["initial_capital"],
                                         "equity": metrics["final_equity"], "realized_pnl": metrics["net_profit"],
                                         "fees_paid": metrics["total_fees"], "return_pct": metrics["return_pct"],
                                         "closed_trades": metrics["trades"]}})
        saved_delta = reference.created_at - datetime(1970, 1, 1, tzinfo=timezone.utc)
        saved_ns = (saved_delta.days * 86400 + saved_delta.seconds) * 10**9 + saved_delta.microseconds * 1000
        if spot_snapshot_stamp(paper) != stamp:
            raise LabError("LAB o Spot PAPER cambió de sesión durante la comparación")
        report = {"schema": "orbis.paper.spot.reserve-comparison.v1", "execution_authority": "NONE",
                  "comparison_basis": "DISTINCT_WINDOWS_AND_EXECUTION", "reference": reference.exported(),
                  "saved_before_paper": saved_ns <= session["started_at_ns"], "overlap_bars": len(overlap),
                  "paper": {**snapshot.ledger, "session_id": session_id, "status": session["status"],
                            "started_at_ns": str(session["started_at_ns"]), "dataset_hash": session["dataset_hash"],
                            "observed_candles": snapshot.observed_candles, "event_count": len(snapshot.events),
                            "snapshot_hash": snapshot.digest},
                  "reserves": reserves}
        return {**report, "hash": content_hash(report)}
    except ResearchReferenceError as exc:
        raise LabError(str(exc)) from None
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        if isinstance(exc, LabError):
            raise
        raise LabError("La evidencia archivada de la comparación Spot no es verificable") from None
