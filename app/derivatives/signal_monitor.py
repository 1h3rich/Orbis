"""Señales causales GRVT y ejecución optativa en el libro PAPER local.

Cada sesión fija un dataset público y una versión LAB. Una observación sólo
se admite durante la vela siguiente a la última observada: los huecos se
marcan, nunca se reconstruyen fingiendo que se vieron en directo.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from threading import Lock
import time
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, field_validator
import sqlcipher3

from app.derivatives.paper_api import LOCAL_HOSTS
from app.derivatives.paper_math import (InstrumentSpec, MarginRules, PaperPerpError,
                                        PerpQuote, decimal_string)
from app.derivatives.isolated import IsolatedPerpPosition
from app.derivatives.paper_store import (PaperCloseQuoteDeferred, PaperCloseWindowElapsed,
                                         PaperPerpStore, RISK_MAX_GAP_NS, signal_action_expiry)
from app.derivatives.paper_access import paper_perp_access, paper_perp_format
from app.core.paper_archive import full_archive_matches, observed_archive_matches
from app.core.paper_archive_chain import (ArchiveChainError, chained_archive_matches,
                                          next_archive_digest, verified_archive_page)
from app.lab.api import ENGINE_VERSION, download_grvt
from app.lab.engine import IndicatorCache, INTERVALS, LabError, evaluate, paper_precision_for_engine, supports_engine_version, validate_candles, validate_strategy
from app.lab.coverage import (higher_timeframe_coverage, uses_higher_timeframe,
                              validate_paper_timeframe_capacity)
from app.lab.store import LabStore
from app.lab.paper_verification import verify_paper_source
from app.lab.paper_signal_replay import verify_archived_paper_signals, verify_paper_signals
from app.lab.paper_indicator_state import (advance_paper_indicator_state,
                                           build_paper_indicator_state,
                                           paper_indicator_state_matches,
                                           paper_indicator_values_match)
from app.lab.recent_paper_evaluator import RECENT_BARS, evaluate_recent, recent_supported
from app.lab.paper_reference import paper_lab_reference
from app.lab.paper_gap import analyze_paper_gap, gap_download_range
from app.lab.signal_frequency import signal_frequency
from app.lab.verification import verify_backtest_run
from app.marketdata.adapters.grvt import GrvtAdapter, GrvtHistoryError

if TYPE_CHECKING:
    from app.derivatives.signal_worker import SignalWorker
    from app.derivatives.signal_risk_worker import SignalRiskWorker
    from app.derivatives.funding_worker import AutoFundingWorker


class SignalMonitorError(ValueError):
    pass


class SignalMonitorUnavailable(SignalMonitorError):
    """Falta una observación pública verificable; la sesión puede reintentarse."""


class SignalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: str
    version_id: str
    dataset_id: str
    source_run_id: str | None = None
    execution_mode: Literal["OBSERVE", "AUTO_PAPER"] = "OBSERVE"
    size: str | None = None
    leverage: str | None = None

    @field_validator("size", "leverage", mode="before")
    @classmethod
    def exact_decimal_text(cls, value):
        if value is not None and not isinstance(value, str):
            raise ValueError("Tamaño y apalancamiento deben ser cadenas decimales")
        return value


class SignalMonitor:
    ARCHIVE_GAP_REASON = "Archivo de velas PAPER divergente; sin ejecución automática"
    INDICATOR_GAP_REASON = "Indicadores PAPER divergentes; sin ejecución automática"

    def __init__(self, paper_path: Path, lab_path: Path, *, adapter_factory=GrvtAdapter,
                 clock_ns=time.time_ns):
        self.paper = PaperPerpStore(paper_path, allow_create=False, clock_ns=clock_ns)
        self.lab = LabStore(lab_path, allow_create=False)
        self.adapter_factory = adapter_factory
        self.clock_ns = clock_ns
        self._risk_stop_cache: dict[tuple, tuple[Decimal, str]] = {}
        self._risk_stop_cache_lock = Lock()

    @staticmethod
    def _session(row) -> dict:
        return {key: row[key] for key in ("id", "account_id", "version_id", "dataset_id", "dataset_hash", "source_run_id",
                                           "engine_version", "symbol", "timeframe", "started_at_ns", "last_t",
                                           "status", "status_reason", "execution_mode", "auto_size",
                                           "auto_leverage", "position_id", "risk_last_quote_ns")}

    def create(self, account_id: str, version_id: str, dataset_id: str,
               execution_mode: str = "OBSERVE", size: str | None = None,
               leverage: str | None = None, source_run_id: str | None = None) -> dict:
        if execution_mode not in ("OBSERVE", "AUTO_PAPER"):
            raise SignalMonitorError("Modo de señales no admitido")
        if execution_mode == "AUTO_PAPER":
            if size is None or leverage is None:
                raise SignalMonitorError("La ejecución PAPER requiere tamaño y apalancamiento explícitos")
            size_value = decimal_string(size, "size", positive=True)
            leverage_value = decimal_string(leverage, "leverage", positive=True)
            if leverage_value < 1:
                raise SignalMonitorError("El apalancamiento debe ser al menos 1")
        elif size is not None or leverage is not None:
            raise SignalMonitorError("El modo de observación no admite tamaño ni apalancamiento")
        now_ns = self.clock_ns()
        now = now_ns // 1_000_000_000
        version = self.lab.version(version_id)
        definition = validate_strategy(version["definition"])
        if execution_mode == "AUTO_PAPER" and definition["direction"] == "SHORT" and Decimal(definition["take_profit"]) >= 1:
            raise SignalMonitorError("El objetivo SHORT automático no puede alcanzar el 100 % del precio de entrada")
        meta, bars = self._initial_history(dataset_id, now)
        if source_run_id is not None:
            reference, _, _ = paper_lab_reference(self.lab, dataset_id, meta["hash"],
                                                  version_id, source_run_id)
            verification = verify_backtest_run(self.lab, self.lab.run(reference["id"]))
            if not verification["verified"]:
                raise SignalMonitorError("El backtest LAB de origen no está verificado: " + verification["reason"])
        validate_paper_timeframe_capacity(definition, meta["timeframe"], bars[0].t)
        session_id = str(uuid4())
        indicator_state = build_paper_indicator_state(
            definition, [bar.json() for bar in bars], paper_precision_for_engine(ENGINE_VERSION))
        with self.paper.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            account = self.paper._account(db, account_id)
            if account["stopped_at"] is not None:
                raise SignalMonitorError("La cuenta PAPER está detenida")
            if execution_mode == "AUTO_PAPER":
                if leverage_value > Decimal(account["max_leverage"]):
                    raise SignalMonitorError("Apalancamiento superior al límite de la cuenta PAPER")
                clash = db.execute("""SELECT id FROM paper_signal_sessions WHERE account_id=? AND symbol=?
                                      AND status='ACTIVE' AND execution_mode='AUTO_PAPER'""",
                                   (account_id, meta["symbol"])).fetchone()
                if clash is not None:
                    raise SignalMonitorError("Ya hay una sesión automática activa para este par y cuenta")
            db.execute("""INSERT INTO paper_signal_sessions
                          (id,account_id,version_id,dataset_id,dataset_hash,engine_version,
                           symbol,timeframe,definition_json,candles_json,started_at_ns,last_t,
                           status,status_reason,execution_mode,auto_size,auto_leverage,position_id,
                           source_run_id,indicator_state_json)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (session_id, account_id, version_id, dataset_id, meta["hash"], ENGINE_VERSION,
                        meta["symbol"], meta["timeframe"], json.dumps(definition, sort_keys=True),
                        json.dumps([bar.json() for bar in bars], sort_keys=True), now_ns, bars[-1].t,
                        "ACTIVE", None, execution_mode, str(size_value) if execution_mode == "AUTO_PAPER" else None,
                        str(leverage_value) if execution_mode == "AUTO_PAPER" else None, None,
                        source_run_id, json.dumps(indicator_state, sort_keys=True)))
            previous = None
            for bar in bars:
                candle_json = json.dumps(bar.json(), sort_keys=True)
                db.execute("INSERT INTO paper_signal_all_candles VALUES (?,?,?,?)",
                           (session_id, bar.t, candle_json, "SEED"))
                previous = next_archive_digest(previous, session_id, bar.t,
                                               "SEED", candle_json, "grvt")
                db.execute("INSERT INTO paper_signal_archive_chain VALUES (?,?,?)",
                           (session_id, bar.t, previous))
        return self.detail(session_id)

    def _initial_history(self, dataset_id: str, now: int):
        meta, rows = self.lab.dataset(dataset_id)
        if (not meta["verified_public"] or meta["source"] != "grvt-perp-trade-public"
                or meta["symbol"] not in GrvtAdapter.known_pairs):
            raise SignalMonitorError("Se requiere histórico público GRVT de perpetuos, no spot ni importación manual")
        if meta["timeframe"] not in INTERVALS or len(rows) >= 5000:
            raise SignalMonitorError("Temporalidad no admitida o histórico sin capacidad para observar otra vela")
        step = INTERVALS[meta["timeframe"]]
        bars, gaps = validate_candles(rows, meta["timeframe"], now=now)
        if gaps or bars[-1].t != (now // step) * step - step:
            raise SignalMonitorError("El histórico inicial debe terminar en la última vela cerrada y no tener huecos")
        return meta, bars

    def readiness(self, dataset_id: str) -> dict:
        """Elegibilidad del histórico ahora; crear vuelve a validar."""
        try:
            meta, bars = self._initial_history(dataset_id, self.clock_ns() // 1_000_000_000)
            return {"ready": True, "reason": "Histórico al día", "dataset_id": dataset_id,
                    "last_t": bars[-1].t, "symbol": meta["symbol"], "timeframe": meta["timeframe"]}
        except (LabError, SignalMonitorError) as exc:
            return {"ready": False, "reason": str(exc), "dataset_id": dataset_id}

    def sessions(self, account_id: str) -> list[dict]:
        with self.paper.connect() as db:
            self.paper._account(db, account_id)
            return [self._session(row) for row in db.execute(
                "SELECT * FROM paper_signal_sessions WHERE account_id=? ORDER BY started_at_ns DESC,id",
                (account_id,))]

    def active_sessions(self) -> list[dict]:
        """Agenda persistida para el observador local, sin datos patrimoniales."""
        if paper_perp_format(self.paper.path) in ("missing", "empty"):
            return []
        with self.paper.connect() as db:
            return [self._session(row) | {"pending": bool(db.execute(
                "SELECT 1 FROM paper_signal_actions WHERE session_id=? AND status='PENDING' LIMIT 1",
                (row["id"],)).fetchone())} for row in db.execute(
                "SELECT * FROM paper_signal_sessions WHERE status='ACTIVE' ORDER BY last_t,id")]

    def active_risk_sessions(self) -> list[dict]:
        """Sólo posiciones AUTO abiertas que requieren ticker continuo."""
        if paper_perp_format(self.paper.path) in ("missing", "empty"):
            return []
        with self.paper.connect() as db:
            return [self._session(row) for row in db.execute("""
                SELECT s.* FROM paper_signal_sessions AS s
                JOIN paper_perp_positions AS p ON p.id=s.position_id
                WHERE s.status='ACTIVE' AND s.execution_mode='AUTO_PAPER'
                  AND p.closed_at_ns IS NULL ORDER BY s.id""")]

    def detail(self, session_id: str) -> dict:
        with self.paper.connect() as db:
            row = db.execute("SELECT * FROM paper_signal_sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise SignalMonitorError("Sesión de señales no encontrada")
            definition = json.loads(row["definition_json"])
            history = json.loads(row["candles_json"])
            coverage = None
            if uses_higher_timeframe(definition):
                bars, _ = validate_candles(history, row["timeframe"],
                                           now=row["last_t"] + 2 * INTERVALS[row["timeframe"]])
                coverage = higher_timeframe_coverage(bars, definition, row["timeframe"])
            events = []
            for event in db.execute("SELECT * FROM paper_signal_observations WHERE session_id=? ORDER BY candle_t",
                                    (session_id,)):
                events.append({"candle_t": event["candle_t"], "observed_at_ns": event["observed_at_ns"],
                               "entry_match": bool(event["entry_match"]),
                               "exit_match": None if event["exit_match"] is None else bool(event["exit_match"]),
                               "evidence": json.loads(event["evidence_json"])})
            actions = [{key: action[key] for key in ("candle_t", "action", "status", "position_id", "reason")}
                       for action in db.execute("SELECT * FROM paper_signal_actions WHERE session_id=? ORDER BY candle_t",
                                                (session_id,))]
            return self._session(row) | {"observations": events, "actions": actions,
                                         "coverage": coverage,
                                         "capacity_remaining": max(0, 5000 - len(history)),
                                         "execution": ("LOCAL_PAPER_APPROXIMATION_NO_REAL_ORDERS"
                                                       if row["execution_mode"] == "AUTO_PAPER"
                                                       else "SIGNALS_ONLY_NO_ORDERS")}

    def candle_page(self, session_id: str, after_t: int | None = None, limit: int = 500) -> dict:
        if type(limit) is not int or not 1 <= limit <= 1000 or (after_t is not None and
                (type(after_t) is not int or after_t < 0)):
            raise SignalMonitorError("Página de velas GRVT PAPER inválida")
        with self.paper.connect() as db:
            db.execute("BEGIN")
            session = db.execute("SELECT symbol,timeframe FROM paper_signal_sessions WHERE id=?",
                                 (session_id,)).fetchone()
            if session is None:
                raise SignalMonitorError("Sesión de señales no encontrada")
            try:
                page = verified_archive_page(db, session_id, "grvt", after_t, limit)
            except ArchiveChainError as exc:
                raise SignalMonitorError(str(exc)) from exc
            return {"session_id": session_id, "symbol": session["symbol"],
                    "timeframe": session["timeframe"],
                    "candles": page["candles"], "next_after_t": page["next_after_t"],
                    "has_more": page["has_more"]}

    def observation_page(self, session_id: str, after_t: int | None = None,
                         limit: int = 500) -> dict:
        if type(limit) is not int or not 1 <= limit <= 1000 or (after_t is not None and
                (type(after_t) is not int or after_t < 0)):
            raise SignalMonitorError("Página de observaciones GRVT PAPER inválida")
        with self.paper.connect() as db:
            session = db.execute("SELECT symbol,timeframe FROM paper_signal_sessions WHERE id=?",
                                 (session_id,)).fetchone()
            if session is None:
                raise SignalMonitorError("Sesión de señales no encontrada")
            rows = db.execute("""SELECT o.candle_t,o.observed_at_ns,o.entry_match,o.exit_match,
                                        o.evidence_json,a.action,a.status AS action_status,a.reason
                                 FROM paper_signal_observations AS o
                                 LEFT JOIN paper_signal_actions AS a
                                   ON a.session_id=o.session_id AND a.candle_t=o.candle_t
                                 WHERE o.session_id=? AND o.candle_t>?
                                 ORDER BY o.candle_t LIMIT ?""",
                              (session_id, after_t if after_t is not None else -1, limit + 1)).fetchall()
            visible = rows[:limit]
            return {"session_id": session_id, "symbol": session["symbol"],
                    "timeframe": session["timeframe"],
                    "observations": [{"candle_t": row["candle_t"],
                                      "observed_at_ns": row["observed_at_ns"],
                                      "entry_match": bool(row["entry_match"]),
                                      "exit_match": None if row["exit_match"] is None else bool(row["exit_match"]),
                                      "evidence": json.loads(row["evidence_json"]),
                                      "paper_action": None if row["action"] is None else {
                                          "action": row["action"], "status": row["action_status"],
                                          "reason": row["reason"]}}
                                     for row in visible],
                    "next_after_t": visible[-1]["candle_t"] if len(rows) > limit else None,
                    "has_more": len(rows) > limit}

    def verify_source(self, session_id: str) -> dict:
        """Revalida versión, dataset y prefijo PAPER sin reinterpretar acciones."""
        with self.paper.connect() as db:
            row = db.execute("SELECT * FROM paper_signal_sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise SignalMonitorError("Sesión de señales no encontrada")
            session = dict(row)
        try:
            definition = json.loads(session["definition_json"])
            history = json.loads(session["candles_json"])
        except (ValueError, TypeError):
            return {"verified": False, "status": "mismatch", "checks": {},
                    "reason": "Definición o histórico PAPER ilegible"}
        return verify_paper_source(self.lab, session, definition, history)

    def gap_analysis(self, session_id: str) -> dict:
        """Señales que habrían aparecido durante un GAP, sin ejecución PAPER."""
        with self.paper.connect() as db:
            row = db.execute("SELECT * FROM paper_signal_sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise SignalMonitorError("Sesión PAPER GRVT no encontrada")
            session = dict(row)
        definition = json.loads(session["definition_json"])
        history = json.loads(session["candles_json"])
        source = verify_paper_source(self.lab, session, definition, history)
        if not source["verified"]:
            raise SignalMonitorError("Origen PAPER no verificable; no se reconstruyen señales")
        now = self.clock_ns() // 1_000_000_000
        start, end = gap_download_range(session, now)
        downloaded = download_grvt(session["symbol"], session["timeframe"], start, end,
                                   adapter=self.adapter_factory(), now=now)
        with self.paper.connect() as db:
            current = db.execute("SELECT * FROM paper_signal_sessions WHERE id=?", (session_id,)).fetchone()
            if current is None or dict(current) != session:
                raise SignalMonitorError("La sesión PAPER cambió durante la consulta del hueco")
        if not verify_paper_source(self.lab, session, definition, history)["verified"]:
            raise SignalMonitorError("El origen PAPER cambió durante la consulta del hueco")
        report = analyze_paper_gap(session, definition, history, downloaded, now=now,
                                   source="grvt-perp-trade-public")
        snapshot = {key: session[key] for key in ("id", "status", "last_t", "timeframe",
                                                  "symbol", "dataset_id", "dataset_hash",
                                                  "version_id", "engine_version")}
        snapshot.update({"definition": definition, "history": history,
                         "reconstruction_now": now})
        saved = self.lab.save_paper_gap_report("GRVT_PERP", snapshot, downloaded, report)
        return report | {"archive": {key: saved[key] for key in ("id", "hash", "created_at")}}

    def gap_analyses(self, session_id: str) -> list[dict]:
        self.detail(session_id)
        return [row for row in self.lab.paper_gap_reports(session_id)
                if row["venue"] == "GRVT_PERP"]

    def comparison(self, session_id: str) -> dict:
        """Referencia LAB y hechos PAPER, sin comparar rentabilidades desiguales."""
        session = self.detail(session_id)
        try:
            reference, dataset_relation, refresh_hops = paper_lab_reference(
                self.lab, session["dataset_id"], session["dataset_hash"], session["version_id"],
                session["source_run_id"])
        except LabError as exc:
            if session["source_run_id"] is None:
                raise
            reference, dataset_relation, refresh_hops = None, None, 0
            historical_error = str(exc)
        else:
            historical_error = None
        historical = None
        saved = None
        if historical_error is not None:
            historical = {"run_id": session["source_run_id"], "created_at": None,
                          "dataset_id": None, "dataset_relation": None, "refresh_hops": 0,
                          "engine_version": None,
                          "verification": {"verified": False, "status": "mismatch",
                                           "reason": historical_error}, "metrics": None}
        if reference is not None:
            try:
                saved = self.lab.run(reference["id"])
                verification = verify_backtest_run(self.lab, saved)
            except LabError:
                verification = {"verified": False, "status": "mismatch",
                                "reason": "El backtest guardado ya no está disponible"}
            historical = {"run_id": reference["id"], "created_at": reference["created_at"],
                          "dataset_id": reference["dataset_id"],
                          "dataset_relation": dataset_relation,
                          "refresh_hops": refresh_hops,
                          "engine_version": reference["engine_version"],
                          "verification": verification,
                          "metrics": saved["metrics"] if saved is not None and verification["verified"] else None}
        positions = [row for row in self.paper.positions(session["account_id"])
                     if row["origin_session_id"] == session_id]
        closed = [row for row in positions if row["closed_at_ns"] is not None]
        funding_covered = [row for row in closed if row["funding_coverage"] == "CLOSED_COMPLETE"]
        funding_incomplete = [row for row in closed if row["funding_coverage"] != "CLOSED_COMPLETE"]
        with localcontext() as context:
            context.prec = 80
            covered_pnl = sum((Decimal(row["realized_pnl"]) for row in funding_covered), Decimal(0))
            provisional_pnl = sum((Decimal(row["realized_pnl"]) for row in funding_incomplete), Decimal(0))
            realized = covered_pnl + provisional_pnl
        actions = session["actions"]
        with self.paper.connect() as db:
            db.execute("BEGIN")
            archived = db.execute("SELECT * FROM paper_signal_sessions WHERE id=?",
                                  (session_id,)).fetchone()
            if archived is None or archived["last_t"] != session["last_t"]:
                signal_replay = {"verified": False, "status": "stale_snapshot",
                                 "reason": "PAPER cambió durante la comparación",
                                 "checked_observations": 0, "first_mismatch_t": None}
            else:
                definition = json.loads(archived["definition_json"])
                signal_replay = verify_paper_signals(
                    self.lab, dict(archived), definition,
                    json.loads(archived["candles_json"]),
                    [{"candle_t": row["candle_t"],
                      "candle": row["evidence"].get("candle"),
                      "entry_match": row["entry_match"], "exit_match": row["exit_match"],
                      "entry": row["evidence"].get("entry"),
                      "exit": row["evidence"].get("exit")}
                     for row in session["observations"]],
                )
                if signal_replay["verified"] and recent_supported(definition, archived["engine_version"]):
                    segmented = verify_archived_paper_signals(
                        self.lab, db, dict(archived), definition, "grvt")
                    if not segmented["verified"]:
                        signal_replay = {**segmented, "reason": "Replay segmentado archivado divergente: "
                                         + segmented["reason"]}
        entry_signals = sum(bool(row["entry_match"]) for row in session["observations"])
        frequency = signal_frequency(historical, saved, signal_replay,
                                     observed_candles=len(session["observations"]),
                                     entry_signals=entry_signals,
                                     paper_signal_times=[row["candle_t"] for row in session["observations"]
                                                         if row["entry_match"]],
                                     paper_observed_times=[row["candle_t"] for row in session["observations"]],
                                     step_seconds=INTERVALS[session["timeframe"]])
        return {"session_id": session_id, "version_id": session["version_id"],
                "source_run_id": session["source_run_id"],
                "dataset_id": session["dataset_id"], "dataset_hash": session["dataset_hash"],
                "status": session["status"], "execution_mode": session["execution_mode"],
                "historical": historical, "signal_replay": signal_replay,
                "signal_frequency": frequency,
                "paper": {"observed_candles": len(session["observations"]),
                          "entry_signals": entry_signals,
                          "executed_opens": sum(row["action"] == "OPEN" and row["status"] == "EXECUTED" for row in actions),
                          "executed_closes": sum(row["action"] == "CLOSE" and row["status"] == "EXECUTED" for row in actions),
                          "closed_positions": len(closed),
                          "open_positions": len(positions) - len(closed),
                          "realized_pnl_usdt": str(realized),
                          "realized_pnl_funding_covered_usdt": str(covered_pnl),
                          "realized_pnl_provisional_usdt": str(provisional_pnl),
                          "funding_covered_closed": len(funding_covered),
                          "funding_incomplete_closed": len(funding_incomplete)},
                "comparison_basis": "NOT_DIRECTLY_COMPARABLE",
                "reason": "Capital, tamaño, periodo, ejecución y financiación pueden diferir; no se calcula una rentabilidad PAPER comparable"}

    def chart_markers(self, account_id: str, symbol: str, timeframe: str,
                      since_t: int) -> dict:
        """Señales observadas, separadas de los eventos de ejecución PAPER."""
        if symbol not in GrvtAdapter.known_pairs or timeframe not in INTERVALS or since_t < 0:
            raise SignalMonitorError("Filtro de gráfico GRVT inválido")
        with self.paper.connect() as db:
            self.paper._account(db, account_id)
            rows = db.execute("""SELECT o.session_id,o.candle_t,o.observed_at_ns,
                                        o.entry_match,o.exit_match,s.version_id,s.definition_json,
                                        s.execution_mode,a.action,a.status AS action_status
                                 FROM paper_signal_observations AS o
                                 JOIN paper_signal_sessions AS s ON s.id=o.session_id
                                 LEFT JOIN paper_signal_actions AS a ON a.session_id=o.session_id AND a.candle_t=o.candle_t
                                 WHERE s.account_id=? AND s.symbol=? AND s.timeframe=?
                                   AND o.candle_t>=? AND (o.entry_match=1 OR o.exit_match=1)
                                 ORDER BY o.candle_t DESC,o.session_id DESC LIMIT 1001""",
                              (account_id, symbol, timeframe, since_t)).fetchall()
        return {"symbol": symbol, "timeframe": timeframe,
                "source": "GRVT_PAPER_OBSERVED_SIGNALS", "execution": "SIGNALS_SEPARATE_FROM_PAPER_EVENTS",
                "truncated": len(rows) > 1000,
                "markers": [{"session_id": row["session_id"], "version_id": row["version_id"],
                             "candle_t": row["candle_t"], "observed_at_ns": row["observed_at_ns"],
                             "direction": json.loads(row["definition_json"])["direction"],
                             "execution_mode": row["execution_mode"], "action": row["action"],
                             "action_status": row["action_status"],
                             "entry_match": bool(row["entry_match"]),
                             "exit_match": bool(row["exit_match"])}
                            for row in reversed(rows[:1000])]}

    def chart_marker_page(self, account_id: str, symbol: str, timeframe: str,
                          since_t: int, until_t: int, after_t: int | None = None,
                          after_session_id: str = "", limit: int = 500) -> dict:
        """Marcas de todas las sesiones visibles, sin perder empates de vela."""
        if (symbol not in GrvtAdapter.known_pairs or timeframe not in INTERVALS or
                type(since_t) is not int or type(until_t) is not int or
                since_t < 0 or until_t < since_t or
                (after_t is not None and (type(after_t) is not int or after_t < since_t)) or
                not isinstance(after_session_id, str) or len(after_session_id) > 64 or
                (after_t is None and after_session_id) or
                type(limit) is not int or not 1 <= limit <= 1000):
            raise SignalMonitorError("Filtro de marcas GRVT inválido")
        with self.paper.connect() as db:
            self.paper._account(db, account_id)
            rows = db.execute("""SELECT o.session_id,o.candle_t,o.observed_at_ns,
                                        o.entry_match,o.exit_match,s.version_id,s.definition_json,
                                        s.execution_mode,a.action,a.status AS action_status
                                 FROM paper_signal_observations AS o
                                 JOIN paper_signal_sessions AS s ON s.id=o.session_id
                                 LEFT JOIN paper_signal_actions AS a
                                   ON a.session_id=o.session_id AND a.candle_t=o.candle_t
                                 WHERE s.account_id=? AND s.symbol=? AND s.timeframe=?
                                   AND o.candle_t BETWEEN ? AND ?
                                   AND (o.entry_match=1 OR o.exit_match=1)
                                   AND (o.candle_t>? OR (o.candle_t=? AND o.session_id>?))
                                 ORDER BY o.candle_t,o.session_id LIMIT ?""",
                              (account_id, symbol, timeframe, since_t, until_t,
                               after_t if after_t is not None else -1,
                               after_t if after_t is not None else -1,
                               after_session_id, limit + 1)).fetchall()
        visible = rows[:limit]
        has_more = len(rows) > limit
        return {"symbol": symbol, "timeframe": timeframe,
                "source": "GRVT_PAPER_OBSERVED_SIGNALS",
                "execution": "SIGNALS_SEPARATE_FROM_PAPER_EVENTS",
                "markers": [{"session_id": row["session_id"], "version_id": row["version_id"],
                             "candle_t": row["candle_t"], "observed_at_ns": row["observed_at_ns"],
                             "direction": json.loads(row["definition_json"])["direction"],
                             "execution_mode": row["execution_mode"], "action": row["action"],
                             "action_status": row["action_status"],
                             "entry_match": bool(row["entry_match"]),
                             "exit_match": bool(row["exit_match"])} for row in visible],
                "next_after_t": visible[-1]["candle_t"] if has_more else None,
                "next_after_session_id": visible[-1]["session_id"] if has_more else None,
                "has_more": has_more}

    def stop(self, session_id: str) -> dict:
        with self.paper.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM paper_signal_sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise SignalMonitorError("Sesión de señales no encontrada")
            if row["status"] == "ACTIVE":
                db.execute("UPDATE paper_signal_sessions SET status='STOPPED',status_reason='Parada manual' WHERE id=?",
                           (session_id,))
                db.execute("""UPDATE paper_signal_actions SET status='EXPIRED',reason='Parada manual'
                              WHERE session_id=? AND status='PENDING'""", (session_id,))
            return self._session(db.execute("SELECT * FROM paper_signal_sessions WHERE id=?", (session_id,)).fetchone())

    def _archive_gap(self, db, session) -> bool:
        if (observed_archive_matches(db, session["id"], session["candles_json"], "grvt")
                and full_archive_matches(db, session["id"], session["candles_json"], "grvt")
                and chained_archive_matches(db, session["id"], "grvt")):
            return False
        db.execute("UPDATE paper_signal_sessions SET status='GAP',status_reason=? WHERE id=? AND status='ACTIVE'",
                   (self.ARCHIVE_GAP_REASON, session["id"]))
        db.execute("""UPDATE paper_signal_actions SET status='EXPIRED',reason=?
                      WHERE session_id=? AND status='PENDING'""",
                   (self.ARCHIVE_GAP_REASON, session["id"]))
        return True

    def _indicator_gap(self, db, session) -> bool:
        try:
            valid = paper_indicator_state_matches(
                json.loads(session["indicator_state_json"]),
                json.loads(session["definition_json"]),
                json.loads(session["candles_json"]),
                paper_precision_for_engine(session["engine_version"]))
        except (KeyError, TypeError, ValueError):
            valid = False
        if valid:
            return False
        db.execute("UPDATE paper_signal_sessions SET status='GAP',status_reason=? WHERE id=? AND status='ACTIVE'",
                   (self.INDICATOR_GAP_REASON, session["id"]))
        db.execute("""UPDATE paper_signal_actions SET status='EXPIRED',reason=?
                      WHERE session_id=? AND status='PENDING'""",
                   (self.INDICATOR_GAP_REASON, session["id"]))
        return True

    def poll(self, session_id: str, *, observer: str = "MANUAL") -> dict:
        if observer not in ("MANUAL", "LOCAL_WORKER"):
            raise SignalMonitorError("Observador de señal no admitido")
        # No procesar una intención pendiente antes de reconocer que el feed
        # de riesgo llevaba demasiado tiempo sin observar la posición.
        self.expire_risk_gaps(session_id)
        now_ns = self.clock_ns()
        now = now_ns // 1_000_000_000
        with self.paper.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            before = db.execute("SELECT * FROM paper_signal_sessions WHERE id=?", (session_id,)).fetchone()
            if before is None:
                raise SignalMonitorError("Sesión de señales no encontrada")
            state_gap = (before["status"] == "ACTIVE"
                         and (self._archive_gap(db, before) or self._indicator_gap(db, before)))
        if state_gap:
            return self.detail(session_id) | {"poll_result": "GAP"}
        pending_result = self._execute_pending(session_id)
        with self.paper.connect() as db:
            row = db.execute("SELECT * FROM paper_signal_sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise SignalMonitorError("Sesión de señales no encontrada")
            session = self._session(row)
            if session["status"] != "ACTIVE":
                return self.detail(session_id) | {"poll_result": session["status"]}
            account = self.paper._account(db, row["account_id"])
            stopped = account["stopped_at"] is not None
            history = json.loads(row["candles_json"])
            definition = json.loads(row["definition_json"])
        step = INTERVALS[session["timeframe"]]
        next_t = session["last_t"] + step
        if stopped:
            return self._terminal(session_id, session["last_t"], "STOPPED", "Cuenta PAPER detenida")
        if not supports_engine_version(session["engine_version"], definition):
            return self._terminal(session_id, session["last_t"], "GAP",
                                  "Versión del motor LAB distinta; señales no reproducibles")
        if now < next_t + step:
            return self.detail(session_id) | {"poll_result": pending_result or "WAIT"}
        if now >= next_t + 2 * step:
            return self._terminal(session_id, session["last_t"], "GAP", "Se perdió una ventana de observación; no hay reconstrucción en directo")
        if len(history) >= 5000:
            return self._terminal(session_id, session["last_t"], "CAPACITY", "Límite de 5000 velas; inicia otra sesión")
        try:
            incoming = self.adapter_factory().get_historical_candles(
                session["symbol"], session["timeframe"], next_t - 2 * step, next_t + step, now=now)
        except GrvtHistoryError as exc:
            raise SignalMonitorUnavailable(f"GRVT no devolvió la siguiente vela verificable: {exc}") from exc
        if not isinstance(incoming, list) or len(incoming) != 3:
            raise SignalMonitorUnavailable("GRVT no devolvió las tres velas cerradas necesarias")
        try:
            overlap, overlap_gaps = validate_candles(incoming, session["timeframe"], now=now)
        except LabError as exc:
            raise SignalMonitorUnavailable(f"Velas GRVT no verificables: {exc}") from exc
        if overlap[-1].t != next_t:
            raise SignalMonitorUnavailable("GRVT todavía no devolvió la siguiente vela cerrada")
        if overlap_gaps or [bar.json() for bar in overlap[:2]] != history[-2:]:
            return self._terminal(session_id, session["last_t"], "GAP", "GRVT revisó velas previas; continuidad no verificable")
        bars, gaps = validate_candles(history + incoming[-1:], session["timeframe"], now=now)
        if gaps:
            return self._terminal(session_id, session["last_t"], "GAP", "Velas discontinuas")
        cache = IndicatorCache(bars, precision=paper_precision_for_engine(session["engine_version"]))
        entry, entry_evidence = evaluate(definition["entry"], bars, len(bars) - 1, cache,
                                         base_timeframe=session["timeframe"])
        exit_match, exit_evidence = (evaluate(definition["exit"], bars, len(bars) - 1, cache,
                                              base_timeframe=session["timeframe"])
                                      if "exit" in definition else (None, None))
        try:
            indicator_state = advance_paper_indicator_state(
                json.loads(row["indicator_state_json"]), definition, bars[-1].json(),
                previous_t=session["last_t"],
                precision=paper_precision_for_engine(session["engine_version"]))
            if not paper_indicator_values_match(indicator_state, bars, definition,
                                                paper_precision_for_engine(session["engine_version"]), cache):
                raise LabError("Paridad incremental PAPER no verificable")
            if len(bars) >= RECENT_BARS and recent_supported(definition, session["engine_version"]):
                window = bars[-RECENT_BARS:]
                if evaluate_recent(definition["entry"], window, indicator_state,
                                   engine_version=session["engine_version"],
                                   base_timeframe=session["timeframe"]) != (entry, entry_evidence):
                    raise LabError("Señal PAPER distinta en ventana reciente")
                if "exit" in definition and evaluate_recent(
                        definition["exit"], window, indicator_state,
                        engine_version=session["engine_version"],
                        base_timeframe=session["timeframe"]) != (exit_match, exit_evidence):
                    raise LabError("Salida PAPER distinta en ventana reciente")
        except (LabError, KeyError, TypeError, ValueError):
            return self._terminal(session_id, session["last_t"], "GAP", self.INDICATOR_GAP_REASON)
        evidence = {"candle": bars[-1].json(), "entry": entry_evidence, "exit": exit_evidence,
                    "source": "grvt-perp-trade-public", "observation_mode": "POLL_ON_TIME_REST",
                    "observer": observer}
        outcome = "OBSERVED"
        with self.paper.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            fresh = db.execute("SELECT * FROM paper_signal_sessions WHERE id=?", (session_id,)).fetchone()
            account = self.paper._account(db, session["account_id"])
            if fresh["status"] != "ACTIVE" or fresh["last_t"] != session["last_t"] or account["stopped_at"] is not None:
                outcome = "CHANGED"
            elif self._archive_gap(db, fresh):
                outcome = "GAP"
            elif self._indicator_gap(db, fresh):
                outcome = "GAP"
            # El proveedor pudo tardar: después del límite ya no es una observación puntual.
            else:
                observation_ns = self.clock_ns()
                if observation_ns // 1_000_000_000 >= next_t + 2 * step:
                    db.execute("UPDATE paper_signal_sessions SET status='GAP',status_reason=? WHERE id=?",
                               ("La respuesta GRVT llegó fuera de la ventana", session_id))
                    outcome = "GAP"
                else:
                    db.execute("""INSERT INTO paper_signal_observations VALUES (?,?,?,?,?,?)""",
                               (session_id, next_t, observation_ns, int(entry),
                                None if exit_match is None else int(exit_match),
                                json.dumps(evidence, sort_keys=True)))
                    db.execute("INSERT INTO paper_signal_candles VALUES (?,?,?)",
                               (session_id, next_t, json.dumps(bars[-1].json(), sort_keys=True)))
                    db.execute("INSERT INTO paper_signal_all_candles VALUES (?,?,?,?)",
                               (session_id, next_t, json.dumps(bars[-1].json(), sort_keys=True), "OBSERVED"))
                    previous = db.execute("""SELECT digest FROM paper_signal_archive_chain
                                             WHERE session_id=? ORDER BY candle_t DESC LIMIT 1""",
                                          (session_id,)).fetchone()["digest"]
                    digest = next_archive_digest(previous, session_id, next_t, "OBSERVED",
                                                 json.dumps(bars[-1].json(), sort_keys=True), "grvt")
                    db.execute("INSERT INTO paper_signal_archive_chain VALUES (?,?,?)",
                               (session_id, next_t, digest))
                    db.execute("""UPDATE paper_signal_sessions
                                  SET candles_json=?,indicator_state_json=?,last_t=? WHERE id=?""",
                               (json.dumps([bar.json() for bar in bars], sort_keys=True),
                                json.dumps(indicator_state, sort_keys=True), next_t, session_id))
                    if fresh["execution_mode"] == "AUTO_PAPER":
                        same_candle_action = db.execute("""SELECT 1 FROM paper_signal_actions
                                                          WHERE session_id=? AND candle_t=?""",
                                                        (session_id, next_t)).fetchone()
                        action, reason, position_id = ((None, "RISK_ACTION_ALREADY_RECORDED", None)
                                                       if same_candle_action else
                                                       self._decision(db, fresh, bars[-1], entry, exit_match, definition))
                        if action:
                            request_id = str(uuid4())
                            digest = self.paper.digest({"session_id": session_id, "candle_t": next_t,
                                                        "action": action, "position_id": position_id,
                                                        "request_id": request_id})
                            db.execute("""INSERT INTO paper_signal_actions VALUES (?,?,?,?,?,?,?,?,?)""",
                                       (session_id, next_t, action, request_id, digest, "PENDING",
                                        position_id, reason, observation_ns))
                        elif reason == "MANUALLY_CLOSED":
                            db.execute("""UPDATE paper_signal_sessions SET status='STOPPED',status_reason=? WHERE id=?""",
                                       ("La posición se cerró fuera del observador", session_id))
                            outcome = "STOPPED"
                        elif reason in ("ENTRY_CLOCK_UNVERIFIABLE", "TRAILING_STATE_UNVERIFIABLE"):
                            db.execute("""UPDATE paper_signal_sessions SET status='GAP',status_reason=? WHERE id=?""",
                                       (("Inicio de posición PAPER sin vela verificable; cierre manual necesario"
                                         if reason == "ENTRY_CLOCK_UNVERIFIABLE" else
                                         "Stop móvil PAPER sin histórico verificable; cierre manual necesario"), session_id))
                            outcome = "GAP"
        if outcome == "OBSERVED":
            self._execute_pending(session_id)
        return self.detail(session_id) | {"poll_result": outcome}

    def expire_risk_gaps(self, session_id: str | None = None) -> list[str]:
        """Corta la automatización ficticia si el stream perdió cobertura.

        Se puede llamar al reconectar o durante una espera del socket. Una
        posición ya abierta no se cierra a un precio supuesto.
        """
        now_ns = self.clock_ns()
        expired: list[str] = []
        with self.paper.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute("""SELECT s.id,s.account_id,s.risk_last_quote_ns,p.id AS position_id,p.opened_at_ns
                                     FROM paper_signal_sessions AS s
                                     JOIN paper_perp_positions AS p ON p.id=s.position_id
                                     WHERE s.status='ACTIVE' AND s.execution_mode='AUTO_PAPER'
                                       AND p.closed_at_ns IS NULL AND (? IS NULL OR s.id=?)""",
                                  (session_id, session_id)).fetchall():
                last_ns = row["risk_last_quote_ns"] or row["opened_at_ns"]
                if now_ns - last_ns > RISK_MAX_GAP_NS:
                    reason = "Sin ticker GRVT continuo durante más de 20 s; posición PAPER abierta para cierre manual"
                    db.execute("UPDATE paper_signal_sessions SET status='GAP',status_reason=? WHERE id=?",
                               (reason, row["id"]))
                    db.execute("""UPDATE paper_signal_actions SET status='EXPIRED',reason=?
                                  WHERE session_id=? AND status='PENDING'""", (reason, row["id"]))
                    self._record_stream_gap(db, row["account_id"], row["position_id"],
                                            last_ns, now_ns, incoming_quote_ns=None)
                    expired.append(row["id"])
        return expired

    @staticmethod
    def _record_stream_gap(db, account_id: str, position_id: str,
                           last_quote_ns: int, detected_at_ns: int,
                           *, incoming_quote_ns: int | None) -> None:
        """Archiva el intervalo sin crear un precio ni una salida ficticios."""
        details = {"risk_type": "STREAM_COVERAGE_GAP",
                   "last_accepted_quote_ns": last_quote_ns,
                   "detected_at_ns": detected_at_ns,
                   "incoming_quote_ns": incoming_quote_ns,
                   "gap_ns": detected_at_ns - last_quote_ns,
                   "liquidation_status": "NOT_DETERMINED"}
        db.execute("INSERT INTO paper_perp_events VALUES (?,?,?,?,?,?,?,?)",
                   (str(uuid4()), account_id, position_id, "OBSERVE", None,
                    detected_at_ns, datetime.now(timezone.utc).isoformat(),
                    json.dumps(details, sort_keys=True, separators=(",", ":"))))

    @staticmethod
    def _stop_level(session, position, definition: dict) -> tuple[Decimal, str]:
        """Nivel causal: sólo velas completas posteriores a la vela de apertura.

        La vela que contiene el fill puede incluir extremos previos al fill;
        se excluye entera. El estado es derivable tras reiniciar, sin un
        máximo mutable que pueda quedar desincronizado del histórico.
        """
        try:
            entry = Decimal(position["entry_price"])
            stop_pct = Decimal(definition["stop_loss"])
            side = position["side"]
            if side not in ("LONG", "SHORT") or side != definition["direction"] or not entry.is_finite() or entry <= 0:
                raise ValueError("posición incompatible")
            sign = 1 if side == "LONG" else -1
            fixed = entry * (1 - sign * stop_pct)
            if "trailing_stop" not in definition:
                return fixed, "STOP"
            step = INTERVALS[session["timeframe"]]
            entry_bucket = position["opened_at_ns"] // 1_000_000_000 // step * step
            rows = json.loads(session["candles_json"])
            if (not isinstance(rows, list) or not rows or not isinstance(rows[-1], dict)
                    or rows[-1].get("t") != session["last_t"]):
                raise ValueError("histórico inválido")
            extreme = entry
            previous_t = None
            for bar in rows:
                candle_t = bar["t"]
                if (type(candle_t) is not int or candle_t > session["last_t"]
                        or (previous_t is not None and candle_t - previous_t != step)):
                    raise ValueError("velas discontinuas")
                previous_t = candle_t
                if candle_t <= entry_bucket:
                    continue
                observed = Decimal(bar["h"] if side == "LONG" else bar["l"])
                if not observed.is_finite() or observed <= 0:
                    raise ValueError("extremo inválido")
                extreme = max(extreme, observed) if side == "LONG" else min(extreme, observed)
            trailing = extreme * (1 - sign * Decimal(definition["trailing_stop"]))
            tighter = trailing > fixed if side == "LONG" else trailing < fixed
            return (trailing, "TRAILING_STOP") if tighter else (fixed, "STOP")
        except (KeyError, TypeError, ValueError, InvalidOperation, json.JSONDecodeError, IndexError) as exc:
            raise SignalMonitorError("Extremo del stop móvil PAPER no verificable") from exc

    @staticmethod
    def _archived_stop_level(db, session, position, definition: dict,
                             *, allow_next_uncommitted: bool = False) -> tuple[Decimal, str]:
        """Stop causal desde velas archivadas, sin `candles_json` creciente.

        El archivo se recorre desde el primer enlace para no aceptar como
        punto de partida una huella anterior que ya pudiera estar alterada.
        """
        if "trailing_stop" not in definition:
            return SignalMonitor._stop_level(session, position, definition)
        try:
            entry = Decimal(position["entry_price"])
            stop_pct = Decimal(definition["stop_loss"])
            trailing_pct = Decimal(definition["trailing_stop"])
            side = position["side"]
            if (side not in ("LONG", "SHORT") or side != definition["direction"]
                    or not entry.is_finite() or entry <= 0 or not stop_pct.is_finite()
                    or not trailing_pct.is_finite()):
                raise ValueError("posición o riesgo incompatible")
            sign = 1 if side == "LONG" else -1
            fixed = entry * (1 - sign * stop_pct)
            step = INTERVALS[session["timeframe"]]
            entry_bucket = position["opened_at_ns"] // 1_000_000_000 // step * step
            extreme = entry
            cursor = None
            previous_t = None
            previous_digest = None
            last_applied_t = None
            uncommitted_count = 0
            while True:
                page = verified_archive_page(db, session["id"], "grvt", cursor, 512)
                if page["candles"] and page["anchor_digest"] != previous_digest:
                    raise ValueError("segmentos de velas sin continuidad")
                for item in page["candles"]:
                    bar = item["candle"]
                    candle_t = bar["t"]
                    if (type(candle_t) is not int
                            or (previous_t is not None and candle_t - previous_t != step)):
                        raise ValueError("velas discontinuas")
                    previous_t = candle_t
                    if candle_t > session["last_t"]:
                        if (not allow_next_uncommitted or candle_t != session["last_t"] + step
                                or uncommitted_count):
                            raise ValueError("vela posterior inesperada")
                        uncommitted_count += 1
                        continue
                    last_applied_t = candle_t
                    if candle_t <= entry_bucket:
                        continue
                    observed = Decimal(bar["h"] if side == "LONG" else bar["l"])
                    if not observed.is_finite() or observed <= 0:
                        raise ValueError("extremo inválido")
                    extreme = max(extreme, observed) if side == "LONG" else min(extreme, observed)
                previous_digest = page["end_digest"]
                if not page["has_more"]:
                    break
                cursor = page["next_after_t"]
            if last_applied_t != session["last_t"]:
                raise ValueError("archivo incompleto")
            trailing = extreme * (1 - sign * trailing_pct)
            tighter = trailing > fixed if side == "LONG" else trailing < fixed
            return (trailing, "TRAILING_STOP") if tighter else (fixed, "STOP")
        except (ArchiveChainError, KeyError, TypeError, ValueError, InvalidOperation,
                IndexError, ArithmeticError, sqlite3.DatabaseError,
                sqlcipher3.DatabaseError) as exc:
            raise SignalMonitorError("Extremo del stop móvil PAPER archivado no verificable") from exc

    def _risk_stop_level(self, db, session, position, definition: dict) -> tuple[Decimal, str]:
        """Reutiliza sólo el nivel derivado de la misma posición y vela cerrada.

        El llamador debe ejecutar `_archive_gap` dentro de la misma transacción
        antes de acceder a esta caché. Nunca persiste un nivel calculado.
        """
        if "trailing_stop" not in definition:
            return self._stop_level(session, position, definition)
        key = (session["id"], session["last_t"], position["id"],
               position["opened_at_ns"], position["entry_price"], position["side"],
               definition["direction"], definition["stop_loss"], definition["trailing_stop"])
        with self._risk_stop_cache_lock:
            cached = self._risk_stop_cache.get(key)
        if cached is not None:
            return cached
        level = self._archived_stop_level(db, session, position, definition)
        with self._risk_stop_cache_lock:
            if len(self._risk_stop_cache) >= 128:
                self._risk_stop_cache.clear()
            self._risk_stop_cache[key] = level
        return level

    def on_risk_quote(self, session_id: str, quote: PerpQuote,
                      margin_rules: MarginRules | None = None, *, rules_checked: bool = False) -> str:
        """Dispara un cierre PAPER cuando el mark público cruza stop/objetivo.

        El precio de salida sigue siendo bid/ask del mismo ticker; no se
        afirma que GRVT habría llenado una orden a ese precio. Se persiste la
        intención antes del cierre para poder reintentar sin duplicados.
        """
        if not isinstance(quote, PerpQuote):
            raise SignalMonitorError("Cotización de riesgo inválida")
        now_ns = self.clock_ns()
        if quote.event_time_ns > now_ns + 2_000_000_000 or now_ns - quote.event_time_ns > 15_000_000_000:
            raise SignalMonitorError("Ticker GRVT de riesgo obsoleto o futuro")
        if margin_rules is None and not rules_checked:
            # Uso directo (p. ej., pruebas): consulta pública una sola vez.
            # El worker pasa rules_checked=True y su caché, incluso si falló.
            symbol = quote.instrument.removesuffix("_Perp").replace("_", "/") + ":PERP"
            if symbol in GrvtAdapter.known_pairs:
                try:
                    raw = self.adapter_factory().get_margin_rules(symbol)
                    margin_rules = MarginRules.from_payload(raw, quote.instrument, self.clock_ns())
                except Exception:
                    margin_rules = None
        action_data = None
        with self.paper.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            session = db.execute("SELECT * FROM paper_signal_sessions WHERE id=?", (session_id,)).fetchone()
            if session is None or session["status"] != "ACTIVE" or session["execution_mode"] != "AUTO_PAPER":
                return "INACTIVE"
            if self._archive_gap(db, session):
                return "GAP"
            position = db.execute("SELECT * FROM paper_perp_positions WHERE id=?", (session["position_id"],)).fetchone()
            if position is None or position["closed_at_ns"] is not None:
                return "NO_POSITION"
            if quote.instrument != position["instrument"] or quote.bid <= 0 or quote.ask < quote.bid or quote.mark <= 0:
                raise SignalMonitorError("Ticker de riesgo no corresponde a la posición")
            prior_ns = session["risk_last_quote_ns"] or position["opened_at_ns"]
            if quote.event_time_ns <= prior_ns:
                return "OLD_QUOTE"
            if quote.event_time_ns - prior_ns > RISK_MAX_GAP_NS:
                reason = "Ticker GRVT discontinuo; no se reconstruye un stop PAPER retroactivo"
                db.execute("UPDATE paper_signal_sessions SET status='GAP',status_reason=? WHERE id=?",
                           (reason, session_id))
                db.execute("""UPDATE paper_signal_actions SET status='EXPIRED',reason=?
                              WHERE session_id=? AND status='PENDING'""", (reason, session_id))
                self._record_stream_gap(db, session["account_id"], position["id"],
                                        prior_ns, quote.event_time_ns,
                                        incoming_quote_ns=quote.event_time_ns)
                return "GAP"
            db.execute("UPDATE paper_signal_sessions SET risk_last_quote_ns=? WHERE id=?",
                       (quote.event_time_ns, session_id))
            # No se usa la instantánea de apertura para decidir un stop actual.
            # Sin reglas públicas recientes, se detiene la automatización.
            try:
                mark = quote.mark if isinstance(quote.mark, Decimal) else Decimal(str(quote.mark))
                if margin_rules is None:
                    raise PaperPerpError("Reglas públicas actuales no disponibles")
                rules = MarginRules.from_payload(margin_rules.json(), position["instrument"], now_ns)
                signed_size = Decimal(position["size"]) * (1 if position["side"] == "LONG" else -1)
                paper_position = IsolatedPerpPosition(
                    position["instrument"], signed_size, Decimal(position["entry_price"]),
                    Decimal(position["collateral"]), Decimal(position["entry_fee"]),
                    Decimal(position["funding_paid"]),
                )
                with localcontext() as context:
                    context.prec = 80
                    requirement = rules.maintenance_requirement(abs(signed_size) * mark)
                    equity = paper_position.equity(mark)
            except (ValueError, TypeError, KeyError, InvalidOperation, PaperPerpError):
                reason = "Margen PAPER no verificable; sesión automática detenida, posición abierta para revisión"
                db.execute("UPDATE paper_signal_sessions SET status='STOPPED',status_reason=? WHERE id=?",
                           (reason, session_id))
                db.execute("""UPDATE paper_signal_actions SET status='EXPIRED',reason=?
                              WHERE session_id=? AND status='PENDING'""", (reason, session_id))
                details = {"risk_type": "CURRENT_RULES_UNVERIFIABLE",
                           "mark_price": str(quote.mark),
                           "rules_fetched_at_ns": margin_rules.fetched_at_ns if margin_rules else None,
                           "liquidation_status": "NOT_DETERMINED"}
                db.execute("INSERT INTO paper_perp_events VALUES (?,?,?,?,?,?,?,?)",
                           (str(uuid4()), session["account_id"], position["id"], "OBSERVE", None,
                            quote.event_time_ns, datetime.now(timezone.utc).isoformat(),
                            json.dumps(details, sort_keys=True, separators=(",", ":"))))
                return "MARGIN_UNVERIFIABLE"
            if equity <= requirement:
                reason = ("Riesgo de margen PAPER según reglas públicas recientes; no se simula liquidación "
                          "ni stop posterior. Posición abierta para revisión manual")
                db.execute("UPDATE paper_signal_sessions SET status='STOPPED',status_reason=? WHERE id=?",
                           (reason, session_id))
                db.execute("""UPDATE paper_signal_actions SET status='EXPIRED',reason=?
                              WHERE session_id=? AND status='PENDING'""", (reason, session_id))
                details = {"risk_type": "CURRENT_RULES_MAINTENANCE_BREACH",
                           "mark_price": str(mark), "isolated_equity": str(equity),
                           "maintenance_requirement": str(requirement),
                           "margin_buffer": str(equity - requirement),
                           "rules_fetched_at_ns": rules.fetched_at_ns,
                           "funding_paid": position["funding_paid"],
                           "funding_checked_through_ns": position["funding_checked_through_ns"],
                           "liquidation_status": "NOT_DETERMINED"}
                db.execute("INSERT INTO paper_perp_events VALUES (?,?,?,?,?,?,?,?)",
                           (str(uuid4()), session["account_id"], position["id"], "OBSERVE", None,
                            quote.event_time_ns, datetime.now(timezone.utc).isoformat(),
                            json.dumps(details, sort_keys=True, separators=(",", ":"))))
                return "MARGIN_RISK"
            pending_close = db.execute("""SELECT * FROM paper_signal_actions
                                          WHERE session_id=? AND action='CLOSE' AND status='PENDING'
                                          ORDER BY candle_t LIMIT 1""", (session_id,)).fetchone()
            if pending_close is not None:
                expiry = signal_action_expiry(pending_close, INTERVALS[session["timeframe"]], self.clock_ns())
                if expiry is not None:
                    self._expire_action(db, session_id, pending_close["request_id"], *expiry)
                    return "GAP"
                action_data = (session["account_id"], position["id"],
                               pending_close["request_id"], pending_close["digest"])
            else:
                definition = json.loads(session["definition_json"])
                entry = Decimal(position["entry_price"])
                target = Decimal(definition["take_profit"])
                try:
                    stop_level, stop_kind = self._risk_stop_level(db, session, position, definition)
                except SignalMonitorError:
                    reason = "Stop móvil PAPER no verificable; posición abierta para cierre manual"
                    db.execute("UPDATE paper_signal_sessions SET status='GAP',status_reason=? WHERE id=?",
                               (reason, session_id))
                    return "GAP"
                if position["side"] == "LONG":
                    stop_hit, target_hit = quote.mark <= stop_level, quote.mark >= entry * (1 + target)
                else:
                    stop_hit, target_hit = quote.mark >= stop_level, quote.mark <= entry * (1 - target)
                if not stop_hit and not target_hit:
                    return "OBSERVED"
                # La vela de decisión puede ser observada después del tick.
                # Sólo cabe una acción por sesión/vela, también si llega a la
                # vez que una salida por regla al cierre.
                step = INTERVALS[session["timeframe"]]
                candle_t = (quote.event_time_ns // 1_000_000_000 // step) * step
                existing = db.execute("""SELECT 1 FROM paper_signal_actions
                                         WHERE session_id=? AND candle_t=?""",
                                      (session_id, candle_t)).fetchone()
                pending = db.execute("""SELECT 1 FROM paper_signal_actions
                                        WHERE session_id=? AND status='PENDING'""", (session_id,)).fetchone()
                if existing or pending:
                    return "ACTION_ALREADY_PENDING"
                request_id = str(uuid4())
                digest = self.paper.digest({"session_id": session_id, "candle_t": candle_t,
                                            "action": "CLOSE", "position_id": position["id"],
                                            "request_id": request_id})
                db.execute("""INSERT INTO paper_signal_actions VALUES (?,?,?,?,?,?,?,?,?)""",
                           (session_id, candle_t, "CLOSE", request_id, digest, "PENDING",
                            position["id"], (("TRAILING_STOP_MARK_STREAM" if stop_kind == "TRAILING_STOP" else "STOP_MARK_STREAM")
                                              if stop_hit else "TARGET_MARK_STREAM"), now_ns))
                action_data = (session["account_id"], position["id"], request_id, digest)
        result = self._close_pending(session_id, *action_data, quote,
                                     error_prefix="Cierre PAPER pendiente tras ticker GRVT")
        return "ACTION_ALREADY_PENDING" if result == "WAIT_QUOTE" else result

    @staticmethod
    def _decision(db, session, bar, entry_match: bool, exit_match: bool | None,
                  definition: dict) -> tuple[str | None, str | None, str | None]:
        position_id = session["position_id"]
        if position_id is not None:
            position = db.execute("SELECT * FROM paper_perp_positions WHERE id=?", (position_id,)).fetchone()
            if position is None or position["closed_at_ns"] is not None:
                return None, "MANUALLY_CLOSED", position_id
            price = Decimal(position["entry_price"])
            target = Decimal(definition["take_profit"])
            try:
                stop_level, stop_kind = SignalMonitor._archived_stop_level(
                    db, session, position, definition, allow_next_uncommitted=True)
            except SignalMonitorError:
                return None, "TRAILING_STATE_UNVERIFIABLE", position_id
            if position["side"] == "LONG":
                stop_hit, target_hit = bar.c <= stop_level, bar.c >= price * (1 + target)
            else:
                stop_hit, target_hit = bar.c >= stop_level, bar.c <= price * (1 - target)
            if stop_hit:
                return "CLOSE", ("TRAILING_STOP_AT_CANDLE_CLOSE" if stop_kind == "TRAILING_STOP"
                                 else "STOP_AT_CANDLE_CLOSE"), position_id
            if target_hit:
                return "CLOSE", "TARGET_AT_CANDLE_CLOSE", position_id
            if exit_match:
                return "CLOSE", "EXIT_RULE_AT_CANDLE_CLOSE", position_id
            if "max_holding_bars" in definition:
                opened = db.execute("""SELECT candle_t FROM paper_signal_actions
                                       WHERE session_id=? AND action='OPEN' AND status='EXECUTED'
                                         AND position_id=? ORDER BY candle_t DESC LIMIT 1""",
                                    (session["id"], position_id)).fetchone()
                step = INTERVALS[session["timeframe"]]
                if (opened is None or type(opened["candle_t"]) is not int
                        or opened["candle_t"] > session["last_t"]
                        or (session["last_t"] - opened["candle_t"]) % step):
                    return None, "ENTRY_CLOCK_UNVERIFIABLE", position_id
                if bar.t - opened["candle_t"] >= definition["max_holding_bars"] * step:
                    return "CLOSE", "TIME_LIMIT_AT_CANDLE_CLOSE", position_id
            return None, None, position_id
        if entry_match:
            return "OPEN", "ENTRY_RULE_AT_CANDLE_CLOSE", None
        return None, None, None

    def _execute_pending(self, session_id: str) -> str | None:
        with self.paper.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            action = db.execute("""SELECT * FROM paper_signal_actions WHERE session_id=? AND status='PENDING'
                                   ORDER BY candle_t LIMIT 1""", (session_id,)).fetchone()
            if action is None:
                return None
            session = db.execute("SELECT * FROM paper_signal_sessions WHERE id=?", (session_id,)).fetchone()
            account = self.paper._account(db, session["account_id"])
            if session["status"] == "ACTIVE" and self._archive_gap(db, session):
                return "GAP"
            if session["status"] == "ACTIVE" and self._indicator_gap(db, session):
                return "GAP"
            step = INTERVALS[session["timeframe"]]
            expiry = (signal_action_expiry(action, step, self.clock_ns())
                      if session["status"] == "ACTIVE" else ("Sesión detenida", None))
            if expiry is not None:
                self._expire_action(db, session_id, action["request_id"], *expiry)
                return "EXPIRED"
            if action["action"] == "OPEN" and account["stopped_at"] is not None:
                db.execute("""UPDATE paper_signal_actions SET status='REJECTED',reason='Cuenta PAPER detenida'
                              WHERE request_id=? AND status='PENDING'""", (action["request_id"],))
                db.execute("""UPDATE paper_signal_sessions SET status='STOPPED',status_reason='Cuenta PAPER detenida'
                              WHERE id=?""", (session_id,))
                return "REJECTED"
            action_data = dict(action)
            session_data = dict(session)
        adapter = self.adapter_factory()
        try:
            instrument = adapter._instrument(session_data["symbol"])
            quote = PerpQuote.from_ticker(adapter.get_ticker(session_data["symbol"]),
                                          instrument, self.clock_ns())
            if action_data["action"] == "OPEN":
                if (action_data["reason"].endswith("_AT_CANDLE_CLOSE")
                        and quote.event_time_ns < (action_data["candle_t"] + step) * 1_000_000_000):
                    raise PaperPerpError("Ticker GRVT anterior al cierre de la vela de señal")
                rules = MarginRules.from_payload(adapter.get_margin_rules(session_data["symbol"]),
                                                 instrument, self.clock_ns())
                spec = InstrumentSpec.from_payload(adapter.get_instrument_spec(session_data["symbol"]),
                                                   instrument, self.clock_ns())
                if self.clock_ns() - quote.event_time_ns > 15_000_000_000:
                    raise PaperPerpError("Cotización GRVT envejeció durante la validación")
        except PaperPerpError as exc:
            raise SignalMonitorError(f"Datos GRVT no verificables para PAPER: {exc}") from exc
        if action_data["action"] == "OPEN":
            try:
                self.paper.open_position(session_data["account_id"], action_data["request_id"],
                                         action_data["digest"], session_data["symbol"],
                                         json.loads(session_data["definition_json"])["direction"],
                                         Decimal(session_data["auto_size"]),
                                         Decimal(session_data["auto_leverage"]), quote, rules, spec,
                                         origin_session_id=session_id)
            except PaperPerpError as exc:
                with self.paper.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute("""UPDATE paper_signal_actions SET status='REJECTED',reason=?
                                  WHERE request_id=? AND status='PENDING'""",
                               (str(exc), action_data["request_id"]))
                    db.execute("""UPDATE paper_signal_sessions SET status='STOPPED',status_reason=?
                                  WHERE id=? AND status='ACTIVE'""",
                               (f"Entrada PAPER rechazada: {exc}", session_id))
                return "REJECTED"
            return "EXECUTED"
        return self._close_pending(session_id, session_data["account_id"], action_data["position_id"],
                                   action_data["request_id"], action_data["digest"], quote,
                                   error_prefix="No se pudo cerrar PAPER")

    @staticmethod
    def _expire_action(db, session_id: str, request_id: str, reason: str,
                       session_reason: str | None) -> bool:
        changed = db.execute("""UPDATE paper_signal_actions SET status='EXPIRED',reason=?
                                WHERE session_id=? AND request_id=? AND status='PENDING'""",
                             (reason, session_id, request_id)).rowcount
        if changed and session_reason is not None:
            db.execute("""UPDATE paper_signal_sessions SET status='GAP',status_reason=?
                          WHERE id=? AND status='ACTIVE'""", (session_reason, session_id))
        return bool(changed)

    def _close_pending(self, session_id: str, account_id: str, position_id: str, request_id: str,
                       digest: str, quote: PerpQuote, *, error_prefix: str) -> str:
        try:
            self.paper.close_position(account_id, position_id, request_id, digest, quote,
                                      origin_session_id=session_id)
        except PaperCloseQuoteDeferred:
            return "WAIT_QUOTE"
        except PaperCloseWindowElapsed as exc:
            with self.paper.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if self._expire_action(db, session_id, request_id, *exc.reasons):
                    return "GAP"
                action = db.execute("SELECT status FROM paper_signal_actions WHERE request_id=?",
                                    (request_id,)).fetchone()
                return "EXECUTED" if action is not None and action["status"] == "EXECUTED" else "INACTIVE"
        except PaperPerpError as exc:
            raise SignalMonitorError(f"{error_prefix}: {exc}") from exc
        return "EXECUTED"

    def _terminal(self, session_id: str, expected_last_t: int, status: str, reason: str) -> dict:
        with self.paper.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("""UPDATE paper_signal_sessions SET status=?,status_reason=?
                          WHERE id=? AND status='ACTIVE' AND last_t=?""",
                       (status, reason, session_id, expected_last_t))
            if changed.rowcount:
                db.execute("""UPDATE paper_signal_actions SET status='EXPIRED',reason=?
                              WHERE session_id=? AND status='PENDING'""", (reason, session_id))
        return self.detail(session_id) | {"poll_result": status if changed.rowcount else "CHANGED"}


def create_signal_monitor_router(paper_path: Path, lab_path: Path, *, adapter_factory=GrvtAdapter,
                                 clock_ns=time.time_ns, monitor: SignalMonitor | None = None,
                                 worker: SignalWorker | None = None,
                                 risk_worker: SignalRiskWorker | None = None,
                                 funding_worker: AutoFundingWorker | None = None) -> APIRouter:
    monitor = monitor or SignalMonitor(paper_path, lab_path, adapter_factory=adapter_factory, clock_ns=clock_ns)
    router = APIRouter(prefix="/paper/perp/signals", tags=["paper-signals"])

    def local_only(request: Request) -> None:
        host = request.client.host if request.client else None
        if host not in LOCAL_HOSTS or request.url.hostname not in LOCAL_HOSTS:
            raise HTTPException(403, "Señales PAPER sólo locales")
        origin = request.headers.get("origin")
        if origin is not None and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Origen externo rechazado")

    def available(request: Request) -> None:
        local_only(request)
        try:
            kind = paper_perp_format(monitor.paper.path)
        except PaperPerpError as exc:
            raise HTTPException(422, str(exc)) from exc
        if kind == "encrypted_or_invalid" and paper_perp_access.get(monitor.paper.path) is None:
            raise HTTPException(423, "GRVT PAPER cifrado bloqueado; desbloquéalo en Ajustes")
        if kind in ("missing", "empty") and not (
            request.method == "GET" and request.url.path == "/paper/perp/signals/readiness"
        ):
            raise HTTPException(423, "Crea primero la base GRVT PAPER cifrada en Ajustes")

    def respond(action, *args):
        try:
            return action(*args)
        except SignalMonitorUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        except (SignalMonitorError, LabError, PaperPerpError) as exc:
            status = (423 if "cifrado bloqueado" in str(exc) else
                      404 if "no encontrada" in str(exc) or "no encontrado" in str(exc) else 422)
            raise HTTPException(status, str(exc)) from exc

    @router.post("", dependencies=[Depends(available)], status_code=201)
    def create(body: SignalCreate) -> dict:
        if body.execution_mode == "AUTO_PAPER" and (
                worker is None or not worker.snapshot()["running"]
                or risk_worker is None or not risk_worker.snapshot()["running"]
                or funding_worker is None or not funding_worker.snapshot()["running"]):
            raise HTTPException(503, "Observadores de velas, riesgo o financiación inactivos; AUTO PAPER no se puede activar")
        session = respond(monitor.create, body.account_id, body.version_id, body.dataset_id,
                          body.execution_mode, body.size, body.leverage, body.source_run_id)
        if worker is not None:
            worker.wake()
        if risk_worker is not None:
            risk_worker.wake()
        if funding_worker is not None:
            funding_worker.wake()
        return session

    @router.get("/readiness", dependencies=[Depends(available)])
    def readiness(response: Response, dataset_id: str = Query(min_length=1)) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return respond(monitor.readiness, dataset_id)

    @router.get("/worker", dependencies=[Depends(local_only)])
    def worker_status() -> dict:
        status = worker.snapshot() if worker is not None else {
            "running": False, "last_cycle_ns": None, "heartbeat_age_seconds": None,
            "last_error": None, "error_count": 0,
            "mode": "NOT_CONFIGURED", "execution": "PAPER_AUTO_OR_OBSERVE_NO_REAL_ORDERS"}
        return status | {"risk_stream": risk_worker.snapshot() if risk_worker is not None else {
            "running": False, "connected": False, "symbols": [], "last_frame_ns": None,
            "last_frame_age_seconds": None, "last_error": None, "error_count": 0,
            "mode": "NOT_CONFIGURED"},
            "funding": funding_worker.snapshot() if funding_worker is not None else {
                "running": False, "last_cycle_ns": None, "heartbeat_age_seconds": None,
                "last_error": None, "error_count": 0, "mode": "NOT_CONFIGURED",
                "execution": "PAPER_PUBLIC_FUNDING_ONLY"}}

    @router.get("", dependencies=[Depends(available)])
    def list_sessions(account_id: str) -> list[dict]:
        return respond(monitor.sessions, account_id)

    @router.get("/chart-markers", dependencies=[Depends(available)])
    def chart_markers(account_id: str, symbol: str, timeframe: str,
                      since_t: int = Query(ge=0)) -> dict:
        return respond(monitor.chart_markers, account_id, symbol, timeframe, since_t)

    @router.get("/chart-markers-page", dependencies=[Depends(available)])
    def chart_marker_page(response: Response, account_id: str, symbol: str, timeframe: str,
                          since_t: int = Query(ge=0), until_t: int = Query(ge=0),
                          after_t: int | None = Query(default=None, ge=0),
                          after_session_id: str = Query(default="", max_length=64),
                          limit: int = Query(default=500, ge=1, le=1000)) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return respond(monitor.chart_marker_page, account_id, symbol, timeframe,
                       since_t, until_t, after_t, after_session_id, limit)

    @router.get("/{session_id}", dependencies=[Depends(available)])
    def detail(session_id: str) -> dict:
        return respond(monitor.detail, session_id)

    @router.get("/{session_id}/candles", dependencies=[Depends(available)])
    def candle_page(session_id: str, response: Response,
                    after_t: int | None = Query(default=None, ge=0),
                    limit: int = Query(default=500, ge=1, le=1000)) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return respond(monitor.candle_page, session_id, after_t, limit)

    @router.get("/{session_id}/observations", dependencies=[Depends(available)])
    def observation_page(session_id: str, response: Response,
                         after_t: int | None = Query(default=None, ge=0),
                         limit: int = Query(default=500, ge=1, le=1000)) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return respond(monitor.observation_page, session_id, after_t, limit)

    @router.get("/{session_id}/verify", dependencies=[Depends(available)])
    def verify_source(session_id: str) -> dict:
        return respond(monitor.verify_source, session_id)

    @router.get("/{session_id}/comparison", dependencies=[Depends(available)])
    def comparison(session_id: str) -> dict:
        return respond(monitor.comparison, session_id)

    @router.get("/{session_id}/gap-analysis", dependencies=[Depends(available)])
    def gap_analysis(session_id: str) -> dict:
        return respond(monitor.gap_analysis, session_id)

    @router.get("/{session_id}/gap-analyses", dependencies=[Depends(available)])
    def gap_analyses(session_id: str) -> list[dict]:
        return respond(monitor.gap_analyses, session_id)

    @router.post("/{session_id}/poll", dependencies=[Depends(available)])
    def poll(session_id: str) -> dict:
        result = respond(monitor.poll, session_id)
        if funding_worker is not None:
            funding_worker.wake()
        return result

    @router.post("/{session_id}/stop", dependencies=[Depends(available)])
    def stop(session_id: str) -> dict:
        session = respond(monitor.stop, session_id)
        if worker is not None:
            worker.wake()
        return session

    return router
