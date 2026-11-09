"""Registro cifrado e idempotente de decisiones MEXC Spot REAL.

No hay transporte de órdenes. Una reserva sólo impide reutilizar localmente
saldo libre mientras se implementan envío, confirmación y conciliación.
Antes de enviar, la intención puede pasar a SUBMISSION_UNKNOWN: ese estado
persiste e impide un segundo envío aunque el proceso caiga o se active STOP.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.live_execution.policy import (
    LiveSpotIntent, LiveSpotPolicy, LiveSpotState, _utc, evaluate_spot_intent,
    sell_base_reservation,
)
from app.portfolio.models import (
    RealLiveOrderObservation, RealLiveSpotIntent, RealLiveSpotPolicy, RealLiveStop, RealMexcAccountLink,
)
from app.portfolio.vault import RealVault


class LiveJournalError(RuntimeError):
    """Identidad reutilizada con datos distintos o estado inconsistente."""


@dataclass(frozen=True, slots=True)
class RecordedIntent:
    intent_id: str
    status: str
    reason: str
    notional_quote: Decimal
    reserved_quote: Decimal
    reserved_base: Decimal


@dataclass(frozen=True, slots=True)
class LiveSpotSignalSource:
    """Procedencia auditada de una señal PAPER; no autoriza una orden."""

    intent_id: str
    signal_id: str
    paper_session_id: str
    strategy_version_id: str
    dataset_id: str
    dataset_hash: str
    engine_version: str
    symbol: str
    timeframe: str
    candle_t: int
    signal_at: datetime
    observed_at: datetime
    entry_evidence: dict
    observation_digest: str | None = None

    def __post_init__(self) -> None:
        for name in ("intent_id", "signal_id", "paper_session_id",
                     "strategy_version_id", "dataset_id"):
            try:
                UUID(getattr(self, name))
            except (TypeError, ValueError, AttributeError) as exc:
                raise ValueError(f"{name} must be a UUID") from exc
        if (not isinstance(self.dataset_hash, str)
                or not re.fullmatch(r"[0-9a-f]{64}", self.dataset_hash)
                or not isinstance(self.engine_version, str) or not self.engine_version
                or not isinstance(self.symbol, str) or not re.fullmatch(r"[A-Z0-9]+/[A-Z0-9]+", self.symbol)
                or not isinstance(self.timeframe, str) or not self.timeframe
                or type(self.candle_t) is not int or self.candle_t <= 0
                or not isinstance(self.entry_evidence, dict) or not self.entry_evidence):
            raise ValueError("Signal source is invalid")
        if (self.observation_digest is not None
                and (not isinstance(self.observation_digest, str)
                     or not re.fullmatch(r"[0-9a-f]{64}", self.observation_digest))):
            raise ValueError("Signal observation digest is invalid")
        object.__setattr__(self, "signal_at", _utc(self.signal_at, "signal_at"))
        object.__setattr__(self, "observed_at", _utc(self.observed_at, "observed_at"))
        if self.observed_at < self.signal_at:
            raise ValueError("Signal observation precedes close")


def _source_matches_intent(source: LiveSpotSignalSource, intent: LiveSpotIntent) -> bool:
    return (source.intent_id == intent.intent_id
            and source.strategy_version_id == intent.strategy_version_id
            and source.symbol.replace("/", "") == intent.symbol
            and source.signal_at == intent.signal_at)


def _canonical(value, *, legacy_source: bool = False) -> str:
    def convert(item):
        if type(item) is Decimal:
            return format(item, "f")
        if isinstance(item, datetime):
            return _utc(item, "timestamp").isoformat()
        raise TypeError("Unsupported live journal value")

    fields_dict = asdict(value)
    if isinstance(value, LiveSpotPolicy) and value.reconciliation_anchor_id is None:
        # Las políticas previas conservan exactamente su representación v1.
        fields_dict.pop("reconciliation_anchor_id")
    if isinstance(value, LiveSpotPolicy) and not value.allow_risk_reducing_exits:
        fields_dict.pop("allow_risk_reducing_exits")
    if isinstance(value, LiveSpotPolicy) and value.resumed_after_stop_at is None:
        fields_dict.pop("resumed_after_stop_at")
    if isinstance(value, LiveSpotIntent) and value.max_spend_quote is None:
        fields_dict.pop("max_spend_quote")
    if legacy_source:
        fields_dict.pop("observation_digest")
    return json.dumps(fields_dict, default=convert, sort_keys=True,
                      ensure_ascii=True, separators=(",", ":"))


def _digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def intent_request_digest(intent: LiveSpotIntent) -> str:
    """Huella canónica para vincular observaciones al pedido exacto."""
    return _digest(_canonical(intent))


def _pending_sell_loss(rows: list[RealLiveSpotIntent], state: LiveSpotState,
                       policy: LiveSpotPolicy) -> Decimal:
    """Reserva la pérdida posible de ventas locales abiertas en la misma divisa.

    Para otra base falta su coste reconciliado: en ese caso se falla cerrado.
    No se compensan pérdidas pendientes con beneficios hipotéticos.
    """
    with localcontext() as context:
        context.prec = 300
        total = Decimal(0)
        for row in rows:
            if row.side != "SELL" or row.quote_asset != policy.quote_asset:
                continue
            if row.base_asset != policy.base_asset or state.average_cost_quote_per_base is None:
                raise LiveJournalError("Pending SELL cost basis is not verifiable")
            try:
                if row.request_json is None or _digest(row.request_json) != row.request_digest:
                    raise ValueError("Missing or changed request")
                raw = json.loads(row.request_json)
                if (not isinstance(raw, dict)
                        or set(raw) != {field.name for field in fields(LiveSpotIntent)} - {"max_spend_quote"}):
                    raise ValueError("Request fields differ")
                for name in ("quantity_base", "limit_price_quote", "estimated_fee_quote"):
                    if not isinstance(raw[name], str):
                        raise ValueError("Money is not text")
                    raw[name] = Decimal(raw[name])
                raw["signal_at"] = datetime.fromisoformat(raw["signal_at"])
                pending = LiveSpotIntent(**raw)
                if (_canonical(pending) != row.request_json or pending.intent_id != row.id
                        or pending.account_ref != row.account_ref or pending.symbol != row.symbol
                        or pending.side != "SELL" or pending.policy_id != row.policy_id
                        or row.quantity_base != pending.quantity_base
                        or row.limit_price_quote != pending.limit_price_quote
                        or row.notional_quote != pending.quantity_base * pending.limit_price_quote
                        or row.reserved_base != sell_base_reservation(pending)):
                    raise ValueError("Stored SELL differs from request")
            except (TypeError, ValueError, KeyError, InvalidOperation) as exc:
                raise LiveJournalError("Pending SELL request is not verifiable") from exc
            effective_fee = max(
                pending.estimated_fee_quote,
                pending.estimated_fee_quote * state.average_cost_quote_per_base
                / pending.limit_price_quote,
            )
            loss = (pending.quantity_base * state.average_cost_quote_per_base
                    + effective_fee - row.notional_quote)
            total += max(Decimal(0), loss)
        return total


def signal_source_request_json(source: LiveSpotSignalSource) -> str:
    """Representación canónica del origen exacto, sin credenciales."""
    return _canonical(source)


def signal_source_request_digest(source: LiveSpotSignalSource) -> str:
    """Huella del origen exacto que debe seguir vinculado antes de un POST."""
    return _digest(signal_source_request_json(source))


def recover_spot_intent(vault: RealVault, intent_id: str) -> LiveSpotIntent:
    """Reconstruye una intención nueva tras reinicio, sin inferir campos ausentes.

    Requiere la petición canónica cifrada introducida en v15. Intenciones
    legadas mantienen su reserva, pero no son candidatas a una cancelación
    automatizada basada en una orden reconstruida por aproximación.
    """
    try:
        intent_id = str(UUID(intent_id))
    except (TypeError, ValueError, AttributeError):
        raise LiveJournalError("Intent ID is invalid") from None
    with vault.session() as db:
        return recover_spot_intent_in_session(db, intent_id)


def recover_spot_intent_in_session(db: Session, intent_id: str) -> LiveSpotIntent:
    """Misma validación dentro de una transacción existente, sin abrir/cerrar otra.

    SQLite/SQLCipher puede reutilizar la conexión del hilo: cerrar una sesión
    anidada revertiría escrituras de su llamante. No hacer I/O de red aquí.
    """
    row = db.get(RealLiveSpotIntent, intent_id)
    if row is None:
        raise LiveJournalError("Intent is not archived")
    if row.request_json is None:
        raise LiveJournalError("Legacy intent has no recoverable exact request")
    if _digest(row.request_json) != row.request_digest:
        raise LiveJournalError("Intent request failed integrity check")
    try:
        raw = json.loads(row.request_json)
        field_names = {field.name for field in fields(LiveSpotIntent)}
        if not isinstance(raw, dict) or set(raw) not in (field_names, field_names - {"max_spend_quote"}):
            raise ValueError("Request fields differ")
        for name in ("quantity_base", "limit_price_quote", "estimated_fee_quote"):
            if not isinstance(raw[name], str):
                raise ValueError("Money is not text")
            raw[name] = Decimal(raw[name])
        if "max_spend_quote" in raw:
            if not isinstance(raw["max_spend_quote"], str):
                raise ValueError("Spending cap is not text")
            raw["max_spend_quote"] = Decimal(raw["max_spend_quote"])
        raw["signal_at"] = datetime.fromisoformat(raw["signal_at"])
        intent = LiveSpotIntent(**raw)
        policy = _stored_policy(db, row.policy_id)
        with localcontext() as context:
            context.prec = 100
            notional = intent.quantity_base * intent.limit_price_quote
        if (_canonical(intent) != row.request_json
                or intent.intent_id != row.id or intent.policy_id != row.policy_id
                or intent.account_ref != row.account_ref or intent.symbol != row.symbol
                or intent.side != row.side
                or intent.strategy_version_id != policy.strategy_version_id
                or policy.account_ref != row.account_ref or policy.symbol != row.symbol
                or row.limit_price_quote != intent.limit_price_quote
                or row.quantity_base != intent.quantity_base
                or row.notional_quote != notional):
            raise ValueError("Request scope differs")
    except (TypeError, ValueError, KeyError, InvalidOperation) as exc:
        raise LiveJournalError("Intent request is inconsistent with journal") from exc
    return intent


def parse_archived_spot_signal_source(row: RealLiveSpotIntent) -> LiveSpotSignalSource | None:
    """Valida una procedencia dentro de una lectura de bóveda ya abierta."""
    if row.signal_source_json is None and row.signal_source_digest is None:
        return None
    if (row.signal_source_json is None or row.signal_source_digest is None
            or row.request_json is None
            or _digest(row.signal_source_json) != row.signal_source_digest
            or _digest(row.request_json) != row.request_digest):
        raise LiveJournalError("Signal source failed integrity check")
    try:
        raw = json.loads(row.signal_source_json)
        request = json.loads(row.request_json)
        current_fields = {field.name for field in fields(LiveSpotSignalSource)}
        legacy = isinstance(raw, dict) and set(raw) == current_fields - {"observation_digest"}
        if (not isinstance(raw, dict) or not isinstance(request, dict)
                or not (set(raw) == current_fields or legacy)):
            raise ValueError("Signal source fields differ")
        raw["signal_at"] = datetime.fromisoformat(raw["signal_at"])
        raw["observed_at"] = datetime.fromisoformat(raw["observed_at"])
        source = LiveSpotSignalSource(**raw)
        if (_canonical(source, legacy_source=legacy) != row.signal_source_json
                or source.intent_id != row.id
                or source.strategy_version_id != request.get("strategy_version_id")
                or source.symbol.replace("/", "") != row.symbol
                or source.symbol.replace("/", "") != request.get("symbol")
                or source.signal_at.isoformat() != request.get("signal_at")):
            raise ValueError("Signal source differs from intent")
    except (TypeError, ValueError, KeyError) as exc:
        raise LiveJournalError("Signal source is inconsistent with intent") from exc
    return source


def recover_spot_signal_source(vault: RealVault, intent_id: str) -> LiveSpotSignalSource:
    """Reabre sólo una procedencia guardada; nunca inventa origen legacy."""
    intent = recover_spot_intent(vault, intent_id)
    with vault.session() as db:
        row = db.get(RealLiveSpotIntent, intent.intent_id)
        if row is None:
            raise LiveJournalError("Intent is not archived")
        source = parse_archived_spot_signal_source(row)
        if source is None:
            raise LiveJournalError("Intent has no archived signal source")
        if not _source_matches_intent(source, intent):
            raise LiveJournalError("Signal source is inconsistent with intent")
        return source


def _record(row: RealLiveSpotIntent) -> RecordedIntent:
    return RecordedIntent(row.id, row.status, row.reason, row.notional_quote,
                          row.reserved_quote, row.reserved_base)


def _stored_policy(db: Session, policy_id: str) -> LiveSpotPolicy:
    stored = db.get(RealLiveSpotPolicy, policy_id)
    if stored is None:
        raise LiveJournalError("Policy is not registered")
    if _digest(stored.payload_json) != stored.payload_digest:
        raise LiveJournalError("Policy content failed integrity check")
    money = ("max_order_quote", "max_daily_turnover_quote", "max_exposure_quote",
             "max_daily_loss_quote", "max_fee_bps", "max_slippage_bps")
    try:
        raw = json.loads(stored.payload_json)
        if not isinstance(raw, dict):
            raise ValueError("Policy payload is not an object")
        for key in money:
            if not isinstance(raw[key], str):
                raise ValueError("Policy monetary field is not text")
            raw[key] = Decimal(raw[key])
        raw["expires_at"] = datetime.fromisoformat(raw["expires_at"])
        if raw.get("resumed_after_stop_at") is not None:
            raw["resumed_after_stop_at"] = datetime.fromisoformat(raw["resumed_after_stop_at"])
        policy = LiveSpotPolicy(**raw)
    except (TypeError, ValueError, KeyError, InvalidOperation) as exc:
        raise LiveJournalError("Policy content is invalid") from exc
    if (stored.id != policy.policy_id
            or stored.account_ref != policy.account_ref or stored.symbol != policy.symbol
            or stored.base_asset != policy.base_asset or stored.quote_asset != policy.quote_asset
            or stored.strategy_version_id != policy.strategy_version_id):
        raise LiveJournalError("Policy scope failed integrity check")
    return policy


def _register_spot_policy_in_transaction(db: Session, policy: LiveSpotPolicy, *, now: datetime) -> str:
    """Añade una política inmutable dentro de una escritura ya serializada."""
    now = _utc(now, "now")
    payload = _canonical(policy)
    digest = _digest(payload)
    existing = db.get(RealLiveSpotPolicy, policy.policy_id)
    if existing is not None:
        if existing.payload_digest != digest or existing.payload_json != payload:
            raise LiveJournalError("Policy ID was reused with different content")
        return policy.policy_id
    db.add(RealLiveSpotPolicy(
        id=policy.policy_id, strategy_version_id=policy.strategy_version_id,
        account_ref=policy.account_ref, symbol=policy.symbol,
        base_asset=policy.base_asset, quote_asset=policy.quote_asset,
        payload_json=payload, payload_digest=digest, created_at=now))
    db.flush()
    return policy.policy_id


def policy_predates_reopening(db: Session, policy_id: str, stop: RealLiveStop | None) -> bool:
    """Una reapertura nunca vuelve a habilitar políticas de la sesión anterior."""
    if stop is None or stop.stopped:
        return False
    stored = db.get(RealLiveSpotPolicy, policy_id)
    return stored is None or stored.created_at < stop.updated_at


def register_spot_policy(vault: RealVault, policy: LiveSpotPolicy, *, now: datetime) -> str:
    """Guarda una definición inmutable; las comprobaciones de activación van aparte."""
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            identifier = _register_spot_policy_in_transaction(db, policy, now=now)
            db.commit()
            return identifier
        except Exception:
            db.rollback()
            raise


def reserve_spot_intent(vault: RealVault, intent: LiveSpotIntent,
                        state: LiveSpotState, *, now: datetime,
                        signal_source: LiveSpotSignalSource | None = None) -> RecordedIntent:
    """Evalúa y registra en una transacción serializada entre procesos.

    `state.free_*` son los saldos libres reportados por el exchange (ya sin
    importes bloqueados allí). `state.pending_*` sólo añade compromisos aún
    no descontados de esos libres; NO incluye reservas locales de ORBIS.
    Éstas se leen aquí y se suman. Se exige conciliación reciente de cuenta.
    """
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            result = reserve_spot_intent_in_transaction(db,intent,state,now=now,signal_source=signal_source)
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise


def reserve_spot_intent_in_transaction(db: Session,intent: LiveSpotIntent,state: LiveSpotState,*,now: datetime,
                                      signal_source: LiveSpotSignalSource | None = None) -> RecordedIntent:
    """Misma reserva, sin abrir/cerrar otra sesión ni confirmar al llamante.

    Permite guardar decisión de bot y reserva bajo un único BEGIN IMMEDIATE.
    No da permiso para enviar ni permite reiniciar una reserva ya existente.
    """
    if not db.connection().connection.dbapi_connection.in_transaction:
        raise LiveJournalError("La reserva requiere una transacción de escritura existente")
    now = _utc(now,"now")
    if signal_source is not None and (not isinstance(signal_source,LiveSpotSignalSource)
            or signal_source.observation_digest is None or not _source_matches_intent(signal_source,intent)):
        raise LiveJournalError("Signal source does not match intent")
    request_digest = intent_request_digest(intent)
    source_json = _canonical(signal_source) if signal_source is not None else None
    source_digest = _digest(source_json) if source_json is not None else None
    existing = db.get(RealLiveSpotIntent,intent.intent_id)
    if existing is not None:
        if existing.request_digest != request_digest:
            raise LiveJournalError("Intent ID was reused with different content")
        if source_json is not None and (existing.signal_source_json != source_json or existing.signal_source_digest != source_digest):
            raise LiveJournalError("Intent source differs from archived signal")
        return _record(existing)
    policy = _stored_policy(db,intent.policy_id)
    stop = db.get(RealLiveStop,intent.account_ref)
    rows = db.scalars(select(RealLiveSpotIntent).where(RealLiveSpotIntent.account_ref == intent.account_ref,
        RealLiveSpotIntent.status.in_(("RESERVED","SUBMISSION_UNKNOWN")))).all()
    with localcontext() as context:
        context.prec = 300
        pending_quote = sum((row.reserved_quote for row in rows if row.quote_asset == policy.quote_asset),Decimal(0))
        pending_base = sum((row.reserved_base for row in rows if row.base_asset == policy.base_asset),Decimal(0))
        # Los límites de cuenta no se multiplican al añadir otra estrategia.
        pending_turnover = sum((row.notional_quote for row in rows if row.quote_asset == policy.quote_asset),Decimal(0))
        effective = replace(state,stopped=state.stopped or bool(stop and stop.stopped),
            pending_quote=state.pending_quote+pending_quote,pending_base=state.pending_base+pending_base,
            pending_turnover_quote=state.pending_turnover_quote+pending_turnover,
            pending_projected_loss_quote=state.pending_projected_loss_quote+_pending_sell_loss(rows,state,policy))
        decision = evaluate_spot_intent(policy,intent,effective,now=now)
    if policy.reconciliation_anchor_id is not None and rows:
        decision = replace(decision,allowed=False,reason="ACCOUNT_ORDER_PENDING")
    if policy_predates_reopening(db,policy.policy_id,stop):
        decision = replace(decision,allowed=False,reason="POLICY_REACTIVATION_REQUIRED")
    row = RealLiveSpotIntent(id=intent.intent_id,policy_id=intent.policy_id,account_ref=intent.account_ref,
        symbol=intent.symbol,base_asset=policy.base_asset,quote_asset=policy.quote_asset,side=intent.side,
        request_digest=request_digest,request_json=_canonical(intent),signal_source_json=source_json,signal_source_digest=source_digest,
        status="RESERVED" if decision.allowed else "REJECTED",reason=decision.reason,notional_quote=decision.notional_quote,
        limit_price_quote=intent.limit_price_quote,quantity_base=intent.quantity_base,
        reserved_quote=decision.reserved_quote if decision.allowed else Decimal(0),
        reserved_base=sell_base_reservation(intent) if decision.allowed else Decimal(0),created_at=now,updated_at=now)
    db.add(row)
    db.flush()
    return _record(row)


def begin_spot_submission(vault: RealVault, intent: LiveSpotIntent,
                          state: LiveSpotState, *, now: datetime) -> RecordedIntent:
    """Marca el punto de no-reintento ANTES de cualquier POST externo.

    Si el proceso muere después de este commit, no se puede distinguir de un
    envío aceptado sin observar el exchange. Por eso no se vuelve a reservar
    ni se permite llamar a este método por segunda vez para el mismo ID.
    """
    now = _utc(now, "now")
    if not isinstance(intent, LiveSpotIntent) or not isinstance(state, LiveSpotState):
        raise ValueError("Fresh intent and observed state are required")
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            row = db.get(RealLiveSpotIntent, intent.intent_id)
            if row is None or row.status != "RESERVED":
                raise LiveJournalError("Intent is not reserved for first submission")
            if row.request_digest != intent_request_digest(intent):
                raise LiveJournalError("Intent content differs from reservation")
            stop = db.get(RealLiveStop, row.account_ref)
            if stop is not None and stop.stopped:
                raise LiveJournalError("Account is stopped")
            if row.reserved_quote <= 0 and row.reserved_base <= 0:
                raise LiveJournalError("Intent has no active local reservation")
            policy = _stored_policy(db, row.policy_id)
            if policy_predates_reopening(db, policy.policy_id, stop):
                raise LiveJournalError("Policy predates the latest account reopening")
            rows = db.scalars(select(RealLiveSpotIntent).where(
                RealLiveSpotIntent.account_ref == row.account_ref,
                RealLiveSpotIntent.status.in_(("RESERVED", "SUBMISSION_UNKNOWN")),
                RealLiveSpotIntent.id != row.id,
            )).all()
            if policy.reconciliation_anchor_id is not None and rows:
                raise LiveJournalError("Another account order is pending")
            pending_quote = sum((item.reserved_quote for item in rows
                                 if item.quote_asset == policy.quote_asset), Decimal(0))
            pending_base = sum((item.reserved_base for item in rows
                                if item.base_asset == policy.base_asset), Decimal(0))
            pending_turnover = sum((item.notional_quote for item in rows
                                    if item.quote_asset == policy.quote_asset), Decimal(0))
            pending_sell_loss = _pending_sell_loss(rows, state, policy)
            effective = replace(
                state,
                stopped=state.stopped or bool(stop and stop.stopped),
                pending_quote=state.pending_quote + pending_quote,
                pending_base=state.pending_base + pending_base,
                pending_turnover_quote=state.pending_turnover_quote + pending_turnover,
                pending_projected_loss_quote=state.pending_projected_loss_quote + pending_sell_loss,
            )
            decision = evaluate_spot_intent(policy, intent, effective, now=now)
            if not decision.allowed:
                raise LiveJournalError(f"Submission preflight rejected: {decision.reason}")
            if (row.notional_quote != decision.notional_quote
                    or row.reserved_quote != decision.reserved_quote
                    or row.reserved_base != sell_base_reservation(intent)):
                raise LiveJournalError("Reservation amounts differ from current intent")
            row.status = "SUBMISSION_UNKNOWN"
            row.reason = "SUBMISSION_OUTCOME_UNKNOWN"
            row.updated_at = now
            db.commit()
            return _record(row)
        except Exception:
            db.rollback()
            raise


def claim_spot_order_post(vault: RealVault, intent: LiveSpotIntent,
                          *, now: datetime) -> RecordedIntent:
    """Marca duraderamente el único intento de POST antes de abrir la red.

    Si el proceso cae entre este commit y el envío, la orden sigue incierta y
    nunca se reintenta automáticamente. Sólo se acepta una segunda puerta de
    riesgo recién ejecutada para la intención exacta.
    """
    now = _utc(now, "now")
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            row = db.get(RealLiveSpotIntent, intent.intent_id)
            if (row is None or row.status != "SUBMISSION_UNKNOWN"
                    or row.reason != "SUBMISSION_OUTCOME_UNKNOWN"
                    or row.request_digest != intent_request_digest(intent)
                    or row.account_ref != intent.account_ref
                    or row.symbol != intent.symbol or row.side != intent.side):
                raise LiveJournalError("Submission is not eligible for a first POST")
            if now < row.updated_at or (now - row.updated_at).total_seconds() > 2:
                raise LiveJournalError("Submission preflight is no longer fresh")
            stop = db.get(RealLiveStop, intent.account_ref)
            if stop is not None and stop.stopped:
                raise LiveJournalError("Account is stopped")
            if policy_predates_reopening(db, intent.policy_id, stop):
                raise LiveJournalError("Policy predates the latest account reopening")
            row.reason = "POST_ATTEMPTED_OUTCOME_UNKNOWN"
            row.updated_at = now
            db.commit()
            return _record(row)
        except Exception:
            db.rollback()
            raise


def _stop_spot_account_in_transaction(db: Session, account_ref: str, now: datetime) -> int:
    row = db.get(RealLiveStop, account_ref)
    if row is None:
        db.add(RealLiveStop(account_ref=account_ref, stopped=True, updated_at=now))
    else:
        row.stopped = True
        row.updated_at = now
    reserved = db.scalars(select(RealLiveSpotIntent).where(
        RealLiveSpotIntent.account_ref == account_ref,
        RealLiveSpotIntent.status == "RESERVED",
    )).all()
    for intent in reserved:
        intent.status = "STOPPED"
        intent.reason = "STOPPED"
        intent.reserved_quote = Decimal(0)
        intent.reserved_base = Decimal(0)
        intent.updated_at = now
    return len(reserved)


def release_unsent_spot_intent(vault: RealVault, intent_id: str, *, now: datetime,
                               account_id: str | None = None) -> dict:
    """Libera sólo RESERVED bajo el mismo bloqueo que la primera marca de envío.

    Si `begin_spot_submission` ganó la carrera, el estado incierto impide esta
    acción. El ID y su petición se conservan; jamás vuelve a ser enviable.
    No detiene ni reabre la cuenta, ni consulta o cancela nada en MEXC.
    """
    now = _utc(now, "now")
    intent = recover_spot_intent(vault, intent_id)
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            row = db.get(RealLiveSpotIntent, intent.intent_id)
            if row is None or row.request_digest != intent_request_digest(intent):
                raise LiveJournalError("La petición archivada cambió")
            if account_id is not None:
                link = db.get(RealMexcAccountLink, row.account_ref)
                if link is None or link.account_id != account_id:
                    raise LiveJournalError("La reserva no pertenece a esta cuenta REAL")
            observed = db.scalar(select(RealLiveOrderObservation.id).where(
                RealLiveOrderObservation.intent_id == intent.intent_id).limit(1))
            if observed is not None:
                raise LiveJournalError("Hay una orden externa observada; no se puede liberar como no enviada")
            if (row.status == "STOPPED" and row.reason in ("ABANDONED_BEFORE_SUBMISSION", "STOPPED")
                    and row.reserved_base == 0 and row.reserved_quote == 0):
                db.commit()
                return {"intent_id": row.id, "status": "ALREADY_RELEASED", "order_sent": False}
            if row.status != "RESERVED" or row.reason != "AUTHORIZED_FOR_RESERVATION":
                raise LiveJournalError("Sólo se puede liberar una reserva que nunca inició el envío")
            if now < row.updated_at:
                raise LiveJournalError("El reloj local retrocedió antes de liberar la reserva")
            row.status = "STOPPED"
            row.reason = "ABANDONED_BEFORE_SUBMISSION"
            row.reserved_base = Decimal(0)
            row.reserved_quote = Decimal(0)
            row.updated_at = now
            db.commit()
            return {"intent_id": row.id, "status": "RELEASED_UNSENT", "order_sent": False}
        except Exception:
            db.rollback()
            raise


def stop_spot_account(vault: RealVault, account_ref: str, *, now: datetime) -> int:
    """Parada persistente: libera sólo reservas inequívocamente no enviadas.

    SUBMISSION_UNKNOWN se conserva íntegra: un stop no puede garantizar que
    MEXC no recibiera el POST y no autoriza reutilizar los mismos fondos.
    """
    now = _utc(now, "now")
    if not isinstance(account_ref, str) or not re.fullmatch(r"[0-9a-f]{64}", account_ref):
        raise ValueError("account_ref is invalid")
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            released = _stop_spot_account_in_transaction(db, account_ref, now)
            db.commit()
            return released
        except Exception:
            db.rollback()
            raise


def stop_spot_account_for_intent(vault: RealVault, intent_id: str,
                                 *, now: datetime) -> dict:
    """Parada local para la cuenta de una intención conocida, sin clave Trade.

    No hace DELETE en MEXC ni cambia intenciones con POST posible. Sirve para
    que la UI no tenga que conocer o aceptar una referencia completa de cuenta.
    """
    now = _utc(now, "now")
    try:
        intent_id = str(UUID(intent_id))
    except (TypeError, ValueError, AttributeError):
        raise LiveJournalError("Identificador de intención inválido") from None
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            selected = db.get(RealLiveSpotIntent, intent_id)
            if selected is None:
                raise LiveJournalError("Intención MEXC no encontrada")
            account_ref = selected.account_ref
            released = _stop_spot_account_in_transaction(db, account_ref, now)
            unresolved = db.scalars(select(RealLiveSpotIntent).where(
                RealLiveSpotIntent.account_ref == account_ref,
                RealLiveSpotIntent.status == "SUBMISSION_UNKNOWN",
            )).all()
            db.commit()
            return {"stopped": True, "released_unsent": released,
                    "unresolved_sent_or_unknown": len(unresolved),
                    "exchange_orders_cancelled": False}
        except Exception:
            db.rollback()
            raise
