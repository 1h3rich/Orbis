"""Diario cifrado e idempotente de ensayos MEXC /order/test.

La reclamación se confirma antes de abrir red. CLAIMED tras una caída es
incierto y jamás causa un reenvío automático, aunque /order/test no ejecute.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import hmac
import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.live_execution.policy import LiveSpotIntent, LiveSpotPolicy, _utc
from app.portfolio.models import RealMexcOrderTestAttempt


STATUSES = frozenset(("CLAIMED", "ACCEPTED", "NOT_CONFIRMED"))


class MexcOrderTestArchiveError(ValueError):
    pass


def _digest(row: RealMexcOrderTestAttempt) -> str:
    fields = {
        "id": row.id, "account_id": row.account_id,
        "account_ref": row.account_ref, "policy_id": row.policy_id,
        "strategy_version_id": row.strategy_version_id,
        "symbol": row.symbol, "quantity_base": format(row.quantity_base, "f"),
        "limit_price_quote": format(row.limit_price_quote, "f"),
        "client_order_id": row.client_order_id, "status": row.status,
        "created_at": _utc(row.created_at, "created_at").isoformat(timespec="microseconds"),
        "completed_at": (_utc(row.completed_at, "completed_at").isoformat(timespec="microseconds")
                         if row.completed_at is not None else None),
    }
    payload = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def read_attempt(row: RealMexcOrderTestAttempt) -> dict:
    try:
        valid = (
            row.status in STATUSES
            and row.client_order_id == UUID(row.id).hex
            and row.created_at.tzinfo is not None
            and ((row.status == "CLAIMED" and row.completed_at is None)
                 or (row.status != "CLAIMED" and row.completed_at is not None
                     and row.completed_at >= row.created_at))
            and hmac.compare_digest(row.payload_digest, _digest(row))
        )
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise MexcOrderTestArchiveError("Ensayo MEXC archivado no verificable")
    return {"id": row.id, "account_id": row.account_id,
            "policy_id": row.policy_id, "strategy_version_id": row.strategy_version_id,
            "symbol": row.symbol, "side": "BUY",
            "quantity_base": format(row.quantity_base, "f"),
            "limit_price_quote": format(row.limit_price_quote, "f"),
            "client_order_id": row.client_order_id,
            "status": row.status, "created_at": row.created_at.isoformat(),
            "completed_at": row.completed_at.isoformat() if row.completed_at else None,
            "accepted_by_test_endpoint": row.status == "ACCEPTED",
            "executed": False, "execution_enabled": False,
            "scope": "MEXC_ORDER_TEST_ONLY", "persisted": True}


def claim_attempt(db: Session, account_id: str, policy: LiveSpotPolicy,
                  intent: LiveSpotIntent, *, now: datetime) -> tuple[dict, bool]:
    """Devuelve (archivo, created); una repetición idéntica nunca reabre red."""
    now = _utc(now, "now")
    if (policy.enabled or intent.side != "BUY" or intent.policy_id != policy.policy_id
            or intent.account_ref != policy.account_ref
            or intent.strategy_version_id != policy.strategy_version_id
            or intent.symbol != policy.symbol):
        raise MexcOrderTestArchiveError("Ensayo MEXC fuera del borrador")
    row = db.get(RealMexcOrderTestAttempt, intent.intent_id)
    if row is not None:
        stored = read_attempt(row)
        if (stored["account_id"] != account_id or stored["policy_id"] != policy.policy_id
                or stored["quantity_base"] != format(intent.quantity_base, "f")
                or stored["limit_price_quote"] != format(intent.limit_price_quote, "f")):
            raise MexcOrderTestArchiveError("ID de prueba MEXC reutilizado con otros parámetros")
        return stored, False
    row = RealMexcOrderTestAttempt(
        id=intent.intent_id, account_id=account_id,
        account_ref=policy.account_ref, policy_id=policy.policy_id,
        strategy_version_id=policy.strategy_version_id, symbol=policy.symbol,
        quantity_base=intent.quantity_base, limit_price_quote=intent.limit_price_quote,
        client_order_id=UUID(intent.intent_id).hex, status="CLAIMED",
        created_at=now, completed_at=None, payload_digest="0" * 64,
    )
    row.payload_digest = _digest(row)
    db.add(row)
    db.commit()
    return read_attempt(row), True


def complete_attempt(db: Session, attempt_id: str, status: str, *, now: datetime) -> dict:
    now = _utc(now, "now")
    if status not in ("ACCEPTED", "NOT_CONFIRMED"):
        raise MexcOrderTestArchiveError("Estado de ensayo MEXC inválido")
    row = db.get(RealMexcOrderTestAttempt, attempt_id)
    if row is None:
        raise MexcOrderTestArchiveError("Ensayo MEXC no encontrado")
    read_attempt(row)
    if row.status != "CLAIMED" or now < row.created_at:
        raise MexcOrderTestArchiveError("Ensayo MEXC ya cerrado o con reloj inválido")
    row.status = status
    row.completed_at = now
    row.payload_digest = _digest(row)
    db.commit()
    return read_attempt(row)


def list_attempts(db: Session, account_id: str, *, policy_id: str | None = None,
                  limit: int = 50) -> list[dict]:
    query = select(RealMexcOrderTestAttempt).where(
        RealMexcOrderTestAttempt.account_id == account_id)
    if policy_id is not None:
        query = query.where(RealMexcOrderTestAttempt.policy_id == policy_id)
    rows = db.scalars(query.order_by(
            RealMexcOrderTestAttempt.created_at.desc(),
            RealMexcOrderTestAttempt.id.desc()).limit(limit)).all()
    return [read_attempt(row) for row in rows]
