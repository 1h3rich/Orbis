"""Cierre de reserva MEXC sólo tras reconfirmar orden, fills y saldo externo.

No envía órdenes ni declara que el patrimonio completo esté conciliado. El
saldo observado es una prueba puntual de cuenta, no un movimiento contable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
from uuid import UUID
from typing import Callable

from sqlalchemy import select

from app.integrations.mexc_readonly import MexcReadOnlyClient, MexcReadOnlyError
from app.live_execution.mexc_fill_event import booked_event_matches
from app.live_execution.mexc_uid_guard import MexcUidGuardError, assert_known_mexc_uid, assert_rotated_mexc_uid
from app.live_execution.policy import _utc
from app.live_execution.reconciliation_view import _project_fills
from app.portfolio.domain import PortfolioError
from app.portfolio.reconciliation import compare_mexc_spot_inventory
from app.portfolio.repository import account_snapshot, verify_account_event_digests
from app.portfolio.models import (
    MexcObservedFill, RealEvent, RealLiveOrderObservation, RealLiveSpotIntent,
    RealLiveSpotSettlement, RealMexcAccountLink, RealMexcFillBooking,
)
from app.portfolio.vault import RealVault


class MexcSettlementError(RuntimeError):
    """La prueba externa no permite cerrar la reserva local."""


_FINAL = frozenset(("FILLED", "CANCELED", "PARTIALLY_CANCELED"))


def _decimal(raw: object) -> Decimal:
    if not isinstance(raw, str) or len(raw) > 100:
        raise MexcSettlementError("MEXC no devolvió un saldo decimal válido")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise MexcSettlementError("MEXC no devolvió un saldo decimal válido") from None
    if (not value.is_finite() or value < 0 or len(value.as_tuple().digits) > 40
            or abs(value.as_tuple().exponent) > 100):
        raise MexcSettlementError("MEXC no devolvió un saldo decimal válido")
    return value


def _balance_evidence(payload: dict, base: str, quote: str,
                      *, now: datetime) -> tuple[datetime, dict[str, tuple[Decimal, Decimal]], str]:
    if (not isinstance(payload, dict)
            or payload.get("source") != "MEXC_SPOT_PRIVATE_READONLY"
            or payload.get("accounting") != "OBSERVED_NOT_IMPORTED"
            or not isinstance(payload.get("balances"), list)):
        raise MexcSettlementError("El saldo MEXC no tiene procedencia verificable")
    try:
        observed_at = _utc(datetime.fromisoformat(payload["observed_at"]), "observed_at")
    except (TypeError, KeyError, ValueError):
        raise MexcSettlementError("La hora del saldo MEXC no es válida") from None
    if observed_at > now or (now - observed_at).total_seconds() > 15:
        raise MexcSettlementError("El saldo MEXC no es reciente")
    amounts: dict[str, tuple[Decimal, Decimal]] = {}
    for row in payload["balances"]:
        if not isinstance(row, dict) or not isinstance(row.get("asset"), str):
            raise MexcSettlementError("El saldo MEXC contiene un activo inválido")
        asset = row["asset"]
        if asset in amounts or not 2 <= len(asset) <= 24 or not asset.isascii() or not asset.isalnum() or asset != asset.upper():
            raise MexcSettlementError("El saldo MEXC contiene un activo duplicado o inválido")
        free, locked, total = (_decimal(row.get(key)) for key in ("free", "locked", "total"))
        with localcontext() as context:
            context.prec = 300
            if free + locked != total:
                raise MexcSettlementError("El saldo MEXC no cuadra")
        amounts[asset] = (free, locked)
    # El adaptador omite saldos nulos. La ausencia de un activo significa cero.
    amounts.setdefault(base, (Decimal(0), Decimal(0)))
    amounts.setdefault(quote, (Decimal(0), Decimal(0)))
    digest_payload = {"observed_at": observed_at.isoformat(),
                      "balances": [[asset, format(free, "f"), format(locked, "f")]
                                   for asset, (free, locked) in sorted(amounts.items())]}
    digest = hashlib.sha256(json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return observed_at, amounts, digest


def _same_fills(live: dict, archived: list[MexcObservedFill], symbol: str, order_id: str) -> bool:
    if (not isinstance(live, dict) or live.get("source") != "MEXC_SPOT_PRIVATE_READONLY"
            or live.get("accounting") != "OBSERVED_NOT_IMPORTED"
            or live.get("coverage") != "ORDER_ID_BELOW_PAGE_LIMIT"
            or live.get("symbol") != symbol or live.get("order_id") != order_id
            or not isinstance(live.get("trades"), list)
            or len(live["trades"]) != len(archived)):
        return False
    by_id = {fill.trade_id: fill for fill in archived}
    if len(by_id) != len(archived):
        return False
    seen = set()
    for row in live["trades"]:
        if not isinstance(row, dict) or row.get("id") not in by_id or row["id"] in seen:
            return False
        seen.add(row["id"])
        fill = by_id[row["id"]]
        try:
            if (row.get("order_id") != order_id or row.get("side") != fill.side
                    or row.get("time_ms") != fill.time_ms
                    or row.get("commission_asset") != fill.commission_asset
                    or any(Decimal(row[key]) != getattr(fill, field) for key, field in (
                        ("price", "price"), ("quantity", "quantity"),
                        ("quote_quantity", "quote_quantity"), ("commission", "commission")))):
                return False
        except (KeyError, TypeError, ValueError, InvalidOperation):
            return False
    return True


def close_final_spot_reservation(vault: RealVault, client: MexcReadOnlyClient,
                                 account_ref: str, intent_id: str,
                                 *, now: datetime | None = None,
                                 matching_account_id: str | None = None,
                                 clock: Callable[[], datetime] | None = None,
                                 renewed_view_ref: str | None = None) -> dict:
    """Cierra sólo una reserva final observada; nunca autoriza reenvío de orden."""
    try:
        intent_id = str(UUID(intent_id))
    except (TypeError, ValueError, AttributeError):
        raise MexcSettlementError("Identificador de intención inválido") from None
    with vault.session() as db:
        intent = db.get(RealLiveSpotIntent, intent_id)
        if intent is None or intent.account_ref != account_ref:
            raise MexcSettlementError("La intención no pertenece a esta conexión MEXC")
        if matching_account_id is not None:
            link = db.get(RealMexcAccountLink, account_ref)
            if link is None or link.account_id != matching_account_id:
                raise MexcSettlementError("La orden no pertenece a esta cuenta REAL")
        if intent.status == "RESERVATION_CLOSED":
            return {"status": "ALREADY_CLOSED", "intent_id": intent_id}
        if intent.status != "SUBMISSION_UNKNOWN":
            raise MexcSettlementError("La intención no tiene una reserva enviada pendiente")
        observed = db.scalars(select(RealLiveOrderObservation).where(
            RealLiveOrderObservation.intent_id == intent_id,
        ).order_by(RealLiveOrderObservation.observed_at.desc(),
                   RealLiveOrderObservation.id.desc()).limit(1)).first()
        if observed is None or observed.status not in _FINAL:
            raise MexcSettlementError("La orden final aún no está archivada")
        initial_observation_id = observed.id
        symbol, order_id = intent.symbol, observed.external_order_id

    if renewed_view_ref is not None and matching_account_id is None:
        raise MexcSettlementError("La recuperación con View renovada requiere una cuenta REAL")
    expected_view_ref = renewed_view_ref or account_ref
    if client.account_reference() != expected_view_ref:
        raise MexcSettlementError("La clave View no pertenece a la intención MEXC")
    try:
        uid = client.account_uid()
        if renewed_view_ref is not None:
            with vault.session() as db:
                db.connection().exec_driver_sql("BEGIN")
                assert_rotated_mexc_uid(db, account_ref, renewed_view_ref, uid)
        else:
            known_uid = assert_known_mexc_uid(vault, account_ref, uid)
            if matching_account_id is not None and not known_uid:
                raise MexcSettlementError("La identidad MEXC no tiene un vínculo verificado")
        external = client.order_by_client_id(symbol, UUID(intent_id).hex)
        live_fills = client.trades_for_order(symbol, order_id)
        balances = client.balances()
    except (MexcReadOnlyError, MexcUidGuardError):
        raise MexcSettlementError("No se pudo verificar la identidad o los datos MEXC") from None
    except MexcSettlementError:
        raise
    except Exception:
        raise MexcSettlementError("No se pudo verificar la identidad o los datos MEXC") from None
    try:
        # Las tres lecturas no son atómicas. Una rotación de conexión entre la
        # primera identidad y el saldo no puede cerrar la reserva de otra cuenta.
        same_identity = (client.account_reference() == expected_view_ref
                         and client.account_uid() == uid)
    except Exception:
        # No propagar mensajes del proveedor: pueden contener datos privados.
        raise MexcSettlementError("La identidad MEXC cambió durante la conciliación") from None
    if not same_identity:
        raise MexcSettlementError("La identidad MEXC cambió durante la conciliación")
    checked_at = _utc(now or (clock or (lambda: datetime.now(timezone.utc)))(), "now")
    observed_at, amounts, balance_digest = _balance_evidence(
        balances, intent.base_asset, intent.quote_asset, now=checked_at,
    )
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            if renewed_view_ref is not None:
                try:
                    assert_rotated_mexc_uid(db, account_ref, renewed_view_ref, uid)
                except MexcUidGuardError:
                    raise MexcSettlementError("La identidad MEXC cambió durante la conciliación") from None
            intent = db.get(RealLiveSpotIntent, intent_id)
            if intent is None or intent.account_ref != account_ref:
                raise MexcSettlementError("La conexión MEXC cambió durante la conciliación")
            if intent.status == "RESERVATION_CLOSED":
                db.commit()
                return {"status": "ALREADY_CLOSED", "intent_id": intent_id}
            if intent.status != "SUBMISSION_UNKNOWN":
                raise MexcSettlementError("La reserva cambió durante la conciliación")
            observed = db.scalars(select(RealLiveOrderObservation).where(
                RealLiveOrderObservation.intent_id == intent_id,
            ).order_by(RealLiveOrderObservation.observed_at.desc(),
                       RealLiveOrderObservation.id.desc()).limit(1)).first()
            try:
                external_price = _decimal(external.get("price"))
                external_quantity = _decimal(external.get("quantity"))
                external_base = _decimal(external.get("executed_quantity"))
                external_quote = _decimal(external.get("cumulative_quote"))
            except AttributeError:
                raise MexcSettlementError("La orden MEXC actual no es válida") from None
            with localcontext() as context:
                context.prec = 300
                external_notional = external_price * external_quantity
            if (observed is None or observed.id != initial_observation_id
                    or observed_at < observed.observed_at
                    or external.get("source") != "MEXC_SPOT_PRIVATE_READONLY"
                    or external.get("client_order_id") != UUID(intent_id).hex
                    or external.get("accounting") != "OBSERVED_NOT_IMPORTED"
                    or external.get("type") != "LIMIT"
                    or external.get("symbol") != intent.symbol
                    or external.get("side") != intent.side
                    or external.get("order_id") != observed.external_order_id
                    or external.get("status") != observed.status
                    or external_notional != intent.notional_quote
                    or (intent.limit_price_quote is not None
                        and external_price != intent.limit_price_quote)
                    or (intent.quantity_base is not None
                        and external_quantity != intent.quantity_base)
                    or (intent.side == "SELL" and intent.quantity_base is None
                        and external_quantity != intent.reserved_base)
                    or external_base != observed.executed_base
                    or external_quote != observed.cumulative_quote):
                raise MexcSettlementError("La orden MEXC actual no concuerda con el archivo")
            fills = db.scalars(select(MexcObservedFill).where(
                MexcObservedFill.account_ref == account_ref,
                MexcObservedFill.symbol == symbol,
                MexcObservedFill.order_id == order_id,
            )).all()
            projection = _project_fills(intent, observed, fills)
            if (projection["status"] not in ("FINAL_WITHOUT_EXECUTION", "NEEDS_ACCOUNT_LINK_AND_BOOKING")
                    or not _same_fills(live_fills, fills, symbol, order_id)):
                raise MexcSettlementError("Los fills MEXC actuales no concuerdan con el archivo")
            if fills:
                booked = db.scalars(select(RealMexcFillBooking).where(
                    RealMexcFillBooking.fill_id.in_([fill.id for fill in fills]),
                )).all()
                if len(booked) != len(fills) or len({row.account_id for row in booked}) != 1:
                    raise MexcSettlementError("Asienta todos los fills en una cuenta REAL antes de cerrar la reserva")
                by_fill = {row.fill_id: row for row in booked}
                link = db.get(RealMexcAccountLink, account_ref)
                for fill in fills:
                    booking = by_fill.get(fill.id)
                    event = db.get(RealEvent, booking.event_id) if booking is not None else None
                    if not booked_event_matches(fill, intent, booking, event, link):
                        raise MexcSettlementError("El asiento REAL no coincide con el fill MEXC archivado")
            base_free, base_locked = amounts[intent.base_asset]
            quote_free, quote_locked = amounts[intent.quote_asset]
            if matching_account_id is not None:
                # El mismo bloqueo de escritura protege libro, vínculo y cierre.
                # La igualdad es puntual y no acredita actividad fuera de esta
                # observación, pero una diferencia impide liberar esta reserva.
                link = db.get(RealMexcAccountLink, account_ref)
                if link is None or link.account_id != matching_account_id:
                    raise MexcSettlementError("El vínculo de la cuenta REAL cambió")
                try:
                    verify_account_event_digests(db, matching_account_id)
                    comparison = compare_mexc_spot_inventory(
                        account_snapshot(db, matching_account_id), balances, now=checked_at,
                    )
                except PortfolioError:
                    raise MexcSettlementError("El libro REAL no es verificable") from None
                if (not comparison["all_quantities_equal"]
                        or comparison["negative_book_cash_assets"]):
                    raise MexcSettlementError("El saldo MEXC no coincide con el libro REAL")
            db.add(RealLiveSpotSettlement(
                intent_id=intent_id, observation_id=observed.id, account_ref=account_ref,
                external_order_id=order_id, external_status=observed.status,
                balance_observed_at=observed_at, free_base=base_free,
                locked_base=base_locked, free_quote=quote_free,
                locked_quote=quote_locked, balance_digest=balance_digest,
                settled_at=checked_at,
            ))
            intent.status = "RESERVATION_CLOSED"
            intent.reason = "FINAL_RECONFIRMED_BALANCE_OBSERVED"
            intent.reserved_base = Decimal(0)
            intent.reserved_quote = Decimal(0)
            intent.updated_at = checked_at
            db.commit()
            return {"status": "RESERVATION_CLOSED", "intent_id": intent_id,
                    "balance_observed_at": observed_at.isoformat(),
                    "book_inventory_matched": matching_account_id is not None}
        except Exception:
            db.rollback()
            raise
