"""Origen causal de un bot LONG: reglas LAB probadas y observaciones PAPER.

No exporta efectivo, cantidades ni precios de ejecución PAPER. Una señal sólo
describe las reglas y la vela que las satisfizo; el capital REAL va aparte.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json

from app.automation.spot_strategy_paper import SpotStrategyPaper
from app.lab.engine import (
    CURRENT_ENGINE_VERSION, Candle, IndicatorCache, INTERVALS, LabError,
    evaluate, validate_candles, validate_strategy,
)
from app.lab.paper_reference import paper_lab_reference
from app.lab.store import content_hash
from app.lab.verification import verify_backtest_run
from app.live_execution.policy import _utc


class SpotBotSourceError(ValueError):
    """El origen LAB/PAPER no es verificable para tomar una decisión REAL."""


@dataclass(frozen=True, slots=True)
class SpotBotObservation:
    session_id: str
    version_id: str
    dataset_id: str
    dataset_hash: str
    source_run_id: str
    engine_version: str
    symbol: str
    timeframe: str
    definition_hash: str
    definition: dict
    candles: tuple[Candle, ...]
    candle_t: int
    observed_at: datetime
    observation_digest: str
    entry_match: bool
    exit_match: bool | None
    entry_evidence: dict
    exit_evidence: dict | None
    market_observation: dict | None = None

    def binding(self) -> dict:
        """Identidad estable; ninguna magnitud financiera ficticia se transfiere."""
        return {name: getattr(self, name) for name in (
            "session_id", "version_id", "dataset_id", "dataset_hash", "source_run_id",
            "engine_version", "symbol", "timeframe", "definition_hash")}


def verified_spot_bot_observation(
    paper: SpotStrategyPaper, session_id: str, *, now: datetime,
    verify_backtest: bool = False,
) -> SpotBotObservation:
    """Lee una instantánea local estable y reproduce entrada y salida LAB.

    La activación solicita además repetir el backtest fijado por PAPER. Cada
    ciclo comprueba versión, dataset, archivo encadenado, estado y última
    observación. No decide si una señal sigue siendo suficientemente reciente
    para enviar: esa vigencia corresponde al planificador y al guardián REAL.
    """
    now = _utc(now, "now")
    try:
        with paper.connect() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None or row["status"] != "ACTIVE":
                raise SpotBotSourceError("La sesión Spot PAPER no está activa")
            session = dict(row)
            definition = validate_strategy(json.loads(row["definition_json"]))
            history = json.loads(row["history_json"])
            state = json.loads(row["state_json"])
            event = db.execute("""SELECT * FROM events WHERE session_id=? AND candle_t=?
                                  AND kind='OBSERVATION'""", (session_id, row["last_t"])).fetchone()
            if (definition["direction"] != "LONG" or row["engine_version"] != CURRENT_ENGINE_VERSION
                    or not paper._valid_session_state(db, row, state, definition)
                    or event is None or not row["source_run_id"]):
                raise SpotBotSourceError("El bot exige LONG, backtest fijado y una observación PAPER íntegra")
            payload_json, observed_ns = event["payload_json"], event["observed_at_ns"]
            payload = json.loads(payload_json)
            if type(observed_ns) is not int or not isinstance(payload, dict):
                raise SpotBotSourceError("La observación PAPER es ilegible")
        version = paper.lab.version(session["version_id"])
        dataset, original = paper.lab.dataset(session["dataset_id"])
        if (definition != validate_strategy(version["definition"])
                or not dataset["verified_public"] or dataset["source"] != "mexc-spot-public"
                or dataset["hash"] != session["dataset_hash"]
                or dataset["symbol"] != session["symbol"] or dataset["timeframe"] != session["timeframe"]
                or not isinstance(history, list) or history[:len(original)] != original
                or not history or history[-1] != payload.get("candle")
                or history[-1]["t"] != session["last_t"]):
            raise SpotBotSourceError("La definición o los datos LAB/PAPER cambiaron")
        reference, _, _ = paper_lab_reference(
            paper.lab, session["dataset_id"], dataset["hash"], session["version_id"], session["source_run_id"])
        saved = paper.lab.run(reference["id"])
        metadata = {"id", "dataset_id", "version_id", "created_at", "engine_version", "dataset_hash",
                    "dataset_source", "strategy_hash", "result_hash"}
        stored_result = {key:value for key,value in saved.items() if key not in metadata}
        if (saved.get("strategy_hash") != content_hash(version["definition"])
                or saved.get("result_hash") != content_hash(stored_result)
                or saved["engine_version"] != session["engine_version"]
                or verify_backtest and not verify_backtest_run(paper.lab, saved)["verified"]):
            raise SpotBotSourceError("El backtest de origen no es verificable")
        bars, gaps = validate_candles(history, session["timeframe"], now=int(now.timestamp()))
        close_at = datetime.fromtimestamp(session["last_t"] + INTERVALS[session["timeframe"]], timezone.utc)
        observed_at = datetime.fromtimestamp(observed_ns // 1_000_000_000, timezone.utc) + timedelta(
            microseconds=observed_ns % 1_000_000_000 // 1000)
        if gaps or not close_at <= observed_at <= now:
            raise SpotBotSourceError("La observación no pertenece a una vela cerrada continua")
        indicators = IndicatorCache(bars)
        entry_match, entry = evaluate(definition["entry"], bars, len(bars) - 1,
                                      indicators, base_timeframe=session["timeframe"])
        exit_match, exit_evidence = (evaluate(definition["exit"], bars, len(bars) - 1,
                                              indicators, base_timeframe=session["timeframe"])
                                    if "exit" in definition else (None, None))
        if (payload.get("entry_match") is not entry_match or payload.get("entry") != entry
                or payload.get("exit_match") is not exit_match or payload.get("exit") != exit_evidence):
            raise SpotBotSourceError("Las reglas LAB no reproducen la observación PAPER")
        return SpotBotObservation(
            session_id=session_id, version_id=session["version_id"], dataset_id=session["dataset_id"],
            dataset_hash=dataset["hash"], source_run_id=saved["id"], engine_version=session["engine_version"],
            symbol=session["symbol"], timeframe=session["timeframe"], definition_hash=content_hash(definition),
            definition=definition, candles=tuple(bars), candle_t=session["last_t"],
            observed_at=observed_at, observation_digest=hashlib.sha256(payload_json.encode()).hexdigest(),
            entry_match=entry_match, exit_match=exit_match, entry_evidence=entry, exit_evidence=exit_evidence)
    except SpotBotSourceError:
        raise
    except (LabError, ValueError, TypeError, KeyError, IndexError, OSError) as exc:
        raise SpotBotSourceError("El origen del bot LAB/PAPER no es verificable") from exc
