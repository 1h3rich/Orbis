"""Tablas exclusivas del almacén REAL; no forman parte de `orbis.db` PAPER."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.portfolio.domain import PortfolioError, utc_datetime


class RealBase(DeclarativeBase):
    pass


class DecimalText(TypeDecorator[Decimal]):
    """Persistencia decimal exacta en SQLite, sin conversión a `float`."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Decimal | None, dialect) -> str | None:
        if value is None:
            return None
        if not isinstance(value, Decimal) or not value.is_finite():
            raise PortfolioError("Financial values must be finite Decimal values")
        return format(value, "f")

    def process_result_value(self, value: str | None, dialect) -> Decimal | None:
        return Decimal(value) if value is not None else None


class UtcTimestamp(TypeDecorator[datetime]):
    """ISO-8601 UTC canónico; orden textual coincide con el temporal."""

    impl = String(27)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> str | None:
        if value is None:
            return None
        return utc_datetime(value).isoformat(timespec="microseconds").replace("+00:00", "Z")

    def process_result_value(self, value: str | None, dialect) -> datetime | None:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value is not None else None


class RealAccount(RealBase):
    __tablename__ = "real_accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    base_currency: Mapped[str] = mapped_column(String(12), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)


class RealEvent(RealBase):
    __tablename__ = "real_events"
    __table_args__ = (
        UniqueConstraint("account_id", "request_id", name="uq_real_event_request"),
        Index("ix_real_events_account_time", "account_id", "occurred_at", "recorded_at", "id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("real_accounts.id"), nullable=False)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    asset: Mapped[str | None] = mapped_column(String(12))
    quote_currency: Mapped[str] = mapped_column(String(12), nullable=False)
    quantity: Mapped[Decimal | None] = mapped_column(DecimalText())
    unit_price: Mapped[Decimal | None] = mapped_column(DecimalText())
    gross_amount: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    fee: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    fee_asset: Mapped[str | None] = mapped_column(String(12))
    occurred_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)


class MexcObservedFill(RealBase):
    """Fill externo observado; nunca es un movimiento contable REAL."""

    __tablename__ = "mexc_observed_fills"
    __table_args__ = (
        UniqueConstraint("account_ref", "symbol", "trade_id", name="uq_mexc_observed_fill"),
        Index("ix_mexc_observed_time", "account_ref", "symbol", "time_ms"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(30), nullable=False)
    trade_id: Mapped[str] = mapped_column(String(128), nullable=False)
    order_id: Mapped[str] = mapped_column(String(128), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    time_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    quote_quantity: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    commission: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    commission_asset: Mapped[str] = mapped_column(String(24), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)


class MexcObservationBatch(RealBase):
    """Cobertura de cada consulta; evita confundir una página con historial total."""

    __tablename__ = "mexc_observation_batches"
    __table_args__ = (Index("ix_mexc_batch_account_time", "account_ref", "observed_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(30), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)
    requested_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    returned_count: Mapped[int] = mapped_column(Integer, nullable=False)
    oldest_time_ms: Mapped[int | None] = mapped_column(Integer)
    newest_time_ms: Mapped[int | None] = mapped_column(Integer)
    requested_start_ms: Mapped[int | None] = mapped_column(Integer)
    requested_end_ms: Mapped[int | None] = mapped_column(Integer)
    request_count: Mapped[int | None] = mapped_column(Integer)
    coverage: Mapped[str | None] = mapped_column(String(48))


class MexcTransferSnapshot(RealBase):
    """Consulta externa saneada; no se deduplica ni altera el libro REAL."""

    __tablename__ = "mexc_transfer_snapshots"
    __table_args__ = (Index("ix_mexc_transfer_account_time", "account_ref", "observed_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)
    requested_start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    deposit_count: Mapped[int] = mapped_column(Integer, nullable=False)
    withdrawal_count: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class MexcTransferIdentityEvidence(RealBase):
    """Prueba local de UID View al capturar un snapshot; nunca guarda el UID."""

    __tablename__ = "mexc_transfer_identity_evidence"

    snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("mexc_transfer_snapshots.id"), primary_key=True)
    account_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    uid_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)


class RealMexcTransferMatch(RealBase):
    """Vínculo explícito, no transferencia ejecutada ni conciliación global."""

    __tablename__ = "real_mexc_transfer_matches"
    __table_args__ = (
        UniqueConstraint("account_ref", "direction", "source_ref",
                         name="uq_mexc_transfer_source_match"),
        UniqueConstraint("event_id", name="uq_mexc_transfer_event_match"),
        Index("ix_mexc_transfer_match_account", "account_ref", "account_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    account_id: Mapped[str] = mapped_column(ForeignKey("real_accounts.id"), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("mexc_transfer_snapshots.id"), nullable=False)
    event_id: Mapped[str] = mapped_column(ForeignKey("real_events.id"), nullable=False)
    direction: Mapped[str] = mapped_column(String(12), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    source_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    event_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)


class GrvtOrderEvidence(RealBase):
    """Orden y fills saneados en la bóveda; nunca un asiento contable."""

    __tablename__ = "grvt_order_evidence"
    __table_args__ = (
        UniqueConstraint("account_ref", "sub_account_id", "client_order_id",
                         "payload_digest", name="uq_grvt_order_evidence_payload"),
        Index("ix_grvt_order_evidence_scope", "account_ref", "sub_account_id",
              "client_order_id", "observed_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    sub_account_id: Mapped[str] = mapped_column(String(30), nullable=False)
    client_order_id: Mapped[str] = mapped_column(String(20), nullable=False)
    order_id: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)
    fill_count: Mapped[int] = mapped_column(Integer, nullable=False)
    sizes_match: Mapped[bool] = mapped_column(Boolean, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class GrvtTestnetOrderIntent(RealBase):
    """Intento de testnet sin clave, cookie ni firma; no es contabilidad REAL."""

    __tablename__ = "grvt_testnet_order_intents"
    __table_args__ = (
        Index("ix_grvt_testnet_intent_scope", "sub_account_id", "created_at"),
    )

    client_order_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    sub_account_id: Mapped[str] = mapped_column(String(30), nullable=False)
    instrument: Mapped[str] = mapped_column(String(80), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    size: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    limit_price: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    max_order_notional: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    external_order_id: Mapped[str | None] = mapped_column(String(80))
    external_status: Mapped[str | None] = mapped_column(String(20))
    external_update_ns: Mapped[str | None] = mapped_column(String(20))
    external_traded_size: Mapped[Decimal | None] = mapped_column(DecimalText())
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)


class GrvtTestnetStop(RealBase):
    """Parada persistente de nuevos envíos en una subcuenta testnet."""

    __tablename__ = "grvt_testnet_stops"

    sub_account_id: Mapped[str] = mapped_column(String(30), primary_key=True)
    stopped: Mapped[bool] = mapped_column(Boolean, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)


class GrvtTestnetStopControl(RealBase):
    """Cabeza del diario de paradas; la proyección booleana sigue siendo cerrada."""
    __tablename__ = 'grvt_testnet_stop_controls'
    sub_account_id: Mapped[str] = mapped_column(ForeignKey('grvt_testnet_stops.sub_account_id'), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    head_digest: Mapped[str | None] = mapped_column(String(64))


class GrvtTestnetStopEvent(RealBase):
    __tablename__ = 'grvt_testnet_stop_events'
    sub_account_id: Mapped[str] = mapped_column(ForeignKey('grvt_testnet_stop_controls.sub_account_id'), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class GrvtTestnetCancelAttempt(RealBase):
    """Marca de cancelación testnet antes de abrir red; nunca se reenvía."""

    __tablename__ = "grvt_testnet_cancel_attempts"

    client_order_id: Mapped[str] = mapped_column(
        ForeignKey("grvt_testnet_order_intents.client_order_id"), primary_key=True,
    )
    sub_account_id: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    external_status: Mapped[str | None] = mapped_column(String(20))
    external_update_ns: Mapped[str | None] = mapped_column(String(20))
    claimed_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)


class GrvtTestnetAuditEvidence(RealBase):
    """Evidencia saneada de orden/fills/cuenta; nunca asiento REAL."""

    __tablename__ = "grvt_testnet_audit_evidence"
    __table_args__ = (
        UniqueConstraint("client_order_id", "payload_digest",
                         name="uq_grvt_testnet_audit_payload"),
        Index("ix_grvt_testnet_audit_intent", "client_order_id", "observed_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    client_order_id: Mapped[str] = mapped_column(
        ForeignKey("grvt_testnet_order_intents.client_order_id"), nullable=False,
    )
    sub_account_id: Mapped[str] = mapped_column(String(30), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class GrvtTestnetPreflightEvidence(RealBase):
    """Estado y decisión anteriores al POST, vinculados al intento testnet."""

    __tablename__ = "grvt_testnet_preflight_evidence"

    client_order_id: Mapped[str] = mapped_column(
        ForeignKey("grvt_testnet_order_intents.client_order_id"), primary_key=True,
    )
    sub_account_id: Mapped[str] = mapped_column(String(30), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class GrvtTestnetReconciliation(RealBase):
    """Recibo TESTNET acotado a dos auditorías; nunca patrimonio REAL."""

    __tablename__ = "grvt_testnet_reconciliations"
    client_order_id: Mapped[str] = mapped_column(
        ForeignKey("grvt_testnet_order_intents.client_order_id"), primary_key=True)
    sub_account_id: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    reconciled_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class GrvtTestnetOcoGroup(RealBase):
    """Contrato de dos salidas nativas TESTNET; no guarda firmas ni claves."""

    __tablename__ = "grvt_testnet_oco_groups"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    parent_client_order_id: Mapped[str] = mapped_column(
        ForeignKey("grvt_testnet_order_intents.client_order_id"), nullable=False)
    sub_account_id: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)


class GrvtTestnetOcoLeg(RealBase):
    """Reserva permanente del ID cliente de cada salida, incluso sin POST."""

    __tablename__ = "grvt_testnet_oco_legs"
    __table_args__ = (UniqueConstraint("group_id", "role", name="uq_grvt_oco_leg_role"),)
    client_order_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    group_id: Mapped[str] = mapped_column(ForeignKey("grvt_testnet_oco_groups.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(12), nullable=False)


class GrvtTestnetOcoClaim(RealBase):
    """Marca irreversible anterior al POST; no autoriza operar por sí sola."""

    __tablename__ = "grvt_testnet_oco_claims"
    group_id: Mapped[str] = mapped_column(ForeignKey("grvt_testnet_oco_groups.id"), primary_key=True)
    claimed_at_ns: Mapped[str] = mapped_column(String(20), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    ack_json: Mapped[str | None] = mapped_column(Text)
    ack_digest: Mapped[str | None] = mapped_column(String(64))
    preflight_json: Mapped[str | None] = mapped_column(Text)
    preflight_digest: Mapped[str | None] = mapped_column(String(64))


class GrvtTestnetOcoObservation(RealBase):
    """Estados saneados de ambas salidas; no fills ni saldo conciliado."""

    __tablename__ = "grvt_testnet_oco_observations"
    __table_args__ = (UniqueConstraint("group_id", "sequence", name="uq_grvt_oco_observation_sequence"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    group_id: Mapped[str] = mapped_column(ForeignKey("grvt_testnet_oco_groups.id"), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class GrvtTestnetOcoCancel(RealBase):
    """Única cancelación expresa del grupo; un acuse no es conciliación."""

    __tablename__ = "grvt_testnet_oco_cancels"
    group_id: Mapped[str] = mapped_column(ForeignKey("grvt_testnet_oco_groups.id"), primary_key=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    ack_json: Mapped[str | None] = mapped_column(Text)
    ack_digest: Mapped[str | None] = mapped_column(String(64))


class GrvtTestnetOcoAudit(RealBase):
    """Snapshot financiero conjunto saneado; separado del libro REAL."""

    __tablename__ = "grvt_testnet_oco_audits"
    __table_args__ = (UniqueConstraint("group_id", "sequence", name="uq_grvt_oco_audit_sequence"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    group_id: Mapped[str] = mapped_column(ForeignKey("grvt_testnet_oco_groups.id"), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class GrvtTestnetOcoReconciliation(RealBase):
    """Recibo financiero conjunto acotado; no es permiso de trading."""

    __tablename__ = "grvt_testnet_oco_reconciliations"
    group_id: Mapped[str] = mapped_column(ForeignKey("grvt_testnet_oco_groups.id"), primary_key=True)
    reconciled_at_ns: Mapped[str] = mapped_column(String(20), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class GrvtTestnetBracketGroup(RealBase):
    """Entrada FOK y dos salidas: términos, no firmas ni claves completas."""

    __tablename__ = 'grvt_testnet_bracket_groups'
    parent_client_order_id: Mapped[str] = mapped_column(String(20),primary_key=True)
    sub_account_id: Mapped[str] = mapped_column(String(30),nullable=False,index=True)
    status: Mapped[str] = mapped_column(String(24),nullable=False)
    payload_json: Mapped[str] = mapped_column(Text,nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64),nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(),nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp(),nullable=False)


class GrvtTestnetMarketSample(RealBase):
    """An immutable diagnostic capture linked to one posted TESTNET bracket."""

    __tablename__ = "grvt_testnet_market_samples"
    __table_args__ = (Index("ix_grvt_market_samples_parent_time",
                            "parent_client_order_id", "observed_at_ns", "id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    parent_client_order_id: Mapped[str] = mapped_column(
        ForeignKey("grvt_testnet_bracket_groups.parent_client_order_id"), nullable=False)
    observed_at_ns: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class GrvtTestnetBracketLeg(RealBase):
    """Reserva permanente de cada ID, también el del padre sin ejecución."""

    __tablename__ = 'grvt_testnet_bracket_legs'
    __table_args__ = (UniqueConstraint('parent_client_order_id','role',name='uq_grvt_bracket_role'),)
    client_order_id: Mapped[str] = mapped_column(String(20),primary_key=True)
    parent_client_order_id: Mapped[str] = mapped_column(ForeignKey('grvt_testnet_bracket_groups.parent_client_order_id'),nullable=False)
    role: Mapped[str] = mapped_column(String(12),nullable=False)


class GrvtTestnetBracketClaim(RealBase):
    """Concesión de un único bulk; nunca se borra para reintentar."""

    __tablename__ = 'grvt_testnet_bracket_claims'
    parent_client_order_id: Mapped[str] = mapped_column(ForeignKey('grvt_testnet_bracket_groups.parent_client_order_id'),primary_key=True)
    claimed_at_ns: Mapped[str] = mapped_column(String(20),nullable=False)
    plan_digest: Mapped[str] = mapped_column(String(64),nullable=False)
    preflight_json: Mapped[str] = mapped_column(Text,nullable=False)
    preflight_digest: Mapped[str] = mapped_column(String(64),nullable=False)
    ack_json: Mapped[str | None] = mapped_column(Text,nullable=True)
    ack_digest: Mapped[str | None] = mapped_column(String(64),nullable=True)
    observation_count: Mapped[int] = mapped_column(Integer,nullable=False,default=0)
    observation_head_digest: Mapped[str | None] = mapped_column(String(64),nullable=True)


class GrvtTestnetBracketObservation(RealBase):
    """Cadena de estados saneados de las tres órdenes; no saldo ni fills."""

    __tablename__ = 'grvt_testnet_bracket_observations'
    __table_args__ = (UniqueConstraint('parent_client_order_id','sequence',name='uq_grvt_bracket_observation_sequence'),)
    id: Mapped[str] = mapped_column(String(36),primary_key=True)
    parent_client_order_id: Mapped[str] = mapped_column(ForeignKey('grvt_testnet_bracket_groups.parent_client_order_id'),nullable=False)
    sequence: Mapped[int] = mapped_column(Integer,nullable=False)
    payload_json: Mapped[str] = mapped_column(Text,nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64),nullable=False)


class GrvtTestnetBracketCancel(RealBase):
    """Un intento irreversible de retirar el grupo, no de cerrar posición."""

    __tablename__ = 'grvt_testnet_bracket_cancels'
    parent_client_order_id: Mapped[str] = mapped_column(ForeignKey('grvt_testnet_bracket_groups.parent_client_order_id'),primary_key=True)
    payload_json: Mapped[str] = mapped_column(Text,nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64),nullable=False)
    ack_json: Mapped[str | None] = mapped_column(Text,nullable=True)
    ack_digest: Mapped[str | None] = mapped_column(String(64),nullable=True)


class GrvtTestnetBracketAudit(RealBase):
    """Evidencia financiera conjunta de entrada y dos salidas TESTNET."""

    __tablename__='grvt_testnet_bracket_audits'
    __table_args__=(UniqueConstraint('parent_client_order_id','sequence',name='uq_grvt_bracket_audit_sequence'),)
    id: Mapped[str]=mapped_column(String(36),primary_key=True)
    parent_client_order_id: Mapped[str]=mapped_column(ForeignKey('grvt_testnet_bracket_groups.parent_client_order_id'),nullable=False)
    sequence: Mapped[int]=mapped_column(Integer,nullable=False)
    payload_json: Mapped[str]=mapped_column(Text,nullable=False)
    payload_digest: Mapped[str]=mapped_column(String(64),nullable=False)


class GrvtTestnetBracketAuditHead(RealBase):
    """Ancla de recuento/cabeza para detectar auditorías truncadas."""

    __tablename__='grvt_testnet_bracket_audit_heads'
    parent_client_order_id: Mapped[str]=mapped_column(ForeignKey('grvt_testnet_bracket_groups.parent_client_order_id'),primary_key=True)
    count: Mapped[int]=mapped_column(Integer,nullable=False)
    digest: Mapped[str]=mapped_column(String(64),nullable=False)


class GrvtTestnetBracketReconciliation(RealBase):
    """Recibo financiero del grupo completo; no asientos REAL ni rearme."""

    __tablename__='grvt_testnet_bracket_reconciliations'
    parent_client_order_id: Mapped[str]=mapped_column(ForeignKey('grvt_testnet_bracket_groups.parent_client_order_id'),primary_key=True)
    reconciled_at_ns: Mapped[str]=mapped_column(String(20),nullable=False)
    payload_json: Mapped[str]=mapped_column(Text,nullable=False)
    payload_digest: Mapped[str]=mapped_column(String(64),nullable=False)


class GrvtTestnetBracketEmergency(RealBase):
    """Ámbito irreversible para un IOC de emergencia; comparte el diario IOC."""

    __tablename__='grvt_testnet_bracket_emergencies'
    client_order_id: Mapped[str]=mapped_column(ForeignKey('grvt_testnet_order_intents.client_order_id'),primary_key=True)
    parent_client_order_id: Mapped[str]=mapped_column(ForeignKey('grvt_testnet_bracket_groups.parent_client_order_id'),nullable=False,index=True)
    payload_json: Mapped[str]=mapped_column(Text,nullable=False)
    payload_digest: Mapped[str]=mapped_column(String(64),nullable=False)


class GrvtTestnetBracketStrategy(RealBase):
    """Definición causal archivada antes de reclamar/enviar la entrada OTOCO."""

    __tablename__ = 'grvt_testnet_bracket_strategies'

    parent_client_order_id: Mapped[str] = mapped_column(
        ForeignKey('grvt_testnet_bracket_groups.parent_client_order_id'), primary_key=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class GrvtMainnetEntryFills(RealBase):
    __tablename__='grvt_mainnet_entry_fills'
    parent_client_order_id: Mapped[str]=mapped_column(ForeignKey('grvt_mainnet_brackets.parent_client_order_id'),primary_key=True)
    payload_json: Mapped[str]=mapped_column(Text,nullable=False)
    payload_digest: Mapped[str]=mapped_column(String(64),nullable=False)


class GrvtMainnetBracket(RealBase):
    """Intento MAINNET propio: no comparte identidades ni permisos TESTNET."""

    __tablename__='grvt_mainnet_brackets'
    parent_client_order_id: Mapped[str]=mapped_column(String(20),primary_key=True)
    sub_account_id: Mapped[str]=mapped_column(String(20),nullable=False,index=True)
    active_slot: Mapped[str | None]=mapped_column(String(20),unique=True)
    status: Mapped[str]=mapped_column(String(24),nullable=False)
    payload_json: Mapped[str]=mapped_column(Text,nullable=False)
    payload_digest: Mapped[str]=mapped_column(String(64),nullable=False)
    ack_json: Mapped[str | None]=mapped_column(Text)
    ack_digest: Mapped[str | None]=mapped_column(String(64))
    observation_count: Mapped[int]=mapped_column(Integer,nullable=False,default=0)
    observation_head: Mapped[str | None]=mapped_column(String(64))


class GrvtMainnetObservation(RealBase):
    __tablename__='grvt_mainnet_observations'
    parent_client_order_id: Mapped[str]=mapped_column(ForeignKey('grvt_mainnet_brackets.parent_client_order_id'),primary_key=True)
    sequence: Mapped[int]=mapped_column(Integer,primary_key=True)
    payload_json: Mapped[str]=mapped_column(Text,nullable=False)
    payload_digest: Mapped[str]=mapped_column(String(64),nullable=False)


class GrvtMainnetLeg(RealBase):
    __tablename__='grvt_mainnet_legs'
    client_order_id: Mapped[str]=mapped_column(String(20),primary_key=True)
    parent_client_order_id: Mapped[str]=mapped_column(ForeignKey('grvt_mainnet_brackets.parent_client_order_id'),nullable=False,index=True)
    role: Mapped[str]=mapped_column(String(16),nullable=False)


class GrvtMainnetLimitAttempt(RealBase):
    __tablename__ = 'grvt_mainnet_limit_attempts'
    client_order_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    sub_account_id: Mapped[str] = mapped_column(ForeignKey('grvt_mainnet_controls.sub_account_id'), index=True)
    signal_key: Mapped[str] = mapped_column(String(64), unique=True)
    payload_json: Mapped[str] = mapped_column(Text)
    payload_digest: Mapped[str] = mapped_column(String(64), unique=True)
    event_count: Mapped[int] = mapped_column(Integer, default=0)
    event_head: Mapped[str | None] = mapped_column(String(64))


class GrvtMainnetLimitSlot(RealBase):
    __tablename__ = 'grvt_mainnet_limit_slots'
    sub_account_id: Mapped[str] = mapped_column(ForeignKey('grvt_mainnet_controls.sub_account_id'), primary_key=True)
    client_order_id: Mapped[str] = mapped_column(ForeignKey('grvt_mainnet_limit_attempts.client_order_id'), unique=True)
    claim_digest: Mapped[str] = mapped_column(String(64))


class GrvtMainnetLimitEvent(RealBase):
    __tablename__ = 'grvt_mainnet_limit_events'
    client_order_id: Mapped[str] = mapped_column(ForeignKey('grvt_mainnet_limit_attempts.client_order_id'), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload_json: Mapped[str] = mapped_column(Text)
    payload_digest: Mapped[str] = mapped_column(String(64))


class GrvtMainnetLimitReduction(RealBase):
    __tablename__ = 'grvt_mainnet_limit_reductions'
    client_order_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    entry_client_order_id: Mapped[str] = mapped_column(ForeignKey('grvt_mainnet_limit_attempts.client_order_id'), index=True)
    payload_json: Mapped[str] = mapped_column(Text)
    payload_digest: Mapped[str] = mapped_column(String(64), unique=True)


class GrvtMainnetAudit(RealBase):
    __tablename__='grvt_mainnet_audits'
    parent_client_order_id: Mapped[str]=mapped_column(ForeignKey('grvt_mainnet_brackets.parent_client_order_id'),primary_key=True)
    sequence: Mapped[int]=mapped_column(Integer,primary_key=True)
    payload_json: Mapped[str]=mapped_column(Text,nullable=False)
    payload_digest: Mapped[str]=mapped_column(String(64),nullable=False)


class GrvtMainnetAuditHead(RealBase):
    __tablename__='grvt_mainnet_audit_heads'
    parent_client_order_id: Mapped[str]=mapped_column(ForeignKey('grvt_mainnet_brackets.parent_client_order_id'),primary_key=True)
    count: Mapped[int]=mapped_column(Integer,nullable=False)
    digest: Mapped[str]=mapped_column(String(64),nullable=False)


class GrvtMainnetReconciliation(RealBase):
    __tablename__='grvt_mainnet_reconciliations'
    parent_client_order_id: Mapped[str]=mapped_column(ForeignKey('grvt_mainnet_brackets.parent_client_order_id'),primary_key=True)
    payload_json: Mapped[str]=mapped_column(Text,nullable=False)
    payload_digest: Mapped[str]=mapped_column(String(64),nullable=False)


class GrvtMainnetSettlementControl(RealBase):
    """Ancla por subcuenta que detecta recibos finales borrados o truncados."""
    __tablename__='grvt_mainnet_settlement_controls'
    sub_account_id: Mapped[str]=mapped_column(String(20),primary_key=True)
    parent_client_order_id: Mapped[str]=mapped_column(ForeignKey('grvt_mainnet_brackets.parent_client_order_id'),nullable=False)
    count: Mapped[int]=mapped_column(Integer,nullable=False)
    digest: Mapped[str]=mapped_column(String(64),nullable=False)


class GrvtMainnetRecovery(RealBase):
    __tablename__='grvt_mainnet_recoveries'
    parent_client_order_id: Mapped[str]=mapped_column(ForeignKey('grvt_mainnet_brackets.parent_client_order_id'),primary_key=True)
    payload_json: Mapped[str]=mapped_column(Text,nullable=False)
    payload_digest: Mapped[str]=mapped_column(String(64),nullable=False)
    event_count: Mapped[int]=mapped_column(Integer,nullable=False)
    head_digest: Mapped[str | None]=mapped_column(String(64))


class GrvtMainnetRecoveryEvent(RealBase):
    __tablename__='grvt_mainnet_recovery_events'
    parent_client_order_id: Mapped[str]=mapped_column(ForeignKey('grvt_mainnet_recoveries.parent_client_order_id'),primary_key=True)
    sequence: Mapped[int]=mapped_column(Integer,primary_key=True)
    payload_json: Mapped[str]=mapped_column(Text,nullable=False)
    payload_digest: Mapped[str]=mapped_column(String(64),nullable=False)


class GrvtMainnetReduction(RealBase):
    __tablename__='grvt_mainnet_reductions'
    __table_args__=(UniqueConstraint('parent_client_order_id','attempt'),)
    client_order_id: Mapped[str]=mapped_column(String(20),primary_key=True)
    parent_client_order_id: Mapped[str]=mapped_column(ForeignKey('grvt_mainnet_recoveries.parent_client_order_id'),nullable=False)
    attempt: Mapped[int]=mapped_column(Integer,nullable=False)
    payload_json: Mapped[str]=mapped_column(Text,nullable=False)
    payload_digest: Mapped[str]=mapped_column(String(64),nullable=False)


class GrvtMainnetControl(RealBase):
    """STOP y ancla del intento pendiente. No restaura autorización de envío."""

    __tablename__='grvt_mainnet_controls'
    sub_account_id: Mapped[str]=mapped_column(String(20),primary_key=True)
    identity_salt: Mapped[str]=mapped_column(String(64),nullable=False)
    account_ref: Mapped[str | None]=mapped_column(String(64))
    stopped: Mapped[bool]=mapped_column(Boolean,nullable=False)
    revision: Mapped[int]=mapped_column(Integer,nullable=False)
    pending_parent_id: Mapped[str | None]=mapped_column(ForeignKey('grvt_mainnet_brackets.parent_client_order_id'))


class GrvtTestnetStrategyClose(RealBase):
    """Solicitud causal de cierre; sin credenciales ni permiso de nuevas entradas."""

    __tablename__='grvt_testnet_strategy_closes'
    parent_client_order_id: Mapped[str]=mapped_column(ForeignKey('grvt_testnet_bracket_groups.parent_client_order_id'),primary_key=True)
    close_client_order_id: Mapped[str]=mapped_column(String(20),nullable=False,unique=True)
    payload_json: Mapped[str]=mapped_column(Text,nullable=False)
    payload_digest: Mapped[str]=mapped_column(String(64),nullable=False)


class GrvtTestnetExitWatch(RealBase):
    """Límites archivados del seguimiento; nunca restaura autoridad o claves."""

    __tablename__ = 'grvt_testnet_exit_watches'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    parent_client_order_id: Mapped[str] = mapped_column(
        ForeignKey('grvt_testnet_bracket_groups.parent_client_order_id'), nullable=False, index=True)
    started_at_ns: Mapped[str] = mapped_column(String(20), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False)
    head_digest: Mapped[str | None] = mapped_column(String(64))


class GrvtTestnetExitWatchEvent(RealBase):
    __tablename__ = 'grvt_testnet_exit_watch_events'
    watch_id: Mapped[str] = mapped_column(ForeignKey('grvt_testnet_exit_watches.id'), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class GrvtTestnetBotRun(RealBase):
    """Un episodio automático acotado; reserva de subcuenta sin secretos."""

    __tablename__ = 'grvt_testnet_bot_runs'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sub_account_id: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    active_slot: Mapped[str | None] = mapped_column(String(30), unique=True)
    started_at_ns: Mapped[str] = mapped_column(String(20), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    selection_json: Mapped[str | None] = mapped_column(Text)
    selection_digest: Mapped[str | None] = mapped_column(String(64))
    terminal_json: Mapped[str | None] = mapped_column(Text)
    terminal_digest: Mapped[str | None] = mapped_column(String(64))


class GrvtTestnetBotCycle(RealBase):
    """Selección, recibo y rearme de un ciclo de una concesión v2."""
    __tablename__ = 'grvt_testnet_bot_cycles'
    run_id: Mapped[str] = mapped_column(ForeignKey('grvt_testnet_bot_runs.id'), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_client_order_id: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    selection_json: Mapped[str] = mapped_column(Text, nullable=False)
    selection_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome_json: Mapped[str | None] = mapped_column(Text)
    outcome_digest: Mapped[str | None] = mapped_column(String(64))
    rearm_json: Mapped[str | None] = mapped_column(Text)
    rearm_digest: Mapped[str | None] = mapped_column(String(64))


class RealLiveSpotPolicy(RealBase):
    """Política REAL inmutable; crearla no habilita órdenes."""

    __tablename__ = "real_live_spot_policies"
    __table_args__ = (Index("ix_real_live_policy_scope", "account_ref", "symbol"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    strategy_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    account_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(30), nullable=False)
    base_asset: Mapped[str] = mapped_column(String(16), nullable=False)
    quote_asset: Mapped[str] = mapped_column(String(16), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)


class RealLiveSpotIntent(RealBase):
    """Decisión y reserva local; no equivale a una orden externa."""

    __tablename__ = "real_live_spot_intents"
    __table_args__ = (
        Index("ix_real_live_intent_reservations", "account_ref", "status", "quote_asset", "base_asset"),
        Index("ix_real_live_intent_policy_time", "policy_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    policy_id: Mapped[str] = mapped_column(ForeignKey("real_live_spot_policies.id"), nullable=False)
    account_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(30), nullable=False)
    base_asset: Mapped[str] = mapped_column(String(16), nullable=False)
    quote_asset: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    # NULL for pre-v15 intents: their complete request cannot be reconstructed.
    request_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # NULL for legacy or manually prepared intents without a PAPER origin.
    signal_source_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    signal_source_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    notional_quote: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    # NULL only for intents written before vault schema v14; their original
    # order terms cannot be reconstructed from the request digest.
    limit_price_quote: Mapped[Decimal | None] = mapped_column(DecimalText(), nullable=True)
    quantity_base: Mapped[Decimal | None] = mapped_column(DecimalText(), nullable=True)
    reserved_quote: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    reserved_base: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)


class RealLiveStop(RealBase):
    """Parada por cuenta; permanece activa tras bloquear o reiniciar."""

    __tablename__ = "real_live_stops"

    account_ref: Mapped[str] = mapped_column(String(64), primary_key=True)
    stopped: Mapped[bool] = mapped_column(Boolean, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)


class RealSpotBot(RealBase):
    """Vínculo de bot probado y cursor REAL; nunca almacena credenciales."""

    __tablename__ = "real_spot_bots"
    __table_args__ = (Index("ix_real_spot_bot_account_status", "account_ref", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("real_accounts.id"), nullable=False)
    account_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_id: Mapped[str] = mapped_column(ForeignKey("real_live_spot_policies.id"), unique=True, nullable=False)
    config_json: Mapped[str] = mapped_column(Text, nullable=False)
    config_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    state_json: Mapped[str] = mapped_column(Text, nullable=False)
    state_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    cycle_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)


class RealSpotBotDecision(RealBase):
    """Decisión propia y, en el protocolo actual, reserva en el mismo commit."""

    __tablename__ = "real_spot_bot_decisions"
    __table_args__ = (Index("ix_real_spot_bot_decision_time", "bot_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    bot_id: Mapped[str] = mapped_column(ForeignKey("real_spot_bots.id"), nullable=False)
    # No FK inmediata: las decisiones heredadas pueden preceder al journal.
    # El protocolo actual verifica el par atómico sin reescribir el legado.
    intent_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)


class RealSpotBotMarketStream(RealBase):
    """Semilla aprobada de reglas/datos REAL, independiente del progreso PAPER."""

    __tablename__ = "real_spot_bot_market_streams"
    policy_id: Mapped[str] = mapped_column(ForeignKey("real_live_spot_policies.id"), primary_key=True)
    bot_id: Mapped[str] = mapped_column(ForeignKey("real_spot_bots.id"), nullable=False)
    seed_json: Mapped[str] = mapped_column(Text, nullable=False)
    seed_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)


class RealSpotBotMarketObservation(RealBase):
    """Una vela cerrada observada por el bot REAL, nunca una ejecución."""

    __tablename__ = "real_spot_bot_market_observations"
    __table_args__ = (UniqueConstraint("policy_id","candle_t",name="uq_bot_real_market_candle"),)
    policy_id: Mapped[str] = mapped_column(ForeignKey("real_spot_bot_market_streams.policy_id"), primary_key=True)
    sequence_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    candle_t: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)


class RealLiveOrderObservation(RealBase):
    """Snapshot externo enlazado a intención; no es un evento contable."""

    __tablename__ = "real_live_order_observations"
    __table_args__ = (
        UniqueConstraint("intent_id", "payload_digest", name="uq_real_live_order_snapshot"),
        Index("ix_real_live_order_intent_time", "intent_id", "observed_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    intent_id: Mapped[str] = mapped_column(ForeignKey("real_live_spot_intents.id"), nullable=False)
    account_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(30), nullable=False)
    external_order_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    executed_base: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    cumulative_quote: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    fee_json: Mapped[str] = mapped_column(Text, nullable=False)
    fill_count: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)


class RealMexcAccountLink(RealBase):
    """Asignación manual de una referencia MEXC a un libro REAL; no prueba UID."""

    __tablename__ = "real_mexc_account_links"

    account_ref: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("real_accounts.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)


class RealMexcParityObservation(RealBase):
    """Doble lectura Spot cifrada; no es conciliación ni permiso de envío."""

    __tablename__ = "real_mexc_parity_observations"
    __table_args__ = (
        Index("ix_real_mexc_parity_account_time", "account_id", "checked_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("real_accounts.id"), nullable=False)
    account_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    book_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    checked_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)
    report_json: Mapped[str] = mapped_column(Text, nullable=False)
    report_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class RealMexcOpenOrdersObservation(RealBase):
    """Órdenes Spot visibles cifradas; cobertura de cuenta nunca demostrada."""

    __tablename__ = "real_mexc_open_orders_observations"
    __table_args__ = (
        Index("ix_real_mexc_open_orders_account_time", "account_id", "observed_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("real_accounts.id"), nullable=False)
    account_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class RealMexcOrderTestAttempt(RealBase):
    """Ensayo firmado sin orden negociable; reclamado antes del POST externo."""

    __tablename__ = "real_mexc_order_test_attempts"
    __table_args__ = (Index("ix_real_mexc_order_test_account_time", "account_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("real_accounts.id"), nullable=False)
    account_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_id: Mapped[str] = mapped_column(String(36), nullable=False)
    strategy_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(30), nullable=False)
    quantity_base: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    limit_price_quote: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    client_order_id: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UtcTimestamp())
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class RealMexcIdentitySalt(RealBase):
    """Sal aleatoria de la bóveda para seudonimizar el UID MEXC."""

    __tablename__ = "real_mexc_identity_salt"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    salt_hex: Mapped[str] = mapped_column(String(64), nullable=False)


class RealMexcUidLink(RealBase):
    """UID seudónimo por clave; nunca guarda el UID ni la credencial."""

    __tablename__ = "real_mexc_uid_links"
    __table_args__ = (Index("ix_real_mexc_uid_digest", "uid_digest"),)

    account_ref: Mapped[str] = mapped_column(String(64), primary_key=True)
    uid_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)


class RealMexcViewBinding(RealBase):
    """Current replacement View reference; credentials and raw UID stay in memory."""

    __tablename__ = "real_mexc_view_bindings"
    __table_args__ = (CheckConstraint("archive_ref <> credential_ref"),)

    archive_ref: Mapped[str] = mapped_column(
        ForeignKey("real_mexc_account_links.account_ref"), primary_key=True)
    credential_ref: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    uid_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    bound_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)


class RealWalletIdentitySalt(RealBase):
    """Sal estable de esta bóveda cifrada; nunca es una clave de gasto."""

    __tablename__ = "real_wallet_identity_salt"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    salt_hex: Mapped[str] = mapped_column(String(64), nullable=False)


class RealWalletLink(RealBase):
    """Asociación declarada tras una prueba EOA; no contiene dirección completa."""

    __tablename__ = "real_wallet_links"
    __table_args__ = (
        UniqueConstraint("chain_id", "address_digest", name="uq_real_wallet_chain_digest"),
        Index("ix_real_wallet_links_account", "account_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("real_accounts.id"), nullable=False)
    chain_id: Mapped[str] = mapped_column(String(18), nullable=False)
    address_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    alias: Mapped[str] = mapped_column(String(80), nullable=False)
    verified_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)


class RealMexcWithdrawal(RealBase):
    """Solicitud USDT/BSC de un solo uso, sin dirección ni secretos persistidos."""

    __tablename__ = "real_mexc_withdrawals"
    __table_args__ = (Index("ix_mexc_withdrawal_pending","account_ref","status"),)
    id: Mapped[str] = mapped_column(String(36),primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("real_accounts.id"),nullable=False)
    account_ref: Mapped[str] = mapped_column(String(64),nullable=False)
    status: Mapped[str] = mapped_column(String(32),nullable=False)
    payload_json: Mapped[str] = mapped_column(Text,nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64),nullable=False)
    state_json: Mapped[str] = mapped_column(Text,nullable=False)
    state_digest: Mapped[str] = mapped_column(String(64),nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(),nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp(),nullable=False)


class RealMexcWithdrawalObservation(RealBase):
    """Foto View de una retirada propia, sin direcciones ni txId públicos."""
    __tablename__ = "real_mexc_withdrawal_observations"
    __table_args__ = (Index("ix_mexc_withdrawal_observed","withdrawal_id","observed_at"),)
    id: Mapped[str] = mapped_column(String(36),primary_key=True)
    withdrawal_id: Mapped[str] = mapped_column(ForeignKey("real_mexc_withdrawals.id"),nullable=False)
    account_ref: Mapped[str] = mapped_column(String(64),nullable=False)
    source_ref: Mapped[str] = mapped_column(String(64),nullable=False)
    status: Mapped[int] = mapped_column(Integer,nullable=False)
    observed_at: Mapped[datetime] = mapped_column(UtcTimestamp(),nullable=False)
    payload_json: Mapped[str] = mapped_column(Text,nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64),nullable=False)


class RealMexcWithdrawalReceipt(RealBase):
    __tablename__ = "real_mexc_withdrawal_receipts"
    __table_args__ = (UniqueConstraint("account_ref","source_ref"),)
    withdrawal_id: Mapped[str] = mapped_column(ForeignKey("real_mexc_withdrawals.id"),primary_key=True)
    observation_id: Mapped[str] = mapped_column(ForeignKey("real_mexc_withdrawal_observations.id"),nullable=False)
    account_ref: Mapped[str] = mapped_column(String(64),nullable=False)
    source_ref: Mapped[str] = mapped_column(String(64),nullable=False)
    payload_json: Mapped[str] = mapped_column(Text,nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64),nullable=False)


class RealMexcWithdrawalBooking(RealBase):
    __tablename__ = "real_mexc_withdrawal_bookings"
    withdrawal_id: Mapped[str] = mapped_column(ForeignKey("real_mexc_withdrawals.id"),primary_key=True)
    source_event_id: Mapped[str | None] = mapped_column(ForeignKey("real_events.id"),unique=True)
    target_event_id: Mapped[str | None] = mapped_column(ForeignKey("real_events.id"),unique=True)
    payload_json: Mapped[str] = mapped_column(Text,nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64),nullable=False)


class RealWalletFundingIntent(RealBase):
    """Solicitud wallet→MEXC, sin direcciones privadas ni hash público de tx."""

    __tablename__ = "real_wallet_funding_intents"
    __table_args__ = (Index("ix_wallet_funding_account", "account_id", "created_at"),
                      Index("ix_wallet_funding_wallet", "wallet_digest", "status"))

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("real_accounts.id"), nullable=False)
    account_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    wallet_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    state_json: Mapped[str] = mapped_column(Text, nullable=False)
    state_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)


class RealWalletSwapIntent(RealBase):
    """Acción DEX manual de un solo uso; direcciones de usuario sólo como HMAC."""

    __tablename__ = "real_wallet_swap_intents"
    __table_args__ = (Index("ix_wallet_swap_account", "account_id", "created_at"),
                      Index("ix_wallet_swap_wallet", "wallet_digest", "status"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("real_accounts.id"), nullable=False)
    wallet_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    state_json: Mapped[str] = mapped_column(Text, nullable=False)
    state_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)


class RealDexDeploymentIntent(RealBase):
    """Owner-wallet CREATE request; no address, signing key or raw transaction is stored."""
    __tablename__ = 'real_dex_deployment_intents'
    __table_args__ = (Index('ix_dex_deployment_account','account_id','created_at'),
                      Index('ix_dex_deployment_wallet','wallet_digest','status'))
    id: Mapped[str] = mapped_column(String(36),primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey('real_accounts.id'),nullable=False)
    wallet_digest: Mapped[str] = mapped_column(String(64),nullable=False)
    payload_json: Mapped[str] = mapped_column(Text,nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64),nullable=False)
    state_json: Mapped[str] = mapped_column(Text,nullable=False)
    state_digest: Mapped[str] = mapped_column(String(64),nullable=False)
    status: Mapped[str] = mapped_column(String(32),nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(),nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp(),nullable=False)


class RealDexControlIntent(RealBase):
    """Reviewed owner-wallet configuration/STOP; addresses and keys stay transient."""
    __tablename__ = 'real_dex_control_intents'
    __table_args__ = (Index('ix_dex_control_account', 'account_id', 'created_at'),
                      Index('ix_dex_control_wallet', 'wallet_digest', 'status'))
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey('real_accounts.id'), nullable=False)
    deployment_id: Mapped[str] = mapped_column(ForeignKey('real_dex_deployment_intents.id'), nullable=False)
    wallet_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    state_json: Mapped[str] = mapped_column(Text, nullable=False)
    state_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)


class RealDexOperatorGrant(RealBase):
    """Gas authority for one exact on-chain policy; no key/address persistence."""
    __tablename__ = 'real_dex_operator_grants'
    __table_args__ = (UniqueConstraint('deployment_id', 'policy_id'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey('real_accounts.id'), nullable=False)
    deployment_id: Mapped[str] = mapped_column(ForeignKey('real_dex_deployment_intents.id'), nullable=False)
    policy_id: Mapped[str] = mapped_column(String(78), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    state_json: Mapped[str] = mapped_column(Text, nullable=False)
    state_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)


class RealDexOperatorIntent(RealBase):
    __tablename__ = 'real_dex_operator_intents'
    __table_args__ = (Index('ix_dex_operator_wallet', 'wallet_digest', 'status'),
                      UniqueConstraint('grant_id', 'decision_ref'))
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey('real_accounts.id'), nullable=False)
    grant_id: Mapped[str] = mapped_column(ForeignKey('real_dex_operator_grants.id'), nullable=False)
    wallet_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    state_json: Mapped[str] = mapped_column(Text, nullable=False)
    state_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)


class RealDexBotBinding(RealBase):
    """Immutable tested source and REAL limits for one executor policy."""
    __tablename__ = 'real_dex_bot_bindings'
    __table_args__ = (UniqueConstraint('grant_id'),Index('ix_dex_bot_binding_deployment','deployment_id','status'))
    id: Mapped[str] = mapped_column(String(36),primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey('real_accounts.id'),nullable=False)
    deployment_id: Mapped[str] = mapped_column(ForeignKey('real_dex_deployment_intents.id'),nullable=False)
    grant_id: Mapped[str] = mapped_column(ForeignKey('real_dex_operator_grants.id'),nullable=False)
    payload_json: Mapped[str] = mapped_column(Text,nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64),nullable=False)
    state_json: Mapped[str] = mapped_column(Text,nullable=False)
    state_digest: Mapped[str] = mapped_column(String(64),nullable=False)
    status: Mapped[str] = mapped_column(String(32),nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(),nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp(),nullable=False)


class RealDexGasFundingIntent(RealBase):
    """Owner-wallet native BNB for an existing bounded bot operator."""
    __tablename__ = 'real_dex_gas_funding_intents'
    __table_args__ = (Index('ix_dex_gas_funding_wallet','wallet_digest','status'),)
    id: Mapped[str] = mapped_column(String(36),primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey('real_accounts.id'),nullable=False)
    binding_id: Mapped[str] = mapped_column(ForeignKey('real_dex_bot_bindings.id'),nullable=False)
    wallet_digest: Mapped[str] = mapped_column(String(64),nullable=False)
    payload_json: Mapped[str] = mapped_column(Text,nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64),nullable=False)
    state_json: Mapped[str] = mapped_column(Text,nullable=False)
    state_digest: Mapped[str] = mapped_column(String(64),nullable=False)
    status: Mapped[str] = mapped_column(String(32),nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(),nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp(),nullable=False)


class RealDexGasReturnIntent(RealBase):
    """Operator BNB back to its proven owner; independent of swap authority."""
    __tablename__='real_dex_gas_return_intents'
    __table_args__=(Index('ix_dex_gas_return_operator','operator_digest','status'),)
    id: Mapped[str]=mapped_column(String(36),primary_key=True)
    account_id: Mapped[str]=mapped_column(ForeignKey('real_accounts.id'),nullable=False)
    binding_id: Mapped[str]=mapped_column(ForeignKey('real_dex_bot_bindings.id'),nullable=False)
    wallet_digest: Mapped[str]=mapped_column(String(64),nullable=False)
    operator_digest: Mapped[str]=mapped_column(String(64),nullable=False)
    payload_json: Mapped[str]=mapped_column(Text,nullable=False)
    payload_digest: Mapped[str]=mapped_column(String(64),nullable=False)
    state_json: Mapped[str]=mapped_column(Text,nullable=False)
    state_digest: Mapped[str]=mapped_column(String(64),nullable=False)
    status: Mapped[str]=mapped_column(String(32),nullable=False)
    created_at: Mapped[datetime]=mapped_column(UtcTimestamp(),nullable=False)
    updated_at: Mapped[datetime]=mapped_column(UtcTimestamp(),nullable=False)


class RealDexBotRun(RealBase):
    __tablename__ = 'real_dex_bot_runs'
    __table_args__ = (Index('ix_dex_bot_run_binding','binding_id','status'),)
    id: Mapped[str] = mapped_column(String(36),primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey('real_accounts.id'),nullable=False)
    binding_id: Mapped[str] = mapped_column(ForeignKey('real_dex_bot_bindings.id'),nullable=False)
    grant_id: Mapped[str] = mapped_column(ForeignKey('real_dex_operator_grants.id'),nullable=False)
    payload_json: Mapped[str] = mapped_column(Text,nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64),nullable=False)
    state_json: Mapped[str] = mapped_column(Text,nullable=False)
    state_digest: Mapped[str] = mapped_column(String(64),nullable=False)
    status: Mapped[str] = mapped_column(String(32),nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(),nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp(),nullable=False)


class RealDexResearchBinding(RealBase):
    """Research qualification; never signing authority or a financial journal."""
    __tablename__ = 'real_dex_research_bindings'
    binding_id: Mapped[str] = mapped_column(ForeignKey('real_dex_bot_bindings.id'), primary_key=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)


class RealDexResearchAuthorization(RealBase):
    """One original-research replay for one exact activation/rearm header."""
    __tablename__ = 'real_dex_research_authorizations'
    run_id: Mapped[str] = mapped_column(ForeignKey('real_dex_bot_runs.id'), primary_key=True)
    binding_id: Mapped[str] = mapped_column(ForeignKey('real_dex_bot_bindings.id'), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)


class RealDexBotDecision(RealBase):
    __tablename__ = 'real_dex_bot_decisions'
    __table_args__ = (UniqueConstraint('run_id','sequence_no'),UniqueConstraint('payload_digest'))
    id: Mapped[str] = mapped_column(String(36),primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey('real_dex_bot_runs.id'),nullable=False)
    sequence_no: Mapped[int] = mapped_column(Integer,nullable=False)
    previous_digest: Mapped[str] = mapped_column(String(64),nullable=False)
    payload_json: Mapped[str] = mapped_column(Text,nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64),nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(),nullable=False)


class RealWalletSwapBooking(RealBase):
    __tablename__ = "real_wallet_swap_bookings"
    action_id: Mapped[str] = mapped_column(ForeignKey("real_wallet_swap_intents.id"), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("real_accounts.id"), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class RealWalletSwapBookingEvent(RealBase):
    """Un asiento no puede atribuirse a dos transacciones DEX."""
    __tablename__ = "real_wallet_swap_booking_events"
    __table_args__ = (Index("ix_wallet_swap_booking_events_action", "action_id"),)
    event_id: Mapped[str] = mapped_column(ForeignKey("real_events.id"), primary_key=True)
    action_id: Mapped[str] = mapped_column(ForeignKey("real_wallet_swap_bookings.action_id"), nullable=False)
    role: Mapped[str] = mapped_column(String(8), nullable=False)


class RealWalletFundingCredit(RealBase):
    """Recibo observado + depósito View acreditado, sin asientos automáticos."""

    __tablename__ = "real_wallet_funding_credits"
    __table_args__ = (UniqueConstraint("account_ref", "source_ref"),)
    funding_id: Mapped[str] = mapped_column(ForeignKey("real_wallet_funding_intents.id"), primary_key=True)
    account_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("mexc_transfer_snapshots.id"), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class RealWalletFundingBooking(RealBase):
    """Un depósito y un asiento REAL, una sola vez en toda la bóveda."""

    __tablename__ = "real_wallet_funding_bookings"
    funding_id: Mapped[str] = mapped_column(ForeignKey("real_wallet_funding_credits.funding_id"), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("real_events.id"), unique=True, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class RealMexcFillBooking(RealBase):
    """Cada fill externo sólo puede acreditarse una vez en toda la bóveda."""

    __tablename__ = "real_mexc_fill_bookings"

    fill_id: Mapped[str] = mapped_column(ForeignKey("mexc_observed_fills.id"), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("real_accounts.id"), nullable=False)
    event_id: Mapped[str] = mapped_column(ForeignKey("real_events.id"), unique=True, nullable=False)
    booked_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)


class RealLiveSpotSettlement(RealBase):
    """Evidencia mínima para cerrar una reserva tras reconfirmar la orden externa."""

    __tablename__ = "real_live_spot_settlements"

    intent_id: Mapped[str] = mapped_column(ForeignKey("real_live_spot_intents.id"), primary_key=True)
    observation_id: Mapped[str] = mapped_column(ForeignKey("real_live_order_observations.id"), nullable=False)
    account_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    external_order_id: Mapped[str] = mapped_column(String(128), nullable=False)
    external_status: Mapped[str] = mapped_column(String(24), nullable=False)
    balance_observed_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)
    free_base: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    locked_base: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    free_quote: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    locked_quote: Mapped[Decimal] = mapped_column(DecimalText(), nullable=False)
    balance_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    settled_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)


class GrvtMainnetBotRun(RealBase):
    """Definición y reserva durable; nunca restaura permiso ni credenciales."""
    __tablename__='grvt_mainnet_bot_runs'
    id: Mapped[str]=mapped_column(String(36),primary_key=True)
    sub_account_id: Mapped[str]=mapped_column(String(20),nullable=False,index=True)
    active_slot: Mapped[str | None]=mapped_column(String(20),unique=True)
    payload_json: Mapped[str]=mapped_column(Text,nullable=False)
    payload_digest: Mapped[str]=mapped_column(String(64),nullable=False)
    event_count: Mapped[int]=mapped_column(Integer,nullable=False)
    head_digest: Mapped[str | None]=mapped_column(String(64))


class GrvtMainnetBotEvent(RealBase):
    __tablename__='grvt_mainnet_bot_events'
    run_id: Mapped[str]=mapped_column(ForeignKey('grvt_mainnet_bot_runs.id'),primary_key=True)
    sequence: Mapped[int]=mapped_column(Integer,primary_key=True)
    payload_json: Mapped[str]=mapped_column(Text,nullable=False)
    payload_digest: Mapped[str]=mapped_column(String(64),nullable=False)


class GrvtMainnetBotSelection(RealBase):
    __tablename__='grvt_mainnet_bot_selections'
    __table_args__=(UniqueConstraint('run_id','sequence'),)
    parent_client_order_id: Mapped[str]=mapped_column(String(20),primary_key=True)
    signal_ref: Mapped[str]=mapped_column(String(64),nullable=False,unique=True)
    run_id: Mapped[str]=mapped_column(ForeignKey('grvt_mainnet_bot_runs.id'),nullable=False)
    sequence: Mapped[int]=mapped_column(Integer,nullable=False)
    digest: Mapped[str]=mapped_column(String(64),nullable=False)


class GrvtMainnetBotExit(RealBase):
    __tablename__='grvt_mainnet_bot_exits'
    parent_client_order_id: Mapped[str]=mapped_column(ForeignKey('grvt_mainnet_bot_selections.parent_client_order_id'),primary_key=True)
    payload_json: Mapped[str]=mapped_column(Text,nullable=False)
    payload_digest: Mapped[str]=mapped_column(String(64),nullable=False)


class GrvtMainnetStopOrigin(RealBase):
    __tablename__='grvt_mainnet_stop_origins'
    sub_account_id: Mapped[str]=mapped_column(ForeignKey('grvt_mainnet_controls.sub_account_id'),primary_key=True)
    payload_json: Mapped[str]=mapped_column(Text,nullable=False)
    payload_digest: Mapped[str]=mapped_column(String(64),nullable=False)


class RealDexReceiptBooking(RealBase):
    """One verified DEX source receipt, one immutable wallet ledger booking."""
    __tablename__='real_dex_receipt_bookings'
    __table_args__=(UniqueConstraint('source_kind','source_id'),Index('ix_dex_receipt_booking_account','account_id'))
    id:Mapped[str]=mapped_column(String(36),primary_key=True)
    source_kind:Mapped[str]=mapped_column(String(24),nullable=False)
    source_id:Mapped[str]=mapped_column(String(36),nullable=False)
    account_id:Mapped[str]=mapped_column(ForeignKey('real_accounts.id'),nullable=False)
    payload_json:Mapped[str]=mapped_column(Text,nullable=False)
    payload_digest:Mapped[str]=mapped_column(String(64),nullable=False)


class RealDexReceiptBookingEvent(RealBase):
    __tablename__='real_dex_receipt_booking_events'
    __table_args__=(Index('ix_dex_receipt_booking_event_parent','booking_id'),)
    event_id:Mapped[str]=mapped_column(ForeignKey('real_events.id'),primary_key=True)
    booking_id:Mapped[str]=mapped_column(ForeignKey('real_dex_receipt_bookings.id'),nullable=False)
    role:Mapped[str]=mapped_column(String(8),nullable=False)


class RealDexDeploymentResolution(RealBase):
    """A confirmed signed replacement; original CREATE evidence remains intact."""
    __tablename__='real_dex_deployment_resolutions'
    id:Mapped[str]=mapped_column(ForeignKey('real_dex_deployment_intents.id'),primary_key=True)
    account_id:Mapped[str]=mapped_column(ForeignKey('real_accounts.id'),nullable=False)
    wallet_digest:Mapped[str]=mapped_column(String(64),nullable=False)
    status:Mapped[str]=mapped_column(String(32),nullable=False)
    payload_json:Mapped[str]=mapped_column(Text,nullable=False)
    payload_digest:Mapped[str]=mapped_column(String(64),nullable=False)
    created_at:Mapped[datetime]=mapped_column(UtcTimestamp(),nullable=False)
    updated_at:Mapped[datetime]=mapped_column(UtcTimestamp(),nullable=False)


class RealDexNativeResolution(RealBase):
    """Mined native replacement, independently linked to its original journal."""
    __tablename__='real_dex_native_resolutions'
    __table_args__=(UniqueConstraint('funding_id'),UniqueConstraint('return_id'),UniqueConstraint('signer_digest','nonce'))
    id:Mapped[str]=mapped_column(String(36),primary_key=True)
    funding_id:Mapped[str|None]=mapped_column(ForeignKey('real_dex_gas_funding_intents.id'),nullable=True)
    return_id:Mapped[str|None]=mapped_column(ForeignKey('real_dex_gas_return_intents.id'),nullable=True)
    account_id:Mapped[str]=mapped_column(ForeignKey('real_accounts.id'),nullable=False)
    wallet_digest:Mapped[str]=mapped_column(String(64),nullable=False)
    signer_digest:Mapped[str]=mapped_column(String(64),nullable=False)
    nonce:Mapped[str]=mapped_column(String(20),nullable=False)
    status:Mapped[str]=mapped_column(String(32),nullable=False)
    payload_json:Mapped[str]=mapped_column(Text,nullable=False)
    payload_digest:Mapped[str]=mapped_column(String(64),nullable=False)
    created_at:Mapped[datetime]=mapped_column(UtcTimestamp(),nullable=False)
    updated_at:Mapped[datetime]=mapped_column(UtcTimestamp(),nullable=False)


class RealDexGasCancellationIntent(RealBase):
    """One separately reviewed zero-self attempt; original return stays intact."""
    __tablename__='real_dex_gas_cancellation_intents'
    __table_args__=(Index('ix_dex_gas_cancellation_return','return_id'),)
    id:Mapped[str]=mapped_column(String(36),primary_key=True)
    return_id:Mapped[str]=mapped_column(ForeignKey('real_dex_gas_return_intents.id'),nullable=False)
    account_id:Mapped[str]=mapped_column(ForeignKey('real_accounts.id'),nullable=False)
    wallet_digest:Mapped[str]=mapped_column(String(64),nullable=False)
    operator_digest:Mapped[str]=mapped_column(String(64),nullable=False)
    payload_json:Mapped[str]=mapped_column(Text,nullable=False)
    payload_digest:Mapped[str]=mapped_column(String(64),nullable=False)
    state_json:Mapped[str]=mapped_column(Text,nullable=False)
    state_digest:Mapped[str]=mapped_column(String(64),nullable=False)
    status:Mapped[str]=mapped_column(String(32),nullable=False)
    created_at:Mapped[datetime]=mapped_column(UtcTimestamp(),nullable=False)
    updated_at:Mapped[datetime]=mapped_column(UtcTimestamp(),nullable=False)


class RealDexWalletCancellationIntent(RealBase):
    """One owner-wallet cancellation mandate; no raw wallet transaction stored."""
    __tablename__='real_dex_wallet_cancellation_intents'
    __table_args__=(Index('ix_dex_wallet_cancellation_funding','funding_id'),)
    id:Mapped[str]=mapped_column(String(36),primary_key=True)
    funding_id:Mapped[str]=mapped_column(ForeignKey('real_dex_gas_funding_intents.id'),nullable=False)
    account_id:Mapped[str]=mapped_column(ForeignKey('real_accounts.id'),nullable=False)
    wallet_digest:Mapped[str]=mapped_column(String(64),nullable=False)
    operator_digest:Mapped[str]=mapped_column(String(64),nullable=False)
    payload_json:Mapped[str]=mapped_column(Text,nullable=False)
    payload_digest:Mapped[str]=mapped_column(String(64),nullable=False)
    state_json:Mapped[str]=mapped_column(Text,nullable=False)
    state_digest:Mapped[str]=mapped_column(String(64),nullable=False)
    status:Mapped[str]=mapped_column(String(32),nullable=False)
    created_at:Mapped[datetime]=mapped_column(UtcTimestamp(),nullable=False)
    updated_at:Mapped[datetime]=mapped_column(UtcTimestamp(),nullable=False)


class RealDexCallResolution(RealBase):
    """One authenticated mined outcome, without rewriting its pending source."""
    __tablename__='real_dex_call_resolutions'
    __table_args__=(UniqueConstraint('swap_id'),UniqueConstraint('operator_id'),UniqueConstraint('signer_digest','nonce'),
        UniqueConstraint('tx_digest'),CheckConstraint("(source_kind = 'WALLET_SWAP' AND swap_id IS NOT NULL AND operator_id IS NULL) OR "
            "(source_kind = 'OPERATOR' AND operator_id IS NOT NULL AND swap_id IS NULL)"))
    id:Mapped[str]=mapped_column(String(36),primary_key=True)
    source_kind:Mapped[str]=mapped_column(String(16),nullable=False)
    swap_id:Mapped[str|None]=mapped_column(ForeignKey('real_wallet_swap_intents.id'),nullable=True)
    operator_id:Mapped[str|None]=mapped_column(ForeignKey('real_dex_operator_intents.id'),nullable=True)
    account_id:Mapped[str]=mapped_column(ForeignKey('real_accounts.id'),nullable=False)
    wallet_digest:Mapped[str]=mapped_column(String(64),nullable=False)
    signer_digest:Mapped[str]=mapped_column(String(64),nullable=False)
    nonce:Mapped[str]=mapped_column(String(20),nullable=False)
    tx_digest:Mapped[str]=mapped_column(String(64),nullable=False)
    status:Mapped[str]=mapped_column(String(16),nullable=False)
    payload_json:Mapped[str]=mapped_column(Text,nullable=False)
    payload_digest:Mapped[str]=mapped_column(String(64),nullable=False)
    created_at:Mapped[datetime]=mapped_column(UtcTimestamp(),nullable=False)
    updated_at:Mapped[datetime]=mapped_column(UtcTimestamp(),nullable=False)


class RealDexExternalObservation(RealBase):
    """Immutable consumed-nonce evidence; accounting remains explicitly pending."""
    __tablename__ = 'real_dex_external_observations'
    __table_args__ = (
        UniqueConstraint('swap_id'), UniqueConstraint('operator_id'),
        UniqueConstraint('signer_digest', 'nonce'), UniqueConstraint('tx_digest'),
        CheckConstraint("(source_kind='WALLET_SWAP' AND swap_id IS NOT NULL AND operator_id IS NULL) OR "
                        "(source_kind='OPERATOR' AND operator_id IS NOT NULL AND swap_id IS NULL)"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    swap_id: Mapped[str | None] = mapped_column(ForeignKey('real_wallet_swap_intents.id'), nullable=True)
    operator_id: Mapped[str | None] = mapped_column(ForeignKey('real_dex_operator_intents.id'), nullable=True)
    account_id: Mapped[str] = mapped_column(ForeignKey('real_accounts.id'), nullable=False)
    wallet_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    signer_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    nonce: Mapped[str] = mapped_column(String(20), nullable=False)
    tx_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)


class RealDexExternalOrderProof(RealBase):
    """Supplemental chronology for an external observation, without a booking."""
    __tablename__ = 'real_dex_external_order_proofs'
    __table_args__ = (UniqueConstraint('observation_id'), UniqueConstraint('signer_digest', 'nonce'),
        UniqueConstraint('tx_digest'), UniqueConstraint('block_number', 'transaction_index'),
        CheckConstraint("source_kind='EXTERNAL_OBSERVATION'"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    observation_id: Mapped[str] = mapped_column(ForeignKey('real_dex_external_observations.id'), nullable=False)
    account_id: Mapped[str] = mapped_column(ForeignKey('real_accounts.id'), nullable=False)
    wallet_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    signer_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    nonce: Mapped[str] = mapped_column(String(20), nullable=False)
    tx_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    block_number: Mapped[str] = mapped_column(String(78), nullable=False)
    transaction_index: Mapped[str] = mapped_column(String(6), nullable=False)
    block_hash: Mapped[str] = mapped_column(String(66), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)


class RealDexExternalBooking(RealBase):
    __tablename__ = 'real_dex_external_bookings'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    observation_id: Mapped[str] = mapped_column(ForeignKey('real_dex_external_observations.id'), unique=True, nullable=False)
    order_id: Mapped[str] = mapped_column(ForeignKey('real_dex_external_order_proofs.id'), unique=True, nullable=False)
    account_id: Mapped[str] = mapped_column(ForeignKey('real_accounts.id'), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)


class RealDexExternalBookingMember(RealBase):
    __tablename__ = 'real_dex_external_booking_members'
    __table_args__ = (UniqueConstraint('event_id'), UniqueConstraint('booking_id', 'ordinal'),
        CheckConstraint("role IN ('GAS','EFFECT')"), CheckConstraint('ordinal >= 0'),)
    booking_id: Mapped[str] = mapped_column(ForeignKey('real_dex_external_bookings.id'), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey('real_events.id'), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(8), nullable=False)


class RealDexExternalBookingAccount(RealBase):
    __tablename__ = 'real_dex_external_booking_accounts'
    booking_id: Mapped[str] = mapped_column(ForeignKey('real_dex_external_bookings.id'), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey('real_accounts.id'), primary_key=True)


class RealDexExternalAcceptanceLink(RealBase):
    __tablename__ = 'real_dex_external_acceptance_links'
    booking_id: Mapped[str] = mapped_column(ForeignKey('real_dex_external_bookings.id'), primary_key=True)
    acceptance_id: Mapped[str] = mapped_column(ForeignKey('real_economic_acceptances.id'), unique=True, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)


class RealDexExternalAcceptanceGroup(RealBase):
    __tablename__ = 'real_dex_external_acceptance_groups'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    acceptance_id: Mapped[str] = mapped_column(ForeignKey('real_economic_acceptances.id'), unique=True, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)


class RealDexExternalGroupLink(RealBase):
    __tablename__ = 'real_dex_external_group_links'
    __table_args__ = (UniqueConstraint('group_id', 'ordinal'), CheckConstraint('ordinal >= 0'),)
    booking_id: Mapped[str] = mapped_column(ForeignKey('real_dex_external_bookings.id'), primary_key=True)
    group_id: Mapped[str] = mapped_column(ForeignKey('real_dex_external_acceptance_groups.id'), nullable=False)
    acceptance_id: Mapped[str] = mapped_column(ForeignKey('real_economic_acceptances.id'), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)


class RealDexReceiptOrderProof(RealBase):
    """Immutable complementary order witness; it never creates ledger events."""
    __tablename__='real_dex_receipt_order_proofs'
    __table_args__=(UniqueConstraint('swap_id'),UniqueConstraint('operator_id'),UniqueConstraint('resolution_id'),
        UniqueConstraint('tx_digest'),UniqueConstraint('signer_digest','nonce'),UniqueConstraint('block_number','transaction_index'),
        Index('ix_dex_order_context_owner','account_id','wallet_digest'),
        CheckConstraint("(source_kind='WALLET_SWAP' AND swap_id IS NOT NULL AND operator_id IS NULL AND resolution_id IS NULL) OR "
            "(source_kind='OPERATOR' AND operator_id IS NOT NULL AND swap_id IS NULL AND resolution_id IS NULL) OR "
            "(source_kind='CALL_RESOLUTION' AND resolution_id IS NOT NULL AND swap_id IS NULL AND operator_id IS NULL)"))
    id:Mapped[str]=mapped_column(String(36),primary_key=True)
    source_kind:Mapped[str]=mapped_column(String(16),nullable=False)
    swap_id:Mapped[str|None]=mapped_column(ForeignKey('real_wallet_swap_intents.id'),nullable=True)
    operator_id:Mapped[str|None]=mapped_column(ForeignKey('real_dex_operator_intents.id'),nullable=True)
    resolution_id:Mapped[str|None]=mapped_column(ForeignKey('real_dex_call_resolutions.id'),nullable=True)
    account_id:Mapped[str]=mapped_column(ForeignKey('real_accounts.id'),nullable=False)
    wallet_digest:Mapped[str]=mapped_column(String(64),nullable=False)
    signer_digest:Mapped[str]=mapped_column(String(64),nullable=False)
    nonce:Mapped[str]=mapped_column(String(20),nullable=False)
    tx_digest:Mapped[str]=mapped_column(String(64),nullable=False)
    block_number:Mapped[str]=mapped_column(String(78),nullable=False)
    transaction_index:Mapped[str]=mapped_column(String(6),nullable=False)
    block_hash:Mapped[str]=mapped_column(String(66),nullable=False)
    payload_json:Mapped[str]=mapped_column(Text,nullable=False)
    payload_digest:Mapped[str]=mapped_column(String(64),nullable=False)
    created_at:Mapped[datetime]=mapped_column(UtcTimestamp(),nullable=False)


GAS_ORDER_SOURCES={
    'WALLET_SWAP':('swap_id','real_wallet_swap_intents'),
    'OPERATOR':('operator_id','real_dex_operator_intents'),
    'CALL_RESOLUTION':('resolution_id','real_dex_call_resolutions'),
    'DEPLOYMENT':('deployment_id','real_dex_deployment_intents'),
    'CONTROL':('control_id','real_dex_control_intents'),
    'GAS_FUNDING':('funding_id','real_dex_gas_funding_intents'),
    'GAS_RETURN':('return_id','real_dex_gas_return_intents'),
    'DEPLOYMENT_RESOLUTION':('deployment_resolution_id','real_dex_deployment_resolutions'),
    'NATIVE_RESOLUTION':('native_resolution_id','real_dex_native_resolutions'),
}


class RealDexGasOrderProof(RealBase):
    """Separate gas certificates preserve the original v73 trade table intact."""
    __tablename__='real_dex_gas_order_proofs'
    __table_args__=(
        *(UniqueConstraint(field)for field,_ in GAS_ORDER_SOURCES.values()),
        UniqueConstraint('tx_digest'),UniqueConstraint('signer_digest','nonce'),UniqueConstraint('block_number','transaction_index'),
        Index('ix_dex_gas_order_context_owner','account_id','wallet_digest'),
        CheckConstraint(' OR '.join('('+"source_kind='"+kind+"' AND "+' AND '.join(
            field+(' IS NOT NULL'if other==kind else ' IS NULL')for other,(field,_)in GAS_ORDER_SOURCES.items())+')'
            for kind in GAS_ORDER_SOURCES)),
    )
    id:Mapped[str]=mapped_column(String(36),primary_key=True)
    source_kind:Mapped[str]=mapped_column(String(24),nullable=False)
    swap_id:Mapped[str|None]=mapped_column(ForeignKey('real_wallet_swap_intents.id'),nullable=True)
    operator_id:Mapped[str|None]=mapped_column(ForeignKey('real_dex_operator_intents.id'),nullable=True)
    resolution_id:Mapped[str|None]=mapped_column(ForeignKey('real_dex_call_resolutions.id'),nullable=True)
    deployment_id:Mapped[str|None]=mapped_column(ForeignKey('real_dex_deployment_intents.id'),nullable=True)
    control_id:Mapped[str|None]=mapped_column(ForeignKey('real_dex_control_intents.id'),nullable=True)
    funding_id:Mapped[str|None]=mapped_column(ForeignKey('real_dex_gas_funding_intents.id'),nullable=True)
    return_id:Mapped[str|None]=mapped_column(ForeignKey('real_dex_gas_return_intents.id'),nullable=True)
    deployment_resolution_id:Mapped[str|None]=mapped_column(ForeignKey('real_dex_deployment_resolutions.id'),nullable=True)
    native_resolution_id:Mapped[str|None]=mapped_column(ForeignKey('real_dex_native_resolutions.id'),nullable=True)
    account_id:Mapped[str]=mapped_column(ForeignKey('real_accounts.id'),nullable=False)
    wallet_digest:Mapped[str]=mapped_column(String(64),nullable=False)
    signer_digest:Mapped[str]=mapped_column(String(64),nullable=False)
    nonce:Mapped[str]=mapped_column(String(20),nullable=False)
    tx_digest:Mapped[str]=mapped_column(String(64),nullable=False)
    block_number:Mapped[str]=mapped_column(String(78),nullable=False)
    transaction_index:Mapped[str]=mapped_column(String(6),nullable=False)
    block_hash:Mapped[str]=mapped_column(String(66),nullable=False)
    payload_json:Mapped[str]=mapped_column(Text,nullable=False)
    payload_digest:Mapped[str]=mapped_column(String(64),nullable=False)
    created_at:Mapped[datetime]=mapped_column(UtcTimestamp(),nullable=False)


class RealEconomicAcceptance(RealBase):
    """Reviewed economic order, preserved independently from original events."""
    __tablename__ = 'real_economic_acceptances'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    root_account_id: Mapped[str] = mapped_column(ForeignKey('real_accounts.id'), nullable=False)
    draft_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp(), nullable=False)


class RealEconomicAcceptanceAccount(RealBase):
    __tablename__ = 'real_economic_acceptance_accounts'
    __table_args__ = (
        UniqueConstraint('account_id', 'previous_acceptance_id'),
        Index('ix_economic_acceptance_account', 'account_id', 'acceptance_id'),
    )
    acceptance_id: Mapped[str] = mapped_column(ForeignKey('real_economic_acceptances.id'), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey('real_accounts.id'), primary_key=True)
    previous_acceptance_id: Mapped[str | None] = mapped_column(ForeignKey('real_economic_acceptances.id'), nullable=True)


Index('ux_economic_acceptance_initial', RealEconomicAcceptanceAccount.account_id,
    unique=True, sqlite_where=RealEconomicAcceptanceAccount.previous_acceptance_id.is_(None))
