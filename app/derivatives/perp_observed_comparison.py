"""Compare one replayed PAPER snapshot with its original out-of-sample reserves."""

from decimal import Decimal

from app.derivatives.perp_observed_archive import ObservedArchive
from app.derivatives.perp_observed_types import ObservedError
from app.derivatives.perp_observed_view import ObservedReplayView
from app.lab.engine import INTERVALS
from app.lab.perp_book_types import exact
from app.lab.perp_validation_store import PerpValidations
from app.lab.store import LabStore
from app.research.perp_reference import reference_from_verified_validation


@exact
def _metrics(capital: Decimal, *, equity: str, profit: str, fees: str, funding: str, trades: int) -> dict:
    return {
        "initial_capital": format(capital, "f"),
        "equity": equity,
        "net_profit": profit,
        "return_pct": format(Decimal(profit) / capital * 100, "f"),
        "fees_paid": fees,
        "funding_paid": funding,
        "closed_trades": trades,
    }


def compare_observed_reserves(lab: LabStore, archive: ObservedArchive, session_id: str, *,
                              snapshot_sequence: int, snapshot_hash: str) -> dict:
    paper_stamp = archive._stamp()
    validations = PerpValidations(lab)
    lab_stamp = validations.experiments._stamp()
    observed = archive.read(session_id, view=ObservedReplayView(candle_limit=1, trade_limit=1, marker_limit=1),
                            snapshot_sequence=snapshot_sequence, snapshot_hash=snapshot_hash)
    archived_reference = observed["origin"]["reference"]
    validation = validations._verified(archived_reference["artifact_id"], expected_session=lab_stamp)
    reference = reference_from_verified_validation(validation)
    if reference.model_dump(mode="json", by_alias=True) != archived_reference:
        raise ObservedError("La reserva LAB no coincide con la referencia original de PAPER")

    session, view = observed["session"], observed["view"]
    checkpoint = session["checkpoint"]
    paper_metrics = view["metrics"]
    capital = Decimal(str(validation["definition"]["initial_capital"]))
    if capital != Decimal(paper_metrics["initial_capital"]) or capital <= 0:
        raise ObservedError("El capital inicial no coincide con la reserva LAB")
    paper = {
        "session_id": session["id"], "sequence": session["sequence"], "head_hash": session["head_hash"],
        "is_latest": observed["snapshot_is_latest"], "status": session["status"],
        "from_ns": checkpoint["started_at_ns"], "to_ns": checkpoint["last_received_ns"],
        "position_open": checkpoint["opened"] is not None,
        "metrics": _metrics(capital, equity=paper_metrics["equity"], profit=paper_metrics["net_profit"],
                            fees=paper_metrics["fees_paid"], funding=paper_metrics["funding_paid"],
                            trades=view["trade_total"]),
    }
    step = INTERVALS[reference.timeframe]
    reserves = []
    for window in validation["payload"]["result"]["windows"]:
        if window["role"] != "RESERVE":
            continue
        values = window["metrics"]
        if window["status"] != "COMPLETE" or values["open_position"]:
            raise ObservedError("La comparación requiere las reservas completas de la sesión original")
        reserves.append({
            "from_ns": str(window["from_t"] * 10**9), "to_ns": str((window["to_t"] + step) * 10**9),
            "bars": window["bars"], "result_hash": window["result_hash"],
            "metrics": _metrics(capital, equity=values["final_equity"], profit=values["net_equity_change"],
                                fees=values["fees_paid"], funding=values["funding_paid"], trades=values["trades"]),
        })
    validations.experiments._stamp(lab_stamp)
    archive._stamp(paper_stamp)
    return {
        "schema": "orbis.paper.perp.reserve-comparison.v1",
        "execution_authority": "NONE",
        "symbol": reference.symbol, "timeframe": reference.timeframe,
        "strategy_version_id": reference.profile.strategy_version_id,
        "validation_id": reference.artifact_id, "validation_hash": reference.artifact_hash,
        "paper": paper, "reserves": reserves,
    }
