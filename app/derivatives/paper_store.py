"""Libro local de PAPER manual o automático para perpetuos; sin credenciales.

Las posiciones conservan OPEN/CLOSE y liquidaciones PAPER de financiación
derivadas de tasas públicas. Ningún evento representa una orden o pago real
enviado a GRVT.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

import sqlcipher3

from app.lab.engine import INTERVALS, paper_precision_for_engine
from app.lab.paper_indicator_state import build_paper_indicator_state
from app.core.paper_archive import full_archive_matches, observed_archive_matches
from app.core.paper_archive_chain import next_archive_digest

from app.derivatives.isolated import IsolatedPerpPosition
from app.derivatives.margin_threshold import estimate_maintenance_threshold
from app.core.local_sqlite import ensure_private_sqlite
from app.derivatives.funding import HOUR_NS, FundingHistoryError, FundingRecord, estimate_funding
from app.derivatives.paper_math import InstrumentSpec, MarginRules, PaperPerpError, PerpQuote, decimal_string
from app.derivatives.paper_access import paper_perp_access, paper_perp_format
from app.derivatives.paper_schema import CURRENT_PAPER_SCHEMA


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decimal(row: sqlite3.Row, field: str) -> Decimal:
    return Decimal(row[field])


class PaperRequestConflict(PaperPerpError):
    pass


class PaperCloseQuoteDeferred(PaperPerpError):
    """La acción sigue pendiente y necesita otra cotización."""


class PaperCloseWindowElapsed(PaperPerpError):
    """La decisión ya no se puede ejecutar automáticamente."""

    def __init__(self, reasons: tuple[str, str]):
        super().__init__(reasons[0])
        self.reasons = reasons


# La cobertura de ticker es un requisito para cierres automáticos PAPER, no
# una garantía de fill real. Compartido con el observador de riesgo.
RISK_MAX_GAP_NS = 20_000_000_000
_STREAM_CLOSE_REASONS = ("STOP_MARK_STREAM", "TRAILING_STOP_MARK_STREAM", "TARGET_MARK_STREAM")


def signal_action_expiry(action, step: int, now_ns: int) -> tuple[str, str] | None:
    if (action["reason"] in _STREAM_CLOSE_REASONS
            and now_ns - action["recorded_at_ns"] > RISK_MAX_GAP_NS):
        return ("Ticker de riesgo vencido; cierre PAPER no reconstruido",
                "Cierre PAPER de riesgo vencido; posición abierta para cierre manual")
    if now_ns >= (action["candle_t"] + 2 * step) * 1_000_000_000:
        return ("Ventana de ejecución vencida", "La decisión PAPER venció sin cotización actual")
    return None


class PaperPerpStore:
    def __init__(self, path: Path, *, passphrase: str | None = None,
                 allow_create: bool = True, clock_ns: Callable[[], int] = time.time_ns):
        self.path = Path(path).expanduser().absolute()
        self.passphrase = passphrase
        self.allow_create = allow_create
        self.clock_ns = clock_ns

    def current_passphrase(self) -> str | None:
        return self.passphrase if self.passphrase is not None else paper_perp_access.get(self.path)

    @contextmanager
    def connect(self):
        kind = paper_perp_format(self.path)
        key = self.current_passphrase()
        if kind == "encrypted_or_invalid" and key is None:
            raise PaperPerpError("GRVT PAPER cifrado bloqueado; desbloquéalo en Ajustes")
        if kind in ("missing", "empty") and not self.allow_create:
            raise PaperPerpError("Crea primero la base GRVT PAPER cifrada en Ajustes")
        if kind == "plain" and key is not None:
            raise PaperPerpError("Clave GRVT PAPER activa para una base sin cifrar")
        ensure_private_sqlite(self.path)
        db = sqlcipher3.connect(self.path, timeout=10)
        try:
            db.row_factory = sqlcipher3.Row
            if key is not None:
                db.execute("PRAGMA key = '" + key.replace("'", "''") + "'")
            db.execute("PRAGMA foreign_keys=ON")
            try:
                version = db.execute("PRAGMA user_version").fetchone()[0]
            except sqlcipher3.DatabaseError:
                raise PaperPerpError("GRVT PAPER no se pudo abrir; contraseña o formato incompatibles") from None
            if version in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13):
                db.execute("BEGIN IMMEDIATE")
                # Otro proceso puede haber migrado mientras esperábamos el
                # bloqueo. La versión autoritativa se lee de nuevo dentro.
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version in (0, 1, 2):
                    if version == 0:
                        db.execute("""CREATE TABLE paper_perp_accounts (
                            id TEXT PRIMARY KEY, name TEXT NOT NULL,
                            initial_balance TEXT NOT NULL, max_position_notional TEXT NOT NULL,
                            max_total_notional TEXT NOT NULL, max_leverage TEXT NOT NULL,
                            max_positions INTEGER NOT NULL, fee_rate TEXT NOT NULL,
                            slippage_bps TEXT NOT NULL, created_at TEXT NOT NULL,
                            stopped_at TEXT
                        )""")
                        db.execute("""CREATE TABLE paper_perp_positions (
                            id TEXT PRIMARY KEY,
                            account_id TEXT NOT NULL REFERENCES paper_perp_accounts(id),
                            symbol TEXT NOT NULL, instrument TEXT NOT NULL,
                            side TEXT NOT NULL CHECK(side IN ('LONG','SHORT')),
                            size TEXT NOT NULL, leverage TEXT NOT NULL,
                            collateral TEXT NOT NULL, entry_price TEXT NOT NULL,
                            entry_mark TEXT NOT NULL, entry_fee TEXT NOT NULL,
                            opened_at_ns INTEGER NOT NULL, opened_recorded_at TEXT NOT NULL,
                            margin_rules_json TEXT NOT NULL, margin_rules_at_ns INTEGER NOT NULL,
                            closed_at_ns INTEGER, exit_price TEXT, exit_mark TEXT,
                            exit_fee TEXT, realized_pnl TEXT, uncovered_loss TEXT,
                            instrument_spec_json TEXT, instrument_spec_at_ns INTEGER
                        )""")
                        db.execute("""CREATE INDEX ix_paper_perp_positions_account_open
                                      ON paper_perp_positions(account_id, closed_at_ns)""")
                        db.execute("""CREATE TABLE paper_perp_events (
                            id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES paper_perp_accounts(id),
                            position_id TEXT NOT NULL REFERENCES paper_perp_positions(id),
                            kind TEXT NOT NULL CHECK(kind IN ('OPEN','CLOSE','OBSERVE')),
                            request_id TEXT, observed_at_ns INTEGER NOT NULL,
                            recorded_at TEXT NOT NULL, details_json TEXT NOT NULL
                        )""")
                        db.execute("""CREATE TABLE paper_perp_requests (
                            request_id TEXT PRIMARY KEY, digest TEXT NOT NULL,
                            response_json TEXT NOT NULL
                        )""")
                    elif version == 1:
                        # Filas previas siguen intactas y sin especificación.
                        db.execute("ALTER TABLE paper_perp_positions ADD COLUMN instrument_spec_json TEXT")
                        db.execute("ALTER TABLE paper_perp_positions ADD COLUMN instrument_spec_at_ns INTEGER")
                    db.execute("""CREATE TABLE paper_signal_sessions (
                        id TEXT PRIMARY KEY,
                        account_id TEXT NOT NULL REFERENCES paper_perp_accounts(id),
                        version_id TEXT NOT NULL, dataset_id TEXT NOT NULL,
                        dataset_hash TEXT NOT NULL, engine_version TEXT NOT NULL,
                        symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
                        definition_json TEXT NOT NULL, candles_json TEXT NOT NULL,
                        started_at_ns INTEGER NOT NULL, last_t INTEGER NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('ACTIVE','GAP','STOPPED','CAPACITY')),
                        status_reason TEXT
                    )""")
                    db.execute("""CREATE TABLE paper_signal_observations (
                        session_id TEXT NOT NULL REFERENCES paper_signal_sessions(id),
                        candle_t INTEGER NOT NULL, observed_at_ns INTEGER NOT NULL,
                        entry_match INTEGER NOT NULL, exit_match INTEGER,
                        evidence_json TEXT NOT NULL,
                        PRIMARY KEY(session_id,candle_t)
                    )""")
                    db.execute("PRAGMA user_version=3")
                    version = 3
                if version == 3:
                    db.execute("ALTER TABLE paper_perp_positions ADD COLUMN funding_paid TEXT NOT NULL DEFAULT '0'")
                    db.execute("ALTER TABLE paper_perp_positions ADD COLUMN funding_checked_through_ns INTEGER")
                    db.execute("""CREATE TABLE paper_perp_funding_events (
                        id TEXT PRIMARY KEY,
                        account_id TEXT NOT NULL REFERENCES paper_perp_accounts(id),
                        position_id TEXT NOT NULL REFERENCES paper_perp_positions(id),
                        request_id TEXT NOT NULL, funding_time_ns INTEGER NOT NULL,
                        rate_percent TEXT NOT NULL, mark_price TEXT NOT NULL,
                        interval_hours INTEGER NOT NULL, payment TEXT NOT NULL,
                        source TEXT NOT NULL, recorded_at TEXT NOT NULL,
                        UNIQUE(position_id, funding_time_ns)
                    )""")
                    db.execute("CREATE INDEX ix_paper_perp_funding_account ON paper_perp_funding_events(account_id, recorded_at)")
                    db.execute("PRAGMA user_version=4")
                    version = 4
                if version == 4:
                    db.execute("ALTER TABLE paper_signal_sessions ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'OBSERVE'")
                    db.execute("ALTER TABLE paper_signal_sessions ADD COLUMN auto_size TEXT")
                    db.execute("ALTER TABLE paper_signal_sessions ADD COLUMN auto_leverage TEXT")
                    db.execute("ALTER TABLE paper_signal_sessions ADD COLUMN position_id TEXT")
                    db.execute("ALTER TABLE paper_perp_positions ADD COLUMN origin_session_id TEXT")
                    db.execute("""CREATE TABLE paper_signal_actions (
                        session_id TEXT NOT NULL REFERENCES paper_signal_sessions(id),
                        candle_t INTEGER NOT NULL,
                        action TEXT NOT NULL CHECK(action IN ('OPEN','CLOSE')),
                        request_id TEXT NOT NULL UNIQUE,
                        digest TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('PENDING','EXECUTED','EXPIRED','REJECTED')),
                        position_id TEXT,
                        reason TEXT,
                        recorded_at_ns INTEGER NOT NULL,
                        PRIMARY KEY(session_id,candle_t)
                    )""")
                    db.execute("""CREATE UNIQUE INDEX ux_paper_signal_auto_active
                                  ON paper_signal_sessions(account_id,symbol)
                                  WHERE status='ACTIVE' AND execution_mode='AUTO_PAPER'""")
                    db.execute("PRAGMA user_version=5")
                    version = 5
                if version == 5:
                    db.execute("ALTER TABLE paper_signal_sessions ADD COLUMN risk_last_quote_ns INTEGER")
                    db.execute("PRAGMA user_version=6")
                    version = 6
                if version == 6:
                    db.execute("""CREATE TABLE paper_perp_funding_anchors (
                        position_id TEXT PRIMARY KEY REFERENCES paper_perp_positions(id),
                        funding_time_ns INTEGER NOT NULL,
                        rate_percent TEXT NOT NULL, mark_price TEXT NOT NULL,
                        interval_hours INTEGER NOT NULL, source TEXT NOT NULL,
                        recorded_at TEXT NOT NULL
                    )""")
                    db.execute("""CREATE TABLE paper_perp_funding_legacy (
                        position_id TEXT PRIMARY KEY REFERENCES paper_perp_positions(id)
                    )""")
                    # Las coberturas anteriores no conservaban el registro
                    # público previo a la apertura: nunca presentarlas como
                    # verificadas por el nuevo esquema sin reconsultar datos.
                    db.execute("""INSERT INTO paper_perp_funding_legacy(position_id)
                                  SELECT id FROM paper_perp_positions
                                  WHERE funding_checked_through_ns IS NOT NULL
                                    AND NOT (closed_at_ns=opened_at_ns
                                             AND funding_checked_through_ns=opened_at_ns)""")
                    db.execute("PRAGMA user_version=7")
                    version = 7
                if version == 7:
                    db.execute("ALTER TABLE paper_signal_sessions ADD COLUMN source_run_id TEXT")
                    db.execute("PRAGMA user_version=8")
                    version = 8
                if version == 8:
                    db.execute("""CREATE TABLE paper_signal_candles (
                        session_id TEXT NOT NULL REFERENCES paper_signal_sessions(id),
                        candle_t INTEGER NOT NULL, candle_json TEXT NOT NULL,
                        PRIMARY KEY(session_id,candle_t)
                    )""")
                    for session in db.execute("SELECT id,candles_json FROM paper_signal_sessions"):
                        history = json.loads(session["candles_json"])
                        by_time = {bar["t"]: bar for bar in history}
                        for observation in db.execute("""SELECT candle_t,evidence_json
                            FROM paper_signal_observations WHERE session_id=? ORDER BY candle_t""",
                                                      (session["id"],)):
                            candle = json.loads(observation["evidence_json"]).get("candle")
                            if by_time.get(observation["candle_t"]) != candle:
                                raise PaperPerpError("Vela GRVT PAPER archivada distinta de la observación")
                            db.execute("INSERT INTO paper_signal_candles VALUES (?,?,?)",
                                       (session["id"], observation["candle_t"], _json(candle)))
                    db.execute("PRAGMA user_version=9")
                    version = 9
                if version == 9:
                    db.execute("ALTER TABLE paper_signal_sessions ADD COLUMN indicator_state_json TEXT")
                    for session in db.execute("""SELECT id,definition_json,candles_json,engine_version
                                                 FROM paper_signal_sessions""").fetchall():
                        snapshot = build_paper_indicator_state(
                            json.loads(session["definition_json"]),
                            json.loads(session["candles_json"]),
                            paper_precision_for_engine(session["engine_version"]))
                        db.execute("UPDATE paper_signal_sessions SET indicator_state_json=? WHERE id=?",
                                   (_json(snapshot), session["id"]))
                    db.execute("PRAGMA user_version=10")
                    version = 10
                if version == 10:
                    db.execute("""CREATE TABLE paper_signal_all_candles (
                        session_id TEXT NOT NULL REFERENCES paper_signal_sessions(id),
                        candle_t INTEGER NOT NULL, candle_json TEXT NOT NULL,
                        origin TEXT NOT NULL CHECK(origin IN ('SEED','OBSERVED')),
                        PRIMARY KEY(session_id,candle_t)
                    )""")
                    for session in db.execute("SELECT id,candles_json FROM paper_signal_sessions").fetchall():
                        if not observed_archive_matches(db, session["id"], session["candles_json"], "grvt"):
                            raise PaperPerpError("Archivo observado GRVT PAPER divergente")
                        observed = {row["candle_t"] for row in db.execute(
                            "SELECT candle_t FROM paper_signal_candles WHERE session_id=?",
                            (session["id"],))}
                        for candle in json.loads(session["candles_json"]):
                            db.execute("INSERT INTO paper_signal_all_candles VALUES (?,?,?,?)",
                                       (session["id"], candle["t"], _json(candle),
                                        "OBSERVED" if candle["t"] in observed else "SEED"))
                        if not full_archive_matches(db, session["id"], session["candles_json"], "grvt"):
                            raise PaperPerpError("Archivo completo GRVT PAPER divergente")
                    db.execute("PRAGMA user_version=11")
                    version = 11
                if version == 11:
                    db.execute("""CREATE TABLE IF NOT EXISTS paper_signal_archive_chain (
                        session_id TEXT NOT NULL,
                        candle_t INTEGER NOT NULL, digest TEXT NOT NULL,
                        PRIMARY KEY(session_id,candle_t),
                        FOREIGN KEY(session_id,candle_t)
                            REFERENCES paper_signal_all_candles(session_id,candle_t)
                    )""")
                    db.execute("DELETE FROM paper_signal_archive_chain")
                    for session in db.execute("SELECT id,candles_json FROM paper_signal_sessions").fetchall():
                        if not full_archive_matches(db, session["id"], session["candles_json"], "grvt"):
                            raise PaperPerpError("Archivo completo GRVT PAPER divergente")
                        previous = None
                        rows = db.execute("""SELECT candle_t,candle_json,origin FROM paper_signal_all_candles
                                             WHERE session_id=? ORDER BY candle_t""", (session["id"],))
                        for candle in rows:
                            previous = next_archive_digest(previous, session["id"], candle["candle_t"],
                                                           candle["origin"], candle["candle_json"], "grvt")
                            db.execute("INSERT INTO paper_signal_archive_chain VALUES (?,?,?)",
                                       (session["id"], candle["candle_t"], previous))
                    db.execute("PRAGMA user_version=12")
                    version = 12
                if version == 12:
                    from app.derivatives.perp_observed_archive_format import DDL
                    for statement in DDL:db.execute(statement)
                    db.execute('PRAGMA user_version=13')
                    version = 13
                if version == 13:
                    from app.derivatives.perp_gap_close_format import DDL as gap_close_ddl
                    db.execute(gap_close_ddl)
                    db.execute('PRAGMA user_version=14')
                    version = 14
                db.commit()
            if version != CURRENT_PAPER_SCHEMA:
                raise PaperPerpError(f"Versión PAPER derivados no compatible: {version}")
            self.path.chmod(0o600)
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def digest(payload: dict) -> str:
        return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()

    @staticmethod
    def _replay(db: sqlite3.Connection, request_id: str, digest: str) -> dict | None:
        row = db.execute("SELECT digest,response_json FROM paper_perp_requests WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            return None
        if row["digest"] != digest:
            raise PaperRequestConflict("request_id reutilizado con una operación distinta")
        return json.loads(row["response_json"])

    def replay(self, request_id: str, digest: str) -> dict | None:
        with self.connect() as db:
            return self._replay(db, request_id, digest)

    @staticmethod
    def _account(db: sqlite3.Connection, account_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM paper_perp_accounts WHERE id=?", (account_id,)).fetchone()
        if row is None:
            raise PaperPerpError("Cuenta PAPER derivados no encontrada")
        return row

    @staticmethod
    def _check_funding_ledger(db: sqlite3.Connection, rows: list[sqlite3.Row]) -> None:
        """Cada cargo PAPER debe concordar con su liquidación y con la posición."""
        if not rows:
            return
        account_id = rows[0]["account_id"]
        if any(row["account_id"] != account_id for row in rows):
            raise PaperPerpError("Posiciones GRVT PAPER de cuentas mezcladas")
        positions = {row["id"]: row for row in rows}
        totals = {row["id"]: Decimal("0") for row in rows}
        latest: dict[str, int] = {}
        first: dict[str, tuple[int, int]] = {}
        last_interval: dict[str, int] = {}
        if len(rows) == 1:
            events = db.execute("""SELECT position_id,account_id,funding_time_ns,
                rate_percent,mark_price,interval_hours,payment
                FROM paper_perp_funding_events WHERE position_id=?
                ORDER BY funding_time_ns""", (rows[0]["id"],))
            anchors = db.execute("SELECT * FROM paper_perp_funding_anchors WHERE position_id=?",
                                 (rows[0]["id"],)).fetchall()
            legacy = db.execute("SELECT position_id FROM paper_perp_funding_legacy WHERE position_id=?",
                                (rows[0]["id"],)).fetchall()
        else:
            events = db.execute("""SELECT f.position_id,f.account_id,f.funding_time_ns,
                f.rate_percent,f.mark_price,f.interval_hours,f.payment
                FROM paper_perp_funding_events f JOIN paper_perp_positions p ON p.id=f.position_id
                WHERE p.account_id=? ORDER BY f.position_id,f.funding_time_ns""", (account_id,))
            anchors = db.execute("""SELECT a.* FROM paper_perp_funding_anchors a
                                    JOIN paper_perp_positions p ON p.id=a.position_id
                                    WHERE p.account_id=?""", (account_id,)).fetchall()
            legacy = db.execute("""SELECT l.position_id FROM paper_perp_funding_legacy l
                                   JOIN paper_perp_positions p ON p.id=l.position_id
                                   WHERE p.account_id=?""", (account_id,)).fetchall()
        anchor_by_position = {row["position_id"]: row for row in anchors}
        legacy_positions = {row["position_id"] for row in legacy}
        try:
            with localcontext() as ctx:
                ctx.prec = 80
                for event in events:
                    position_id = event["position_id"]
                    if position_id not in totals or event["account_id"] != account_id:
                        raise PaperPerpError("Diario de financiación GRVT PAPER incoherente")
                    position = positions[position_id]
                    size = Decimal(position["size"])
                    rate = Decimal(event["rate_percent"])
                    mark = Decimal(event["mark_price"])
                    payment = Decimal(event["payment"])
                    funding_time = event["funding_time_ns"]
                    opened = position["opened_at_ns"]
                    closed = position["closed_at_ns"]
                    if (not all(value.is_finite() for value in (size, rate, mark, payment))
                            or size <= 0 or mark <= 0 or abs(rate) > 100
                            or position["side"] not in ("LONG", "SHORT")
                            or event["interval_hours"] not in (1, 4, 8)
                            or type(funding_time) is not int or type(opened) is not int
                            or funding_time <= opened
                            or (closed is not None and (type(closed) is not int or funding_time >= closed))):
                        raise PaperPerpError("Diario de financiación GRVT PAPER ilegible")
                    signed_size = size if position["side"] == "LONG" else -size
                    if payment != signed_size * mark * rate / Decimal("100"):
                        raise PaperPerpError("Financiación GRVT PAPER no coincide con su liquidación")
                    # Sin la liquidación anterior a la apertura no conocemos
                    # su hora exacta, pero ningún intervalo admitido supera
                    # ocho horas. Una primera liquidación posterior ocultaría
                    # al menos un cargo PAPER omitido.
                    if position_id not in latest and funding_time >= opened + 8 * HOUR_NS:
                        raise PaperPerpError("Falta la primera liquidación GRVT PAPER")
                    if (position_id in latest and funding_time - latest[position_id]
                            != event["interval_hours"] * HOUR_NS):
                        raise PaperPerpError("Hueco en liquidaciones GRVT PAPER asentadas")
                    totals[position_id] += payment
                    if position_id not in first:
                        first[position_id] = (funding_time, event["interval_hours"])
                    latest[position_id] = funding_time
                    last_interval[position_id] = event["interval_hours"]
                for row in rows:
                    booked = Decimal(row["funding_paid"])
                    checked = row["funding_checked_through_ns"]
                    opened = row["opened_at_ns"]
                    closed = row["closed_at_ns"]
                    if checked is not None and (type(checked) is not int
                                                or type(opened) is not int
                                                or checked < opened
                                                or (closed is not None and checked > closed)
                                                or (row["id"] not in latest
                                                    and checked >= opened + 8 * HOUR_NS)):
                        raise PaperPerpError("Cobertura de financiación GRVT PAPER incoherente")
                    anchor = anchor_by_position.get(row["id"])
                    is_legacy = row["id"] in legacy_positions
                    same_tick = (closed == opened and checked == opened)
                    if anchor is None:
                        if ((checked is not None and not same_tick and not is_legacy)
                                or (is_legacy and (checked is None or same_tick))):
                            raise PaperPerpError("Falta el ancla pública de financiación GRVT PAPER")
                    else:
                        anchor_time = anchor["funding_time_ns"]
                        interval = anchor["interval_hours"]
                        anchor_rate = Decimal(anchor["rate_percent"])
                        anchor_mark = Decimal(anchor["mark_price"])
                        if (is_legacy or checked is None or type(anchor_time) is not int
                                or type(opened) is not int or anchor_time <= 0
                                or anchor_time >= opened or interval not in (1, 4, 8)
                                or not anchor_rate.is_finite() or not anchor_mark.is_finite()
                                or abs(anchor_rate) > 100 or anchor_mark <= 0
                                or not anchor["source"] or not anchor["recorded_at"]):
                            raise PaperPerpError("Ancla pública de financiación GRVT PAPER ilegible")
                        if row["id"] in first:
                            first_time, first_interval = first[row["id"]]
                            if first_time - anchor_time != first_interval * HOUR_NS:
                                raise PaperPerpError("Falta la primera liquidación GRVT PAPER")
                        elif checked >= anchor_time + interval * HOUR_NS:
                            raise PaperPerpError("Cobertura GRVT PAPER supera la primera liquidación")
                    if (not booked.is_finite() or booked != totals[row["id"]]
                            or (row["id"] in latest and (type(checked) is not int
                                                           or checked < latest[row["id"]]
                                                           or checked >= latest[row["id"]]
                                                           + last_interval[row["id"]] * HOUR_NS))):
                        raise PaperPerpError("Financiación GRVT PAPER no coincide con su diario")
        except (InvalidOperation, TypeError) as exc:
            raise PaperPerpError("Diario de financiación GRVT PAPER ilegible") from exc
        PaperPerpStore._check_open_reserves(db, rows)
        PaperPerpStore._check_closed_results(db, rows)

    @staticmethod
    def _check_open_reserves(db: sqlite3.Connection, rows: list[sqlite3.Row]) -> None:
        """El colateral reservado debe cubrir margen y conservar su apertura."""
        open_events: dict[str, sqlite3.Row] = {}
        if any(row["instrument_spec_json"] is not None for row in rows):
            relevant = {row["id"] for row in rows}
            try:
                for event in db.execute("""SELECT account_id,position_id,observed_at_ns,details_json
                        FROM paper_perp_events WHERE account_id=? AND kind='OPEN'""",
                                        (rows[0]["account_id"],)):
                    position_id = event["position_id"]
                    if position_id not in relevant:
                        continue
                    if position_id in open_events:
                        raise PaperPerpError("Aperturas GRVT PAPER duplicadas en el diario")
                    open_events[position_id] = event
            except sqlcipher3.DatabaseError as exc:
                raise PaperPerpError("Diario de apertura GRVT PAPER no disponible") from exc
        try:
            with localcontext() as ctx:
                # La reserva se calculó con precisión 60 al abrir. Repetir
                # la división a 80 dígitos rechazaría apalancamientos como 3.
                ctx.prec = 60
                for row in rows:
                    size = Decimal(row["size"])
                    leverage = Decimal(row["leverage"])
                    price = Decimal(row["entry_price"])
                    fee = Decimal(row["entry_fee"])
                    collateral = Decimal(row["collateral"])
                    amounts = (size, leverage, price, fee, collateral)
                    if (not all(value.is_finite() for value in amounts)
                            or size <= 0 or leverage < 1 or leverage > 50
                            or price <= 0 or fee < 0
                            or collateral < size * price / leverage + fee):
                        raise PaperPerpError("Reserva de colateral GRVT PAPER incoherente")
                    if row["instrument_spec_json"] is None:
                        continue  # v1 anterior al archivo de instrumento y aperturas.
                    event = open_events.get(row["id"])
                    if event is None:
                        raise PaperPerpError("Apertura GRVT PAPER ausente del diario")
                    details = json.loads(event["details_json"])
                    if not isinstance(details, dict):
                        raise PaperPerpError("Diario de apertura GRVT PAPER ilegible")
                    event_amounts = (Decimal(details["size"]), Decimal(details["leverage"]),
                                     Decimal(details["fill_price"]), Decimal(details["fee"]),
                                     Decimal(details["collateral"]))
                    if (event["account_id"] != row["account_id"]
                            or event["observed_at_ns"] != row["opened_at_ns"]
                            or not all(value.is_finite() for value in event_amounts)
                            or event_amounts != amounts
                            or details["side"] != row["side"]
                            or details["symbol"] != row["symbol"]):
                        raise PaperPerpError("Reserva GRVT PAPER no coincide con su apertura")
        except (InvalidOperation, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise PaperPerpError("Apertura GRVT PAPER ilegible") from exc

    @staticmethod
    def _check_closed_results(db: sqlite3.Connection, rows: list[sqlite3.Row]) -> None:
        """El efectivo liberado debe concordar con la fórmula y el diario de cierre."""
        relevant = {row["id"] for row in rows}
        close_events: dict[str, sqlite3.Row] = {}
        if any(row["closed_at_ns"] is not None for row in rows):
            try:
                for event in db.execute("""SELECT account_id,position_id,observed_at_ns,details_json
                        FROM paper_perp_events WHERE account_id=? AND kind='CLOSE'""",
                                        (rows[0]["account_id"],)):
                    position_id = event["position_id"]
                    if position_id not in relevant:
                        continue
                    if position_id in close_events:
                        raise PaperPerpError("Cierres GRVT PAPER duplicados en el diario")
                    close_events[position_id] = event
            except sqlcipher3.DatabaseError as exc:
                raise PaperPerpError("Diario de cierre GRVT PAPER no disponible") from exc
        try:
            with localcontext() as ctx:
                ctx.prec = 80
                for row in rows:
                    closed = row["closed_at_ns"]
                    fields = ("exit_price", "exit_fee", "realized_pnl", "uncovered_loss")
                    if closed is None:
                        if any(row[field] is not None for field in fields) or row["id"] in close_events:
                            raise PaperPerpError("Resultado GRVT PAPER sin cierre registrado")
                        continue
                    if (type(closed) is not int or closed < row["opened_at_ns"]
                            or any(row[field] is None for field in fields)
                            or row["side"] not in ("LONG", "SHORT")
                            or row["id"] not in close_events):
                        raise PaperPerpError("Cierre GRVT PAPER incompleto")
                    size = Decimal(row["size"])
                    entry = Decimal(row["entry_price"])
                    exit_price = Decimal(row["exit_price"])
                    entry_fee = Decimal(row["entry_fee"])
                    exit_fee = Decimal(row["exit_fee"])
                    collateral = Decimal(row["collateral"])
                    funding = Decimal(row["funding_paid"])
                    realized = Decimal(row["realized_pnl"])
                    uncovered = Decimal(row["uncovered_loss"])
                    amounts = (size, entry, exit_price, entry_fee, exit_fee,
                               collateral, funding, realized, uncovered)
                    if (not all(value.is_finite() for value in amounts)
                            or size <= 0 or entry <= 0 or exit_price <= 0
                            or entry_fee < 0 or exit_fee < 0 or collateral <= 0
                            or uncovered < 0):
                        raise PaperPerpError("Resultado GRVT PAPER ilegible")
                    signed_size = size if row["side"] == "LONG" else -size
                    raw = (exit_price - entry) * signed_size - entry_fee - exit_fee - funding
                    if (realized != max(raw, -collateral)
                            or uncovered != max(-collateral - raw, Decimal("0"))):
                        raise PaperPerpError("Resultado GRVT PAPER no coincide con el cierre")
                    event = close_events[row["id"]]
                    details = json.loads(event["details_json"])
                    if not isinstance(details, dict):
                        raise PaperPerpError("Diario de cierre GRVT PAPER ilegible")
                    original_funding = Decimal(details["funding_paid"])
                    original_realized = Decimal(details["realized_pnl"])
                    original_uncovered = Decimal(details["uncovered_loss"])
                    original_gross = Decimal(details["gross_pnl"])
                    gross = (exit_price - entry) * signed_size
                    close_raw = gross - entry_fee - exit_fee - original_funding
                    if (event["account_id"] != row["account_id"]
                            or event["observed_at_ns"] != closed
                            or not all(value.is_finite() for value in
                                       (original_funding, original_realized,
                                        original_uncovered, original_gross))
                            or Decimal(details["fill_price"]) != exit_price
                            or Decimal(details["entry_fee"]) != entry_fee
                            or Decimal(details["exit_fee"]) != exit_fee
                            or original_gross != gross
                            or original_realized != max(close_raw, -collateral)
                            or original_uncovered != max(-collateral - close_raw, Decimal("0"))):
                        raise PaperPerpError("Cierre GRVT PAPER no coincide con su diario")
        except (InvalidOperation, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise PaperPerpError("Resultado GRVT PAPER ilegible") from exc

    @staticmethod
    def _snapshot(db: sqlite3.Connection, account: sqlite3.Row) -> dict:
        rows = db.execute("SELECT * FROM paper_perp_positions WHERE account_id=?", (account["id"],)).fetchall()
        PaperPerpStore._check_funding_ledger(db, rows)
        with localcontext() as ctx:
            ctx.prec = 80
            reserved = sum((_decimal(row, "collateral") for row in rows if row["closed_at_ns"] is None), Decimal("0"))
            realized = sum((_decimal(row, "realized_pnl") for row in rows if row["closed_at_ns"] is not None), Decimal("0"))
            funding = sum((_decimal(row, "funding_paid") for row in rows), Decimal("0"))
            balance = _decimal(account, "initial_balance") + realized - reserved
        open_count = sum(row["closed_at_ns"] is None for row in rows)
        return {
            "id": account["id"], "name": account["name"], "venue": "grvt",
            "instrument_type": "PERPETUAL", "quote_currency": "USDT",
            "initial_balance": account["initial_balance"],
            "available_cash": str(balance), "reserved_collateral": str(reserved),
            "realized_pnl": str(realized), "open_positions": open_count,
            "funding_paid_total": str(funding),
            "closed_positions": len(rows) - open_count,
            "max_position_notional": account["max_position_notional"],
            "max_total_notional": account["max_total_notional"],
            "max_leverage": account["max_leverage"],
            "max_positions": account["max_positions"],
            "fee_rate": account["fee_rate"], "slippage_bps": account["slippage_bps"],
            "created_at": account["created_at"], "stopped_at": account["stopped_at"],
            "execution_model": "PAPER_MANUAL_BEST_QUOTE_APPROXIMATION",
            "funding_model": "OPT_IN_PUBLIC_HISTORY_PAPER_ONLY",
            "liquidation_model": "NOT_IMPLEMENTED",
        }

    def create_account(self, name: str, initial_balance: str, max_position_notional: str,
                       max_total_notional: str, max_leverage: str, max_positions: int,
                       fee_rate: str, slippage_bps: str) -> dict:
        if not 1 <= len(name.strip()) <= 100:
            raise PaperPerpError("Nombre de cuenta inválido")
        balance = decimal_string(initial_balance, "initial_balance", positive=True)
        per_position = decimal_string(max_position_notional, "max_position_notional", positive=True)
        total = decimal_string(max_total_notional, "max_total_notional", positive=True)
        leverage = decimal_string(max_leverage, "max_leverage", positive=True)
        fee = decimal_string(fee_rate, "fee_rate")
        slippage = decimal_string(slippage_bps, "slippage_bps")
        if per_position > total or total > balance * leverage or not 1 <= leverage <= 50:
            raise PaperPerpError("Límites de nocional o apalancamiento incoherentes")
        if not 1 <= max_positions <= 100 or not 0 <= fee <= Decimal("0.1") or not 0 <= slippage <= 500:
            raise PaperPerpError("Límites PAPER fuera de rango")
        row_id = str(uuid4())
        with self.connect() as db:
            db.execute("""INSERT INTO paper_perp_accounts VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                       (row_id, name.strip(), str(balance), str(per_position), str(total),
                        str(leverage), max_positions, str(fee), str(slippage),
                        datetime.now(timezone.utc).isoformat(), None))
            return self._snapshot(db, self._account(db, row_id))

    def account(self, account_id: str) -> dict:
        with self.connect() as db:
            db.execute("BEGIN")
            return self._snapshot(db, self._account(db, account_id))

    def accounts(self) -> list[dict]:
        if paper_perp_format(self.path) in ("missing", "empty"):
            return []
        with self.connect() as db:
            db.execute("BEGIN")
            rows = db.execute("SELECT * FROM paper_perp_accounts ORDER BY created_at,id").fetchall()
            return [self._snapshot(db, row) for row in rows]

    def stop_account(self, account_id: str) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            account = self._account(db, account_id)
            if account["stopped_at"] is None:
                db.execute("UPDATE paper_perp_accounts SET stopped_at=? WHERE id=?",
                           (datetime.now(timezone.utc).isoformat(), account_id))
            return self._snapshot(db, self._account(db, account_id))

    @staticmethod
    def _position(row: sqlite3.Row, *, db: sqlite3.Connection | None = None) -> dict:
        checked = row["funding_checked_through_ns"]
        funding_coverage = ("CLOSED_COMPLETE" if row["closed_at_ns"] is not None
                            and checked is not None and checked >= row["closed_at_ns"]
                            else "PARTIAL" if checked is not None else "UNASSESSED")
        anchor_status = "NOT_ASSESSED"
        if (checked is not None
                and not (row["closed_at_ns"] == row["opened_at_ns"] == checked)
                and db is not None):
            legacy = db.execute("SELECT 1 FROM paper_perp_funding_legacy WHERE position_id=?",
                                (row["id"],)).fetchone()
            anchor_status = "LEGACY_UNANCHORED" if legacy else "PUBLIC_ANCHORED"
        return {key: row[key] for key in (
            "id", "account_id", "symbol", "instrument", "side", "size", "leverage",
            "collateral", "entry_price", "entry_mark", "entry_fee", "opened_at_ns",
            "opened_recorded_at", "margin_rules_at_ns", "closed_at_ns", "exit_price",
            "exit_mark", "exit_fee", "realized_pnl", "uncovered_loss", "instrument_spec_at_ns",
            "funding_paid", "funding_checked_through_ns",
        )} | {"margin_rules": json.loads(row["margin_rules_json"]),
              "instrument_spec": json.loads(row["instrument_spec_json"]) if row["instrument_spec_json"] else None,
              "instrument_spec_checked": row["instrument_spec_json"] is not None,
              "funding_coverage": funding_coverage,
              "funding_anchor_status": anchor_status, "venue": "grvt",
              "origin_session_id": row["origin_session_id"],
              "mode": "PAPER_AUTO" if row["origin_session_id"] else "PAPER_MANUAL"}

    def positions(self, account_id: str) -> list[dict]:
        with self.connect() as db:
            db.execute("BEGIN")
            self._account(db, account_id)
            rows = db.execute("SELECT * FROM paper_perp_positions WHERE account_id=? ORDER BY opened_at_ns,id",
                              (account_id,)).fetchall()
            self._check_funding_ledger(db, rows)
            return [self._position(row, db=db) for row in rows]

    def auto_funding_candidates(self) -> list[dict]:
        """Posiciones AUTO con cobertura pendiente, incluso tras cerrar/GAP.

        No expone posiciones manuales ni supone que una tasa ausente es cero.
        """
        if paper_perp_format(self.path) in ("missing", "empty"):
            return []
        with self.connect() as db:
            db.execute("BEGIN")
            # Validar antes de filtrar: una cobertura falsa de una posición
            # cerrada no debe hacerla desaparecer de la cola AUTO sin aviso.
            rows = db.execute("""SELECT * FROM paper_perp_positions
                                 WHERE account_id IN (
                                     SELECT account_id FROM paper_perp_positions
                                     WHERE origin_session_id IS NOT NULL)
                                 ORDER BY account_id,opened_at_ns,id""").fetchall()
            by_account: dict[str, list[sqlite3.Row]] = {}
            for row in rows:
                by_account.setdefault(row["account_id"], []).append(row)
            for account_rows in by_account.values():
                self._check_funding_ledger(db, account_rows)
            fields = ("id", "account_id", "symbol", "instrument", "opened_at_ns",
                      "closed_at_ns", "funding_checked_through_ns")
            candidates = [row for row in rows if row["origin_session_id"] is not None
                          and (row["closed_at_ns"] is None
                               or row["funding_checked_through_ns"] is None
                               or row["funding_checked_through_ns"] < row["closed_at_ns"])]
            return [{field: row[field] for field in fields} for row in sorted(
                candidates, key=lambda row: (row["opened_at_ns"], row["id"]))]

    def position(self, account_id: str, position_id: str) -> dict:
        with self.connect() as db:
            db.execute("BEGIN")
            self._account(db, account_id)
            row = db.execute("SELECT * FROM paper_perp_positions WHERE account_id=? AND id=?",
                             (account_id, position_id)).fetchone()
            if row is None:
                raise PaperPerpError("Posición PAPER derivados no encontrada")
            self._check_funding_ledger(db, [row])
            return self._position(row, db=db)

    def events(self, account_id: str) -> list[dict]:
        with self.connect() as db:
            db.execute("BEGIN")
            self._account(db, account_id)
            rows = db.execute("SELECT * FROM paper_perp_events WHERE account_id=? ORDER BY recorded_at,id",
                              (account_id,)).fetchall()
            events = [{key: row[key] for key in ("id", "account_id", "position_id", "kind", "request_id",
                                                       "observed_at_ns", "recorded_at")} | {
                           "details": json.loads(row["details_json"])} for row in rows]
            funding = db.execute("SELECT * FROM paper_perp_funding_events WHERE account_id=?",
                                 (account_id,)).fetchall()
            events.extend({"id": row["id"], "account_id": row["account_id"],
                           "position_id": row["position_id"], "kind": "FUNDING_PAPER",
                           "request_id": row["request_id"], "observed_at_ns": row["funding_time_ns"],
                           "recorded_at": row["recorded_at"], "details": {
                               "funding_time_ns": row["funding_time_ns"],
                               "rate_percent": row["rate_percent"], "mark_price": row["mark_price"],
                               "interval_hours": row["interval_hours"], "payment": row["payment"],
                               "source": row["source"], "mode": "PAPER_SIMULATED",
                           }} for row in funding)
            return sorted(events, key=lambda item: (item["recorded_at"], item["id"]))

    def valuation_snapshots(self, account_id: str, *, now_ns: int) -> dict[str, dict]:
        """Última valoración puntual por posición abierta, nunca un mark actual supuesto.

        El historial OBSERVE no se modifica. Los importes sólo salen de esta
        vista si el ticker archivado sigue dentro de la ventana de 15 s.
        """
        if type(now_ns) is not int or now_ns < 0:
            raise PaperPerpError("Reloj de valoración inválido")
        with self.connect() as db:
            db.execute("BEGIN")
            self._account(db, account_id)
            positions = db.execute("""SELECT * FROM paper_perp_positions
                                    WHERE account_id=? AND closed_at_ns IS NULL""",
                                   (account_id,)).fetchall()
            self._check_funding_ledger(db, positions)
            funding_by_position = {row["id"]: row["funding_paid"] for row in positions}
            snapshots = {row["id"]: {"status": "NO_OBSERVATION", "observed_at_ns": None,
                                      "remaining_ms": 0, "details": None,
                                      "last_risk_type": None} for row in positions}
            if not snapshots:
                return snapshots
            rows = db.execute("""SELECT position_id,observed_at_ns,details_json FROM (
                SELECT e.position_id,e.observed_at_ns,e.details_json,
                       ROW_NUMBER() OVER (
                           PARTITION BY e.position_id
                           ORDER BY e.observed_at_ns DESC,e.id DESC
                       ) AS rank
                FROM paper_perp_events e
                JOIN paper_perp_positions p ON p.id=e.position_id
                WHERE e.account_id=? AND e.kind='OBSERVE' AND p.closed_at_ns IS NULL
            ) WHERE rank=1""", (account_id,)).fetchall()
            for row in rows:
                observed_ns = row["observed_at_ns"]
                age_ns = now_ns - observed_ns
                try:
                    details = json.loads(row["details_json"])
                except (TypeError, ValueError):
                    details = None
                if not isinstance(details, dict):
                    status = "UNVERIFIABLE"
                elif details.get("risk_type") == "STREAM_COVERAGE_GAP":
                    status = "COVERAGE_GAP"
                elif "funding_paid" not in details:
                    status = "UNVERIFIABLE"
                else:
                    try:
                        booked_funding = Decimal(funding_by_position[row["position_id"]])
                        observed_funding = Decimal(details["funding_paid"])
                        funding_matches = (booked_funding.is_finite() and observed_funding.is_finite()
                                           and booked_funding == observed_funding)
                    except (KeyError, TypeError, ValueError, ArithmeticError):
                        funding_matches = False
                    if not funding_matches:
                        status = "OUTDATED_BOOKING"
                    elif age_ns < 0 or age_ns > 15_000_000_000:
                        status = "STALE"
                    else:
                        status = "FRESH"
                last_risk = None
                if isinstance(details, dict):
                    if isinstance(details.get("risk_type"), str):
                        last_risk = details["risk_type"]
                    elif details.get("maintenance_status") == "AT_OR_BELOW_MAINTENANCE_ON_MARK":
                        last_risk = "OBSERVED_MAINTENANCE_BREACH"
                snapshots[row["position_id"]] = {
                    "status": status, "observed_at_ns": observed_ns,
                    "remaining_ms": ((15_000_000_000 - age_ns) // 1_000_000
                                     if status == "FRESH" else 0),
                    "details": details if status == "FRESH" else None,
                    "last_risk_type": last_risk,
                }
            return snapshots

    @staticmethod
    def _check_market_order(db: sqlite3.Connection, position_id: str, quote: PerpQuote) -> None:
        last_time = db.execute(
            "SELECT MAX(observed_at_ns) FROM paper_perp_events WHERE position_id=?",
            (position_id,),
        ).fetchone()[0]
        if last_time is not None and quote.event_time_ns < last_time:
            raise PaperPerpError("Cotización anterior al último evento de la posición")

    def open_position(self, account_id: str, request_id: str, digest: str, symbol: str,
                      side: str, size: Decimal, leverage: Decimal,
                      quote: PerpQuote, rules: MarginRules, spec: InstrumentSpec,
                      origin_session_id: str | None = None) -> dict:
        if side not in ("LONG", "SHORT") or size <= 0 or leverage < 1:
            raise PaperPerpError("Posición PAPER inválida")
        if quote.instrument != rules.instrument or quote.instrument != spec.instrument:
            raise PaperPerpError("Cotización y restricciones de instrumentos distintos")
        with self.connect() as db, localcontext() as ctx:
            ctx.prec = 60
            db.execute("BEGIN IMMEDIATE")
            replay = self._replay(db, request_id, digest)
            if replay is not None:
                return replay
            account = self._account(db, account_id)
            if account["stopped_at"] is not None:
                raise PaperPerpError("Cuenta PAPER detenida; sólo puede cerrar posiciones")
            if origin_session_id is not None:
                session = db.execute("SELECT * FROM paper_signal_sessions WHERE id=?", (origin_session_id,)).fetchone()
                action = db.execute("SELECT * FROM paper_signal_actions WHERE request_id=?", (request_id,)).fetchone()
                if (session is None or session["account_id"] != account_id or session["symbol"] != symbol
                        or session["status"] != "ACTIVE" or session["position_id"] is not None
                        or session["execution_mode"] != "AUTO_PAPER" or action is None
                        or action["session_id"] != origin_session_id or action["action"] != "OPEN"
                        or action["status"] != "PENDING" or action["digest"] != digest):
                    raise PaperPerpError("Entrada automática PAPER ya no autorizada")
            snapshot = self._snapshot(db, account)
            if snapshot["open_positions"] >= account["max_positions"]:
                raise PaperPerpError("Límite de posiciones simultáneas")
            if leverage > _decimal(account, "max_leverage"):
                raise PaperPerpError("Apalancamiento superior al límite de la cuenta")
            fill = quote.fill_price(side, "OPEN", _decimal(account, "slippage_bps"))
            # Bid/mark conservador: el slippage supuesto no debe hacer que
            # una orden demasiado pequeña parezca superar el mínimo real.
            spec.validate_open(size, min(quote.bid, quote.mark))
            notional = size * fill
            tier = rules.tier_for(notional)
            if size > rules.max_size or leverage > tier.max_leverage:
                raise PaperPerpError("Tamaño o apalancamiento superior al tramo GRVT")
            if notional > _decimal(account, "max_position_notional"):
                raise PaperPerpError("Límite de nocional por posición")
            open_rows = db.execute("SELECT instrument,size,entry_price FROM paper_perp_positions WHERE account_id=? AND closed_at_ns IS NULL",
                                   (account_id,)).fetchall()
            same_instrument_size = sum((Decimal(row["size"]) for row in open_rows
                                        if row["instrument"] == quote.instrument), Decimal("0"))
            if same_instrument_size + size > rules.max_size or (spec.max_size is not None
                                                               and same_instrument_size + size > spec.max_size):
                raise PaperPerpError("Posición agregada superior al máximo GRVT")
            total_exposure = sum((Decimal(row["size"]) * Decimal(row["entry_price"]) for row in open_rows), Decimal("0"))
            if total_exposure + notional > _decimal(account, "max_total_notional"):
                raise PaperPerpError("Límite de exposición total")
            fee = notional * _decimal(account, "fee_rate")
            initial_margin = max(notional / leverage, notional * tier.initial_rate)
            collateral = initial_margin + fee
            if Decimal(snapshot["available_cash"]) < collateral:
                raise PaperPerpError("Saldo ficticio insuficiente")
            signed_size = size if side == "LONG" else -size
            position = IsolatedPerpPosition(quote.instrument, signed_size, fill, collateral, fee)
            if not position.can_open(leverage):
                raise PaperPerpError("Margen inicial insuficiente")
            position_id, event_id = str(uuid4()), str(uuid4())
            now = datetime.now(timezone.utc).isoformat()
            rules_payload = rules.json()
            db.execute("""INSERT INTO paper_perp_positions VALUES
                          (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (position_id, account_id, symbol, quote.instrument, side, str(size), str(leverage),
                        str(collateral), str(fill), str(quote.mark), str(fee), quote.event_time_ns,
                        now, _json(rules_payload), rules.fetched_at_ns,
                        None, None, None, None, None, None, _json(spec.json()), spec.fetched_at_ns,
                        "0", None, origin_session_id))
            details = {"symbol": symbol, "side": side, "size": str(size), "fill_price": str(fill),
                       "mark_price": str(quote.mark), "best_bid": str(quote.bid), "best_ask": str(quote.ask),
                       "fee": str(fee), "collateral": str(collateral), "leverage": str(leverage),
                       "margin_rules": rules_payload, "instrument_spec": spec.json(),
                       "execution_model": "BEST_QUOTE_APPROXIMATION",
                       "funding_model": "NOT_IMPLEMENTED", "liquidation_model": "NOT_IMPLEMENTED",
                       "origin_session_id": origin_session_id}
            db.execute("INSERT INTO paper_perp_events VALUES (?,?,?,?,?,?,?,?)",
                       (event_id, account_id, position_id, "OPEN", request_id, quote.event_time_ns, now, _json(details)))
            row = db.execute("SELECT * FROM paper_perp_positions WHERE id=?", (position_id,)).fetchone()
            result = {"position": self._position(row, db=db), "account": self._snapshot(db, account), "event_id": event_id}
            db.execute("INSERT INTO paper_perp_requests VALUES (?,?,?)", (request_id, digest, _json(result)))
            if origin_session_id is not None:
                db.execute("UPDATE paper_signal_actions SET status='EXECUTED',position_id=? WHERE request_id=?",
                           (position_id, request_id))
                db.execute("UPDATE paper_signal_sessions SET position_id=?,risk_last_quote_ns=NULL WHERE id=?",
                           (position_id, origin_session_id))
            return result

    def close_position(self, account_id: str, position_id: str, request_id: str,
                       digest: str, quote: PerpQuote,
                       origin_session_id: str | None = None) -> dict:
        with self.connect() as db, localcontext() as ctx:
            ctx.prec = 60
            db.execute("BEGIN IMMEDIATE")
            replay = self._replay(db, request_id, digest)
            if replay is not None:
                return replay
            account = self._account(db, account_id)
            row = db.execute("SELECT * FROM paper_perp_positions WHERE id=? AND account_id=?",
                             (position_id, account_id)).fetchone()
            if row is None:
                raise PaperPerpError("Posición PAPER derivados no encontrada")
            self._check_funding_ledger(db, [row])
            if origin_session_id is not None:
                session = db.execute("SELECT * FROM paper_signal_sessions WHERE id=?", (origin_session_id,)).fetchone()
                action = db.execute("SELECT * FROM paper_signal_actions WHERE request_id=?", (request_id,)).fetchone()
                if (session is None or session["account_id"] != account_id or session["status"] != "ACTIVE"
                        or session["position_id"] != position_id or row["origin_session_id"] != origin_session_id
                        or action is None or action["session_id"] != origin_session_id
                        or action["action"] != "CLOSE" or action["status"] != "PENDING"
                        or action["digest"] != digest):
                    raise PaperPerpError("Cierre automático PAPER ya no autorizado")
                now_ns = self.clock_ns()
                step = INTERVALS[session["timeframe"]]
                expiry = signal_action_expiry(action, step, now_ns)
                if expiry is not None:
                    raise PaperCloseWindowElapsed(expiry)
                risk_anchor = session["risk_last_quote_ns"] or row["opened_at_ns"]
                if quote.event_time_ns < risk_anchor:
                    raise PaperCloseQuoteDeferred("Cierre automático PAPER anterior al último ticker de riesgo")
                if quote.event_time_ns - risk_anchor > RISK_MAX_GAP_NS:
                    raise PaperPerpError("Cierre automático PAPER sin cobertura continua de ticker GRVT")
                if action["reason"].endswith("_AT_CANDLE_CLOSE"):
                    earliest_ns = (action["candle_t"] + step) * 1_000_000_000
                    if not earliest_ns <= quote.event_time_ns < earliest_ns + step * 1_000_000_000:
                        raise PaperCloseQuoteDeferred("Cotización fuera de la vela de ejecución PAPER")
                if (quote.event_time_ns > now_ns + 2_000_000_000
                        or now_ns - quote.event_time_ns > 15_000_000_000):
                    raise PaperCloseQuoteDeferred("Cotización GRVT obsoleta o futura al cerrar PAPER")
            if row["closed_at_ns"] is not None:
                raise PaperPerpError("Posición ya cerrada")
            if quote.instrument != row["instrument"] or quote.event_time_ns < row["opened_at_ns"]:
                raise PaperPerpError("Cotización no corresponde a la posición abierta")
            if (row["funding_checked_through_ns"] is not None
                    and quote.event_time_ns < row["funding_checked_through_ns"]):
                raise PaperPerpError("Cotización anterior a la cobertura de financiación PAPER")
            self._check_market_order(db, position_id, quote)
            last_funding = db.execute(
                "SELECT MAX(funding_time_ns) FROM paper_perp_funding_events WHERE position_id=?",
                (position_id,),
            ).fetchone()[0]
            if last_funding is not None and quote.event_time_ns <= last_funding:
                raise PaperPerpError("Cotización no posterior a la financiación PAPER registrada")
            same_tick = db.execute(
                """SELECT details_json FROM paper_perp_events
                   WHERE position_id=? AND kind='OBSERVE' AND observed_at_ns=?
                   ORDER BY recorded_at DESC,id DESC LIMIT 1""",
                (position_id, quote.event_time_ns),
            ).fetchone()
            if same_tick is not None:
                same_details = json.loads(same_tick["details_json"])
                if same_details.get("risk_type") == "STREAM_COVERAGE_GAP":
                    raise PaperPerpError("Un hueco de ticker no es una cotización para cerrar")
                if Decimal(same_details["mark_price"]) != quote.mark:
                    raise PaperPerpError("Mark contradictorio para una cotización ya observada")
            size = _decimal(row, "size")
            fill = quote.fill_price(row["side"], "CLOSE", _decimal(account, "slippage_bps"))
            fee = size * fill * _decimal(account, "fee_rate")
            signed_size = size if row["side"] == "LONG" else -size
            position = IsolatedPerpPosition(row["instrument"], signed_size, _decimal(row, "entry_price"),
                                            _decimal(row, "collateral"), _decimal(row, "entry_fee"),
                                            _decimal(row, "funding_paid"))
            gross = (fill - position.entry_price) * signed_size
            raw_pnl = gross - position.entry_fee - fee - position.funding_paid
            realized = max(raw_pnl, -position.collateral)
            uncovered = max(-position.collateral - raw_pnl, Decimal("0"))
            # Dos acciones secuenciales pueden compartir el timestamp público.
            # Sin intervalo de exposición no existe una liquidación intermedia;
            # evitar que la cobertura AUTO quede pendiente para siempre.
            funding_checked = (quote.event_time_ns if quote.event_time_ns == row["opened_at_ns"]
                               else row["funding_checked_through_ns"])
            now = datetime.now(timezone.utc).isoformat()
            db.execute("""UPDATE paper_perp_positions SET closed_at_ns=?,exit_price=?,exit_mark=?,
                          exit_fee=?,realized_pnl=?,uncovered_loss=?,
                          funding_checked_through_ns=? WHERE id=?""",
                       (quote.event_time_ns, str(fill), str(quote.mark), str(fee), str(realized),
                        str(uncovered), funding_checked, position_id))
            event_id = str(uuid4())
            details = {"fill_price": str(fill), "mark_price": str(quote.mark),
                       "best_bid": str(quote.bid), "best_ask": str(quote.ask),
                       "gross_pnl": str(gross), "entry_fee": str(position.entry_fee),
                       "exit_fee": str(fee), "realized_pnl": str(realized),
                       "uncovered_loss": str(uncovered), "funding_paid": str(position.funding_paid),
                       "funding_coverage": ("CLOSED_COMPLETE_ZERO_DURATION" if quote.event_time_ns
                                            == row["opened_at_ns"] else "PARTIAL_UNLESS_CHECKED_THROUGH_CLOSE"),
                       "execution_model": "BEST_QUOTE_APPROXIMATION"}
            db.execute("INSERT INTO paper_perp_events VALUES (?,?,?,?,?,?,?,?)",
                       (event_id, account_id, position_id, "CLOSE", request_id,
                        quote.event_time_ns, now, _json(details)))
            closed = db.execute("SELECT * FROM paper_perp_positions WHERE id=?", (position_id,)).fetchone()
            result = {"position": self._position(closed, db=db), "account": self._snapshot(db, account), "event_id": event_id}
            db.execute("INSERT INTO paper_perp_requests VALUES (?,?,?)", (request_id, digest, _json(result)))
            if origin_session_id is not None:
                db.execute("UPDATE paper_signal_actions SET status='EXECUTED',position_id=? WHERE request_id=?",
                           (position_id, request_id))
                db.execute("UPDATE paper_signal_sessions SET position_id=NULL WHERE id=?", (origin_session_id,))
            return result

    def observe(self, account_id: str, position_id: str, quote: PerpQuote,
                rules: MarginRules) -> dict:
        with self.connect() as db, localcontext() as ctx:
            ctx.prec = 60
            db.execute("BEGIN IMMEDIATE")
            self._account(db, account_id)
            row = db.execute("SELECT * FROM paper_perp_positions WHERE id=? AND account_id=?",
                             (position_id, account_id)).fetchone()
            if row is None or row["closed_at_ns"] is not None:
                raise PaperPerpError("Posición abierta no encontrada")
            self._check_funding_ledger(db, [row])
            if (quote.instrument != row["instrument"] or rules.instrument != row["instrument"]
                    or quote.event_time_ns < row["opened_at_ns"]):
                raise PaperPerpError("Cotización no corresponde a la posición abierta")
            self._check_market_order(db, position_id, quote)
            previous = db.execute(
                """SELECT id,details_json FROM paper_perp_events
                   WHERE position_id=? AND kind='OBSERVE' AND observed_at_ns=?
                   ORDER BY recorded_at DESC,id DESC LIMIT 1""",
                (position_id, quote.event_time_ns),
            ).fetchone()
            if previous is not None:
                details = json.loads(previous["details_json"])
                if details.get("risk_type") == "STREAM_COVERAGE_GAP":
                    raise PaperPerpError("Un hueco de ticker no es una cotización de margen")
                if Decimal(details["mark_price"]) != quote.mark:
                    raise PaperPerpError("Mark contradictorio para una cotización ya observada")
                if Decimal(details.get("funding_paid", "0")) == _decimal(row, "funding_paid"):
                    return details | {"event_id": previous["id"]}
            signed_size = _decimal(row, "size") if row["side"] == "LONG" else -_decimal(row, "size")
            position = IsolatedPerpPosition(row["instrument"], signed_size, _decimal(row, "entry_price"),
                                            _decimal(row, "collateral"), _decimal(row, "entry_fee"),
                                            _decimal(row, "funding_paid"))
            notional = abs(signed_size) * quote.mark
            requirement = rules.maintenance_requirement(notional)
            equity = position.equity(quote.mark)
            threshold = estimate_maintenance_threshold(position, rules, quote.mark)
            result = {"position_id": position_id, "mark_price": str(quote.mark),
                      "unrealized_pnl": str(position.unrealized_pnl(quote.mark)),
                      "funding_paid": str(position.funding_paid),
                      "isolated_equity": str(equity),
                      "maintenance_requirement": str(requirement),
                      "margin_buffer": str(equity - requirement),
                      "margin_rules": rules.json(),
                      "observed_at_ns": quote.event_time_ns,
                      "estimated_maintenance_mark": (format(threshold, ".12g")
                                                     if threshold is not None else None),
                      "maintenance_mark_model": "ISOLATED_STATIC_SNAPSHOT_NOT_LIQUIDATION_PRICE",
                      "maintenance_status": ("AT_OR_BELOW_MAINTENANCE_ON_MARK" if equity <= requirement
                                             else "ABOVE_MAINTENANCE_ON_MARK"),
                      "liquidation_status": "NOT_DETERMINED_NO_CONTINUOUS_MARK_OR_FUNDING"}
            event_id = str(uuid4())
            db.execute("INSERT INTO paper_perp_events VALUES (?,?,?,?,?,?,?,?)",
                       (event_id, account_id, position_id, "OBSERVE", None, quote.event_time_ns,
                        datetime.now(timezone.utc).isoformat(), _json(result)))
            return result | {"event_id": event_id}

    def settle_funding(self, account_id: str, position_id: str, request_id: str,
                       digest: str, records: list[FundingRecord], through_ns: int) -> dict:
        """Asienta sólo liquidaciones nuevas en PAPER; nunca cargos de GRVT real.

        La estimación se recalcula bajo el bloqueo de escritura. Un cambio en
        un registro público ya asentado aborta la operación entera.
        """
        with self.connect() as db, localcontext() as ctx:
            ctx.prec = 80
            db.execute("BEGIN IMMEDIATE")
            replay = self._replay(db, request_id, digest)
            if replay is not None:
                return replay
            account = self._account(db, account_id)
            row = db.execute("SELECT * FROM paper_perp_positions WHERE id=? AND account_id=?",
                             (position_id, account_id)).fetchone()
            if row is None:
                raise PaperPerpError("Posición PAPER derivados no encontrada")
            self._check_funding_ledger(db, [row])
            if row["closed_at_ns"] is not None and row["closed_at_ns"] != through_ns:
                raise PaperPerpError("La posición se cerró durante la consulta; vuelve a intentarlo")
            checked = row["funding_checked_through_ns"]
            if checked is not None and through_ns < checked:
                raise PaperPerpError("No se puede retroceder la cobertura de financiación")
            estimate = estimate_funding(self._position(row, db=db), records, through_ns)
            anchor_record = next((record for record in records
                                  if record.time_ns == estimate["anchor_time_ns"]), None)
            if anchor_record is None:
                raise FundingHistoryError("Falta el ancla pública de financiación GRVT")
            old_anchor = db.execute("SELECT * FROM paper_perp_funding_anchors WHERE position_id=?",
                                    (position_id,)).fetchone()
            if old_anchor is not None and (
                    old_anchor["funding_time_ns"] != anchor_record.time_ns
                    or old_anchor["interval_hours"] != anchor_record.interval_hours
                    or Decimal(old_anchor["rate_percent"]) != anchor_record.rate_percent
                    or Decimal(old_anchor["mark_price"]) != anchor_record.mark
                    or old_anchor["source"] != estimate["source"]):
                raise FundingHistoryError("El histórico GRVT contradice el ancla PAPER archivada")
            proposed = {item["funding_time_ns"]: item for item in estimate["settlements"]}
            booked = db.execute(
                "SELECT * FROM paper_perp_funding_events WHERE position_id=? ORDER BY funding_time_ns",
                (position_id,),
            ).fetchall()
            for old in booked:
                current = proposed.get(old["funding_time_ns"])
                if (current is None or Decimal(current["rate_percent"]) != Decimal(old["rate_percent"])
                        or Decimal(current["mark_price"]) != Decimal(old["mark_price"])
                        or current["interval_hours"] != old["interval_hours"]
                        or Decimal(current["estimated_payment"]) != Decimal(old["payment"])):
                    raise FundingHistoryError("El histórico GRVT contradice una liquidación PAPER ya asentada")
            old_times = {item["funding_time_ns"] for item in booked}
            new = [item for item in estimate["settlements"] if item["funding_time_ns"] not in old_times]
            now = datetime.now(timezone.utc).isoformat()
            if old_anchor is None:
                db.execute("""INSERT INTO paper_perp_funding_anchors VALUES (?,?,?,?,?,?,?)""",
                           (position_id, anchor_record.time_ns,
                            str(anchor_record.rate_percent), str(anchor_record.mark),
                            anchor_record.interval_hours, estimate["source"], now))
                db.execute("DELETE FROM paper_perp_funding_legacy WHERE position_id=?", (position_id,))
            for item in new:
                db.execute("""INSERT INTO paper_perp_funding_events VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                           (str(uuid4()), account_id, position_id, request_id,
                            item["funding_time_ns"], item["rate_percent"], item["mark_price"],
                            item["interval_hours"], item["estimated_payment"], estimate["source"], now))
            total = sum((Decimal(item["estimated_payment"]) for item in estimate["settlements"]), Decimal("0"))
            if total != Decimal(estimate["estimated_funding_paid"]):
                raise FundingHistoryError("Suma de liquidaciones GRVT inconsistente")
            db.execute("""UPDATE paper_perp_positions
                          SET funding_paid=?,funding_checked_through_ns=? WHERE id=?""",
                       (str(total), through_ns, position_id))
            realized_delta = Decimal("0")
            if row["closed_at_ns"] is not None:
                signed_size = _decimal(row, "size") if row["side"] == "LONG" else -_decimal(row, "size")
                gross = (_decimal(row, "exit_price") - _decimal(row, "entry_price")) * signed_size
                raw_pnl = gross - _decimal(row, "entry_fee") - _decimal(row, "exit_fee") - total
                realized = max(raw_pnl, -_decimal(row, "collateral"))
                uncovered = max(-_decimal(row, "collateral") - raw_pnl, Decimal("0"))
                realized_delta = realized - _decimal(row, "realized_pnl")
                db.execute("UPDATE paper_perp_positions SET realized_pnl=?,uncovered_loss=? WHERE id=?",
                           (str(realized), str(uncovered), position_id))
            updated = db.execute("SELECT * FROM paper_perp_positions WHERE id=?", (position_id,)).fetchone()
            result = {"position": self._position(updated, db=db), "account": self._snapshot(db, account),
                      "new_settlements": len(new),
                      "new_payment": str(sum((Decimal(item["estimated_payment"]) for item in new), Decimal("0"))),
                      "realized_pnl_delta": str(realized_delta),
                      "source": estimate["source"], "mode": "PAPER_SIMULATED"}
            db.execute("INSERT INTO paper_perp_requests VALUES (?,?,?)", (request_id, digest, _json(result)))
            return result
