"""DCA PAPER spot MEXC con dinero ficticio y reloj verificable.

El proceso consulta una vela diaria recién cerrada y un ticker público
reciente. No envía órdenes, no usa credenciales y no reconstruye compras
durante intervalos perdidos. El mismo paso de decisión se usa en backtesting.
"""

from __future__ import annotations

from app.lab.schema import DATASET_READ_VERSIONS

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import json
from pathlib import Path
import time
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import sqlcipher3

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

from app.automation.dca import (
    DcaState, SUPPORTED_ENGINE_VERSIONS, apply_dca_step, dca_average_decision, dca_engine_for_rule,
    dca_signal_window_key, dca_sma_check, dca_volume_check, precision_for_version, seed_dca_ema,
    validate_dca, validate_dca_state,
)
from app.automation.store import AutomationStore, _json
from app.automation.access import automation_format
from app.automation.paper_dca_comparison import DAY, WINDOW_SECONDS, decision_payloads, initial_paper_state, replay_paper_comparison
from app.automation.paper_dca_gap import MAX_MISSED_DAYS, analyze_dca_paper_gap, dca_paper_overlap_count
from app.automation.verification import verify_dca_run, verify_dca_validation
from app.lab.engine import LabError, number, validate_candles
from app.lab.incremental_indicators import EmaState
from app.lab.access import lab_access, lab_format
from app.lab.store import LabStore, content_hash
from app.marketdata.adapters.mexc import MexcAdapter

if TYPE_CHECKING:
    from app.automation.paper_worker import PaperDcaWorker


QUOTE_FRESH_NS = 15_000_000_000


def _ema_origin_for_rule(rule: dict, seed: list[dict]) -> dict | None:
    state = seed_dca_ema(rule, (number(row["c"], positive=True) for row in seed))
    return state.snapshot() if state is not None else None


class PaperDcaError(ValueError):
    pass


class PaperDcaUnavailable(PaperDcaError):
    pass


class PaperDcaCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version_id: str
    dataset_id: str
    source_run_id: str | None = None


class PaperDca:
    def __init__(self, automation_path: Path, lab_path: Path, *, adapter_factory=MexcAdapter,
                 clock_ns=time.time_ns):
        self.store = AutomationStore(automation_path, allow_create=False)
        self.lab = LabStore(lab_path, allow_create=False)
        self.adapter_factory = adapter_factory
        self.clock_ns = clock_ns

    @staticmethod
    def _ledger_matches(db, row, state: DcaState, rule: dict, *, ema_origin: dict | None = None,
                        volume_origin: list[dict] | None = None) -> bool:
        """Reconstruye las compras ficticias y las compara con el estado guardado."""
        if row["engine_version"] not in SUPPORTED_ENGINE_VERSIONS:
            return False
        try:
            with localcontext() as context:
                context.prec = precision_for_version(row["engine_version"])
                budget = Decimal(rule["budget_total"])
                cash, spent, units, fees = budget, Decimal(0), Decimal(0), Decimal(0)
                monthly: dict[str, Decimal] = {}
                triggered: set[str] = set()
                extra_count = 0
                last_extra_index = None
                observations = 0
                observed_t = None
                gate = rule.get("sma_gate")
                adjustment = rule.get("ma_adjustment")
                volume = rule.get("volume_gate")
                window_key = dca_signal_window_key(rule)
                overlap_count = dca_paper_overlap_count(rule)
                previous_window = None
                rejection, signal_evidence, factor = None, {}, None
                ema = None
                dip_amounts = {tier["drop_pct"]: tier["amount"] for tier in rule["dips"]}
                first_due = (row["started_at_ns"] // 1_000_000_000 // DAY) * DAY + DAY
                for item in db.execute("""SELECT execution_t,sequence_no,event_json FROM paper_dca_events
                                          WHERE session_id=? ORDER BY execution_t,sequence_no""",
                                       (row["id"],)):
                    payload = json.loads(item["event_json"])
                    if not isinstance(payload, dict):
                        return False
                    kind = payload.get("kind")
                    when = item["execution_t"]
                    if window_key and kind in ("BUY", "SKIP"):
                        if (any(payload.get(key) != value or volume and key not in payload
                                for key, value in signal_evidence.items())
                                or rejection and (kind == "BUY" or payload.get("reason") != rejection)
                                or not rejection and payload.get("reason") in (
                                    "SMA_WARMUP", "SMA_CONDITION_NOT_MET", "MA_WARMUP", "MA_ZERO_AMOUNT",
                                    "VOLUME_WARMUP", "VOLUME_BASELINE_ZERO", "VOLUME_BELOW_MINIMUM")):
                            return False
                        if adjustment:
                            base = dip_amounts.get(payload.get("drop_pct")) if payload.get("trigger_kind") == "DIP" else rule["amount"]
                            if (base is None or payload.get("base_requested") != base
                                    or Decimal(payload["requested"]) != Decimal(base) * (factor or Decimal(0))):
                                return False
                    if kind == "OBSERVATION":
                        if (item["sequence_no"] != 0 or when != first_due + observations * DAY
                                or not isinstance(payload.get("signal_candle"), dict)
                                or payload["signal_candle"].get("t") != when - DAY):
                            return False
                        if window_key:
                            window = payload.get(window_key)
                            prior = previous_window[1:] if previous_window is not None else volume_origin
                            if (not isinstance(window, list) or len(window) != overlap_count + 1
                                    or window[-1] != payload["signal_candle"]
                                    or payload.get("engine_version") != row["engine_version"]
                                    or prior is not None and prior != window[:-1]):
                                return False
                            bars, gaps = validate_candles(window, "1d", now=when)
                            if gaps or [bar.json() for bar in bars] != window:
                                return False
                            previous_window = window
                            signal = {"signal_time": bars[-1].t, "signal_close": bars[-1].c}
                            if gate:
                                rejection, signal_evidence = dca_sma_check(gate, bars, **signal)
                            elif volume:
                                rejection, signal_evidence = dca_volume_check(volume, bars, **signal)
                            else:
                                if adjustment["kind"] == "ema" and observations == 0:
                                    starting = payload.get("ema_origin")
                                    if ema_origin is not None and starting != ema_origin:
                                        return False
                                    ema = EmaState.restore(starting)
                                elif "ema_origin" in payload:
                                    return False
                                factor, signal_evidence = dca_average_decision(adjustment, bars, ema=ema, **signal)
                                rejection = "MA_WARMUP" if factor is None else "MA_ZERO_AMOUNT" if factor == 0 else None
                            signal_evidence["signal_close"] = str(bars[-1].c)
                        observations += 1
                        observed_t = when
                    elif kind == "BUY":
                        if observed_t != when or item["sequence_no"] <= 0 or payload.get("execution_time") != when:
                            return False
                        amount = Decimal(payload["spent"])
                        acquired = Decimal(payload["units"])
                        fee = Decimal(payload["fee"])
                        price = Decimal(payload["price"])
                        if (not all(value.is_finite() for value in (amount, acquired, fee, price))
                                or amount <= 0 or acquired <= 0 or price <= 0 or fee < 0
                                or fee >= amount or acquired != (amount - fee) / price
                                or adjustment and amount > Decimal(payload["requested"])):
                            return False
                        cash -= amount
                        spent += amount
                        units += acquired
                        fees += fee
                        month = datetime.fromtimestamp(when, timezone.utc).strftime("%Y-%m")
                        monthly[month] = monthly.get(month, Decimal(0)) + amount
                        if payload.get("trigger_kind") == "DIP":
                            drop = payload["drop_pct"]
                            if drop in triggered or drop not in dip_amounts:
                                return False
                            triggered.add(drop)
                            extra_count += 1
                            last_extra_index = observations
                        elif payload.get("trigger_kind") != "BASE":
                            return False
                    elif kind == "SKIP":
                        if observed_t != when or item["sequence_no"] <= 0:
                            return False
                    else:
                        return False
                ema_final = (ema.snapshot() if ema is not None else ema_origin)
                last_window = previous_window[1:] if previous_window is not None else volume_origin
                return (observations == row["tick_index"]
                        and row["next_exec_t"] == first_due + observations * DAY
                        and (last_window is None or json.loads(row["overlap_json"]) == last_window)
                        and (ema_final is None or state.ema is not None and state.ema.snapshot() == ema_final)
                        and cash == state.cash and spent == state.spent
                        and units == state.units and fees == state.fees
                        and monthly == state.monthly_spent
                        and triggered == state.triggered
                        and extra_count == state.extra_count
                        and last_extra_index == state.last_extra_index)
        except (KeyError, TypeError, ValueError, InvalidOperation, ArithmeticError, OverflowError):
            return False

    def _state_valid(self, db, row, raw_state: object, rule: dict, *, ema_origin: dict | None = None,
                     volume_origin: list[dict] | None = None) -> bool:
        try:
            if dca_signal_window_key(rule) and validate_dca(rule) != rule:
                return False
            state = DcaState.from_json(raw_state)
            validate_dca_state(state, rule, tick_index=row["tick_index"],
                               engine_version=row["engine_version"])
            return self._ledger_matches(db, row, state, rule, ema_origin=ema_origin, volume_origin=volume_origin)
        except (KeyError, TypeError, ValueError, InvalidOperation, LabError):
            return False

    def _summary(self, row, db) -> dict:
        try:
            saved_rule = json.loads(row["rule_json"])
            reference_mode = saved_rule.get("reference_mode", "initial") if isinstance(saved_rule, dict) else "unknown"
        except (TypeError, ValueError):
            saved_rule = None
            reference_mode = "unknown"
        try:
            state = json.loads(row["state_json"])
            if not isinstance(state, dict):
                state = None
        except (TypeError, ValueError):
            state = None
        if state is not None and (not isinstance(saved_rule, dict)
                                  or not self._state_valid(db, row, state, saved_rule)):
            state = None
        gate = ({key: saved_rule[key] for key in ("sma_gate", "ma_adjustment", "volume_gate") if key in saved_rule}
                if state is not None else {})
        return {key: row[key] for key in ("id", "version_id", "engine_version", "dataset_id", "dataset_hash", "source_run_id", "symbol",
                                           "source", "next_exec_t", "tick_index", "started_at_ns", "status",
                                           "status_reason")} | {
            "state": state,
            "reference_mode": reference_mode,
            "last_quote": json.loads(row["last_quote_json"]) if row["last_quote_json"] else None,
            "mode": "PAPER_DCA_MEXC_SPOT_APPROXIMATION", "execution": "NO_EXCHANGE_ORDERS"} | gate

    def create(self, version_id: str, dataset_id: str, source_run_id: str | None = None) -> dict:
        now_ns = self.clock_ns()
        now = now_ns // 1_000_000_000
        version = self.store.version(version_id)
        rule = validate_dca(version["definition"])
        engine_version = dca_engine_for_rule(rule)
        overlap_count = dca_paper_overlap_count(rule)
        meta, raw = self.lab.dataset(dataset_id)
        if (not meta["verified_public"] or meta["source"] != "mexc-spot-public"
                or meta["timeframe"] != "1d" or meta["symbol"] not in MexcAdapter.known_pairs
                or not meta["symbol"].endswith("/USDT")):
            raise PaperDcaError("PAPER DCA requiere histórico diario MEXC Spot/USDT descargado y verificado")
        bars, gaps = validate_candles(raw, "1d", now=now)
        today = now // DAY * DAY
        if gaps or meta["gaps"] or bars[-1].t != today - DAY:
            raise PaperDcaError("El histórico inicial debe acabar en la última vela diaria cerrada y no tener huecos")
        warmup = rule.get("ma_adjustment", {}).get("period", 0) - 1
        if len(bars) < max(overlap_count, warmup):
            message = ("El histórico inicial no cubre la ventana de volumen del plan" if "volume_gate" in rule
                       else "El histórico inicial no cubre la ventana SMA del plan" if "sma_gate" in rule
                       else "El histórico inicial no cubre el periodo de la media móvil")
            raise PaperDcaError(message)
        if source_run_id is not None:
            origin = self.store.run(source_run_id)
            if (origin["version_id"] != version_id or origin["dataset_id"] != dataset_id
                    or origin["dataset_hash"] != meta["hash"]
                    or dca_signal_window_key(rule) and origin["engine_version"] != engine_version):
                raise PaperDcaError("La prueba DCA de origen no coincide con la versión y el histórico PAPER")
            verified = verify_dca_run(self.store, self.lab, origin)
            if not verified["verified"]:
                raise PaperDcaError("La prueba DCA de origen no está verificada: " + verified["reason"])
        state = initial_paper_state(rule, bars)
        session_id = str(uuid4())
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute("""SELECT id FROM paper_dca_sessions
                WHERE version_id=? AND dataset_id=? AND status='ACTIVE' LIMIT 1""",
                (version_id, dataset_id)).fetchone()
            if active is not None:
                raise PaperDcaError("Esta versión DCA ya tiene una sesión PAPER activa sobre este histórico")
            db.execute("""INSERT INTO paper_dca_sessions
                        (id,version_id,dataset_id,dataset_hash,symbol,source,rule_json,state_json,
                         overlap_json,next_exec_t,tick_index,started_at_ns,status,status_reason,last_quote_json,engine_version,source_run_id)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (session_id, version_id, dataset_id, meta["hash"], meta["symbol"], meta["source"],
                        _json(rule), _json(state.json()), _json([bar.json() for bar in bars[-overlap_count:]]),
                        today + DAY, 0, now_ns, "ACTIVE", None, None, engine_version, source_run_id))
            row = db.execute("SELECT * FROM paper_dca_sessions WHERE id=?", (session_id,)).fetchone()
            return self._summary(row, db)

    def sessions(self, *, session_id: str | None = None) -> list[dict]:
        if automation_format(self.store.path) in ("missing", "empty"):
            return []
        with self.store.connect() as db:
            rows = (db.execute("SELECT * FROM paper_dca_sessions WHERE id=?", (session_id,)) if session_id is not None
                    else db.execute("SELECT * FROM paper_dca_sessions ORDER BY started_at_ns DESC,id"))
            return [self._summary(row, db) for row in rows]

    def detail(self, session_id: str) -> dict:
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM paper_dca_sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise PaperDcaError("Sesión PAPER DCA no encontrada")
            events = []
            for item in db.execute("""SELECT * FROM paper_dca_events
                                      WHERE session_id=? ORDER BY execution_t,sequence_no""", (session_id,)):
                try:
                    payload = json.loads(item["event_json"])
                    if not isinstance(payload, dict):
                        raise ValueError("Evento sin objeto")
                except (TypeError, ValueError):
                    payload = {"kind": "UNREADABLE", "unreadable": True}
                events.append({"execution_t": item["execution_t"],
                               "observed_at_ns": item["observed_at_ns"],
                               "quote_event_ns": item["quote_event_ns"], **payload})
            return self._summary(row, db) | {"events": events}

    def chart(self, session_id: str) -> dict:
        """Velas verificadas y compras PAPER observadas, nunca reconstruidas."""
        session = self.detail(session_id)
        if session["state"] is None:
            raise PaperDcaError("Estado DCA no verificable; gráfico oculto")
        meta, raw = self.lab.dataset(session["dataset_id"])
        if (meta["hash"] != session["dataset_hash"] or meta["symbol"] != session["symbol"]
                or meta["source"] != session["source"] or meta["timeframe"] != "1d"
                or not meta["verified_public"] or meta["gaps"]):
            raise PaperDcaError("El histórico inicial PAPER DCA no coincide con la sesión")
        now = int(time.time())
        seed, gaps = validate_candles(raw, "1d", now=now)
        if gaps:
            raise PaperDcaError("El histórico inicial PAPER DCA contiene huecos")
        adjustment = session.get("ma_adjustment")
        if adjustment and adjustment["kind"] == "ema":
            expected = _ema_origin_for_rule({"ma_adjustment": adjustment}, raw)
            observed_seed = (session["events"][0].get("ema_origin") if session["tick_index"]
                             else session["state"]["ema"])
            if expected != observed_seed:
                raise PaperDcaError("La semilla EMA no coincide con el histórico PAPER")
        candles = [bar.json() for bar in seed]
        if session.get("volume_gate") and session["tick_index"]:
            period = session["volume_gate"]["period"]
            if session["events"][0]["volume_window"][:-1] != candles[-period:]:
                raise PaperDcaError("La ventana de volumen no coincide con el histórico PAPER")
        observed_at: set[int] = set()
        for event in session["events"]:
            if event.get("kind") != "OBSERVATION":
                continue
            execution_t = event["execution_t"]
            row = event.get("signal_candle")
            if (type(execution_t) is not int or execution_t in observed_at
                    or not isinstance(row, dict) or row.get("t") != execution_t - DAY):
                raise PaperDcaError("Captura PAPER DCA incoherente")
            try:
                trio, missing = validate_candles(candles[-2:] + [row], "1d", now=now)
            except LabError as exc:
                raise PaperDcaError("Vela observada PAPER DCA inválida") from exc
            if missing or trio[-1].t != trio[-2].t + DAY:
                raise PaperDcaError("La observación PAPER DCA tiene un hueco")
            candles.append(trio[-1].json())
            observed_at.add(execution_t)
        grouped: dict[int, list[dict]] = {}
        for event in session["events"]:
            if event.get("kind") != "BUY":
                continue
            execution_t = event["execution_t"]
            if (type(execution_t) is not int or execution_t not in observed_at
                    or event.get("execution_time") != execution_t
                    or event.get("signal_time") != execution_t - DAY):
                raise PaperDcaError("Compra PAPER DCA sin observación coherente")
            # El libro ya validó estos decimales calculados por el motor.
            # Un importe ajustado puede tener más dígitos que un dato de entrada.
            price = str(Decimal(event["price"]))
            grouped.setdefault(execution_t, []).append({"price": price,
                "trigger": event.get("trigger_kind"), "spent": event["spent"]})
        marks = []
        for execution_t, buys in sorted(grouped.items()):
            if len({item["price"] for item in buys}) != 1:
                raise PaperDcaError("Compras PAPER DCA con precios incompatibles en una ventana")
            marks.append({"execution_t": execution_t, "signal_t": execution_t - DAY,
                          "price": buys[0]["price"], "count": len(buys),
                          "triggers": [item["trigger"] for item in buys],
                          "spent": [item["spent"] for item in buys]})
        return {"schema": "orbis.paper-dca.chart.v1", "environment": "PAPER",
                "execution": "NO_EXCHANGE_ORDERS", "session_id": session_id,
                "symbol": session["symbol"], "dataset_hash": session["dataset_hash"],
                "seed_count": len(seed), "observed_count": len(observed_at),
                "candles": candles, "buys": marks}

    def comparison(self, session_id: str) -> dict:
        """Referencia DCA histórica exacta y hechos PAPER, sin falso delta."""
        session = self.detail(session_id)
        if session["state"] is None:
            raise PaperDcaError("Estado DCA no verificable; comparación oculta")
        matches = [run for run in self.store.runs()
                   if run["version_id"] == session["version_id"]
                   and run["dataset_id"] == session["dataset_id"]
                   and run["dataset_hash"] == session["dataset_hash"]]
        if session["source_run_id"] is not None:
            reference = next((run for run in matches if run["id"] == session["source_run_id"]), None)
        else:
            reference = max(matches, key=lambda run: (run["created_at"], run["id"])) if matches else None
        historical = None
        if session["source_run_id"] is not None and reference is None:
            historical = {"run_id": session["source_run_id"], "created_at": None,
                          "engine_version": None, "verification": {"verified": False,
                          "status": "mismatch", "reason": "La prueba DCA de origen no existe o cambió de versión/histórico"},
                          "conditional_metrics": None}
        if reference is not None:
            saved = None
            try:
                saved = self.store.run(reference["id"])
                verification = verify_dca_run(self.store, self.lab, saved)
            except LabError:
                verification = {"verified": False, "status": "mismatch",
                                "reason": "La simulación DCA guardada ya no está disponible"}
            historical = {"run_id": reference["id"], "created_at": reference["created_at"],
                          "engine_version": reference["engine_version"],
                          "verification": verification,
                          "conditional_metrics": saved["results"]["conditional"]["metrics"]
                          if saved is not None and verification["verified"] else None}
        events = session["events"]
        state = session["state"]
        return {"session_id": session_id, "version_id": session["version_id"],
                "source_run_id": session["source_run_id"],
                "dataset_id": session["dataset_id"], "dataset_hash": session["dataset_hash"],
                "status": session["status"], "historical": historical,
                "paper": {"observed_days": sum(row["kind"] == "OBSERVATION" for row in events),
                          "paper_buys": sum(row["kind"] == "BUY" for row in events),
                          "spent_usdt": state["spent"], "fees_usdt": state["fees"],
                          "units": state["units"], "remaining_cash_usdt": state["cash"]},
                "comparison_basis": "NOT_DIRECTLY_COMPARABLE",
                "reason": "Periodo, precio bid/ask y ejecución difieren; no se calcula una diferencia de rentabilidad DCA LAB-PAPER"}

    def reserve_comparison(self, session_id: str, validation_id: str) -> dict:
        """Compare a pinned PAPER snapshot with a replayed reserve of the same rule."""
        lab_epoch = lab_access.epoch(self.lab.path)
        automation_key = self.store.current_passphrase()
        if (lab_format(self.lab.path) == "encrypted_or_invalid" and lab_epoch is None
                or automation_format(self.store.path) == "encrypted_or_invalid" and automation_key is None):
            raise PaperDcaUnavailable("Desbloquea DCA y LAB para comparar la reserva")
        with self.store.connect() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM paper_dca_sessions WHERE id=?", (session_id,)).fetchone()
            reserve_row = db.execute("SELECT * FROM dca_validation_runs WHERE id=?", (validation_id,)).fetchone()
            if row is None:
                raise PaperDcaError("Sesión PAPER DCA no encontrada")
            if reserve_row is None:
                raise PaperDcaError("Validación DCA no encontrada")
            session = dict(row)
            reserve = self.store._validation_payload(reserve_row)
            events = [dict(item) for item in db.execute("""SELECT * FROM paper_dca_events
                WHERE session_id=? ORDER BY execution_t,sequence_no""", (session_id,))]

        try:
            rule = validate_dca(json.loads(session["rule_json"]))
            if (any(session[key] != reserve[key] for key in ("version_id", "dataset_id", "dataset_hash"))
                    or session["engine_version"] != reserve["dca_engine_version"]
                    or content_hash(rule) != reserve["definition_hash"]
                    or reserve["test_end_t"] + DAY > session["started_at_ns"] // 1_000_000_000):
                raise LabError("La reserva requiere la misma versión, histórico y motor, con un tramo anterior a PAPER")
            verified = verify_dca_validation(self.store, self.lab, reserve)
            if not verified["verified"]:
                raise LabError(verified["reason"])
            dataset, raw = self.lab.dataset(session["dataset_id"])
            if (dataset["hash"] != session["dataset_hash"] or not dataset["verified_public"] or dataset["gaps"]
                    or dataset["timeframe"] != "1d" or dataset["symbol"] != session["symbol"]
                    or dataset["source"] != session["source"] or session["source"] != "mexc-spot-public"):
                raise LabError("El histórico no coincide con el origen PAPER")
            seed, gaps = validate_candles(raw, "1d", now=session["started_at_ns"] // 1_000_000_000)
            if gaps:
                raise LabError("El histórico inicial contiene huecos")
            paper = replay_paper_comparison(session, events, rule, seed, quote_at=self._quote)
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            if isinstance(exc, LabError):
                raise PaperDcaError(str(exc)) from None
            raise PaperDcaError("La evidencia archivada de la comparación no es verificable") from None
        if lab_access.epoch(self.lab.path) is not lab_epoch or self.store.current_passphrase() != automation_key:
            raise PaperDcaUnavailable("DCA o LAB cambió de sesión durante la comparación")
        metrics = reserve["out_of_sample"]["results"]["conditional"]["metrics"]
        return {"schema": "orbis.paper-dca.reserve-comparison.v1", "execution_authority": "NONE",
                "symbol": session["symbol"], "version_id": session["version_id"],
                "dataset_hash": session["dataset_hash"], "engine_version": session["engine_version"],
                "paper": paper, "reserve": {"validation_id": validation_id, "result_hash": reserve["result_hash"],
                    "start_t": reserve["test_start_t"], "end_t": reserve["test_end_t"] + DAY,
                    "days": reserve["test_bars"], "metrics": {
                        key: rule["budget_total"] if key == "initial_capital" else metrics[key]
                        for key in paper["metrics"]}},
                "comparison_basis": "DISTINCT_WINDOWS_AND_EXECUTION"}

    def gap_analysis(self, session_id: str) -> dict:
        """Explora decisiones perdidas con cierres históricos, sin tocar PAPER."""
        return self._gap_evidence(session_id)[2]

    def _gap_evidence(self, session_id: str) -> tuple[dict, list[dict], dict]:
        started_ns = self.clock_ns()
        now = started_ns // 1_000_000_000
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM paper_dca_sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise PaperDcaError("Sesión PAPER DCA no encontrada")
            if row["status"] != "GAP":
                raise PaperDcaError("Sólo una sesión DCA con hueco admite análisis posterior")
            pinned = tuple(row)
        if row["engine_version"] not in SUPPORTED_ENGINE_VERSIONS:
            raise PaperDcaError("Motor DCA de la sesión no disponible")
        missed = (now // DAY * DAY - row["next_exec_t"]) // DAY + 1
        if not 1 <= missed <= MAX_MISSED_DAYS:
            raise PaperDcaError("El hueco DCA supera la cobertura teórica verificable")
        try:
            rule = validate_dca(json.loads(row["rule_json"]))
            overlap = json.loads(row["overlap_json"])
            original_state = json.loads(row["state_json"])
            DcaState.from_json(original_state)
            version = self.store.version(row["version_id"])
            dataset, seed = self.lab.dataset(row["dataset_id"])
        except (LabError, KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise PaperDcaError(f"Origen DCA del hueco no verificable: {exc}") from exc
        if (rule != json.loads(row["rule_json"]) or validate_dca(version["definition"]) != rule
                or row["engine_version"] == "dca-1.1" and rule.get("reference_mode", "initial") != "initial"
                or row["source"] != "mexc-spot-public"
                or row["symbol"] not in MexcAdapter.known_pairs
                or dataset["hash"] != row["dataset_hash"] or dataset["symbol"] != row["symbol"]
                or dataset["source"] != row["source"] or dataset["timeframe"] != "1d"
                or not dataset["verified_public"] or dataset["gaps"]):
            raise PaperDcaError("Regla o histórico DCA cambió desde la activación PAPER")
        count = dca_paper_overlap_count(rule)
        if (not isinstance(overlap, list) or len(overlap) != count
                or not isinstance(overlap[0], dict) or type(overlap[0].get("t")) is not int):
            raise PaperDcaError("Solapamiento DCA no verificable")
        if dca_signal_window_key(rule) and row["tick_index"] == 0 and overlap != seed[-count:]:
            label = "del volumen" if "volume_gate" in rule else "de la media móvil"
            raise PaperDcaError(f"El solapamiento {label} no coincide con el histórico inicial")
        ema_origin = _ema_origin_for_rule(rule, seed)
        volume_origin = seed[-count:] if "volume_gate" in rule else None
        with self.store.connect() as db:
            current = db.execute("SELECT * FROM paper_dca_sessions WHERE id=?", (session_id,)).fetchone()
            if (current is None or tuple(current) != pinned
                    or not self._state_valid(db, current, original_state, rule,
                                             ema_origin=ema_origin, volume_origin=volume_origin)):
                raise PaperDcaError("Estado DCA del hueco no verificable")
        downloaded = self.adapter_factory().get_candles(
            row["symbol"], timeframe="1d", limit=missed + count, since=overlap[0]["t"], before=now // DAY * DAY)
        if not isinstance(downloaded, list):
            raise PaperDcaUnavailable("Histórico MEXC no disponible para investigar el hueco")
        checked_ns = self.clock_ns()
        if checked_ns < started_ns or checked_ns // (DAY * 1_000_000_000) != started_ns // (DAY * 1_000_000_000):
            raise PaperDcaUnavailable("El reloj cambió durante la investigación DCA")
        with self.store.connect() as db:
            fresh = db.execute("SELECT * FROM paper_dca_sessions WHERE id=?", (session_id,)).fetchone()
            if (fresh is None or tuple(fresh) != pinned
                    or not self._state_valid(db, fresh, original_state, rule,
                                             ema_origin=ema_origin, volume_origin=volume_origin)):
                raise PaperDcaUnavailable("La sesión PAPER DCA cambió durante el análisis")
        try:
            current_version = self.store.version(row["version_id"])
            current_dataset, _ = self.lab.dataset(row["dataset_id"])
            current_rule = validate_dca(current_version["definition"])
        except LabError:
            raise PaperDcaUnavailable("El origen LAB/DCA cambió durante el análisis") from None
        if current_rule != rule or current_dataset["hash"] != row["dataset_hash"]:
            raise PaperDcaUnavailable("El origen LAB/DCA cambió durante el análisis")
        snapshot = {"id": session_id, "status": row["status"], "rule": rule,
                    "state": original_state, "overlap": overlap,
                    "next_exec_t": row["next_exec_t"], "tick_index": row["tick_index"],
                    "dataset_id": row["dataset_id"], "version_id": row["version_id"],
                    "symbol": row["symbol"], "source": row["source"],
                    "dataset_hash": row["dataset_hash"], "engine_version": row["engine_version"],
                    "reconstruction_now": now}
        return snapshot, downloaded, analyze_dca_paper_gap(snapshot, downloaded, now=now)

    def save_gap_analysis(self, session_id: str) -> dict:
        snapshot, downloaded, result = self._gap_evidence(session_id)
        saved = self.store.save_paper_dca_gap_report(snapshot, downloaded, result)
        return result | {"archive": {key: saved[key] for key in ("id", "hash", "created_at", "verified")}}

    def gap_analyses(self, session_id: str) -> list[dict]:
        with self.store.connect() as db:
            identifiers = [row[0] for row in db.execute(
                "SELECT id FROM paper_dca_gap_reports WHERE session_id=? ORDER BY created_at DESC,id DESC",
                (session_id,))]
        if not identifiers:
            self.detail(session_id)
        return [{key: report[key] for key in ("id", "session_id", "hash", "created_at", "verified")}
                | {"missed_days": report["result"]["missed_days"]}
                for report_id in identifiers
                for report in (self.saved_gap_analysis(session_id, report_id),)]

    def saved_gap_analysis(self, session_id: str, report_id: str) -> dict:
        report = self.store.paper_dca_gap_report(report_id)
        if report["session_id"] != session_id:
            raise PaperDcaError("El informe DCA no pertenece a esta sesión")
        snapshot = report["snapshot"]
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM paper_dca_sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise PaperDcaError("Sesión PAPER DCA no encontrada")
            if (row["status"] != snapshot["status"]
                    or row["version_id"] != snapshot["version_id"]
                    or row["dataset_id"] != snapshot["dataset_id"]
                    or row["dataset_hash"] != snapshot["dataset_hash"]
                    or row["symbol"] != snapshot["symbol"]
                    or row["source"] != snapshot["source"]
                    or row["engine_version"] != snapshot["engine_version"]
                    or row["next_exec_t"] != snapshot["next_exec_t"]
                    or row["tick_index"] != snapshot["tick_index"]
                    or json.loads(row["rule_json"]) != snapshot["rule"]
                    or json.loads(row["state_json"]) != snapshot["state"]
                    or json.loads(row["overlap_json"]) != snapshot["overlap"]):
                raise PaperDcaError("La sesión original del informe DCA cambió")
        try:
            version = self.store.version(snapshot["version_id"])
            dataset, seed = self.lab.dataset(snapshot["dataset_id"])
        except LabError as exc:
            raise PaperDcaError(f"Origen del informe DCA no verificable: {exc}") from exc
        if (validate_dca(version["definition"]) != snapshot["rule"]
                or dataset["hash"] != snapshot["dataset_hash"]
                or dataset["symbol"] != snapshot["symbol"]
                or dataset["source"] != snapshot["source"]):
            raise PaperDcaError("El origen del informe DCA cambió")
        ema_origin = _ema_origin_for_rule(snapshot["rule"], seed)
        if ema_origin is not None:
            with self.store.connect() as db:
                current = db.execute("SELECT * FROM paper_dca_sessions WHERE id=?", (session_id,)).fetchone()
                if (current is None or tuple(current) != tuple(row)
                        or not self._state_valid(db, current, snapshot["state"], snapshot["rule"], ema_origin=ema_origin)):
                    raise PaperDcaError("La semilla EMA del informe DCA no es verificable")
        return report

    def stop(self, session_id: str) -> dict:
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM paper_dca_sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise PaperDcaError("Sesión PAPER DCA no encontrada")
            if row["status"] == "ACTIVE":
                db.execute("UPDATE paper_dca_sessions SET status='STOPPED',status_reason='Parada manual' WHERE id=?",
                           (session_id,))
        return self.detail(session_id)

    def _gap(self, session_id: str, expected_t: int, reason: str) -> dict:
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("""UPDATE paper_dca_sessions SET status='GAP',status_reason=?
                          WHERE id=? AND status='ACTIVE' AND next_exec_t=?""",
                       (reason, session_id, expected_t))
        return self.detail(session_id) | {"poll_result": "GAP" if changed.rowcount else "CHANGED"}

    @staticmethod
    def _quote(raw: dict | None, symbol: str, now_ns: int) -> tuple[Decimal, Decimal, dict]:
        if not isinstance(raw, dict) or raw.get("symbol") != symbol or raw.get("source") != "mexc-spot-public":
            raise PaperDcaUnavailable("Ticker MEXC ausente o de otro mercado")
        try:
            event_ns = raw["event_time_ns"]
            if type(event_ns) is not int or event_ns > now_ns + 2_000_000_000 or now_ns - event_ns > QUOTE_FRESH_NS:
                raise PaperDcaUnavailable("Ticker MEXC obsoleto o futuro")
            bid = number(raw.get("bid"), positive=True)
            ask = number(raw.get("ask"), positive=True)
            last = number(raw.get("last"), positive=True)
        except (KeyError, TypeError, LabError) as exc:
            raise PaperDcaUnavailable("Ticker MEXC incompleto") from exc
        if bid > ask or ask / bid > Decimal("1.02"):
            raise PaperDcaUnavailable("Bid/ask MEXC incoherente o spread superior al 2 %")
        return bid, ask, {"symbol": symbol, "source": raw["source"], "event_time_ns": event_ns,
                          "bid": str(bid), "ask": str(ask), "last": str(last)}

    def poll(self, session_id: str) -> dict:
        now_ns = self.clock_ns()
        now = now_ns // 1_000_000_000
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM paper_dca_sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise PaperDcaError("Sesión PAPER DCA no encontrada")
            expected_t = row["next_exec_t"]
            status = row["status"]
            engine_version = row["engine_version"]
            symbol = row["symbol"]
        if status != "ACTIVE":
            return self.detail(session_id) | {"poll_result": status}
        try:
            overlap = json.loads(row["overlap_json"])
            stored_rule = json.loads(row["rule_json"])
            if not isinstance(overlap, list) or not isinstance(stored_rule, dict):
                raise ValueError("Formato inválido")
        except (TypeError, ValueError):
            return self._gap(session_id, expected_t, "Regla o solapamiento PAPER DCA ilegible")
        if engine_version not in SUPPORTED_ENGINE_VERSIONS:
            return self._gap(session_id, expected_t, "Versión del motor DCA distinta; sesión no reproducible")
        if (engine_version == "dca-1.1"
                and stored_rule.get("reference_mode", "initial") != "initial"):
            return self._gap(session_id, expected_t, "Referencia DCA no compatible con el motor guardado")
        if now < expected_t:
            return self.detail(session_id) | {"poll_result": "WAIT"}
        if now >= expected_t + WINDOW_SECONDS:
            return self._gap(session_id, expected_t, "Ventana diaria perdida; no se reconstruyen compras")
        lab_kind = lab_format(self.lab.path)
        if lab_kind in ("missing", "empty") or (lab_kind == "encrypted_or_invalid"
                                                and self.lab.current_passphrase() is None):
            raise PaperDcaUnavailable("LAB no disponible o cifrado bloqueado")
        try:
            version = self.store.version(row["version_id"])
            pinned_rule = validate_dca(version["definition"])
            dataset, seed = self.lab.dataset(row["dataset_id"])
        except LabError as exc:
            if "LAB no se pudo abrir" in str(exc):
                raise PaperDcaUnavailable("LAB cifrado no disponible") from exc
            return self._gap(session_id, expected_t, f"Entradas DCA no verificables: {exc}")
        except (KeyError, TypeError, ValueError) as exc:
            return self._gap(session_id, expected_t, f"Entradas DCA no verificables: {exc}")
        if pinned_rule != stored_rule:
            return self._gap(session_id, expected_t, "La regla DCA cambió desde la activación PAPER")
        count = dca_paper_overlap_count(pinned_rule)
        window_key = dca_signal_window_key(pinned_rule)
        if (len(overlap) != count or not isinstance(overlap[0], dict)
                or type(overlap[0].get("t")) is not int
                or window_key and row["tick_index"] == 0 and overlap != seed[-count:]):
            return self._gap(session_id, expected_t, "Solapamiento PAPER DCA no verificable")
        if (dataset["hash"] != row["dataset_hash"] or dataset["symbol"] != symbol
                or dataset["source"] != row["source"] or dataset["timeframe"] != "1d"
                or not dataset["verified_public"] or dataset["gaps"]):
            return self._gap(session_id, expected_t, "El dataset DCA cambió desde la activación PAPER")
        ema_origin = _ema_origin_for_rule(pinned_rule, seed)
        volume_origin = seed[-count:] if "volume_gate" in pinned_rule else None
        try:
            saved_state = json.loads(row["state_json"])
        except (TypeError, ValueError):
            saved_state = None
        with self.store.connect() as db:
            current = db.execute("SELECT * FROM paper_dca_sessions WHERE id=?", (session_id,)).fetchone()
            state_valid = (current is not None and tuple(current) == tuple(row)
                           and self._state_valid(db, current, saved_state, stored_rule,
                                                 ema_origin=ema_origin, volume_origin=volume_origin))
        if not state_valid:
            return self._gap(session_id, expected_t, "Estado DCA no verificable; sesión detenida")
        adapter = self.adapter_factory()
        fresh_rows = adapter.get_candles(symbol, timeframe="1d", limit=count + 1,
                                         since=overlap[0]["t"], before=expected_t)
        if not isinstance(fresh_rows, list) or len(fresh_rows) != count + 1:
            message = (f"MEXC no devolvió las {count + 1} velas diarias esperadas" if window_key
                       else "MEXC no devolvió tres velas diarias verificables")
            raise PaperDcaUnavailable(message)
        try:
            bars, gaps = validate_candles(fresh_rows, "1d", now=self.clock_ns() // 1_000_000_000)
        except LabError as exc:
            raise PaperDcaUnavailable(f"Velas MEXC inválidas: {exc}") from exc
        signal_bar = bars[-1]
        if gaps or [bar.json() for bar in bars[:count]] != overlap or signal_bar.t != expected_t - DAY:
            return self._gap(session_id, expected_t, "Histórico MEXC revisado o discontinuo")
        quote_raw = adapter.get_paper_quote(symbol)
        bid, ask, quote = self._quote(quote_raw, symbol, self.clock_ns())
        if quote["event_time_ns"] < expected_t * 1_000_000_000:
            raise PaperDcaUnavailable("Ticker MEXC anterior a la ejecución DCA programada")
        outcome = "OBSERVED"
        with self.store.connect() as db, localcontext() as context:
            context.prec = precision_for_version(engine_version)
            try:
                db.execute("ATTACH DATABASE ? AS lab KEY ?",
                           (str(self.lab.path), self.lab.current_passphrase() or ""))
                if db.execute("PRAGMA lab.user_version").fetchone()[0] not in DATASET_READ_VERSIONS:
                    raise PaperDcaUnavailable("Versión LAB no compatible")
            except sqlcipher3.DatabaseError:
                raise PaperDcaUnavailable("LAB cifrado no disponible") from None
            db.execute("BEGIN IMMEDIATE")
            fresh = db.execute("SELECT * FROM paper_dca_sessions WHERE id=?", (session_id,)).fetchone()
            if fresh["status"] != "ACTIVE" or fresh["next_exec_t"] != expected_t:
                outcome = "CHANGED"
            else:
                version_row = db.execute("SELECT definition_json FROM dca_versions WHERE id=?",
                                         (fresh["version_id"],)).fetchone()
                dataset_row = db.execute("SELECT * FROM lab.datasets WHERE id=?",
                                         (fresh["dataset_id"],)).fetchone()
                try:
                    locked_rule = (validate_dca(json.loads(version_row["definition_json"]))
                                   if version_row else None)
                    material = ({"symbol": dataset_row["symbol"], "timeframe": dataset_row["timeframe"],
                                 "source": dataset_row["source"],
                                 "candles": json.loads(dataset_row["candles_json"])}
                                if dataset_row else None)
                    if material is not None and dataset_row["verified_public"]:
                        material["verified_public"] = True
                    inputs_ok = (fresh["rule_json"] == row["rule_json"]
                                 and fresh["state_json"] == row["state_json"]
                                 and fresh["overlap_json"] == row["overlap_json"]
                                 and fresh["version_id"] == row["version_id"]
                                 and fresh["dataset_id"] == row["dataset_id"]
                                 and fresh["dataset_hash"] == row["dataset_hash"]
                                 and fresh["engine_version"] == engine_version
                                 and locked_rule == stored_rule
                                 and dataset_row is not None
                                 and dataset_row["hash"] == row["dataset_hash"]
                                 and content_hash(material) == row["dataset_hash"]
                                 and dataset_row["symbol"] == symbol
                                 and dataset_row["source"] == row["source"]
                                 and dataset_row["timeframe"] == "1d"
                                 and bool(dataset_row["verified_public"])
                                 and json.loads(dataset_row["gaps_json"]) == [])
                    if inputs_ok:
                        inputs_ok = self._state_valid(
                            db, fresh, json.loads(fresh["state_json"]), stored_rule, ema_origin=ema_origin)
                except (LabError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                    inputs_ok = False
                observed_ns = self.clock_ns()
                if not inputs_ok:
                    db.execute("UPDATE paper_dca_sessions SET status='GAP',status_reason=? WHERE id=?",
                               ("Entradas DCA modificadas durante la observación", session_id))
                    outcome = "GAP"
                elif observed_ns // 1_000_000_000 >= expected_t + WINDOW_SECONDS:
                    db.execute("UPDATE paper_dca_sessions SET status='GAP',status_reason=? WHERE id=?",
                               ("Respuesta MEXC fuera de la ventana", session_id))
                    outcome = "GAP"
                elif observed_ns < now_ns or observed_ns < expected_t * 1_000_000_000:
                    # Un ajuste del reloj mientras se consulta MEXC no puede
                    # dejar una compra ficticia fechada antes de su ventana.
                    # Reintentar dentro de la ventana es seguro: no se escribió
                    # ningún evento en esta transacción.
                    raise PaperDcaUnavailable("Reloj local retrocedió durante la observación DCA")
                elif observed_ns - quote["event_time_ns"] > QUOTE_FRESH_NS:
                    raise PaperDcaUnavailable("Ticker MEXC envejeció durante la decisión")
                else:
                    state = DcaState.from_json(json.loads(fresh["state_json"]))
                    rule = json.loads(fresh["rule_json"])
                    events, skipped = apply_dca_step(state, rule, signal_time=signal_bar.t,
                        signal_close=signal_bar.c, execution_time=expected_t,
                        execution_price=ask, mark_price=bid,
                        index=fresh["tick_index"] + 1, engine_version=engine_version,
                        signal_bars=bars if window_key else None)
                    observation = {"kind": "OBSERVATION", "signal_candle": signal_bar.json(),
                                   "quote": quote, "engine_version": engine_version,
                                   "execution_model": "BID_ASK_APPROXIMATION_NO_ORDER"}
                    if window_key:
                        observation[window_key] = [bar.json() for bar in bars]
                    if ema_origin is not None and fresh["tick_index"] == 0:
                        observation["ema_origin"] = ema_origin
                    payloads = decision_payloads(observation, events, skipped)
                    for index, payload in enumerate(payloads):
                        db.execute("INSERT INTO paper_dca_events VALUES (?,?,?,?,?,?)",
                                   (session_id, expected_t, index, observed_ns,
                                    quote["event_time_ns"], _json(payload)))
                    db.execute("""UPDATE paper_dca_sessions
                                  SET state_json=?,overlap_json=?,next_exec_t=?,tick_index=?,last_quote_json=?
                                  WHERE id=?""",
                               (_json(state.json()), _json([bar.json() for bar in bars[-count:]]),
                                expected_t + DAY, fresh["tick_index"] + 1, _json(quote), session_id))
        return self.detail(session_id) | {"poll_result": outcome}


def create_paper_dca_router(automation_path: Path, lab_path: Path, *, adapter_factory=MexcAdapter,
                            clock_ns=time.time_ns, paper: PaperDca | None = None,
                            worker: PaperDcaWorker | None = None) -> APIRouter:
    paper = paper or PaperDca(automation_path, lab_path, adapter_factory=adapter_factory, clock_ns=clock_ns)
    router = APIRouter(prefix="/automation/dca/paper", tags=["paper-dca"])
    local_hosts = frozenset(("127.0.0.1", "::1", "localhost", "testserver", "testclient"))

    def local_only(request: Request) -> None:
        host = request.client.host if request.client else None
        if host not in local_hosts or request.url.hostname not in local_hosts:
            raise HTTPException(403, "PAPER DCA sólo local")
        origin = request.headers.get("origin")
        if origin is not None and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Origen externo rechazado")

    def respond(fn, *args):
        try:
            return fn(*args)
        except PaperDcaUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        except (PaperDcaError, LabError) as exc:
            code = 404 if "no encontrada" in str(exc) or "no encontrado" in str(exc) else 422
            raise HTTPException(code, str(exc)) from exc

    @router.post("/sessions", dependencies=[Depends(local_only)], status_code=201)
    def create(body: PaperDcaCreate) -> dict:
        session = respond(paper.create, body.version_id, body.dataset_id, body.source_run_id)
        if worker is not None:
            worker.wake()
        return session

    @router.get("/worker", dependencies=[Depends(local_only)])
    def worker_status() -> dict:
        return worker.snapshot() if worker is not None else {
            "running": False, "last_cycle_ns": None, "last_error": None,
            "heartbeat_age_seconds": None, "error_count": 0,
            "mode": "NOT_CONFIGURED", "execution": "NO_EXCHANGE_ORDERS"}

    @router.get("/sessions", dependencies=[Depends(local_only)])
    def sessions() -> list[dict]:
        return paper.sessions()

    @router.get("/sessions/{session_id}", dependencies=[Depends(local_only)])
    def detail(session_id: str) -> dict:
        return respond(paper.detail, session_id)

    @router.get("/sessions/{session_id}/chart", dependencies=[Depends(local_only)])
    def chart(session_id: str) -> dict:
        return respond(paper.chart, session_id)

    @router.get("/sessions/{session_id}/comparison", dependencies=[Depends(local_only)])
    def comparison(session_id: str) -> dict:
        return respond(paper.comparison, session_id)

    @router.get("/sessions/{session_id}/reserve-comparison", dependencies=[Depends(local_only)])
    def reserve_comparison(session_id: UUID, validation_id: UUID, response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return respond(paper.reserve_comparison, str(session_id), str(validation_id))

    @router.get("/sessions/{session_id}/gap-analysis", dependencies=[Depends(local_only)])
    def gap_analysis(session_id: str) -> dict:
        return respond(paper.gap_analysis, session_id)

    @router.post("/sessions/{session_id}/gap-analyses", dependencies=[Depends(local_only)])
    def save_gap_analysis(session_id: str) -> dict:
        return respond(paper.save_gap_analysis, session_id)

    @router.get("/sessions/{session_id}/gap-analyses", dependencies=[Depends(local_only)])
    def gap_analyses(session_id: str) -> list[dict]:
        return respond(paper.gap_analyses, session_id)

    @router.get("/sessions/{session_id}/gap-analyses/{report_id}", dependencies=[Depends(local_only)])
    def saved_gap_analysis(session_id: str, report_id: str) -> dict:
        return respond(paper.saved_gap_analysis, session_id, report_id)

    @router.post("/sessions/{session_id}/poll", dependencies=[Depends(local_only)])
    def poll(session_id: str) -> dict:
        return respond(paper.poll, session_id)

    @router.post("/sessions/{session_id}/stop", dependencies=[Depends(local_only)])
    def stop(session_id: str) -> dict:
        session = respond(paper.stop, session_id)
        if worker is not None:
            worker.wake()
        return session

    return router
