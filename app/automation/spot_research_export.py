"""Bounded, local research export of verified Spot PAPER observations."""

from hashlib import sha256
import json
from typing import TYPE_CHECKING

from app.automation.paper_dca import PaperDca, PaperDcaUnavailable
from app.automation.spot_verified_snapshot import spot_snapshot_stamp, verified_spot_snapshot
from app.lab.engine import LabError
from app.lab.store import content_hash, encoded

if TYPE_CHECKING:
    from app.automation.spot_strategy_paper import SpotStrategyPaper


SCHEMA = "orbis.paper.spot.research.v1"
MAX_BYTES = 12 * 1024 * 1024
MAX_EVENTS = 10_000
CANDLE_FIELDS = ("t", "o", "h", "l", "c", "v")
ACTION_DECIMALS = ("units", "price", "fee", "cash_change", "pnl")
REASONS = {
    "OBSERVATION": {"ENTRY_RULE", "STOP_CLOSE", "TARGET_CLOSE", "TRAILING_STOP_CLOSE", "EXIT_RULE", "TIME_LIMIT"},
    "INTRABAR_EXIT": {"STOP_OBSERVED_BID", "TARGET_OBSERVED_BID", "TRAILING_STOP_OBSERVED_BID"},
    "MANUAL_CLOSE": {"MANUAL_CLOSE"},
}


def _observed_rows(events: list[dict], symbol: str) -> list[dict]:
    rows = []
    episode = None
    episode_count = 0
    for index, event in enumerate(events, start=1):
        payload = json.loads(event["payload_json"])
        kind, stamp = event["kind"], event["observed_at_ns"]
        row = {"kind": kind, "ordinal": index, "observed_at_ns": str(stamp),
               "input": None, "outcome": None}
        if kind == "RISK_COVERAGE_GAP":
            start, end = payload["from_ns"], payload["to_ns"]
            if (type(start) is not int or type(end) is not int or not 0 < start < end == stamp
                    or payload["unobserved_seconds"] != (end - start) // 10**9
                    or payload["position_open"] is not True or episode is None or payload.get("action") is not None):
                raise LabError("Hueco de riesgo Spot PAPER no verificable")
            row["coverage"] = {"from_ns": str(start), "to_ns": str(end), "episode": episode}
            rows.append(row)
            continue

        action = payload.get("action")
        _, _, quote = PaperDca._quote(payload["quote"], symbol, stamp)
        row["input"] = {"quote": {**quote, "event_time_ns": str(quote["event_time_ns"])},
                        "position_open_before": episode is not None}
        decision = None
        if kind == "OBSERVATION":
            entry, exit_match = payload["entry_match"], payload["exit_match"]
            row["input"]["candle"] = {key: payload["candle"][key] for key in CANDLE_FIELDS}
            row["input"]["signal"] = {"entry": entry, "exit": exit_match}
            if entry:
                decision = ("EXECUTED_PAPER" if action and action["side"] == "BUY" else
                            "SKIPPED_POSITION_OPEN" if episode is not None else "SKIPPED_NO_CAPITAL")
            if payload.get("entry_status", decision) != decision:
                raise LabError("Decisión de entrada Spot PAPER no verificable")
        elif kind not in REASONS or action is None:
            raise LabError("Evento Spot PAPER no exportable")

        execution = None
        if action is not None:
            side, reason = action["side"], action["reason"]
            if reason not in REASONS[kind] or (side == "BUY") != (reason == "ENTRY_RULE"):
                raise LabError("Motivo de ejecución Spot PAPER no verificable")
            if side == "BUY":
                if episode is not None or decision != "EXECUTED_PAPER":
                    raise LabError("Entrada Spot PAPER sin señal verificable")
                episode_count += 1
                episode = episode_count
            elif episode is None:
                raise LabError("Salida Spot PAPER sin entrada archivada")
            financial = {key: action[key] for key in ACTION_DECIMALS}
            if any(type(value) is not str for key, value in financial.items()
                   if not (key == "pnl" and side == "BUY" and value is None)):
                raise LabError("Importes Spot PAPER con formato no verificable")
            execution = {"side": side, "reason": reason, "episode": episode, **financial}
            if side == "SELL":
                episode = None
        row["outcome"] = {"entry_decision": decision, "execution": execution}
        rows.append(row)
    return rows


def spot_research_export(paper: "SpotStrategyPaper", session_id: str, revision: str | None = None) -> dict:
    stamp = spot_snapshot_stamp(paper)
    try:
        snapshot = verified_spot_snapshot(paper, session_id)
        session = snapshot.session
        if not snapshot.events:
            raise LabError("Todavía no hay observaciones Spot PAPER para exportar")
        if len(snapshot.events) > MAX_EVENTS:
            raise LabError("El diario Spot PAPER supera el límite de exportación")
        seed, candles = paper.lab.dataset(session["dataset_id"])
        if not seed["verified_public"] or seed["source"] != "mexc-spot-public" or seed["hash"] != session["dataset_hash"]:
            raise LabError("Histórico inicial MEXC Spot no verificable")
        header = {"kind": "HEADER", "schema": SCHEMA, "environment": "PAPER", "execution_authority": "NONE",
                  "session_id": session_id, "symbol": session["symbol"], "timeframe": session["timeframe"],
                  "source": seed["source"], "engine_version": session["engine_version"],
                  "strategy_version_id": session["version_id"], "definition": snapshot.definition,
                  "definition_sha256": content_hash(snapshot.definition),
                  "seed_dataset_id": seed["id"], "seed_sha256": seed["hash"],
                  "seed_candles": [{key: candle[key] for key in CANDLE_FIELDS} for candle in candles],
                  "started_at_ns": str(session["started_at_ns"]), "through_ns": snapshot.ledger["through_ns"],
                  "status_at_export": session["status"]}
        rows = [header, *_observed_rows(snapshot.events, session["symbol"])]
        current = content_hash(rows)
        report = {"schema": SCHEMA, "revision": current, "record_count": len(rows),
                  "event_count": len(snapshot.events)}
        if revision is not None:
            if revision != current:
                raise LabError("El diario Spot PAPER cambió; vuelve a revisar la exportación")
            body = "\n".join(encoded({**row, "row_sha256": content_hash(row)}) for row in rows) + "\n"
            raw = body.encode("utf-8")
            if len(raw) > MAX_BYTES:
                raise LabError("El diario Spot PAPER supera 12 MiB")
            report.update(jsonl=body, sha256=sha256(raw).hexdigest())
        if spot_snapshot_stamp(paper) != stamp:
            raise LabError("LAB o Spot PAPER cambió de sesión durante la exportación")
        return report
    except PaperDcaUnavailable as exc:
        raise LabError(str(exc)) from None
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        if isinstance(exc, LabError):
            raise
        raise LabError("El diario de investigación Spot PAPER no es verificable") from None
