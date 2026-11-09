"""Vincula una orden MEXC observada con una intención exacta, sin mutar el libro."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import re
from uuid import uuid4
from uuid import UUID

from sqlalchemy.orm import Session

from app.integrations.mexc_readonly import MexcReadOnlyClient
from app.live_execution.journal import intent_request_digest
from app.live_execution.policy import LiveSpotIntent, _utc
from app.portfolio.mexc_observations import save_observation_in_transaction
from app.portfolio.models import MexcObservedFill, RealLiveOrderObservation, RealLiveSpotIntent
from app.portfolio.vault import RealVault


class MexcOrderObservationError(RuntimeError):
    """Estado no verificable: conserva la reserva y requiere conciliación."""


_EXTERNAL_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


@dataclass(frozen=True, slots=True)
class MexcSpotOrderKey:
    intent_id: str
    symbol: str
    side: str
    quantity_base: Decimal
    limit_price_quote: Decimal


@dataclass(frozen=True, slots=True)
class ObservedMexcOrder:
    intent_id: str
    external_order_id: str
    status: str
    executed_base: Decimal
    cumulative_quote: Decimal
    fills: tuple[dict, ...]
    fees_by_asset: tuple[tuple[str, Decimal], ...]
    final: bool


def _order_snapshot_digest(intent_id: str, order_id: str, status: str,
                           executed_base: Decimal, cumulative_quote: Decimal,
                           fee_json: str, fill_ids: list[str]) -> str:
    payload = {"intent_id": intent_id, "order_id": order_id,
               "status": status, "executed_base": format(executed_base, "f"),
               "cumulative_quote": format(cumulative_quote, "f"),
               "fee_json": fee_json, "fill_ids": sorted(fill_ids)}
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def observed_order_digest_matches(observed: RealLiveOrderObservation,
                                  fills: list[MexcObservedFill]) -> bool:
    """Contrasta los campos de orden y los ID de fills con la huella archivada."""
    try:
        return observed.payload_digest == _order_snapshot_digest(
            observed.intent_id, observed.external_order_id, observed.status,
            observed.executed_base, observed.cumulative_quote, observed.fee_json,
            [fill.trade_id for fill in fills],
        )
    except (TypeError, ValueError, ArithmeticError, AttributeError):
        return False


def observe_spot_intent(client: MexcReadOnlyClient,
                        intent: LiveSpotIntent | MexcSpotOrderKey) -> ObservedMexcOrder:
    """No convierte ausencia/error de red en rechazo o permiso de reenvío."""
    client_order_id = UUID(intent.intent_id).hex
    try:
        row = client.order_by_client_id(intent.symbol, client_order_id)
    except Exception:
        raise MexcOrderObservationError("Estado de orden MEXC no verificable") from None
    try:
        if (not isinstance(row, dict)
                or row.get("source") != "MEXC_SPOT_PRIVATE_READONLY"
                or row.get("accounting") != "OBSERVED_NOT_IMPORTED"
                or row.get("type") != "LIMIT"
                or row.get("symbol") != intent.symbol
                or row.get("client_order_id") != client_order_id
                or not isinstance(row["order_id"], str)
                or not _EXTERNAL_ID.fullmatch(row["order_id"])):
            raise ValueError("Invalid order id")
        side, price, quantity = row["side"], Decimal(row["price"]), Decimal(row["quantity"])
        if not price.is_finite() or not quantity.is_finite() or price <= 0 or quantity <= 0:
            raise ValueError("Invalid order amount")
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise MexcOrderObservationError("Estado de orden MEXC no verificable") from None
    if (side != intent.side or price != intent.limit_price_quote
            or quantity != intent.quantity_base):
        raise MexcOrderObservationError("La orden observada no coincide con la intención")
    try:
        observed_fills = client.trades_for_order(intent.symbol, row["order_id"])
    except Exception:
        raise MexcOrderObservationError("Fills de orden MEXC no verificables") from None
    try:
        if (not isinstance(observed_fills, dict)
                or observed_fills.get("source") != "MEXC_SPOT_PRIVATE_READONLY"
                or observed_fills.get("accounting") != "OBSERVED_NOT_IMPORTED"
                or observed_fills.get("coverage") != "ORDER_ID_BELOW_PAGE_LIMIT"
                or observed_fills.get("symbol") != intent.symbol
                or observed_fills.get("order_id") != row["order_id"]):
            raise ValueError("Invalid scoped fill page")
        fills = observed_fills["trades"]
        if not isinstance(fills, list) or len(fills) >= 1000:
            raise ValueError("Invalid fill list")
        if any(fill["side"] != intent.side or fill["order_id"] != row["order_id"]
               for fill in fills):
            raise MexcOrderObservationError("Un fill MEXC no coincide con el lado de la intención")
        with localcontext() as context:
            context.prec = 300
            executed = sum((Decimal(fill["quantity"]) for fill in fills), Decimal(0))
            quote = sum((Decimal(fill["quote_quantity"]) for fill in fills), Decimal(0))
            fees: dict[str, Decimal] = {}
            for fill in fills:
                asset = fill["commission_asset"]
                fees[asset] = fees.get(asset, Decimal(0)) + Decimal(fill["commission"])
            reported_executed = Decimal(row["executed_quantity"])
            reported_quote = Decimal(row["cumulative_quote"])
        if (not all(value.is_finite() and value >= 0 for value in
                    (executed, quote, reported_executed, reported_quote, *fees.values()))):
            raise ValueError("Invalid fill amount")
        status = row["status"]
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise MexcOrderObservationError("Fills de orden MEXC no verificables") from None
    if executed != reported_executed or quote != reported_quote:
        raise MexcOrderObservationError("Estado y fills MEXC aún no concuerdan")
    return ObservedMexcOrder(intent.intent_id, row["order_id"], status,
                             executed, quote, tuple(fills), tuple(sorted(fees.items())),
                             status in ("FILLED", "CANCELED", "PARTIALLY_CANCELED"))


def archive_observed_spot_order(vault: RealVault, intent: LiveSpotIntent,
                                observed: ObservedMexcOrder, *, now: datetime) -> dict:
    """Archiva fills y snapshot en una única transacción cifrada, sin contabilidad."""
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            result = archive_spot_observation_in_transaction(db, intent, observed, now=now)
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise


def archive_spot_observation_in_transaction(db: Session, intent: LiveSpotIntent, observed: ObservedMexcOrder, *, now: datetime) -> dict:
    """Use the caller's immediate transaction without committing."""
    now = _utc(now, "now")
    if (observed.intent_id != intent.intent_id
            or not isinstance(observed.external_order_id, str)
            or not _EXTERNAL_ID.fullmatch(observed.external_order_id)
            or observed.status not in ("NEW", "FILLED", "PARTIALLY_FILLED", "CANCELED",
                                       "PARTIALLY_CANCELED")
            or observed.final != (observed.status in ("FILLED", "CANCELED", "PARTIALLY_CANCELED"))
            or len(observed.fills) >= 1000):
        raise MexcOrderObservationError("Snapshot MEXC no verificable")
    try:
        with localcontext() as context:
            context.prec = 300
            total_base = sum((Decimal(fill["quantity"]) for fill in observed.fills), Decimal(0))
            total_quote = sum((Decimal(fill["quote_quantity"]) for fill in observed.fills), Decimal(0))
            fees: dict[str, Decimal] = {}
            for fill in observed.fills:
                if fill["order_id"] != observed.external_order_id or fill["side"] != intent.side:
                    raise ValueError("Wrong order")
                asset = fill["commission_asset"]
                fees[asset] = fees.get(asset, Decimal(0)) + Decimal(fill["commission"])
        if (total_base != observed.executed_base or total_quote != observed.cumulative_quote
                or tuple(sorted(fees.items())) != observed.fees_by_asset):
            raise ValueError("Amounts differ")
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise MexcOrderObservationError("Snapshot MEXC no verificable") from None
    if (observed.executed_base < 0 or observed.cumulative_quote < 0
            or observed.executed_base > intent.quantity_base
            or (observed.status == "NEW" and observed.executed_base != 0)
            or (observed.status == "FILLED" and observed.executed_base != intent.quantity_base)
            or (observed.status in ("PARTIALLY_FILLED", "PARTIALLY_CANCELED")
                and not 0 < observed.executed_base < intent.quantity_base)):
        raise MexcOrderObservationError("El progreso MEXC no concuerda con la intención")
    snapshot = {
        "source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
        "coverage": "ORDER_ID_BELOW_PAGE_LIMIT", "symbol": intent.symbol,
        "order_id": observed.external_order_id, "limit": 1000,
        "observed_at": now.isoformat(), "trades": list(observed.fills),
    }
    fee_json = json.dumps([[asset, format(amount, "f")]
                           for asset, amount in observed.fees_by_asset], separators=(",", ":"))
    digest = _order_snapshot_digest(
        intent.intent_id, observed.external_order_id, observed.status,
        observed.executed_base, observed.cumulative_quote, fee_json,
        [fill["id"] for fill in observed.fills],
    )
    row = db.get(RealLiveSpotIntent, intent.intent_id)
    if (row is None or row.status != "SUBMISSION_UNKNOWN"
            or row.request_digest != intent_request_digest(intent)
            or row.account_ref != intent.account_ref or row.symbol != intent.symbol
            or row.side != intent.side):
        raise MexcOrderObservationError("La intención MEXC no está pendiente de conciliación")
    linked = db.query(RealLiveOrderObservation).filter_by(intent_id=intent.intent_id).first()
    if linked is not None and linked.external_order_id != observed.external_order_id:
        raise MexcOrderObservationError("La intención MEXC ya tiene otra orden externa")
    reused = db.query(RealLiveOrderObservation).filter_by(
        account_ref=intent.account_ref, symbol=intent.symbol,
        external_order_id=observed.external_order_id,
    ).first()
    if reused is not None and reused.intent_id != intent.intent_id:
        raise MexcOrderObservationError("La orden externa MEXC ya pertenece a otra intención")
    existing = db.query(RealLiveOrderObservation).filter_by(
        intent_id=intent.intent_id, payload_digest=digest,
    ).first()
    prior = db.query(RealLiveOrderObservation).filter_by(intent_id=intent.intent_id).all()
    latest = max(prior, key=lambda item: (item.observed_at, item.id), default=None)
    if existing is not None:
        if latest is None or existing.id != latest.id or now < existing.observed_at:
            raise MexcOrderObservationError("La observación MEXC retrocedió respecto al archivo")
        try:
            fill_archive = save_observation_in_transaction(db, intent.account_ref, snapshot)
        except Exception:
            raise MexcOrderObservationError("Los fills MEXC cambiaron desde su archivo") from None
        # `observed_at` es la última confirmación de este contenido,
        # no sólo la primera. Sin esto una lectura View idéntica no
        # puede servir como evidencia reciente para cancelar.
        existing.observed_at = now
        return {"observation_id": existing.id, "new_snapshot": False,
                "new_fills": fill_archive["new_fills"]}
    for previous in prior:
        if now < previous.observed_at:
            raise MexcOrderObservationError("La hora de observación MEXC retrocedió")
        if previous.status in ("FILLED", "CANCELED", "PARTIALLY_CANCELED"):
            raise MexcOrderObservationError("La orden MEXC ya tiene un estado final distinto")
        if (observed.executed_base < previous.executed_base
                or observed.cumulative_quote < previous.cumulative_quote
                or len(observed.fills) < previous.fill_count):
            raise MexcOrderObservationError("El progreso MEXC retrocedió respecto al archivo")
        old_fees = {asset: Decimal(amount) for asset, amount in json.loads(previous.fee_json)}
        if any(dict(observed.fees_by_asset).get(asset, Decimal(0)) < amount
               for asset, amount in old_fees.items()):
            raise MexcOrderObservationError("Las comisiones MEXC retrocedieron")
    archived_ids = {fill.trade_id for fill in db.query(MexcObservedFill).filter_by(
        account_ref=intent.account_ref, symbol=intent.symbol,
        order_id=observed.external_order_id,
    ).all()}
    if not archived_ids.issubset({fill["id"] for fill in observed.fills}):
        raise MexcOrderObservationError("La página MEXC omitió fills ya archivados")
    try:
        fill_archive = save_observation_in_transaction(db, intent.account_ref, snapshot)
    except Exception:
        raise MexcOrderObservationError("No se pudieron archivar los fills MEXC") from None
    identifier = str(uuid4())
    db.add(RealLiveOrderObservation(
        id=identifier, intent_id=intent.intent_id, account_ref=intent.account_ref,
        symbol=intent.symbol, external_order_id=observed.external_order_id,
        status=observed.status, executed_base=observed.executed_base,
        cumulative_quote=observed.cumulative_quote, fee_json=fee_json,
        fill_count=len(observed.fills), payload_digest=digest, observed_at=now,
    ))
    return {"observation_id": identifier, "new_snapshot": True,
            "new_fills": fill_archive["new_fills"]}
