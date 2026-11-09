"""Seguimiento PAPER spot MEXC de una versión LAB, sin órdenes reales.

Cada sesión es una cuenta virtual independiente. Sólo una vela cerrada nueva
puede cambiar su estado; la compra/venta se aproxima con bid/ask público
reciente y queda asentada atómicamente junto a la observación.
"""

from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, localcontext
import json
from pathlib import Path
from app.security.local_session import LocalStoreUnlock
import sqlite3
import time
from typing import TYPE_CHECKING, Callable, Literal
from uuid import UUID, uuid4

import sqlcipher3

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.automation.paper_dca import PaperDcaUnavailable, PaperDca
from app.automation.spot_paper_access import (
    SpotPaperAccessError, spot_paper_access, spot_paper_format,
)
from app.core.local_sqlite import ensure_private_sqlite
from app.core.paper_archive import full_archive_matches, observed_archive_matches
from app.core.paper_archive_chain import (ArchiveChainError, chained_archive_matches,
                                          next_archive_digest, verified_archive_page)
from app.lab.api import ENGINE_VERSION, LOCAL_HOSTS, download_binance
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
from app.marketdata.adapters.mexc import MexcAdapter

if TYPE_CHECKING:
    from app.automation.spot_strategy_worker import SpotStrategyWorker


class SpotStrategyError(ValueError):
    pass


class SpotStrategyUnavailable(SpotStrategyError):
    pass


class SpotPaperLocked(SpotStrategyError):
    pass


class SpotStrategyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version_id: str
    dataset_id: str
    source_run_id: str | None = None


class SpotResearchExport(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation: Literal["EXPORTAR_JSONL_SPOT_PAPER"]


class SpotPaperUnlock(LocalStoreUnlock):
    pass


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _event_view(row) -> dict:
    try:
        payload = json.loads(row["payload_json"])
        if not isinstance(payload, dict):
            raise ValueError("Evento sin objeto")
    except (ValueError, TypeError):
        payload = {"unreadable": True, "action": None}
    return {"candle_t": row["candle_t"], "kind": row["kind"],
            "observed_at_ns": row["observed_at_ns"], **payload,
            "observed_at_ns_exact": str(row["observed_at_ns"])}


def _action_chart_marker(row, payload: dict, step: int,
                         archived: Callable[[int], bool]) -> dict:
    action = payload["action"]
    kind = row["kind"]
    observed_ns = row["observed_at_ns"]
    execution_time = observed_ns // 1_000_000_000
    candle_time = execution_time // step * step
    quote = payload.get("quote") or {}
    quote_ns = quote.get("event_time_ns")
    if kind == "MANUAL_CLOSE":
        label = "Venta PAPER manual"
    elif kind == "INTRABAR_EXIT":
        label = "Venta PAPER intravela observada"
    else:
        label = "Compra PAPER" if action["side"] == "BUY" else "Venta PAPER"
    return {"time": candle_time, "execution_time": execution_time,
            "kind": action["side"], "label": label,
            "observed_at_ns": observed_ns, "observed_at_ns_exact": str(observed_ns),
            "source_candle_t": row["candle_t"] if kind == "OBSERVATION" else None,
            "price": action["price"], "reason": action["reason"],
            "quote_bid": quote.get("bid"), "quote_ask": quote.get("ask"),
            "quote_time_ns_exact": str(quote_ns) if type(quote_ns) is int else None,
            "pending_candle": not archived(candle_time)}


class SpotStrategyPaper:
    RISK_COVERAGE_NS = 30_000_000_000
    INVALID_STATE_REASON = "Estado PAPER spot incoherente; sesión detenida sin operar"
    CHANGED_STATE_REASON = "Estado PAPER spot cambió durante la observación; sesión detenida"

    def __init__(self, path: Path, lab_path: Path, *, adapter_factory=MexcAdapter,
                 clock_ns=time.time_ns, passphrase: str | None = None,
                 allow_create: bool = True):
        self.path = Path(path).expanduser().absolute()
        self.lab = LabStore(lab_path, allow_create=False)
        self.adapter_factory = adapter_factory
        self.clock_ns = clock_ns
        self.passphrase = passphrase
        self.allow_create = allow_create

    def current_passphrase(self) -> str | None:
        return self.passphrase if self.passphrase is not None else spot_paper_access.get(self.path)

    @contextmanager
    def connect(self):
        kind = spot_paper_format(self.path)
        key = self.current_passphrase()
        if kind == "encrypted_or_invalid" and key is None:
            raise SpotPaperLocked("Spot PAPER cifrado bloqueado; desbloquéalo en Ajustes")
        if kind in ("missing", "empty") and not self.allow_create:
            raise SpotPaperLocked("Crea primero la base Spot PAPER cifrada en Ajustes")
        if kind == "plain" and key is not None:
            raise SpotStrategyError("Clave Spot PAPER activa para una base sin cifrar")
        ensure_private_sqlite(self.path)
        driver = sqlcipher3 if key is not None else sqlite3
        db = driver.connect(self.path, timeout=5)
        try:
            db.row_factory = driver.Row
            if key is not None:
                db.execute("PRAGMA key = '" + key.replace("'", "''") + "'")
            db.execute("PRAGMA foreign_keys=ON")
            try:
                version = db.execute("PRAGMA user_version").fetchone()[0]
            except sqlcipher3.DatabaseError:
                raise SpotStrategyError("Spot PAPER no se pudo abrir; contraseña o formato incompatibles") from None
            if version in (0, 1, 2, 3, 4, 5):
                db.execute("BEGIN IMMEDIATE")
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version == 0:
                    db.execute("""CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, version_id TEXT NOT NULL, dataset_id TEXT NOT NULL,
                    dataset_hash TEXT NOT NULL, engine_version TEXT NOT NULL,
                    symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
                    definition_json TEXT NOT NULL, history_json TEXT NOT NULL,
                    state_json TEXT NOT NULL, started_at_ns INTEGER NOT NULL,
                    last_t INTEGER NOT NULL, status TEXT NOT NULL,
                    status_reason TEXT, last_quote_json TEXT, source_run_id TEXT
                )""")
                    db.execute("""CREATE TABLE IF NOT EXISTS events (
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    candle_t INTEGER NOT NULL, kind TEXT NOT NULL,
                    observed_at_ns INTEGER NOT NULL, payload_json TEXT NOT NULL,
                    PRIMARY KEY(session_id,candle_t,kind)
                )""")
                    db.execute("PRAGMA user_version=2")
                    version = 2
                if version == 1:
                    db.execute("ALTER TABLE sessions ADD COLUMN source_run_id TEXT")
                    db.execute("PRAGMA user_version=2")
                    version = 2
                if version == 2:
                    db.execute("""CREATE TABLE observed_candles (
                        session_id TEXT NOT NULL REFERENCES sessions(id),
                        candle_t INTEGER NOT NULL, candle_json TEXT NOT NULL,
                        PRIMARY KEY(session_id,candle_t)
                    )""")
                    for session in db.execute("SELECT id,history_json FROM sessions"):
                        history = json.loads(session["history_json"])
                        by_time = {bar["t"]: bar for bar in history}
                        for event in db.execute("""SELECT candle_t,payload_json FROM events
                                                 WHERE session_id=? AND kind='OBSERVATION'
                                                 ORDER BY candle_t""", (session["id"],)):
                            candle = json.loads(event["payload_json"]).get("candle")
                            if by_time.get(event["candle_t"]) != candle:
                                raise SpotStrategyError("Vela PAPER archivada distinta de la observación")
                            db.execute("INSERT INTO observed_candles VALUES (?,?,?)",
                                       (session["id"], event["candle_t"], _json(candle)))
                    db.execute("PRAGMA user_version=3")
                    version = 3
                if version == 3:
                    db.execute("ALTER TABLE sessions ADD COLUMN indicator_state_json TEXT")
                    for session in db.execute("""SELECT id,definition_json,history_json,engine_version
                                                 FROM sessions""").fetchall():
                        definition = json.loads(session["definition_json"])
                        history = json.loads(session["history_json"])
                        precision = paper_precision_for_engine(session["engine_version"])
                        snapshot = build_paper_indicator_state(definition, history, precision)
                        db.execute("UPDATE sessions SET indicator_state_json=? WHERE id=?",
                                   (_json(snapshot), session["id"]))
                    db.execute("PRAGMA user_version=4")
                    version = 4
                if version == 4:
                    db.execute("""CREATE TABLE session_candles (
                        session_id TEXT NOT NULL REFERENCES sessions(id),
                        candle_t INTEGER NOT NULL, candle_json TEXT NOT NULL,
                        origin TEXT NOT NULL CHECK(origin IN ('SEED','OBSERVED')),
                        PRIMARY KEY(session_id,candle_t)
                    )""")
                    for session in db.execute("SELECT id,history_json FROM sessions").fetchall():
                        if not observed_archive_matches(db, session["id"], session["history_json"], "spot"):
                            raise SpotStrategyError("Archivo observado PAPER Spot divergente")
                        history = json.loads(session["history_json"])
                        observed = {row["candle_t"] for row in db.execute(
                            "SELECT candle_t FROM observed_candles WHERE session_id=?", (session["id"],))}
                        for candle in history:
                            db.execute("INSERT INTO session_candles VALUES (?,?,?,?)",
                                       (session["id"], candle["t"], _json(candle),
                                        "OBSERVED" if candle["t"] in observed else "SEED"))
                        if not full_archive_matches(db, session["id"], session["history_json"], "spot"):
                            raise SpotStrategyError("Archivo completo PAPER Spot divergente")
                    db.execute("PRAGMA user_version=5")
                    version = 5
                if version == 5:
                    db.execute("""CREATE TABLE IF NOT EXISTS session_archive_chain (
                        session_id TEXT NOT NULL,
                        candle_t INTEGER NOT NULL, digest TEXT NOT NULL,
                        PRIMARY KEY(session_id,candle_t),
                        FOREIGN KEY(session_id,candle_t)
                            REFERENCES session_candles(session_id,candle_t)
                    )""")
                    db.execute("DELETE FROM session_archive_chain")
                    for session in db.execute("SELECT id,history_json FROM sessions").fetchall():
                        if not full_archive_matches(db, session["id"], session["history_json"], "spot"):
                            raise SpotStrategyError("Archivo completo PAPER Spot divergente")
                        previous = None
                        rows = db.execute("""SELECT candle_t,candle_json,origin FROM session_candles
                                             WHERE session_id=? ORDER BY candle_t""", (session["id"],))
                        for candle in rows:
                            previous = next_archive_digest(previous, session["id"], candle["candle_t"],
                                                           candle["origin"], candle["candle_json"])
                            db.execute("INSERT INTO session_archive_chain VALUES (?,?,?)",
                                       (session["id"], candle["candle_t"], previous))
                    db.execute("PRAGMA user_version=6")
                    version = 6
                db.commit()
            if version != 6:
                raise SpotStrategyError(f"Versión PAPER spot no compatible: {version}")
            self.path.chmod(0o600)
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _valid_state(state: object, definition: dict, last_t: int, step: int) -> bool:
        """Comprueba el libro de caja antes de valorar o decidir una acción PAPER."""
        if not isinstance(state, dict) or type(state.get("closed_trades")) is not int or state["closed_trades"] < 0:
            return False
        try:
            with localcontext() as context:
                context.prec = 80
                cash = Decimal(state["cash"])
                units = Decimal(state["units"])
                realized = Decimal(state["realized_pnl"])
                initial = Decimal(definition["initial_capital"])
                if not all(value.is_finite() for value in (cash, units, realized, initial)):
                    return False
                if cash < 0 or units < 0 or initial <= 0:
                    return False
                if units == 0:
                    if any(state.get(key) is not None for key in (
                            "entry_price", "entry_cost", "trailing_extreme", "entry_signal_t")):
                        return False
                    cost = Decimal(0)
                else:
                    price = Decimal(state["entry_price"])
                    cost = Decimal(state["entry_cost"])
                    if not price.is_finite() or not cost.is_finite() or price <= 0 or cost <= 0:
                        return False
                    if "trailing_stop" in definition:
                        extreme = Decimal(state["trailing_extreme"])
                        if not extreme.is_finite() or extreme < price:
                            return False
                    elif state.get("trailing_extreme") is not None:
                        return False
                    if not SpotStrategyPaper._entry_clock_valid(state, definition, last_t, step):
                        return False
                # Las tres magnitudes se redondean a 80 cifras en operaciones
                # distintas. Su error acumulado puede ser sub-unidad en la
                # última cifra, pero nunca justificar una diferencia monetaria.
                return abs(cash + cost - realized - initial) <= initial * Decimal("1e-70")
        except (KeyError, TypeError, ValueError, InvalidOperation, ArithmeticError):
            return False

    @staticmethod
    def _ledger_matches(db, row, state: dict, definition: dict) -> bool:
        """Reconstruye caja y unidades desde acciones asentadas, sin cotizaciones externas."""
        try:
            with localcontext() as context:
                context.prec = 80
                initial = Decimal(definition["initial_capital"])
                cash, units, cost, realized = initial, Decimal(0), Decimal(0), Decimal(0)
                entry_price = None
                closes = 0
                tolerance = initial * Decimal("1e-70")
                for event in db.execute(
                        "SELECT kind,payload_json FROM events WHERE session_id=? ORDER BY rowid",
                        (row["id"],)):
                    payload = json.loads(event["payload_json"])
                    if not isinstance(payload, dict):
                        return False
                    action = payload.get("action")
                    if action is None:
                        continue
                    if not isinstance(action, dict) or event["kind"] not in (
                            "OBSERVATION", "INTRABAR_EXIT", "MANUAL_CLOSE"):
                        return False
                    change = Decimal(action["cash_change"])
                    action_units = Decimal(action["units"])
                    price = Decimal(action["price"])
                    fee = Decimal(action["fee"])
                    if not all(v.is_finite() for v in (change, action_units, price, fee)):
                        return False
                    if action_units <= 0 or price <= 0 or fee < 0:
                        return False
                    if action["side"] == "BUY":
                        if units != 0 or change >= 0 or action.get("pnl") is not None or event["kind"] != "OBSERVATION":
                            return False
                        cash += change
                        units, cost, entry_price = action_units, -change, price
                    elif action["side"] == "SELL":
                        pnl = Decimal(action["pnl"])
                        if (units <= 0 or action_units != units or change < 0
                                or not pnl.is_finite() or abs(pnl - (change - cost)) > tolerance):
                            return False
                        cash += change
                        realized += pnl
                        units, cost, entry_price = Decimal(0), Decimal(0), None
                        closes += 1
                    else:
                        return False
                return (abs(cash - Decimal(state["cash"])) <= tolerance
                        and abs(units - Decimal(state["units"])) <= tolerance
                        and abs(realized - Decimal(state["realized_pnl"])) <= tolerance
                        and closes == state["closed_trades"]
                        and (entry_price is None and state.get("entry_price") is None
                             or entry_price is not None and Decimal(state["entry_price"]) == entry_price)
                        and (cost == 0 and state.get("entry_cost") is None
                             or cost > 0 and abs(Decimal(state["entry_cost"]) - cost) <= tolerance))
        except (KeyError, TypeError, ValueError, InvalidOperation, ArithmeticError):
            return False

    def _valid_session_state(self, db, row, state: object, definition: dict) -> bool:
        try:
            indicator = json.loads(row["indicator_state_json"])
            precision = paper_precision_for_engine(row["engine_version"])
            history = json.loads(row["history_json"])
        except (ValueError, TypeError, KeyError, LabError):
            return False
        try:
            return (self._valid_state(state, definition, row["last_t"], INTERVALS[row["timeframe"]])
                    and self._ledger_matches(db, row, state, definition)
                    and observed_archive_matches(db, row["id"], row["history_json"], "spot")
                    and full_archive_matches(db, row["id"], row["history_json"], "spot")
                    and chained_archive_matches(db, row["id"])
                    and paper_indicator_state_matches(indicator, definition, history, precision))
        except (sqlite3.DatabaseError, sqlcipher3.DatabaseError):
            return False

    def _summary(self, row, db) -> dict:
        try:
            state = json.loads(row["state_json"])
        except (ValueError, TypeError):
            state = None
        try:
            quote = self._saved_quote(row)
            quote_valid = True
        except (KeyError, TypeError, ValueError, ArithmeticError):
            quote = None
            quote_valid = False
        definition = json.loads(row["definition_json"])
        quote_time_ns = quote.get("event_time_ns") if quote else None
        now_ns = self.clock_ns()
        quote_fresh = (type(quote_time_ns) is int and quote_time_ns <= now_ns
                       and now_ns - quote_time_ns <= 15_000_000_000)
        with localcontext() as context:
            context.prec = 80
            if not quote_valid or not self._valid_session_state(db, row, state, definition):
                valuation = {"basis": "UNAVAILABLE_INVALID_STATE", "estimated_exit_price": None,
                             "estimated_exit_fee": None, "estimated_proceeds": None,
                             "equity_if_closed": None, "unrealized_pnl_if_closed": None,
                             "total_pnl_if_closed": None, "quote_event_time_ns": None}
            elif (cash := Decimal(state["cash"])) >= 0 and (units := Decimal(state["units"])) == 0:
                valuation = {"basis": "CASH_ONLY", "estimated_exit_price": None,
                             "estimated_exit_fee": "0", "estimated_proceeds": "0",
                             "equity_if_closed": str(cash), "unrealized_pnl_if_closed": "0",
                             "total_pnl_if_closed": str(Decimal(state["realized_pnl"])),
                             "quote_event_time_ns": None}
            elif quote is None:
                valuation = {"basis": "UNAVAILABLE_NO_QUOTE", "estimated_exit_price": None,
                             "estimated_exit_fee": None, "estimated_proceeds": None,
                             "equity_if_closed": None, "unrealized_pnl_if_closed": None,
                             "total_pnl_if_closed": None, "quote_event_time_ns": None}
            elif not quote_fresh:
                valuation = {"basis": "UNAVAILABLE_STALE_QUOTE", "estimated_exit_price": None,
                             "estimated_exit_fee": None, "estimated_proceeds": None,
                             "equity_if_closed": None, "unrealized_pnl_if_closed": None,
                             "total_pnl_if_closed": None,
                             "quote_event_time_ns": quote_time_ns}
            else:
                price = Decimal(quote["bid"]) * (1 - Decimal(definition["slippage_rate"]))
                gross = units * price
                exit_fee = gross * Decimal(definition["fee_rate"])
                proceeds = gross - exit_fee
                unrealized = proceeds - Decimal(state["entry_cost"])
                valuation = {"basis": "ESTIMATED_POST_CLOSE_BID_COSTS_NOT_EXECUTED",
                             "estimated_exit_price": str(price),
                             "estimated_exit_fee": str(exit_fee),
                             "estimated_proceeds": str(proceeds),
                             "equity_if_closed": str(cash + proceeds),
                             "unrealized_pnl_if_closed": str(unrealized),
                             "total_pnl_if_closed": str(Decimal(state["realized_pnl"]) + unrealized),
                             "quote_event_time_ns": quote["event_time_ns"]}
        return {key: row[key] for key in ("id", "version_id", "dataset_id", "dataset_hash", "source_run_id",
                                          "engine_version", "symbol", "timeframe", "started_at_ns",
                                          "last_t", "status", "status_reason")} | {
            "state": state, "last_quote": quote, "valuation": valuation,
            "execution": "LOCAL_SPOT_PAPER_BID_ASK_APPROXIMATION_NO_REAL_ORDERS",
        }

    def _initial_history(self, dataset_id: str, now: int):
        meta, raw = self.lab.dataset(dataset_id)
        if (not meta["verified_public"] or meta["source"] != "mexc-spot-public"
                or meta["symbol"] not in MexcAdapter.known_pairs
                or not meta["symbol"].endswith("/USDT")
                or meta["timeframe"] not in INTERVALS):
            raise SpotStrategyError("Se requiere un dataset MEXC Spot/USDT público y verificado")
        bars, gaps = validate_candles(raw, meta["timeframe"], now=now)
        step = INTERVALS[meta["timeframe"]]
        if gaps or meta["gaps"] or len(bars) >= 5000 or bars[-1].t != (now // step) * step - step:
            raise SpotStrategyError("El histórico debe acabar en la última vela cerrada, sin huecos y bajo 5000 velas")
        return meta, bars

    def readiness(self, dataset_id: str) -> dict:
        """Elegibilidad del histórico ahora; crear revalida antes de escribir."""
        try:
            meta, bars = self._initial_history(dataset_id, self.clock_ns() // 1_000_000_000)
            return {"ready": True, "reason": "Histórico al día", "dataset_id": dataset_id,
                    "last_t": bars[-1].t, "symbol": meta["symbol"], "timeframe": meta["timeframe"]}
        except (LabError, SpotStrategyError) as exc:
            return {"ready": False, "reason": str(exc), "dataset_id": dataset_id}

    def create(self, version_id: str, dataset_id: str, source_run_id: str | None = None) -> dict:
        now_ns = self.clock_ns()
        now = now_ns // 1_000_000_000
        definition = validate_strategy(self.lab.version(version_id)["definition"])
        if definition["direction"] != "LONG":
            raise SpotStrategyError("Spot PAPER no permite SHORT sin préstamo; usa GRVT PAPER")
        meta, bars = self._initial_history(dataset_id, now)
        if source_run_id is not None:
            reference, _, _ = paper_lab_reference(self.lab, dataset_id, meta["hash"],
                                                  version_id, source_run_id)
            verification = verify_backtest_run(self.lab, self.lab.run(reference["id"]))
            if not verification["verified"]:
                raise SpotStrategyError("El backtest LAB de origen no está verificado: " + verification["reason"])
        validate_paper_timeframe_capacity(definition, meta["timeframe"], bars[0].t)
        session_id = str(uuid4())
        state = {"cash": definition["initial_capital"], "units": "0", "entry_price": None,
                 "entry_cost": None, "trailing_extreme": None,
                 "realized_pnl": "0", "closed_trades": 0}
        indicator_state = build_paper_indicator_state(
            definition, [bar.json() for bar in bars], paper_precision_for_engine(ENGINE_VERSION))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("""SELECT 1 FROM sessions WHERE version_id=? AND dataset_id=?
                             AND status='ACTIVE' LIMIT 1""", (version_id, dataset_id)).fetchone():
                raise SpotStrategyError("Esta versión y dataset ya tienen una sesión spot activa")
            db.execute("""INSERT INTO sessions
                          (id,version_id,dataset_id,dataset_hash,engine_version,symbol,timeframe,
                           definition_json,history_json,state_json,started_at_ns,last_t,status,
                           status_reason,last_quote_json,source_run_id,indicator_state_json)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (session_id, version_id, dataset_id, meta["hash"], ENGINE_VERSION,
                        meta["symbol"], meta["timeframe"], _json(definition),
                        _json([bar.json() for bar in bars]), _json(state), now_ns,
                        bars[-1].t, "ACTIVE", None, None, source_run_id, _json(indicator_state)))
            previous = None
            for bar in bars:
                candle_json = _json(bar.json())
                db.execute("INSERT INTO session_candles VALUES (?,?,?,?)",
                           (session_id, bar.t, candle_json, "SEED"))
                previous = next_archive_digest(previous, session_id, bar.t, "SEED", candle_json)
                db.execute("INSERT INTO session_archive_chain VALUES (?,?,?)",
                           (session_id, bar.t, previous))
        return self.detail(session_id)

    def sessions(self, *, session_id: str | None = None) -> list[dict]:
        if spot_paper_format(self.path) in ("missing", "empty"):
            return []
        with self.connect() as db:
            rows = (db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)) if session_id is not None
                    else db.execute("SELECT * FROM sessions ORDER BY started_at_ns DESC,id"))
            return [self._summary(row, db) for row in rows]

    def detail(self, session_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise SpotStrategyError("Sesión PAPER spot no encontrada")
            events = []
            for event in db.execute("SELECT * FROM events WHERE session_id=? ORDER BY candle_t,kind",
                                    (session_id,)):
                events.append(_event_view(event))
            return self._detail_base(db, row) | {"events": events}

    def _detail_base(self, db, row) -> dict:
        definition = json.loads(row["definition_json"])
        history = json.loads(row["history_json"])
        coverage = None
        if uses_higher_timeframe(definition):
            bars, _ = validate_candles(history, row["timeframe"],
                                       now=row["last_t"] + 2 * INTERVALS[row["timeframe"]])
            coverage = higher_timeframe_coverage(bars, definition, row["timeframe"])
        return self._summary(row, db) | {"coverage": coverage,
                                         "capacity_remaining": max(0, 5000 - len(history))}

    def summary(self, session_id: str) -> dict:
        """Resumen para la vista: nunca serializa todo el diario de eventos."""
        with self.connect() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise SpotStrategyError("Sesión PAPER spot no encontrada")
            event_count = db.execute("SELECT count(*) FROM events WHERE session_id=?", (session_id,)).fetchone()[0]
            return self._detail_base(db, row) | {"event_count": event_count}

    def verify_source(self, session_id: str) -> dict:
        """Contrasta el origen archivado sin alterar señales ni capital PAPER."""
        with self.connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise SpotStrategyError("Sesión PAPER spot no encontrada")
            session = dict(row)
        try:
            definition = json.loads(session["definition_json"])
            history = json.loads(session["history_json"])
        except (ValueError, TypeError):
            return {"verified": False, "status": "mismatch", "checks": {},
                    "reason": "Definición o histórico PAPER ilegible"}
        return verify_paper_source(self.lab, session, definition, history)

    def gap_analysis(self, session_id: str) -> dict:
        """Consulta histórica posterior a GAP; nunca cambia la sesión PAPER."""
        with self.connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise SpotStrategyError("Sesión PAPER spot no encontrada")
            session = dict(row)
        definition = json.loads(session["definition_json"])
        history = json.loads(session["history_json"])
        source = verify_paper_source(self.lab, session, definition, history)
        if not source["verified"]:
            raise SpotStrategyError("Origen PAPER no verificable; no se reconstruyen señales")
        now = self.clock_ns() // 1_000_000_000
        start, end = gap_download_range(session, now)
        downloaded = download_binance(session["symbol"], session["timeframe"],
                                      start, end, adapter=self.adapter_factory(), now=now)
        with self.connect() as db:
            current = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if current is None or dict(current) != session:
                raise SpotStrategyError("La sesión PAPER cambió durante la consulta del hueco")
        if not verify_paper_source(self.lab, session, definition, history)["verified"]:
            raise SpotStrategyError("El origen PAPER cambió durante la consulta del hueco")
        report = analyze_paper_gap(session, definition, history, downloaded, now=now,
                                   source="mexc-spot-public")
        snapshot = {key: session[key] for key in ("id", "status", "last_t", "timeframe",
                                                  "symbol", "dataset_id", "dataset_hash",
                                                  "version_id", "engine_version")}
        snapshot.update({"definition": definition, "history": history,
                         "reconstruction_now": now})
        saved = self.lab.save_paper_gap_report("MEXC_SPOT", snapshot, downloaded, report)
        return report | {"archive": {key: saved[key] for key in ("id", "hash", "created_at")}}

    def gap_analyses(self, session_id: str) -> list[dict]:
        self.detail(session_id)
        return [row for row in self.lab.paper_gap_reports(session_id)
                if row["venue"] == "MEXC_SPOT"]

    def comparison(self, session_id: str) -> dict:
        """Vincula una sesión PAPER con su LAB exacto, sin equiparar retornos."""
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
        observations = [row for row in session["events"] if row["kind"] == "OBSERVATION"]
        actions = [row["action"] for row in session["events"] if row.get("action") is not None]
        with self.connect() as db:
            db.execute("BEGIN")
            archived = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if archived is None or archived["last_t"] != session["last_t"]:
                signal_replay = {"verified": False, "status": "stale_snapshot",
                                 "reason": "PAPER cambió durante la comparación",
                                 "checked_observations": 0, "first_mismatch_t": None}
            else:
                definition = json.loads(archived["definition_json"])
                signal_replay = verify_paper_signals(
                    self.lab, dict(archived), definition,
                    json.loads(archived["history_json"]),
                    [{"candle_t": row["candle_t"], "candle": row.get("candle"),
                      "entry_match": row.get("entry_match"), "exit_match": row.get("exit_match"),
                      "entry": row.get("entry"), "exit": row.get("exit")}
                     for row in observations],
                )
                if signal_replay["verified"] and recent_supported(definition, archived["engine_version"]):
                    segmented = verify_archived_paper_signals(
                        self.lab, db, dict(archived), definition, "spot")
                    if not segmented["verified"]:
                        signal_replay = {**segmented, "reason": "Replay segmentado archivado divergente: "
                                         + segmented["reason"]}
        entry_signals = sum(bool(row.get("entry_match")) for row in observations)
        frequency = signal_frequency(historical, saved, signal_replay,
                                     observed_candles=len(observations),
                                     entry_signals=entry_signals,
                                     paper_signal_times=[row["candle_t"] for row in observations
                                                         if row.get("entry_match")],
                                     paper_observed_times=[row["candle_t"] for row in observations],
                                     step_seconds=INTERVALS[session["timeframe"]])
        return {"session_id": session_id, "version_id": session["version_id"],
                "source_run_id": session["source_run_id"],
                "dataset_id": session["dataset_id"], "dataset_hash": session["dataset_hash"],
                "status": session["status"], "historical": historical,
                "signal_replay": signal_replay, "signal_frequency": frequency,
                "paper": {"observed_candles": len(observations),
                          "entry_signals": entry_signals,
                          "paper_buys": sum(action["side"] == "BUY" for action in actions),
                          "paper_sells": sum(action["side"] == "SELL" for action in actions),
                          "open_units": session["state"]["units"],
                          "realized_pnl_usdt": session["state"]["realized_pnl"],
                          "estimated_unrealized_if_closed_usdt": session["valuation"]["unrealized_pnl_if_closed"]},
                "comparison_basis": "NOT_DIRECTLY_COMPARABLE",
                "reason": "Periodo, capital, precio de ejecución y cobertura de stop pueden diferir; no se calcula una diferencia de rentabilidad LAB-PAPER"}

    def chart(self, session_id: str) -> dict:
        """Velas conservadas y marcas de acciones PAPER con procedencia explícita.

        La señal conserva el anclaje a la vela evaluada. Las acciones conservan
        el intervalo para los consumidores antiguos y añaden el instante de
        ejecución ficticia, independiente de la vela y de la hora del ticker.
        """
        with self.connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise SpotStrategyError("Sesión PAPER spot no encontrada")
            bars = json.loads(row["history_json"])
            times = {bar["t"] for bar in bars}
            step = INTERVALS[row["timeframe"]]
            initial_last_t = (row["started_at_ns"] // 1_000_000_000 // step) * step - step
            markers = ([{"time": initial_last_t, "kind": "START", "label": "Inicio PAPER",
                         "observed_at_ns": row["started_at_ns"]}]
                       if initial_last_t in times else [])
            for event in db.execute("""SELECT candle_t,kind,observed_at_ns,payload_json FROM events
                                      WHERE session_id=? AND kind='OBSERVATION'
                                      ORDER BY candle_t""", (session_id,)):
                if event["candle_t"] not in times:
                    continue
                payload = json.loads(event["payload_json"])
                action = payload.get("action")
                if payload["entry_match"]:
                    entry_status = payload.get("entry_status") or (
                        "EXECUTED_PAPER" if action and action.get("side") == "BUY" else "UNSPECIFIED")
                    skipped = entry_status != "EXECUTED_PAPER"
                    markers.append({"time": event["candle_t"],
                                    "kind": "SIGNAL_SKIPPED" if skipped else "SIGNAL",
                                    "label": "Señal sin entrada" if skipped else "Señal LONG",
                                    "reason": entry_status,
                                    "observed_at_ns": event["observed_at_ns"]})
                if action is not None:
                    markers.append(_action_chart_marker(event, payload, step, times.__contains__))
            for event in db.execute("""SELECT candle_t,kind,observed_at_ns,payload_json FROM events
                                      WHERE session_id=? AND kind IN ('INTRABAR_EXIT','MANUAL_CLOSE')
                                      ORDER BY observed_at_ns""", (session_id,)):
                payload = json.loads(event["payload_json"])
                markers.append(_action_chart_marker(event, payload, step, times.__contains__))
            return {"session_id": session_id, "symbol": row["symbol"],
                    "timeframe": row["timeframe"], "candles": bars, "markers": markers,
                    "initial_last_t": initial_last_t,
                    "execution": "PAPER_ACTION_AFTER_SIGNAL_CANDLE_CLOSE_NO_REAL_ORDER"}

    def candle_page(self, session_id: str, after_t: int | None = None, limit: int = 500) -> dict:
        if type(limit) is not int or not 1 <= limit <= 1000 or (after_t is not None and
                (type(after_t) is not int or after_t < 0)):
            raise SpotStrategyError("Página de velas PAPER inválida")
        with self.connect() as db:
            db.execute("BEGIN")
            session = db.execute("SELECT symbol,timeframe,last_t FROM sessions WHERE id=?", (session_id,)).fetchone()
            if session is None:
                raise SpotStrategyError("Sesión PAPER spot no encontrada")
            event_state = db.execute("""SELECT count(*) AS count,max(observed_at_ns) AS last_ns
                                        FROM events WHERE session_id=?""", (session_id,)).fetchone()
            try:
                page = verified_archive_page(db, session_id, "spot", after_t, limit)
            except ArchiveChainError as exc:
                raise SpotStrategyError(str(exc)) from exc
            return {"session_id": session_id, "symbol": session["symbol"],
                    "timeframe": session["timeframe"],
                    "snapshot": {"last_t": session["last_t"], "event_count": event_state["count"],
                                 "last_event_ns": event_state["last_ns"]},
                    "candles": page["candles"], "next_after_t": page["next_after_t"],
                    "has_more": page["has_more"]}

    def event_page(self, session_id: str, after_t: int | None = None,
                   after_kind: str = "", limit: int = 500) -> dict:
        if (type(limit) is not int or not 1 <= limit <= 1000 or
                (after_t is not None and (type(after_t) is not int or after_t < 0)) or
                not isinstance(after_kind, str) or len(after_kind) > 32 or
                (after_t is None and after_kind)):
            raise SpotStrategyError("Página de eventos PAPER inválida")
        with self.connect() as db:
            session = db.execute("SELECT symbol,timeframe FROM sessions WHERE id=?", (session_id,)).fetchone()
            if session is None:
                raise SpotStrategyError("Sesión PAPER spot no encontrada")
            rows = db.execute("""SELECT candle_t,kind,observed_at_ns,payload_json FROM events
                                 WHERE session_id=? AND (candle_t>? OR (candle_t=? AND kind>?))
                                 ORDER BY candle_t,kind LIMIT ?""",
                              (session_id, after_t if after_t is not None else -1,
                               after_t if after_t is not None else -1, after_kind, limit + 1)).fetchall()
            visible = rows[:limit]
            events = [_event_view(row) for row in visible]
            has_more = len(rows) > limit
            return {"session_id": session_id, "symbol": session["symbol"],
                    "timeframe": session["timeframe"], "events": events,
                    "next_after_t": visible[-1]["candle_t"] if has_more else None,
                    "next_after_kind": visible[-1]["kind"] if has_more else None,
                    "has_more": has_more}

    def recent_event_page(self, session_id: str, before_t: int | None = None,
                          before_kind: str = "", limit: int = 30) -> dict:
        if (type(limit) is not int or not 1 <= limit <= 100 or
                (before_t is not None and (type(before_t) is not int or before_t < 0)) or
                not isinstance(before_kind, str) or len(before_kind) > 32 or
                (before_t is None and before_kind)):
            raise SpotStrategyError("Página reciente de eventos PAPER inválida")
        with self.connect() as db:
            db.execute("BEGIN")
            session = db.execute("SELECT symbol,timeframe FROM sessions WHERE id=?", (session_id,)).fetchone()
            if session is None:
                raise SpotStrategyError("Sesión PAPER spot no encontrada")
            rows = db.execute("""SELECT candle_t,kind,observed_at_ns,payload_json FROM events
                                 WHERE session_id=? AND (candle_t<? OR (candle_t=? AND kind<?))
                                 ORDER BY candle_t DESC,kind DESC LIMIT ?""",
                              (session_id, before_t if before_t is not None else 1 << 62,
                               before_t if before_t is not None else 1 << 62,
                               before_kind, limit + 1)).fetchall()
            visible = rows[:limit]
            has_more = len(rows) > limit
            return {"session_id": session_id, "symbol": session["symbol"],
                    "timeframe": session["timeframe"],
                    "events": [_event_view(row) for row in visible],
                    "next_before_t": visible[-1]["candle_t"] if has_more else None,
                    "next_before_kind": visible[-1]["kind"] if has_more else None,
                    "has_more": has_more}

    def chart_marker_page(self, session_id: str, after_t: int | None = None,
                          after_kind: str = "", limit: int = 500) -> dict:
        if (type(limit) is not int or not 1 <= limit <= 1000 or
                (after_t is not None and (type(after_t) is not int or after_t < 0)) or
                not isinstance(after_kind, str) or len(after_kind) > 32 or
                (after_t is None and after_kind)):
            raise SpotStrategyError("Página de marcas PAPER inválida")
        with self.connect() as db:
            db.execute("BEGIN")
            session = db.execute("SELECT symbol,timeframe,started_at_ns,last_t FROM sessions WHERE id=?",
                                 (session_id,)).fetchone()
            if session is None:
                raise SpotStrategyError("Sesión PAPER spot no encontrada")
            event_state = db.execute("""SELECT count(*) AS count,max(observed_at_ns) AS last_ns
                                        FROM events WHERE session_id=?""", (session_id,)).fetchone()
            step = INTERVALS[session["timeframe"]]
            initial_last_t = (session["started_at_ns"] // 1_000_000_000 // step) * step - step
            rows = db.execute("""SELECT candle_t,kind,observed_at_ns,payload_json FROM events
                                 WHERE session_id=? AND kind IN ('OBSERVATION','INTRABAR_EXIT','MANUAL_CLOSE')
                                   AND (candle_t>? OR (candle_t=? AND kind>?))
                                 ORDER BY candle_t,kind LIMIT ?""",
                              (session_id, after_t if after_t is not None else -1,
                               after_t if after_t is not None else -1, after_kind, limit + 1)).fetchall()
            visible = rows[:limit]
            def archived(candle_t: int) -> bool:
                return db.execute("""SELECT 1 FROM session_candles
                                     WHERE session_id=? AND candle_t=?""",
                                  (session_id, candle_t)).fetchone() is not None
            markers = []
            if after_t is None and archived(initial_last_t):
                markers.append({"time": initial_last_t, "kind": "START", "label": "Inicio PAPER",
                                "observed_at_ns": session["started_at_ns"]})
            observations = []
            for row in visible:
                payload = json.loads(row["payload_json"])
                if row["kind"] == "OBSERVATION":
                    if not archived(row["candle_t"]):
                        continue
                    observations.append(_event_view(row))
                    action = payload.get("action")
                    if payload["entry_match"]:
                        entry_status = payload.get("entry_status") or (
                            "EXECUTED_PAPER" if action and action.get("side") == "BUY" else "UNSPECIFIED")
                        skipped = entry_status != "EXECUTED_PAPER"
                        markers.append({"time": row["candle_t"],
                                        "kind": "SIGNAL_SKIPPED" if skipped else "SIGNAL",
                                        "label": "Señal sin entrada" if skipped else "Señal LONG",
                                        "reason": entry_status,
                                        "observed_at_ns": row["observed_at_ns"]})
                    if action is None:
                        continue
                markers.append(_action_chart_marker(row, payload, step, archived))
            has_more = len(rows) > limit
            return {"session_id": session_id, "symbol": session["symbol"],
                    "timeframe": session["timeframe"], "markers": markers,
                    "snapshot": {"last_t": session["last_t"], "event_count": event_state["count"],
                                 "last_event_ns": event_state["last_ns"]},
                    "observations": observations, "initial_last_t": initial_last_t,
                    "next_after_t": visible[-1]["candle_t"] if has_more else None,
                    "next_after_kind": visible[-1]["kind"] if has_more else None,
                    "has_more": has_more,
                    "execution": "PAPER_ACTION_AFTER_SIGNAL_CANDLE_CLOSE_NO_REAL_ORDER"}

    def active_sessions(self) -> list[dict]:
        return [row for row in self.sessions() if row["status"] == "ACTIVE"]

    def scheduled_sessions(self) -> list[dict]:
        """Plan the next check from active rows without reading the candle or event archive."""
        if spot_paper_format(self.path) in ("missing", "empty"):
            return []
        now_ns = self.clock_ns()
        with self.connect() as db:
            rows = db.execute("""SELECT id,last_t,timeframe,state_json,last_quote_json,started_at_ns
                                 FROM sessions WHERE status='ACTIVE' ORDER BY started_at_ns DESC,id""")
            schedule = []
            for row in rows:
                try:
                    close_due = row["last_t"] + 2 * INTERVALS[row["timeframe"]]
                    units = Decimal(json.loads(row["state_json"])["units"])
                    if not units.is_finite() or units < 0:
                        raise ValueError("Unidades inválidas")
                    due = (min(close_due, self._last_risk_check_ns(row, now_ns) // 1_000_000_000 + 5)
                           if units > 0 else close_due)
                except (KeyError, TypeError, ValueError, ArithmeticError):
                    # The authoritative poll validates the state and marks divergence.
                    due = 0
                schedule.append({"id": row["id"], "due_at": due})
            return schedule

    @contextmanager
    def _observation_write(self, epoch: object | None):
        try:
            if spot_paper_access.epoch(self.path) is not epoch:
                raise SpotPaperLocked("La sesión Spot PAPER cambió durante la consulta")
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                yield db
                with spot_paper_access.guard(self.path, epoch):
                    db.commit()
        except SpotPaperAccessError as exc:
            raise SpotPaperLocked(str(exc)) from None

    def stop(self, session_id: str) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE sessions SET status='STOPPED',status_reason='Parada manual' WHERE id=? AND status='ACTIVE'",
                                 (session_id,))
            if not changed.rowcount and db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone() is None:
                raise SpotStrategyError("Sesión PAPER spot no encontrada")
        return self.detail(session_id)

    def close_position(self, session_id: str) -> dict:
        """Cierre manual ficticio a bid reciente, incluso tras GAP o parada.

        Nunca reconstruye el precio de un intervalo perdido. Repetir la
        petición cuando ya no hay unidades no crea otra venta.
        """
        epoch = spot_paper_access.epoch(self.path)
        before = self.detail(session_id)
        if before["valuation"]["basis"] == "UNAVAILABLE_INVALID_STATE":
            raise SpotStrategyError(self.INVALID_STATE_REASON)
        if Decimal(before["state"]["units"]) <= 0:
            return before | {"close_result": "NO_POSITION"}
        raw = self.adapter_factory().get_paper_quote(before["symbol"])
        try:
            bid, _, quote = PaperDca._quote(raw, before["symbol"], self.clock_ns())
        except PaperDcaUnavailable as exc:
            raise SpotStrategyUnavailable(str(exc)) from exc
        with self._observation_write(epoch) as db, localcontext() as context:
            context.prec = 80
            row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            state = json.loads(row["state_json"])
            definition = json.loads(row["definition_json"])
            if (state != before["state"] or not self._valid_session_state(
                    db, row, state, definition)):
                raise SpotStrategyError(self.CHANGED_STATE_REASON)
            units = Decimal(state["units"])
            if units <= 0:
                outcome = "NO_POSITION"
            else:
                now_ns = self.clock_ns()
                if now_ns - quote["event_time_ns"] > 15_000_000_000:
                    raise SpotStrategyUnavailable("Ticker MEXC envejeció antes del cierre manual")
                price = bid * (1 - Decimal(definition["slippage_rate"]))
                gross = units * price
                exit_fee = gross * Decimal(definition["fee_rate"])
                proceeds = gross - exit_fee
                pnl = proceeds - Decimal(state["entry_cost"])
                state = {**state, "cash": str(Decimal(state["cash"]) + proceeds),
                         "units": "0", "entry_price": None, "entry_cost": None,
                         "entry_signal_t": None, "trailing_extreme": None,
                         "realized_pnl": str(Decimal(state["realized_pnl"]) + pnl),
                         "closed_trades": state["closed_trades"] + 1}
                action = {"side": "SELL", "reason": "MANUAL_CLOSE", "price": str(price),
                          "units": str(units), "fee": str(exit_fee),
                          "cash_change": str(proceeds), "pnl": str(pnl)}
                db.execute("INSERT INTO events VALUES (?,?,?,?,?)",
                           (session_id, now_ns // 1_000_000_000, "MANUAL_CLOSE", now_ns,
                            _json({"quote": quote, "action": action,
                                   "execution_model": "MANUAL_SPOT_PAPER_BID_NO_REAL_ORDER"})))
                reason_after_close = row["status_reason"]
                if (row["status"] == "GAP" and reason_after_close ==
                        "Stop/objetivo sin observación continua; posición ficticia abierta"):
                    reason_after_close = "Cobertura de riesgo perdida; posición cerrada manualmente"
                db.execute("UPDATE sessions SET state_json=?,last_quote_json=?,status_reason=? WHERE id=?",
                           (_json(state), _json(quote), reason_after_close, session_id))
                outcome = "CLOSED"
        return self.detail(session_id) | {"close_result": outcome}

    def _terminal(self, session_id: str, last_t: int, status: str, reason: str, *,
                  epoch: object | None) -> str:
        with self._observation_write(epoch) as db:
            changed = db.execute("UPDATE sessions SET status=?,status_reason=? WHERE id=? AND status='ACTIVE' AND last_t=?",
                                 (status, reason, session_id, last_t))
        return status if changed.rowcount else "CHANGED"

    @staticmethod
    def _window(step: int) -> int:
        return min(900, max(30, step // 10))

    @staticmethod
    def _saved_quote(session) -> dict | None:
        raw = session["last_quote_json"]
        if raw is None:
            return None
        quote = json.loads(raw)
        if not isinstance(quote, dict):
            raise ValueError("Cotización PAPER inválida")
        event = quote["event_time_ns"]
        checked = quote.get("checked_at_ns", event)
        bid = Decimal(quote["bid"])
        if (type(event) is not int or type(checked) is not int or checked <= 0
                or not 0 < event <= checked + 2_000_000_000
                or not bid.is_finite() or bid <= 0):
            raise ValueError("Cotización PAPER inválida")
        return quote

    @classmethod
    def _last_risk_check_ns(cls, session, now_ns: int) -> int:
        quote = cls._saved_quote(session)
        checked = (quote.get("checked_at_ns", quote["event_time_ns"])
                   if quote else session["started_at_ns"])
        # Legacy quotes only carry exchange time, with the same clock tolerance as ingestion.
        upper_bound = now_ns + (2_000_000_000 if quote and "checked_at_ns" not in quote else 0)
        if type(checked) is not int or not 0 < checked <= upper_bound:
            raise ValueError("Instante de observación PAPER inválido")
        return checked

    @staticmethod
    def _stop_level(state: dict, definition: dict) -> tuple[Decimal, str]:
        entry = Decimal(state["entry_price"])
        fixed = entry * (1 - Decimal(definition["stop_loss"]))
        if "trailing_stop" not in definition:
            return fixed, "STOP"
        extreme = Decimal(state["trailing_extreme"])
        mobile = extreme * (1 - Decimal(definition["trailing_stop"]))
        return (mobile, "TRAILING_STOP") if mobile > fixed else (fixed, "STOP")

    def _risk_coverage_gap(self, session_id: str, session: dict, *, from_ns: int, now_ns: int,
                           epoch: object | None) -> str:
        """Congela una posición no vigilada; no reconstruye un stop perdido."""
        with self._observation_write(epoch) as db:
            fresh = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if fresh["status"] != "ACTIVE" or fresh["last_t"] != session["last_t"]:
                outcome = "CHANGED"
            elif fresh["state_json"] != session["state_json"]:
                db.execute("UPDATE sessions SET status='GAP',status_reason=? WHERE id=?",
                           (self.CHANGED_STATE_REASON, session_id))
                outcome = "GAP"
            elif fresh["last_quote_json"] != session["last_quote_json"]:
                outcome = "CHANGED"
            else:
                step = INTERVALS[fresh["timeframe"]]
                candle_t = now_ns // 1_000_000_000 // step * step
                db.execute("INSERT INTO events VALUES (?,?,?,?,?)",
                           (session_id, candle_t, "RISK_COVERAGE_GAP", now_ns,
                            _json({"from_ns": from_ns, "to_ns": now_ns,
                                   "unobserved_seconds": (now_ns - from_ns) // 1_000_000_000,
                                   "position_open": True, "action": None})))
                db.execute("""UPDATE sessions SET status='GAP',
                           status_reason='Stop/objetivo sin observación continua; posición ficticia abierta'
                           WHERE id=?""", (session_id,))
                outcome = "GAP"
        return outcome

    def _poll_intrabar(self, session_id: str, session: dict, *, due: int,
                       started_ns: int, epoch: object | None) -> str:
        """Sólo actúa sobre un bid público visto ahora; nunca reconstruye ticks perdidos."""
        prior = json.loads(session["state_json"])
        if Decimal(prior["units"]) <= 0:
            return "WAIT"
        raw = self.adapter_factory().get_paper_quote(session["symbol"])
        quote_checked_ns = self.clock_ns()
        if quote_checked_ns < started_ns:
            raise SpotStrategyUnavailable("Reloj local retrocedió durante PAPER spot")
        try:
            bid, _, quote = PaperDca._quote(raw, session["symbol"], quote_checked_ns)
        except PaperDcaUnavailable as exc:
            raise SpotStrategyUnavailable(str(exc)) from exc
        with self._observation_write(epoch) as db, localcontext() as context:
            context.prec = 80
            fresh = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if fresh["status"] != "ACTIVE" or fresh["last_t"] != session["last_t"]:
                outcome = "CHANGED"
            elif fresh["state_json"] != session["state_json"]:
                db.execute("UPDATE sessions SET status='GAP',status_reason=? WHERE id=?",
                           (self.CHANGED_STATE_REASON, session_id))
                outcome = "GAP"
            elif not self._valid_session_state(db, fresh, prior, json.loads(fresh["definition_json"])):
                db.execute("UPDATE sessions SET status='GAP',status_reason=? WHERE id=?",
                           (self.INVALID_STATE_REASON, session_id))
                outcome = "GAP"
            elif (checked_ns := self.clock_ns()) < quote_checked_ns:
                raise SpotStrategyUnavailable("Reloj local retrocedió durante PAPER spot")
            elif checked_ns // 1_000_000_000 >= due:
                outcome = "DUE"
            elif checked_ns - quote["event_time_ns"] > 15_000_000_000:
                raise SpotStrategyUnavailable("Ticker MEXC envejeció durante el control de riesgo")
            else:
                state = json.loads(fresh["state_json"])
                units = Decimal(state["units"])
                quote = {**quote, "checked_at_ns": checked_ns}
                if units <= 0:
                    outcome = "CHANGED"
                else:
                    definition = json.loads(fresh["definition_json"])
                    start = Decimal(state["entry_price"])
                    stop_level, stop_kind = self._stop_level(state, definition)
                    reason = (("TRAILING_STOP_OBSERVED_BID" if stop_kind == "TRAILING_STOP" else "STOP_OBSERVED_BID")
                              if bid <= stop_level
                              else "TARGET_OBSERVED_BID" if bid >= start * (1 + Decimal(definition["take_profit"]))
                              else None)
                    if reason:
                        price = bid * (1 - Decimal(definition["slippage_rate"]))
                        gross = units * price
                        exit_fee = gross * Decimal(definition["fee_rate"])
                        proceeds = gross - exit_fee
                        pnl = proceeds - Decimal(state["entry_cost"])
                        state = {**state, "cash": str(Decimal(state["cash"]) + proceeds),
                                 "units": "0", "entry_price": None, "entry_cost": None,
                                 "entry_signal_t": None, "trailing_extreme": None,
                                 "realized_pnl": str(Decimal(state["realized_pnl"]) + pnl),
                                 "closed_trades": state["closed_trades"] + 1}
                        action = {"side": "SELL", "reason": reason, "price": str(price),
                                  "units": str(units), "fee": str(exit_fee),
                                  "cash_change": str(proceeds), "pnl": str(pnl)}
                        candle_t = (checked_ns // 1_000_000_000 // INTERVALS[fresh["timeframe"]]
                                    * INTERVALS[fresh["timeframe"]])
                        db.execute("INSERT INTO events VALUES (?,?,?,?,?)",
                                   (session_id, candle_t, "INTRABAR_EXIT", checked_ns,
                                    _json({"quote": quote, "action": action,
                                           "execution_model": "OBSERVED_BID_SPOT_PAPER_NO_PROTECTIVE_ORDER"})))
                        db.execute("UPDATE sessions SET state_json=?,last_quote_json=? WHERE id=?",
                                   (_json(state), _json(quote), session_id))
                        outcome = "RISK_EXIT"
                    else:
                        db.execute("UPDATE sessions SET last_quote_json=? WHERE id=?",
                                   (_json(quote), session_id))
                        outcome = "WAIT"
        return outcome

    @staticmethod
    def _entry_clock_valid(state: dict, definition: dict, last_t: int, step: int) -> bool:
        if Decimal(state["units"]) <= 0:
            return True
        if "trailing_stop" in definition:
            try:
                extreme = Decimal(state["trailing_extreme"])
                entry = Decimal(state["entry_price"])
                if not extreme.is_finite() or not entry.is_finite() or extreme < entry:
                    return False
            except (KeyError, TypeError, ValueError, InvalidOperation):
                return False
        if "max_holding_bars" not in definition:
            return True
        entry_t = state.get("entry_signal_t")
        return (type(entry_t) is int and entry_t <= last_t
                and (last_t - entry_t) % step == 0)

    @staticmethod
    def _advance(state: dict, definition: dict, *, entry: bool, exit_match: bool | None,
                 signal_close: Decimal, signal_high: Decimal, signal_t: int, step: int,
                 bid: Decimal, ask: Decimal) -> tuple[dict, dict | None]:
        """Caja/unidades exactas; límite tras N velas cerradas observadas desde compra."""
        with localcontext() as context:
            context.prec = 80
            cash, units = Decimal(state["cash"]), Decimal(state["units"])
            fee = Decimal(definition["fee_rate"])
            slip = Decimal(definition["slippage_rate"])
            action = None
            if units > 0:
                start = Decimal(state["entry_price"])
                stop_level, stop_kind = SpotStrategyPaper._stop_level(state, definition)
                reason = (("TRAILING_STOP_CLOSE" if stop_kind == "TRAILING_STOP" else "STOP_CLOSE")
                          if signal_close <= stop_level
                          else "TARGET_CLOSE" if signal_close >= start * (1 + Decimal(definition["take_profit"]))
                          else "EXIT_RULE" if exit_match else "TIME_LIMIT"
                          if ("max_holding_bars" in definition and
                              signal_t - state["entry_signal_t"] >= definition["max_holding_bars"] * step)
                          else None)
                if reason:
                    price = bid * (1 - slip)
                    gross = units * price
                    exit_fee = gross * fee
                    proceeds = gross - exit_fee
                    pnl = proceeds - Decimal(state["entry_cost"])
                    cash += proceeds
                    action = {"side": "SELL", "reason": reason, "price": str(price),
                              "units": str(units), "fee": str(exit_fee), "cash_change": str(proceeds),
                              "pnl": str(pnl)}
                    state = {**state, "cash": str(cash), "units": "0", "entry_price": None,
                             "entry_cost": None, "entry_signal_t": None, "trailing_extreme": None,
                             "realized_pnl": str(Decimal(state["realized_pnl"]) + pnl),
                             "closed_trades": state["closed_trades"] + 1}
                elif "trailing_stop" in definition:
                    # La máxima de esta vela no ajusta el stop usado para
                    # decidir sobre ella; sólo rige desde la siguiente.
                    state = {**state, "trailing_extreme": str(max(Decimal(state["trailing_extreme"]), signal_high))}
            elif entry:
                budget = cash * Decimal(definition["allocation"])
                if budget > 0:
                    price = ask * (1 + slip)
                    units = budget / (price * (1 + fee))
                    entry_fee = units * price * fee
                    cash -= budget
                    action = {"side": "BUY", "reason": "ENTRY_RULE", "price": str(price),
                              "units": str(units), "fee": str(entry_fee),
                              "cash_change": str(-budget), "pnl": None}
                    state = {**state, "cash": str(cash), "units": str(units),
                             "entry_price": str(price), "entry_cost": str(budget)}
                    if "trailing_stop" in definition:
                        state["trailing_extreme"] = str(price)
                    if "max_holding_bars" in definition:
                        state["entry_signal_t"] = signal_t
            return state, action

    def poll(self, session_id: str) -> dict:
        outcome = self._poll_outcome(session_id)
        return self.detail(session_id) | {"poll_result": outcome}

    def observe(self, session_id: str) -> dict:
        return {"poll_result": self._poll_outcome(session_id)}

    def _poll_outcome(self, session_id: str) -> str:
        epoch = spot_paper_access.epoch(self.path)
        now_ns = self.clock_ns()
        now = now_ns // 1_000_000_000
        with self.connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise SpotStrategyError("Sesión PAPER spot no encontrada")
            session = dict(row)
            definition = json.loads(session["definition_json"])
            try:
                state = json.loads(session["state_json"])
            except (ValueError, TypeError):
                state = None
            state_valid = self._valid_session_state(db, row, state, definition)
        if session["status"] != "ACTIVE":
            return session["status"]
        if not supports_engine_version(session["engine_version"], definition):
            return self._terminal(session_id, session["last_t"], "GAP", "Motor LAB distinto; sesión no reproducible", epoch=epoch)
        if not state_valid:
            return self._terminal(session_id, session["last_t"], "GAP", self.INVALID_STATE_REASON, epoch=epoch)
        if Decimal(state["units"]) > 0:
            try:
                from_ns = self._last_risk_check_ns(session, now_ns)
            except (KeyError, TypeError, ValueError, ArithmeticError):
                return self._terminal(session_id, session["last_t"], "GAP", self.INVALID_STATE_REASON, epoch=epoch)
            if now_ns - from_ns > self.RISK_COVERAGE_NS:
                return self._risk_coverage_gap(session_id, session, from_ns=from_ns, now_ns=now_ns, epoch=epoch)
        step = INTERVALS[session["timeframe"]]
        next_t = session["last_t"] + step
        due = next_t + step
        if now < due:
            return self._poll_intrabar(session_id, session, due=due, started_ns=now_ns, epoch=epoch)
        if now >= due + self._window(step):
            return self._terminal(session_id, session["last_t"], "GAP", "Ventana de vela perdida; sin ejecución retrospectiva", epoch=epoch)
        history = json.loads(session["history_json"])
        if len(history) >= 5000:
            return self._terminal(session_id, session["last_t"], "CAPACITY", "Límite de 5000 velas", epoch=epoch)
        adapter = self.adapter_factory()
        fresh_rows = adapter.get_candles(session["symbol"], timeframe=session["timeframe"],
                                         limit=3, since=next_t - 2 * step, before=due)
        if not isinstance(fresh_rows, list):
            raise SpotStrategyUnavailable("MEXC no devolvió velas spot")
        selected = [row for row in fresh_rows if next_t - 2 * step <= row.get("t", -1) <= next_t]
        if len(selected) != 3:
            raise SpotStrategyUnavailable("MEXC no devolvió tres velas cerradas verificables")
        candles_checked_ns = self.clock_ns()
        if candles_checked_ns < now_ns:
            raise SpotStrategyUnavailable("Reloj local retrocedió durante PAPER spot")
        try:
            overlap, gaps = validate_candles(selected, session["timeframe"], now=candles_checked_ns // 1_000_000_000)
        except LabError as exc:
            raise SpotStrategyUnavailable(f"Velas MEXC inválidas: {exc}") from exc
        if gaps or [bar.json() for bar in overlap[:2]] != history[-2:] or overlap[-1].t != next_t:
            return self._terminal(session_id, session["last_t"], "GAP", "MEXC revisó velas o perdió continuidad", epoch=epoch)
        quote_raw = adapter.get_paper_quote(session["symbol"])
        quote_checked_ns = self.clock_ns()
        if quote_checked_ns < candles_checked_ns:
            raise SpotStrategyUnavailable("Reloj local retrocedió durante PAPER spot")
        try:
            bid, ask, quote = PaperDca._quote(quote_raw, session["symbol"], quote_checked_ns)
        except PaperDcaUnavailable as exc:
            raise SpotStrategyUnavailable(str(exc)) from exc
        if quote["event_time_ns"] < due * 1_000_000_000:
            raise SpotStrategyUnavailable("Ticker MEXC anterior al cierre de la vela de señal")
        history_checked_ns = self.clock_ns()
        if history_checked_ns < quote_checked_ns:
            raise SpotStrategyUnavailable("Reloj local retrocedió durante PAPER spot")
        bars, gaps = validate_candles(history + [overlap[-1].json()], session["timeframe"], now=history_checked_ns // 1_000_000_000)
        if gaps:
            return self._terminal(session_id, session["last_t"], "GAP", "Velas discontinuas", epoch=epoch)
        cache = IndicatorCache(bars, precision=paper_precision_for_engine(session["engine_version"]))
        entry, entry_evidence = evaluate(definition["entry"], bars, len(bars) - 1, cache,
                                         base_timeframe=session["timeframe"])
        exit_match, exit_evidence = (evaluate(definition["exit"], bars, len(bars) - 1, cache,
                                              base_timeframe=session["timeframe"])
                                      if "exit" in definition else (None, None))
        try:
            indicator_state = advance_paper_indicator_state(
                json.loads(session["indicator_state_json"]), definition, bars[-1].json(),
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
            return self._terminal(session_id, session["last_t"], "GAP",
                                  "Indicadores PAPER incrementales no verificables", epoch=epoch)
        with self._observation_write(epoch) as db:
            fresh = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if fresh["status"] != "ACTIVE" or fresh["last_t"] != session["last_t"]:
                outcome = "CHANGED"
            elif fresh["state_json"] != session["state_json"]:
                db.execute("UPDATE sessions SET status='GAP',status_reason=? WHERE id=?",
                           (self.CHANGED_STATE_REASON, session_id))
                outcome = "GAP"
            elif (observed_ns := self.clock_ns()) < history_checked_ns:
                raise SpotStrategyUnavailable("Reloj local retrocedió durante PAPER spot")
            elif observed_ns // 1_000_000_000 >= due + self._window(step):
                db.execute("UPDATE sessions SET status='GAP',status_reason='Respuesta MEXC fuera de ventana' WHERE id=?", (session_id,))
                outcome = "GAP"
            else:
                if observed_ns - quote["event_time_ns"] > 15_000_000_000:
                    raise SpotStrategyUnavailable("Ticker MEXC envejeció durante la decisión")
                prior_state = json.loads(fresh["state_json"])
                if not self._valid_session_state(db, fresh, prior_state, definition):
                    db.execute("""UPDATE sessions SET status='GAP',
                               status_reason=? WHERE id=?""", (self.INVALID_STATE_REASON, session_id))
                    outcome = "GAP"
                else:
                    had_position = Decimal(prior_state["units"]) > 0
                    state, action = self._advance(prior_state, definition,
                                                  entry=entry, exit_match=exit_match,
                                                  signal_close=bars[-1].c, signal_high=bars[-1].h,
                                                  signal_t=next_t,
                                                  step=step, bid=bid, ask=ask)
                    quote = {**quote, "checked_at_ns": observed_ns}
                    evidence = {"candle": bars[-1].json(), "entry_match": entry,
                                "entry_status": ("EXECUTED_PAPER" if action and action["side"] == "BUY"
                                                 else "SKIPPED_POSITION_OPEN" if entry and had_position
                                                 else "SKIPPED_NO_CAPITAL" if entry else None),
                                "exit_match": exit_match, "entry": entry_evidence, "exit": exit_evidence,
                                "quote": quote, "action": action,
                                "execution_model": "POST_CLOSE_BID_ASK_PLUS_SLIPPAGE_NO_REAL_ORDER"}
                    db.execute("INSERT INTO events VALUES (?,?,?,?,?)",
                               (session_id, next_t, "OBSERVATION", observed_ns, _json(evidence)))
                    db.execute("INSERT INTO observed_candles VALUES (?,?,?)",
                               (session_id, next_t, _json(bars[-1].json())))
                    db.execute("INSERT INTO session_candles VALUES (?,?,?,?)",
                               (session_id, next_t, _json(bars[-1].json()), "OBSERVED"))
                    previous = db.execute("""SELECT digest FROM session_archive_chain
                                             WHERE session_id=? ORDER BY candle_t DESC LIMIT 1""",
                                          (session_id,)).fetchone()["digest"]
                    digest = next_archive_digest(previous, session_id, next_t,
                                                 "OBSERVED", _json(bars[-1].json()))
                    db.execute("INSERT INTO session_archive_chain VALUES (?,?,?)",
                               (session_id, next_t, digest))
                    db.execute("""UPDATE sessions SET history_json=?,state_json=?,indicator_state_json=?,
                                  last_t=?,last_quote_json=? WHERE id=?""",
                               (_json([bar.json() for bar in bars]), _json(state), _json(indicator_state),
                                next_t, _json(quote), session_id))
                    outcome = "OBSERVED"
        return outcome


def create_spot_strategy_router(path: Path, lab_path: Path, *, paper: SpotStrategyPaper | None = None,
                                worker: SpotStrategyWorker | None = None,
                                on_access_change: Callable[[], None] | None = None) -> APIRouter:
    paper = paper or SpotStrategyPaper(path, lab_path, allow_create=False)
    router = APIRouter(prefix="/paper/spot/strategies", tags=["spot-paper-strategies"])

    def local_only(request: Request) -> None:
        host = request.client.host if request.client else None
        if host not in LOCAL_HOSTS or request.url.hostname not in LOCAL_HOSTS:
            raise HTTPException(403, "PAPER spot sólo local")
        origin = request.headers.get("origin")
        if origin is not None and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Origen externo rechazado")

    def available(request: Request) -> None:
        local_only(request)
        try:
            kind = spot_paper_format(paper.path)
        except SpotPaperAccessError as exc:
            raise HTTPException(422, str(exc)) from exc
        if kind == "encrypted_or_invalid" and spot_paper_access.get(paper.path) is None:
            raise HTTPException(423, "Spot PAPER cifrado bloqueado; desbloquéalo en Ajustes")
        if kind in ("missing", "empty") and not (
            request.method == "GET" and request.url.path in (
                "/paper/spot/strategies", "/paper/spot/strategies/readiness")
        ):
            raise HTTPException(423, "Crea primero la base Spot PAPER cifrada en Ajustes")

    @router.get("/status", dependencies=[Depends(local_only)])
    def paper_status(response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return spot_paper_access.status(paper.path)
        except SpotPaperAccessError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/unlock", dependencies=[Depends(local_only)])
    def unlock_paper(body: SpotPaperUnlock, response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            result = spot_paper_access.unlock(paper.path, body.passphrase.get_secret_value(),
                                          session_seconds=body.session_seconds)
        except SpotPaperAccessError as exc:
            raise HTTPException(422, str(exc)) from exc
        if on_access_change is not None:
            on_access_change()
        return result

    @router.post("/create", dependencies=[Depends(local_only)])
    def create_encrypted_paper(body: SpotPaperUnlock, response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            result = spot_paper_access.create(paper.path, body.passphrase.get_secret_value(),
                                          session_seconds=body.session_seconds)
        except SpotPaperAccessError as exc:
            raise HTTPException(422, str(exc)) from exc
        if on_access_change is not None:
            on_access_change()
        return result

    @router.post("/lock", dependencies=[Depends(local_only)])
    def lock_paper(response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        result = spot_paper_access.lock(paper.path)
        if on_access_change is not None:
            on_access_change()
        return result

    def respond(action, *args):
        try:
            return action(*args)
        except SpotPaperLocked as exc:
            raise HTTPException(423, str(exc)) from exc
        except SpotStrategyUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        except (SpotStrategyError, LabError) as exc:
            status = 404 if "no encontrada" in str(exc) or "no encontrado" in str(exc) else 422
            raise HTTPException(status, str(exc)) from exc

    @router.post("", dependencies=[Depends(available)], status_code=201)
    def create(body: SpotStrategyCreate) -> dict:
        if worker is None or not worker.snapshot()["running"]:
            raise HTTPException(503, "Observador local inactivo; no se activa PAPER spot")
        result = respond(paper.create, body.version_id, body.dataset_id, body.source_run_id)
        worker.wake()
        return result

    @router.get("/readiness", dependencies=[Depends(available)])
    def readiness(response: Response, dataset_id: str = Query(min_length=1)) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return respond(paper.readiness, dataset_id)

    @router.get("/worker", dependencies=[Depends(local_only)])
    def worker_status() -> dict:
        return worker.snapshot() if worker is not None else {"running": False, "mode": "NOT_CONFIGURED"}

    @router.get("", dependencies=[Depends(available)])
    def sessions() -> list[dict]:
        return respond(paper.sessions)

    @router.get("/{session_id}", dependencies=[Depends(available)])
    def detail(session_id: str) -> dict:
        return respond(paper.detail, session_id)

    @router.get("/{session_id}/summary", dependencies=[Depends(available)])
    def summary(session_id: str, response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return respond(paper.summary, session_id)

    @router.get("/{session_id}/verify", dependencies=[Depends(available)])
    def verify_source(session_id: str) -> dict:
        return respond(paper.verify_source, session_id)

    @router.get("/{session_id}/chart", dependencies=[Depends(available)])
    def chart(session_id: str) -> dict:
        return respond(paper.chart, session_id)

    @router.get("/{session_id}/candles", dependencies=[Depends(available)])
    def candle_page(session_id: str, response: Response,
                    after_t: int | None = Query(default=None, ge=0),
                    limit: int = Query(default=500, ge=1, le=1000)) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return respond(paper.candle_page, session_id, after_t, limit)

    @router.get("/{session_id}/events", dependencies=[Depends(available)])
    def event_page(session_id: str, response: Response,
                   after_t: int | None = Query(default=None, ge=0),
                   after_kind: str = Query(default="", max_length=32),
                   limit: int = Query(default=500, ge=1, le=1000)) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return respond(paper.event_page, session_id, after_t, after_kind, limit)

    @router.get("/{session_id}/events/recent", dependencies=[Depends(available)])
    def recent_event_page(session_id: str, response: Response,
                          before_t: int | None = Query(default=None, ge=0),
                          before_kind: str = Query(default="", max_length=32),
                          limit: int = Query(default=30, ge=1, le=100)) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return respond(paper.recent_event_page, session_id, before_t, before_kind, limit)

    @router.get("/{session_id}/chart-markers-page", dependencies=[Depends(available)])
    def chart_marker_page(session_id: str, response: Response,
                          after_t: int | None = Query(default=None, ge=0),
                          after_kind: str = Query(default="", max_length=32),
                          limit: int = Query(default=500, ge=1, le=1000)) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return respond(paper.chart_marker_page, session_id, after_t, after_kind, limit)

    @router.get("/{session_id}/comparison", dependencies=[Depends(available)])
    def comparison(session_id: str) -> dict:
        return respond(paper.comparison, session_id)

    @router.get("/{session_id}/reserve-comparison", dependencies=[Depends(available)])
    def reserve_comparison(session_id: UUID, validation_id: UUID, response: Response) -> dict:
        from app.automation.spot_reserve_comparison import compare_spot_reserve

        response.headers["Cache-Control"] = "no-store"
        return respond(compare_spot_reserve, paper, str(session_id), str(validation_id))

    @router.get("/{session_id}/research-export", dependencies=[Depends(available)])
    def research_export_preview(session_id: UUID, response: Response) -> dict:
        from app.automation.spot_research_export import spot_research_export

        response.headers["Cache-Control"] = "no-store"
        return respond(spot_research_export, paper, str(session_id))

    @router.post("/{session_id}/research-export", dependencies=[Depends(available)])
    def research_export(session_id: UUID, body: SpotResearchExport, response: Response) -> dict:
        from app.automation.spot_research_export import spot_research_export

        response.headers["Cache-Control"] = "no-store"
        return respond(spot_research_export, paper, str(session_id), body.revision)

    @router.get("/{session_id}/gap-analysis", dependencies=[Depends(available)])
    def gap_analysis(session_id: str) -> dict:
        return respond(paper.gap_analysis, session_id)

    @router.get("/{session_id}/gap-analyses", dependencies=[Depends(available)])
    def gap_analyses(session_id: str) -> list[dict]:
        return respond(paper.gap_analyses, session_id)

    @router.post("/{session_id}/poll", dependencies=[Depends(available)])
    def poll(session_id: str) -> dict:
        return respond(paper.poll, session_id)

    @router.post("/{session_id}/stop", dependencies=[Depends(available)])
    def stop(session_id: str) -> dict:
        return respond(paper.stop, session_id)

    @router.post("/{session_id}/close", dependencies=[Depends(available)])
    def close_position(session_id: str) -> dict:
        return respond(paper.close_position, session_id)

    return router
