"""Importación contable explícita de recibos DEX observados, nunca ejecución.

La cartera de destino procede del vínculo EOA, no del contexto donde se abrió
la operación. Gas nativo y contravalor se conservan en unidades exactas; no se
crea capital, tipo de cambio ni precio de mercado del BNB gastado.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, ROUND_FLOOR, localcontext
from uuid import UUID, uuid5
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.portfolio.domain import CALC_PRECISION, EventInput, PortfolioEvent, PortfolioError, fold_events
from app.portfolio.models import RealEvent, RealAccount, RealMexcAccountLink, RealWalletSwapBooking, RealWalletSwapBookingEvent
from app.portfolio.repository import _digest, _from_row, account_book_revision, append_event_in_transaction, list_events, verify_account_event_digests
from app.portfolio.wallet_funding import _encoded, _decoded, _time
from app.portfolio.wallet_funding_credit import _wallet_owner
from app.portfolio.wallet_proof import canonical_wallet_address
from app.portfolio.wallet_swap import WalletSwapError, _identity, swap_record


CONTRACT = "WALLET_SWAP_BOOKING_V1"


def _amount(units: str | int) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = CALC_PRECISION
        return Decimal(units)/Decimal(10**18)


def _wei(amount: Decimal) -> int:
    with localcontext() as ctx:
        ctx.prec = CALC_PRECISION
        return int((amount * 10**18).to_integral_value(rounding=ROUND_FLOOR))


def _trade(terms: dict, receipt: dict, at: datetime) -> EventInput | None:
    if terms["action"] != "SWAP" or receipt["status"] != "FINALIZED":
        return None
    buying = terms["token_in"] == "USDT"
    quantity = _amount(receipt["amount_out_units"] if buying else terms["amount_units"])
    gross = _amount(terms["amount_units"] if buying else receipt["amount_out_units"])
    with localcontext() as ctx:
        ctx.prec = 40
        indicative = gross/quantity
    with localcontext() as ctx:
        ctx.prec = CALC_PRECISION
        override = gross if indicative*quantity != gross else None
    return EventInput("BUY" if buying else "SELL", "USDT", at, asset="WBNB", quantity=quantity,
        unit_price=indicative, gross_override=override, note="Swap BNB · ORBIS DEX " + terms["id"][:8])


def _item(item: EventInput) -> dict:
    return {"kind": item.kind, "asset": item.asset, "quote_currency": item.quote_currency,
            "quantity": str(item.quantity) if item.quantity is not None else None,
            "unit_price": str(item.unit_price) if item.unit_price is not None else None,
            "gross_amount": str(item.gross_amount), "amount": str(item.amount) if item.amount is not None else None,
            "occurred_at": item.occurred_at.isoformat(), "note": item.note}


def _context(db: Session, identifier: str, context_account_id: str, wallet_address: str):
    row, terms, state = swap_record(db, identifier)
    wallet = canonical_wallet_address(wallet_address)
    if row.account_id != context_account_id or _identity(db, "wallet", wallet) != row.wallet_digest:
        raise WalletSwapError("La operación no pertenece a este contexto o wallet")
    if row.status not in {"FINALIZED", "REVERTED"}:
        raise WalletSwapError("Comprueba primero el recibo finalizado de esta acción")
    return row, terms, state


def _source(db: Session, identifier: str, context_account_id: str, wallet_address: str):
    row, terms, state = _context(db, identifier, context_account_id, wallet_address)
    wallet = canonical_wallet_address(wallet_address)
    owner = _wallet_owner(db, wallet)
    if owner is None or db.scalar(select(RealMexcAccountLink.account_ref).where(RealMexcAccountLink.account_id == owner.id)) is not None:
        raise WalletSwapError("Vincula la wallet a una cuenta REAL propia, separada del exchange")
    return row, terms, state, owner


def _gas_items(book, gas_wei: int, at: datetime, action_id: str, base_currency: str) -> list[EventInput]:
    remaining, result = gas_wei, []
    cash = min(remaining, max(0, _wei(book.cash.get("BNB", Decimal(0)))))
    note = "Comisión de red BNB · ORBIS DEX " + action_id[:8]
    if cash:
        result.append(EventInput("FEE", "BNB", at, amount=_amount(cash), note=note)); remaining -= cash
    positions = sorted((position for position in book.positions.values() if position.asset == "BNB"),
        key=lambda position: (position.quote_currency != base_currency, position.quote_currency))
    for position in positions:
        take = min(remaining, max(0, _wei(position.quantity)))
        if take:
            result.append(EventInput("FEE", position.quote_currency, at, asset="BNB", quantity=_amount(take), note=note))
            remaining -= take
    if remaining:
        raise WalletSwapError("Falta BNB registrado en esta wallet para contabilizar el gas; revisa su historial")
    return result


def _gas_units(item: EventInput) -> int:
    value = item.amount if item.kind == "FEE" and item.asset is None and item.quote_currency == "BNB" else (
        item.quantity if item.kind == "FEE" and item.asset == "BNB" else None)
    if value is None or _amount(_wei(value)) != value:
        raise WalletSwapError("El asiento no representa una comisión exacta de BNB")
    return _wei(value)


def _matches_trade(actual: EventInput, expected: EventInput | None) -> bool:
    return (expected is not None and actual.kind == expected.kind and actual.asset == expected.asset
            and actual.quote_currency == expected.quote_currency and actual.quantity == expected.quantity
            and actual.gross_amount == expected.gross_amount and actual.fee == 0 and actual.fee_asset is None
            and abs(actual.occurred_at-expected.occurred_at) <= timedelta(minutes=2))


def booking_record(db: Session, identifier: str):
    row = db.get(RealWalletSwapBooking, str(UUID(identifier)))
    if row is None:
        return None
    action, terms, state = swap_record(db, row.action_id)
    payload = _decoded(row.payload_json, row.payload_digest)
    try:
        receipt = state["receipt"]
        if (set(payload) != {"contract", "action_id", "context_account_id", "account_id", "wallet_digest",
                "plan_digest", "receipt_digest", "revision_before", "booked_at", "events"}
                or payload["contract"] != CONTRACT or payload["action_id"] != row.action_id
                or payload["context_account_id"] != action.account_id or payload["account_id"] != row.account_id
                or payload["wallet_digest"] != action.wallet_digest or payload["plan_digest"] != action.payload_digest
                or payload["receipt_digest"] != _encoded(receipt)[1] or action.status not in {"FINALIZED", "REVERTED"}
                or not re.fullmatch(r"[0-9a-f]{64}", payload["revision_before"])
                or db.get(RealAccount, row.account_id) is None or _time(datetime.fromisoformat(payload["booked_at"])) < action.updated_at):
            raise ValueError
        refs = payload["events"]
        children = db.scalars(select(RealWalletSwapBookingEvent).where(RealWalletSwapBookingEvent.action_id == row.action_id)).all()
        if not isinstance(refs, list) or not 1 <= len(refs) <= 100 or len(children) != len(refs) or len({item["id"] for item in refs}) != len(refs):
            raise ValueError
        from app.portfolio.dex_receipt_booking import claimed_event_ids
        claimed=claimed_event_ids(db)
        if any(child.event_id in claimed for child in children):raise ValueError
        linked = {child.event_id: child.role for child in children}
        trades, gas = [], 0
        at = _time(datetime.fromisoformat(receipt["block_at"]))
        expected_trade = _trade(terms, receipt, at)
        for ref in refs:
            event = db.get(RealEvent, ref["id"])
            if (set(ref) != {"id", "digest", "role", "existing"} or type(ref["existing"]) is not bool
                    or event is None or event.account_id != row.account_id
                    or event.payload_digest != ref["digest"] or ref["digest"] != _digest(_from_row(event).input)
                    or linked.get(event.id) != ref["role"]):
                raise ValueError
            item = _from_row(event).input
            if (not ref["existing"] and item.occurred_at != at
                    or ref["existing"] and abs(item.occurred_at-at) > timedelta(minutes=2)):
                raise ValueError
            if ref["role"] == "TRADE":
                if not _matches_trade(item, expected_trade) or not ref["existing"] and item != expected_trade:
                    raise ValueError
                trades.append(item)
            elif ref["role"] == "GAS":
                gas += _gas_units(item)
            else:
                raise ValueError
        if gas != int(receipt["gas_fee_wei"]) or len(trades) != (0 if expected_trade is None else 1):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise WalletSwapError("El registro contable DEX cambió o no coincide con su recibo") from None
    return row, payload


def _status(db: Session, identifier: str) -> dict:
    stored = booking_record(db, identifier)
    if stored is None:
        return {"booked": False}
    row, payload = stored
    return {"booked": True, "target_account_id": row.account_id, "events_created": 0,
            "target_account_name": db.get(RealAccount, row.account_id).name,
            "events_count": len(payload["events"]), "booked_at": payload["booked_at"]}


def _proposal(db: Session, row, terms: dict, state: dict, owner, *, trade_event_id: str | None,
              gas_event_ids: list[str] | None, contract: str = CONTRACT, extras: dict | None = None,
              wallet_address: str | None = None):
    if wallet_address is not None and db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()>=71:
        from app.portfolio.dex_call_booking import require_wallet_trade_order
        require_wallet_trade_order(db,row,terms,state,wallet_address)
    verify_account_event_digests(db, owner.id)
    at = _time(datetime.fromisoformat(state["receipt"]["block_at"]))
    events = list_events(db, owner.id)
    before = fold_events(owner.id, [event for event in events if event.occurred_at <= at])
    trade = _trade(terms, state["receipt"], at)
    if contract == CONTRACT:
        from app.portfolio.accounting_view import require_receipt_extension
        require_receipt_extension(db, owner.id, 'WALLET_SWAP', row.id, gas_only=trade is None)
    gas_wei = int(state["receipt"]["gas_fee_wei"])
    used = set(db.scalars(select(RealWalletSwapBookingEvent.event_id)))
    from app.portfolio.models import RealMexcFillBooking
    used.update(db.scalars(select(RealMexcFillBooking.event_id)))
    from app.portfolio.dex_receipt_booking import claimed_event_ids
    used.update(claimed_event_ids(db))
    trade_choices, gas_choices = {}, {}
    for event in events:
        if event.id in used or abs(event.occurred_at-at) > timedelta(minutes=2):
            continue
        if _matches_trade(event.input, trade):
            trade_choices[event.id] = event
        try:
            native = _gas_units(event.input)
        except WalletSwapError:
            continue
        if 0 < native <= gas_wei:
            gas_choices[event.id] = event
    selected_trade = str(UUID(trade_event_id)) if trade_event_id else None
    selected_gas = [str(UUID(value)) for value in gas_event_ids or []]
    if (selected_trade is not None and selected_trade not in trade_choices or len(selected_gas) > 100
            or len(selected_gas) != len(set(selected_gas)) or any(value not in gas_choices for value in selected_gas)):
        raise WalletSwapError("Un asiento seleccionado cambió, ya está vinculado o no coincide con el recibo")
    selected_gas.sort()
    linked = ([("TRADE", trade_choices[selected_trade])] if selected_trade else []) + [("GAS", gas_choices[value]) for value in selected_gas]
    remaining_gas = gas_wei - sum(_gas_units(event.input) for role, event in linked if role == "GAS")
    if remaining_gas < 0:
        raise WalletSwapError("Las comisiones seleccionadas superan el gas de este recibo")
    needs_selection = bool(trade_choices and selected_trade is None or gas_choices and not selected_gas)
    new_items = []
    if not needs_selection:
        if trade is not None and not selected_trade:
            if trade.kind == "BUY" and before.cash.get("USDT", Decimal(0)) < trade.gross_amount:
                raise WalletSwapError("Falta USDT registrado antes del swap; no se inventa una aportación")
            new_items.append(("TRADE", trade))
        new_items += [("GAS", item) for item in _gas_items(before, remaining_gas, at, row.id, owner.base_currency)]
        stamp = max((event.recorded_at for event in events), default=row.updated_at)+timedelta(microseconds=1)
        simulated = [PortfolioEvent(str(index), owner.id, str(index), stamp+timedelta(microseconds=index), item)
                     for index, (_role, item) in enumerate(new_items)]
        fold_events(owner.id, [*events, *simulated])
        if len(new_items)+len(linked) > 100:
            raise WalletSwapError("Demasiados asientos de gas para un único recibo; revisión necesaria")
    result = {"contract": contract, "action_id": row.id, "context_account_id": row.account_id,
              "target_account_id": owner.id, "target_account_name": owner.name, "revision": account_book_revision(db, owner.id),
              "booked": False, "available": not needs_selection, "reason": "Revisa los asientos compatibles y enlaza los ya registrados" if needs_selection else None,
              "trade": _item(trade) if trade else None, "gas_amount": str(_amount(gas_wei)),
              "gas_items": [_item(item) for role, item in new_items if role == "GAS"],
              "trade_event_id": selected_trade, "gas_event_ids": selected_gas,
              "trade_candidates": [{"id": event.id, **_item(event.input)} for event in trade_choices.values()],
              "gas_candidates": [{"id": event.id, "native_amount": str(_amount(_gas_units(event.input))), **_item(event.input)} for event in gas_choices.values()]}
    result.update(extras or {})
    result["preview_digest"] = _encoded(result)[1]
    return result, new_items, linked


def preview_booking(db: Session, identifier: str, *, context_account_id: str, wallet_address: str,
                    trade_event_id: str | None = None, gas_event_ids: list[str] | None = None) -> dict:
    _context(db, identifier, context_account_id, wallet_address)
    stored = _status(db, identifier)
    if stored["booked"]:
        return stored
    row, terms, state, owner = _source(db, identifier, context_account_id, wallet_address)
    return _proposal(db, row, terms, state, owner, trade_event_id=trade_event_id, gas_event_ids=gas_event_ids,wallet_address=wallet_address)[0]


def book_swap(db: Session, identifier: str, *, context_account_id: str, wallet_address: str,
              expected_revision: str, preview_digest: str, now: datetime, trade_event_id: str | None = None,
              gas_event_ids: list[str] | None = None) -> dict:
    try:
        now = _time(now)
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        _context(db, identifier, context_account_id, wallet_address)
        existing = _status(db, identifier)
        if existing["booked"]:
            return existing
        row, terms, state, owner = _source(db, identifier, context_account_id, wallet_address)
        preview, items, linked = _proposal(db, row, terms, state, owner, trade_event_id=trade_event_id, gas_event_ids=gas_event_ids,wallet_address=wallet_address)
        if preview["revision"] != expected_revision or preview["preview_digest"] != preview_digest:
            raise WalletSwapError("La cartera cambió desde la revisión; abre otra vista previa")
        if not preview["available"]:
            raise WalletSwapError("Selecciona primero los asientos compatibles para no duplicarlos")
        if now < row.updated_at:
            raise WalletSwapError("El reloj de registro retrocedió")
        refs = [{"id": event.id, "digest": _digest(event.input), "role": role, "existing": True} for role, event in linked]
        for index, (role, item) in enumerate(items):
            event = append_event_in_transaction(db, owner.id, str(uuid5(UUID(row.id), f"{CONTRACT}:{index}")), item)
            refs.append({"id": event.id, "digest": _digest(item), "role": role, "existing": False})
        payload = {"contract": CONTRACT, "action_id": row.id, "context_account_id": context_account_id,
                   "account_id": owner.id, "wallet_digest": row.wallet_digest, "plan_digest": row.payload_digest,
                   "receipt_digest": _encoded(state["receipt"])[1], "revision_before": expected_revision,
                   "booked_at": now.isoformat(), "events": refs}
        raw, digest = _encoded(payload)
        db.add(RealWalletSwapBooking(action_id=row.id, account_id=owner.id, payload_json=raw, payload_digest=digest)); db.flush()
        for ref in refs:
            db.add(RealWalletSwapBookingEvent(event_id=ref["id"], action_id=row.id, role=ref["role"]))
        db.flush()
        from app.portfolio.repository import account_snapshot
        account_snapshot(db, owner.id)
        result = {**_status(db, identifier), "events_created": len(items)}
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
