"""Persistencia de Cartera Real. Requiere un motor exclusivo de la bóveda REAL."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.live_execution.mexc_uid_guard import MexcUidGuardError, claim_mexc_uid_in_transaction
from app.portfolio.domain import (
    CALC_PRECISION,
    AccountSnapshot,
    EventInput,
    PortfolioError,
    PortfolioEvent,
    _symbol,
    contains_wallet_address,
    decimal_value,
    fold_events,
)
from app.portfolio.models import (RealAccount, RealEvent, RealLiveSpotIntent,
                                  RealMexcAccountLink)


class RequestConflict(PortfolioError):
    """Un request_id ya existe con una propuesta diferente."""


def _transfer_net_quantity(sent: Decimal, fee: Decimal) -> Decimal:
    # Hasta 40 dígitos y exponentes ±100 por entrada: conservar la comisión
    # más pequeña antes de validar la cantidad neta como nuevo EventInput.
    with localcontext() as context:
        context.prec = CALC_PRECISION
        return sent - fee


def _uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise PortfolioError("request_id must be a UUID") from exc


def _decimal_key(value: Decimal | None) -> str | None:
    if value is None:
        return None
    # Decimal.normalize() usa el contexto global y podría redondear una
    # cantidad con más de 28 dígitos, creando colisiones de idempotencia.
    raw = format(value, "f")
    canonical = raw.rstrip("0").rstrip(".") if "." in raw else raw
    return "0" if canonical == "-0" else canonical


def _digest(item: EventInput) -> str:
    payload = {
        "kind": item.kind,
        "asset": item.asset,
        "quote_currency": item.quote_currency,
        "quantity": _decimal_key(item.quantity),
        "unit_price": _decimal_key(item.unit_price),
        "amount": _decimal_key(item.amount),
        "fee": _decimal_key(item.fee),
        "occurred_at": item.occurred_at.isoformat(),
        "note": item.note,
    }
    if item.gross_override is not None:
        payload["gross_override"] = _decimal_key(item.gross_override)
    if item.fee_asset is not None:
        payload["fee_asset"] = item.fee_asset
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _from_row(row: RealEvent) -> PortfolioEvent:
    gross_override = None
    if row.kind in ("BUY", "SELL"):
        with localcontext() as context:
            context.prec = CALC_PRECISION
            if row.gross_amount != row.quantity * row.unit_price:
                gross_override = row.gross_amount
    elif row.kind in ("TRANSFER_IN", "TRANSFER_OUT"):
        gross_override = row.gross_amount
    item = EventInput(
        kind=row.kind,
        quote_currency=row.quote_currency,
        occurred_at=row.occurred_at,
        asset=row.asset,
        quantity=row.quantity,
        unit_price=row.unit_price,
        amount=row.gross_amount if row.kind in (
            "CONTRIBUTION", "WITHDRAWAL", "CASH_TRANSFER_IN", "CASH_TRANSFER_OUT") or row.kind == "FEE" and row.asset is None else None,
        gross_override=gross_override,
        fee=row.fee,
        fee_asset=row.fee_asset,
        note=row.note,
    )
    return PortfolioEvent(
        id=row.id,
        account_id=row.account_id,
        request_id=row.request_id,
        recorded_at=row.recorded_at,
        input=item,
    )


def create_account(db: Session, name: str, base_currency: str = "EUR") -> RealAccount:
    clean_name = name.strip() if isinstance(name, str) else ""
    if not clean_name or len(clean_name) > 100:
        raise PortfolioError("Account name must contain 1–100 characters")
    if contains_wallet_address(clean_name):
        raise PortfolioError("Use an alias instead of a complete wallet address")
    account = RealAccount(
        id=str(uuid4()),
        name=clean_name,
        base_currency=_symbol(base_currency, "base currency"),
        created_at=datetime.now(timezone.utc),
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def list_original_events(db: Session, account_id: str, *, recorded_through: datetime | None = None) -> list[PortfolioEvent]:
    query = db.query(RealEvent).filter(RealEvent.account_id == account_id)
    if recorded_through is not None:
        query = query.filter(RealEvent.recorded_at <= recorded_through)
    rows = query.order_by(RealEvent.occurred_at, RealEvent.recorded_at, RealEvent.id).all()
    return [_from_row(row) for row in rows]


def _accounting_inputs(db: Session, account_id: str, recorded_through=None):
    connection = db.connection()
    if connection.exec_driver_sql('PRAGMA user_version').scalar_one() < 74:
        return None
    from app.portfolio.models import RealEconomicAcceptance
    if db.scalar(select(RealEconomicAcceptance.id).limit(1)) is None:
        return None
    from app.portfolio.accounting_view import capture_accounting_inputs
    driver = connection.connection.driver_connection
    owns_read = not driver.in_transaction
    try:
        return capture_accounting_inputs(db, account_id, recorded_through=recorded_through)
    finally:
        # Public reads preserve the caller's transaction boundary. A detached
        # capture owns its temporary read snapshot only when no SQL transaction
        # was active on entry. In particular, never roll back a caller's write.
        if owns_read and driver.in_transaction:
            connection.exec_driver_sql('ROLLBACK')
            db.expire_all()


def list_events(db: Session, account_id: str, *, recorded_through: datetime | None = None) -> list[PortfolioEvent]:
    captured = _accounting_inputs(db, account_id, recorded_through)
    if captured is None:
        return list_original_events(db, account_id, recorded_through=recorded_through)
    from app.portfolio.accounting_view import replay_accounting_view
    projected = replay_accounting_view(captured)
    return _annotated_events(captured, projected, account_id)


def _annotated_events(captured, projected, account_id):
    from dataclasses import replace
    from app.portfolio.domain import AccountingContext
    references = tuple(sorted(json.loads(item.payload_json)['id'] for item in captured.accepted))
    prefix_ids = {event.id for item in captured.accepted for event in item.inputs.events}
    return [replace(item.original, accounting=AccountingContext(item.original, item.accounting_input,
        item.ordinal, references, item.original.id in prefix_ids, item.order_record_id, item.transfer_basis_derived))
        for item in projected.events if item.original.account_id == account_id]


def _validate_proposed_events(db: Session, account_id: str, proposed: list[PortfolioEvent]) -> list[PortfolioEvent]:
    captured = _accounting_inputs(db, account_id)
    if captured is None:
        combined = [*list_original_events(db, account_id), *proposed]
        fold_events(account_id, combined)
        return combined
    from dataclasses import replace
    from app.portfolio.accounting_view import replay_accounting_view
    projected = replay_accounting_view(replace(captured, events=(*captured.events, *proposed)))
    return _annotated_events(captured, projected, account_id)


def verify_account_event_digests(db: Session, account_id: str) -> int:
    """Rechaza un libro con filas alteradas antes de usarlo como riesgo REAL."""
    rows = db.query(RealEvent).filter(RealEvent.account_id == account_id).all()
    for row in rows:
        try:
            if row.payload_digest != _digest(_from_row(row).input):
                raise PortfolioError("El libro REAL contiene un asiento alterado")
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise PortfolioError("El libro REAL contiene un asiento alterado") from exc
    return len(rows)


def _rows_book_revision(account_id: str, rows: list[RealEvent]) -> str:
    """Same published row fingerprint, also usable for an exact archived prefix."""
    fields = [tuple(str(getattr(row, column.key)) if getattr(row, column.key) is not None
                    else None for column in RealEvent.__table__.columns) for row in sorted(rows, key=lambda row: row.id)]
    payload = json.dumps([account_id, fields], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def original_book_revision(db: Session, account_id: str, *,
                          recorded_through: datetime | None = None) -> str:
    """Huella de filas exactas; permite revalidar el prefijo de una apertura."""
    query = select(RealEvent).where(RealEvent.account_id == account_id)
    if recorded_through is not None:
        query = query.where(RealEvent.recorded_at <= recorded_through)
    rows = db.scalars(query.order_by(RealEvent.id)).all()
    return _rows_book_revision(account_id, rows)


def account_book_revision(db: Session, account_id: str, *,
                          recorded_through: datetime | None = None) -> str:
    return account_book_identity(db, account_id, recorded_through=recorded_through)[0]


def account_book_identity(db: Session, account_id: str, *,
                          recorded_through: datetime | None = None) -> tuple[str, str | None]:
    """Bind the current rows separately from the accepted accounting method."""
    captured = _accounting_inputs(db, account_id, recorded_through)
    if captured is None:
        return original_book_revision(db, account_id, recorded_through=recorded_through), None
    from app.portfolio.wallet_funding import _encoded
    from app.portfolio.accounting_view import VIEW_ENGINE
    references = {json.loads(item.payload_json)['id']: _encoded(json.loads(item.payload_json))[1]
        for item in captured.accepted}
    method = _encoded({'schema': 'orbis.portfolio.accounting.method.v1', 'engine': VIEW_ENGINE,
        'account_id': account_id, 'acceptances': references})[1]
    return captured.revision, method


def account_snapshot(db: Session, account_id: str) -> AccountSnapshot:
    snapshot, _ = account_snapshot_with_events(db, account_id)
    return snapshot


def account_snapshot_with_events(db: Session, account_id: str) -> tuple[AccountSnapshot, list[PortfolioEvent]]:
    if db.get(RealAccount, account_id) is None:
        raise PortfolioError("Real account not found")
    events = list_events(db, account_id)
    return fold_events(account_id, events), events


def verify_paired_transfers(db: Session) -> int:
    """Comprueba ambos lados de cada traslado antes de aceptar una copia REAL."""
    rows = db.query(RealEvent).filter(
        RealEvent.kind.in_(("TRANSFER_IN", "TRANSFER_OUT", "CASH_TRANSFER_IN", "CASH_TRANSFER_OUT")),
    ).all()
    pairs: dict[str, list[RealEvent]] = {}
    for row in rows:
        pairs.setdefault(row.request_id, []).append(row)
    for request_id, pair in pairs.items():
        by_kind = {row.kind: row for row in pair}
        asset_pair = "TRANSFER_OUT" in by_kind and "TRANSFER_IN" in by_kind
        if asset_pair:
            sent = by_kind["TRANSFER_OUT"]
            received = by_kind["TRANSFER_IN"]
            quantity_valid = (_transfer_net_quantity(sent.quantity, sent.fee) == received.quantity
                              and received.fee == 0)
        elif "CASH_TRANSFER_OUT" in by_kind and "CASH_TRANSFER_IN" in by_kind:
            quantity_valid = (by_kind["CASH_TRANSFER_OUT"].quantity is None
                              and by_kind["CASH_TRANSFER_IN"].quantity is None
                              and by_kind["CASH_TRANSFER_IN"].fee == 0)
        else:
            quantity_valid = (pair[0].quantity == pair[1].quantity
                              and pair[0].fee == pair[1].fee) if len(pair) == 2 else False
        if (len(pair) != 2 or {row.kind for row in pair} not in (
                {"TRANSFER_IN", "TRANSFER_OUT"}, {"CASH_TRANSFER_IN", "CASH_TRANSFER_OUT"})
                or pair[0].account_id == pair[1].account_id
                or pair[0].asset != pair[1].asset
                or pair[0].quote_currency != pair[1].quote_currency
                or not quantity_valid
                or pair[0].gross_amount != pair[1].gross_amount
                or pair[0].occurred_at != pair[1].occurred_at
                or pair[0].note != pair[1].note
                or any(row.payload_digest != _digest(_from_row(row).input) for row in pair)):
            raise PortfolioError(f"Paired transfer {request_id} is incomplete or inconsistent")
    return len(pairs)


def append_event_in_transaction(db: Session, account_id: str, request_id: str,
                                item: EventInput) -> PortfolioEvent:
    """Parte componible: el llamador posee BEGIN IMMEDIATE, commit y rollback.

    Permite grabar el asiento y su evidencia atómicamente, sin abrir otra sesión.
    """
    rid = _uuid(request_id)
    if item.kind in ("TRANSFER_IN", "TRANSFER_OUT", "CASH_TRANSFER_IN", "CASH_TRANSFER_OUT"):
        raise PortfolioError("Use an atomic paired transfer for separate accounts")
    if db.bind is None or db.bind.dialect.name != "sqlite":
        raise PortfolioError("Real portfolio currently requires SQLite")
    if not db.connection().connection.driver_connection.in_transaction:
        raise PortfolioError("An explicit write transaction is required")
    if db.get(RealAccount,account_id) is None:
        raise PortfolioError("Real account not found")
    digest = _digest(item)
    existing = db.query(RealEvent).filter_by(account_id=account_id,request_id=rid).first()
    if existing is not None:
        if existing.payload_digest != digest:
            raise RequestConflict("request_id already belongs to another real event")
        return _from_row(existing)
    recorded_at = datetime.now(timezone.utc)
    event = PortfolioEvent(str(uuid4()),account_id,rid,recorded_at,item)
    _validate_proposed_events(db, account_id, [event])
    db.add(RealEvent(id=event.id,account_id=account_id,request_id=rid,payload_digest=digest,
        kind=item.kind,asset=item.asset,quote_currency=item.quote_currency,quantity=item.quantity,
        unit_price=item.unit_price,gross_amount=item.gross_amount,fee=item.fee,fee_asset=item.fee_asset,
        occurred_at=item.occurred_at,recorded_at=recorded_at,note=item.note))
    db.flush()
    return event


def append_event(
    db: Session,
    account_id: str,
    request_id: str,
    item: EventInput,
) -> PortfolioEvent:
    """Añade un hecho o devuelve su reintento; valida todo el libro al final.

    `BEGIN IMMEDIATE` serializa escrituras SQLite para que dos ventas o dos
    compras retroactivas no aprueben el mismo inventario simultáneamente.
    El motor debe ser el almacén REAL, nunca la sesión PAPER de la API.
    """
    rid = _uuid(request_id)
    if item.kind in ("TRANSFER_IN", "TRANSFER_OUT", "CASH_TRANSFER_IN", "CASH_TRANSFER_OUT"):
        raise PortfolioError("Use an atomic paired transfer for separate accounts")
    if db.bind is None or db.bind.dialect.name != "sqlite":
        raise PortfolioError("Real portfolio currently requires SQLite")

    try:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        event = append_event_in_transaction(db,account_id,rid,item)
        db.commit()
        return event
    except Exception:
        db.rollback()
        raise


def transfer_asset_between_accounts(
    db: Session, source_account_id: str, target_account_id: str,
    request_id: str, *, asset: str, quote_currency: str, quantity: Decimal,
    occurred_at: datetime, note: str | None = None,
    dry_run: bool = False, expected_cost_basis: Decimal | None = None,
    fee: Decimal = Decimal(0),
) -> dict:
    """Registra ambos lados de un traslado manual, sin llamar wallet ni exchange.

    La base de coste se asigna con el mismo promedio y orden temporal que el
    libro fuente. Ambos hechos comparten request_id y se confirman juntos.
    """
    rid = _uuid(request_id)
    if source_account_id == target_account_id:
        raise PortfolioError("Source and target accounts must differ")
    if db.bind is None or db.bind.dialect.name != "sqlite":
        raise PortfolioError("Real portfolio currently requires SQLite")
    source_template = EventInput("TRANSFER_OUT", quote_currency, occurred_at,
                                 asset=asset, quantity=quantity, gross_override=Decimal(0),
                                 fee=fee, fee_asset=asset if fee else None, note=note)
    net_quantity = _transfer_net_quantity(quantity, fee)
    target_template = EventInput("TRANSFER_IN", quote_currency, occurred_at,
                                 asset=asset, quantity=net_quantity, gross_override=Decimal(0), note=note)
    try:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        if db.get(RealAccount, source_account_id) is None or db.get(RealAccount, target_account_id) is None:
            raise PortfolioError("Real account not found")
        existing = db.query(RealEvent).filter(
            RealEvent.request_id == rid,
            RealEvent.kind.in_(("TRANSFER_OUT", "TRANSFER_IN")),
        ).all()
        if existing:
            if (len(existing) != 2
                    or {(row.account_id, row.kind) for row in existing} != {
                        (source_account_id, "TRANSFER_OUT"), (target_account_id, "TRANSFER_IN")}
                    or existing[0].gross_amount != existing[1].gross_amount):
                raise RequestConflict("Paired transfer request_id is inconsistent")
            basis = existing[0].gross_amount
            if expected_cost_basis is not None and basis != expected_cost_basis:
                raise RequestConflict("Transferred cost changed since preview")
            source_item = EventInput("TRANSFER_OUT", source_template.quote_currency,
                                     source_template.occurred_at, asset=source_template.asset,
                                     quantity=quantity, gross_override=basis, fee=fee,
                                     fee_asset=source_template.fee_asset, note=note)
            target_item = EventInput("TRANSFER_IN", target_template.quote_currency,
                                     target_template.occurred_at, asset=target_template.asset,
                                     quantity=net_quantity, gross_override=basis, note=note)
            by_kind = {row.kind: row for row in existing}
            if (by_kind["TRANSFER_OUT"].payload_digest != _digest(source_item)
                    or by_kind["TRANSFER_IN"].payload_digest != _digest(target_item)):
                raise RequestConflict("request_id already belongs to another real transfer")
            result = {"status": "ALREADY_RECORDED", "request_id": rid,
                      "source_event_id": by_kind["TRANSFER_OUT"].id,
                      "target_event_id": by_kind["TRANSFER_IN"].id,
                      "cost_basis": format(basis, "f"), "received_quantity": format(net_quantity, "f")}
            if dry_run:
                db.rollback()
            else:
                db.commit()
            return result
        if db.query(RealEvent).filter(RealEvent.request_id == rid,
                                      RealEvent.account_id.in_((source_account_id, target_account_id))).first():
            raise RequestConflict("request_id already belongs to another real event")

        recorded_at = datetime.now(timezone.utc)
        source_events = list_events(db, source_account_id)
        source_id, target_id = str(uuid4()), str(uuid4())
        source_key = PortfolioEvent(source_id, source_account_id, rid, recorded_at, source_template).sort_key
        preceding = [event for event in source_events
                     if event.sort_key < source_key]
        before = fold_events(source_account_id, preceding)
        position = before.positions.get((source_template.asset, source_template.quote_currency))
        if position is None or quantity > position.quantity:
            raise PortfolioError("Movement quantity exceeds available real position")
        with localcontext() as context:
            context.prec = CALC_PRECISION
            basis = (position.cost_basis if quantity == position.quantity
                     else position.average_cost * quantity)
        if expected_cost_basis is not None and basis != expected_cost_basis:
            raise RequestConflict("Transferred cost changed since preview")
        source_item = EventInput("TRANSFER_OUT", source_template.quote_currency,
                                 source_template.occurred_at, asset=source_template.asset,
                                 quantity=quantity, gross_override=basis, fee=fee,
                                 fee_asset=source_template.fee_asset, note=note)
        target_item = EventInput("TRANSFER_IN", target_template.quote_currency,
                                 target_template.occurred_at, asset=target_template.asset,
                                 quantity=net_quantity, gross_override=basis, note=note)
        source_event = PortfolioEvent(source_id, source_account_id, rid, recorded_at, source_item)
        target_event = PortfolioEvent(target_id, target_account_id, rid, recorded_at, target_item)
        fold_events(source_account_id, [*source_events, source_event])
        fold_events(target_account_id, [*list_events(db, target_account_id), target_event])
        if dry_run:
            db.rollback()
            return {"status": "PREVIEW", "request_id": rid,
                    "cost_basis": format(basis, "f"), "received_quantity": format(net_quantity, "f"),
                    "available_at_time": format(position.quantity, "f"),
                    "remaining_at_time": format(position.quantity - quantity, "f")}
        for event in (source_event, target_event):
            item = event.input
            db.add(RealEvent(
                id=event.id, account_id=event.account_id, request_id=rid,
                payload_digest=_digest(item), kind=item.kind, asset=item.asset,
                quote_currency=item.quote_currency, quantity=item.quantity,
                unit_price=None, gross_amount=item.gross_amount, fee=item.fee,
                fee_asset=item.fee_asset, occurred_at=item.occurred_at, recorded_at=recorded_at,
                note=item.note,
            ))
        db.flush()
        account_snapshot(db, source_account_id)
        account_snapshot(db, target_account_id)
        db.commit()
        return {"status": "RECORDED", "request_id": rid,
                "source_event_id": source_id, "target_event_id": target_id,
                "cost_basis": format(basis, "f"), "received_quantity": format(net_quantity, "f")}
    except Exception:
        db.rollback()
        raise


def transfer_cash_in_transaction(
    db: Session, source_account_id: str, target_account_id: str,
    request_id: str, *, currency: str, amount: Decimal,
    occurred_at: datetime, note: str | None = None,
    dry_run: bool = False, expected_source_balance: Decimal | None = None,
    fee: Decimal = Decimal(0),
) -> dict:
    """Parte componible: el llamador posee BEGIN IMMEDIATE, commit/rollback."""
    rid = _uuid(request_id)
    if source_account_id == target_account_id:
        raise PortfolioError("Source and target accounts must differ")
    if db.bind is None or db.bind.dialect.name != "sqlite":
        raise PortfolioError("Real portfolio currently requires SQLite")
    if not db.connection().connection.driver_connection.in_transaction:
        raise PortfolioError("An explicit write transaction is required")
    outgoing = EventInput("CASH_TRANSFER_OUT", currency, occurred_at, amount=amount,fee=fee,note=note)
    incoming = EventInput("CASH_TRANSFER_IN", currency, occurred_at, amount=amount, note=note)
    if expected_source_balance is not None:
        decimal_value(expected_source_balance)
    try:
        if db.get(RealAccount, source_account_id) is None or db.get(RealAccount, target_account_id) is None:
            raise PortfolioError("Real account not found")
        existing = db.query(RealEvent).filter(
            RealEvent.request_id == rid,
            RealEvent.kind.in_(("CASH_TRANSFER_OUT", "CASH_TRANSFER_IN")),
        ).all()
        if existing:
            by_kind = {row.kind: row for row in existing}
            if (len(existing) != 2 or len(by_kind) != 2
                    or by_kind["CASH_TRANSFER_OUT"].account_id != source_account_id
                    or by_kind["CASH_TRANSFER_IN"].account_id != target_account_id
                    or by_kind["CASH_TRANSFER_OUT"].payload_digest != _digest(outgoing)
                    or by_kind["CASH_TRANSFER_IN"].payload_digest != _digest(incoming)):
                raise RequestConflict("request_id already belongs to another real transfer")
            result = {"status": "ALREADY_RECORDED", "request_id": rid,
                      "source_event_id": by_kind["CASH_TRANSFER_OUT"].id,
                      "target_event_id": by_kind["CASH_TRANSFER_IN"].id}
            return result
        if db.query(RealEvent).filter(RealEvent.request_id == rid,
                                      RealEvent.account_id.in_((source_account_id, target_account_id))).first():
            raise RequestConflict("request_id already belongs to another real event")
        recorded_at = datetime.now(timezone.utc)
        source_events = list_events(db, source_account_id)
        source_id, target_id = str(uuid4()), str(uuid4())
        source_key = PortfolioEvent(source_id, source_account_id, rid, recorded_at, outgoing).sort_key
        preceding = [event for event in source_events
                     if event.sort_key < source_key]
        available = fold_events(source_account_id, preceding).cash.get(outgoing.quote_currency, Decimal(0))
        with localcontext() as context:
            context.prec = CALC_PRECISION
            remaining = available-amount-fee
        if remaining < 0:
            raise PortfolioError("Cash transfer exceeds available balance at that time")
        if expected_source_balance is not None and available != expected_source_balance:
            raise RequestConflict("Source cash balance changed since preview")
        source_event = PortfolioEvent(source_id, source_account_id, rid, recorded_at, outgoing)
        target_event = PortfolioEvent(target_id, target_account_id, rid, recorded_at, incoming)
        fold_events(source_account_id, [*source_events, source_event])
        fold_events(target_account_id, [*list_events(db, target_account_id), target_event])
        if dry_run:
            return {"status": "PREVIEW", "request_id": rid,
                    "available_at_time": format(available, "f"),
                    "remaining_at_time": format(remaining, "f")}
        for event in (source_event, target_event):
            item = event.input
            db.add(RealEvent(
                id=event.id, account_id=event.account_id, request_id=rid,
                payload_digest=_digest(item), kind=item.kind, asset=None,
                quote_currency=item.quote_currency, quantity=None, unit_price=None,
                gross_amount=item.amount, fee=item.fee, fee_asset=None,
                occurred_at=item.occurred_at, recorded_at=recorded_at, note=item.note,
            ))
        db.flush()
        account_snapshot(db, source_account_id)
        account_snapshot(db, target_account_id)
        return {"status": "RECORDED", "request_id": rid,
                "source_event_id": source_id, "target_event_id": target_id}
    except Exception:
        raise


def transfer_cash_between_accounts(
    db: Session,source_account_id: str,target_account_id: str,request_id: str,*,currency: str,
    amount: Decimal,occurred_at: datetime,note: str | None = None,dry_run: bool = False,
    expected_source_balance: Decimal | None = None,fee: Decimal = Decimal(0),
) -> dict:
    """Asiento pareado atómico. La comisión sale sólo de la cuenta origen."""
    try:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        result = transfer_cash_in_transaction(db,source_account_id,target_account_id,request_id,currency=currency,
            amount=amount,occurred_at=occurred_at,note=note,dry_run=dry_run,
            expected_source_balance=expected_source_balance,fee=fee)
        if dry_run:
            db.rollback()
        else:
            db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def import_events(
    db: Session,
    account_id: str,
    entries: list[tuple[str, EventInput]],
    *,
    dry_run: bool,
    opening_account_ref: str | None = None,
    opening_uid: str | None = None,
) -> dict[str, int]:
    """Valida el libro entero y aplica un lote en una sola transacción.

    Un `request_id` existente con el mismo contenido se omite. Uno distinto
    aborta el lote completo. La vista previa toma el mismo bloqueo y reglas,
    pero siempre hace rollback; el commit vuelve a validarlo todo.
    """
    if db.bind is None or db.bind.dialect.name != "sqlite":
        raise PortfolioError("Real portfolio currently requires SQLite")
    if not entries:
        raise PortfolioError("CSV REAL sin movimientos")
    if any(item.kind in ("TRANSFER_IN", "TRANSFER_OUT", "CASH_TRANSFER_IN", "CASH_TRANSFER_OUT") for _, item in entries):
        raise PortfolioError("CSV cannot import unpaired account transfers")
    if (opening_account_ref is None) != (opening_uid is None):
        raise PortfolioError("Identidad MEXC incompleta para inventario inicial")
    try:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        if db.get(RealAccount, account_id) is None:
            raise PortfolioError("Real account not found")
        existing_rows = db.query(RealEvent).filter(RealEvent.account_id == account_id).all()
        if opening_account_ref is not None:
            link = db.get(RealMexcAccountLink, opening_account_ref)
            if link is not None and link.account_id != account_id:
                raise RequestConflict("La referencia MEXC ya pertenece a otra cuenta REAL")
            if db.query(RealLiveSpotIntent).filter(
                    RealLiveSpotIntent.account_ref == opening_account_ref).first() is not None:
                raise RequestConflict("Hay intenciones REAL MEXC; no se puede abrir inventario")
            proposed_ids = {_uuid(request_id) for request_id, _ in entries}
            old_ids = {row.request_id for row in existing_rows}
            if (len(proposed_ids) != len(entries)
                    or old_ids and (old_ids != proposed_ids or len(existing_rows) != len(entries))):
                raise RequestConflict("El inventario inicial requiere una cuenta vacía")
            try:
                claim_mexc_uid_in_transaction(db, opening_account_ref, opening_uid)
            except MexcUidGuardError as exc:
                raise RequestConflict(str(exc)) from None
        known = {row.request_id: row.payload_digest for row in existing_rows}
        pending: list[PortfolioEvent] = []
        pending_rows: list[RealEvent] = []
        skipped = 0
        recorded_at = datetime.now(timezone.utc)
        for index, (request_id, item) in enumerate(entries):
            rid = _uuid(request_id)
            digest = _digest(item)
            old_digest = known.get(rid)
            if old_digest is not None:
                if old_digest != digest:
                    raise RequestConflict("request_id CSV ya existe con otro movimiento")
                skipped += 1
                continue
            known[rid] = digest
            # Mantiene el orden del CSV en empates de `occurred_at`.
            imported_at = recorded_at + timedelta(microseconds=index)
            event = PortfolioEvent(
                id=str(uuid4()), account_id=account_id, request_id=rid,
                recorded_at=imported_at, input=item,
            )
            pending.append(event)
            pending_rows.append(RealEvent(
                id=event.id, account_id=account_id, request_id=rid,
                payload_digest=digest, kind=item.kind, asset=item.asset,
                quote_currency=item.quote_currency, quantity=item.quantity,
                unit_price=item.unit_price, gross_amount=item.gross_amount,
                fee=item.fee, fee_asset=item.fee_asset, occurred_at=item.occurred_at,
                recorded_at=imported_at, note=item.note,
            ))
        _validate_proposed_events(db, account_id, pending)
        result = {"total": len(entries), "new": len(pending), "already_present": skipped}
        if dry_run:
            db.rollback()
        else:
            db.add_all(pending_rows)
            if opening_account_ref is not None and link is None:
                db.add(RealMexcAccountLink(account_ref=opening_account_ref,
                                           account_id=account_id, created_at=recorded_at))
            db.commit()
        return result
    except Exception:
        db.rollback()
        raise
