"""Prueba causal común de señales PAPER. No autentica ni autoriza un exchange."""
from contextlib import contextmanager
from dataclasses import dataclass
import json
from typing import Callable
from app.lab.engine import supports_engine_version,validate_strategy
from app.lab.paper_signal_replay import verify_paper_signals
from app.marketdata.adapters.grvt import GrvtAdapter
from app.live_execution.grvt_testnet_preflight import client_id_for_signal

SIGNAL_MAX_AGE_NS=20_000_000_000

class GrvtPaperSignalError(ValueError):
    """No se puede reproducir una señal PAPER reciente."""

@dataclass(frozen=True,slots=True)
class GrvtPaperSignalBinding:
    session_id: str
    action_request_id: str
    action: str
    version_id: str
    dataset_id: str
    dataset_hash: str
    client_order_id: str

@contextmanager
def reviewed_paper_signal(paper, lab, *, instrument: str,
                            session_id: str, action_request_id: str, side: str, reduce_only: bool,
                            now_ns: int, clock_ns: Callable[[], int]):
    """Reproduce la fuente bajo transacción; no recibe cliente, firma ni envío.

    Una vista previa usa la misma evidencia que IOC/OTOCO, sin inventar una
    orden firmada ni sustituir sus verificadores por callbacks vacíos.
    """
    if type(now_ns) is not int or now_ns <= 0:
        raise GrvtPaperSignalError("Reloj de señal inválido")
    client_id = client_id_for_signal(session_id, action_request_id)
    with paper.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        session = db.execute("SELECT * FROM paper_signal_sessions WHERE id=?", (session_id,)).fetchone()
        action = db.execute("SELECT * FROM paper_signal_actions WHERE request_id=? AND session_id=?",
                            (action_request_id, session_id)).fetchone()
        if session is None or action is None or session["execution_mode"] != "AUTO_PAPER":
            raise GrvtPaperSignalError("Acción GRVT PAPER automática no encontrada")
        if action["status"] != "EXECUTED" or action["action"] not in ("OPEN", "CLOSE"):
            raise GrvtPaperSignalError("La acción PAPER no está ejecutada")
        if not 0 <= now_ns - action["recorded_at_ns"] <= SIGNAL_MAX_AGE_NS:
            raise GrvtPaperSignalError("La acción PAPER ya no es reciente")
        latest = db.execute("""SELECT request_id FROM paper_signal_actions WHERE session_id=?
                               ORDER BY recorded_at_ns DESC,candle_t DESC LIMIT 1""",
                            (session_id,)).fetchone()
        if latest is None or latest["request_id"] != action_request_id:
            raise GrvtPaperSignalError("Hay una acción PAPER posterior")
        expected_digest = paper.digest({"session_id": session_id, "candle_t": action["candle_t"],
                                        "action": action["action"], "position_id": (
                                            None if action["action"] == "OPEN" else action["position_id"]),
                                        "request_id": action_request_id})
        if action["digest"] != expected_digest:
            raise GrvtPaperSignalError("La acción PAPER perdió integridad")
        booked = db.execute("SELECT digest FROM paper_perp_requests WHERE request_id=?",
                            (action_request_id,)).fetchone()
        event = db.execute("""SELECT kind FROM paper_perp_events
                              WHERE request_id=? AND account_id=? AND position_id=?""",
                           (action_request_id, session["account_id"], action["position_id"])).fetchone()
        if booked is None or booked["digest"] != action["digest"] or event is None or event["kind"] != action["action"]:
            raise GrvtPaperSignalError("Asiento PAPER de la acción no verificable")
        account = paper._account(db, session["account_id"])
        if action["action"] == "OPEN" and (session["status"] != "ACTIVE" or account["stopped_at"] is not None):
            raise GrvtPaperSignalError("La entrada PAPER está detenida")
        if session["status"] not in ("ACTIVE", "STOPPED", "GAP"):
            raise GrvtPaperSignalError("Estado PAPER no autorizable")
        position = db.execute("""SELECT * FROM paper_perp_positions
                                 WHERE id=? AND account_id=? AND origin_session_id=?""",
                              (action["position_id"], session["account_id"], session_id)).fetchone()
        if position is None or position["instrument"] != instrument:
            raise GrvtPaperSignalError("Posición PAPER de origen no verificable")
        if action["action"] == "OPEN" and (position["closed_at_ns"] is not None
                                            or session["position_id"] != position["id"]):
            raise GrvtPaperSignalError("La posición PAPER ya no está abierta")
        if action["action"] == "CLOSE" and position["closed_at_ns"] is None:
            raise GrvtPaperSignalError("El cierre PAPER no está asentado")
        symbol = session["symbol"]
        observed_instrument = GrvtAdapter()._instrument(symbol)
        if observed_instrument != instrument:
            raise GrvtPaperSignalError("Mercado de ejecución distinto del PAPER")
        definition = validate_strategy(json.loads(session["definition_json"]))
        if not supports_engine_version(session["engine_version"], definition):
            raise GrvtPaperSignalError("Motor de estrategia PAPER no reproducible")
        version = lab.version(session["version_id"])
        dataset, _ = lab.dataset(session["dataset_id"])
        if (validate_strategy(version["definition"]) != definition
                or dataset["hash"] != session["dataset_hash"]
                or dataset["symbol"] != symbol or dataset["timeframe"] != session["timeframe"]
                or not dataset["verified_public"] or dataset["source"] != "grvt-perp-trade-public"):
            raise GrvtPaperSignalError("Procedencia LAB/PAPER no reproducible")
        observations = []
        for row in db.execute("""SELECT candle_t,entry_match,exit_match,evidence_json
                                 FROM paper_signal_observations
                                 WHERE session_id=? ORDER BY candle_t""", (session_id,)):
            try:
                evidence = json.loads(row["evidence_json"])
                observations.append({"candle_t": row["candle_t"],
                                     "entry_match": bool(row["entry_match"]),
                                     "exit_match": None if row["exit_match"] is None else bool(row["exit_match"]),
                                     "candle": evidence["candle"],
                                     "entry": evidence["entry"], "exit": evidence["exit"]})
            except (KeyError, TypeError, ValueError):
                raise GrvtPaperSignalError("Evidencia PAPER no reproducible") from None
        try:
            history = json.loads(session["candles_json"])
        except (TypeError, ValueError):
            raise GrvtPaperSignalError("Histórico PAPER no reproducible") from None
        replay = verify_paper_signals(lab, dict(session), definition,
                                      history, observations)
        if not replay["verified"]:
            raise GrvtPaperSignalError("Señales PAPER no reproducibles antes de GRVT de ejecución")
        if action["action"] == "OPEN":
            observation = db.execute("""SELECT entry_match FROM paper_signal_observations
                                        WHERE session_id=? AND candle_t=?""",
                                     (session_id, action["candle_t"])).fetchone()
            if observation is None or observation["entry_match"] != 1:
                raise GrvtPaperSignalError("Entrada sin señal observada")
        opening_buy = definition["direction"] == "LONG"
        expected_buy = opening_buy if action["action"] == "OPEN" else not opening_buy
        if (side not in ("BUY", "SELL") or type(reduce_only) is not bool
                or (side == "BUY") is not expected_buy
                or reduce_only is not (action["action"] == "CLOSE")):
            raise GrvtPaperSignalError("Lado o reduce-only distinto de la acción PAPER")
        binding = GrvtPaperSignalBinding(session_id, action_request_id,
                                           action["action"], session["version_id"],
                                           session["dataset_id"], session["dataset_hash"], client_id)
        def fresh_clock() -> int:
            stamp = clock_ns()
            if type(stamp) is not int or not 0 <= stamp - action["recorded_at_ns"] <= SIGNAL_MAX_AGE_NS:
                raise GrvtPaperSignalError("La acción PAPER venció durante el preflight de ejecución")
            return stamp

        source = {"kind": "GRVT_AUTO_PAPER", "session_id": session_id,
                  "action_request_id": action_request_id, "action": action["action"],
                  "direction": definition["direction"], "version_id": session["version_id"],
                  "dataset_id": session["dataset_id"], "dataset_hash": session["dataset_hash"],
                  "client_order_id": client_id}
        yield binding, source, definition, action['recorded_at_ns'], fresh_clock

