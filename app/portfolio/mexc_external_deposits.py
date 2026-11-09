"""Book final USDT credits that arrived outside ORBIS, using MEXC View."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy.orm import Session

from app.portfolio.domain import EventInput, PortfolioError
from app.portfolio.mexc_transfer_matches import (
    EXTERNAL_DEPOSIT_NOTE, _digest, _source_row, external_deposit_request_id,
    verify_mexc_transfer_match,
)
from app.portfolio.mexc_transfers import read_transfer_snapshot
from app.portfolio.models import (
    RealEvent, RealMexcAccountLink, RealMexcTransferMatch, RealWalletFundingBooking, RealWalletFundingCredit,
)
from app.portfolio.repository import (
    account_book_revision, account_snapshot, append_event_in_transaction, list_events, verify_account_event_digests,
)


def external_deposit_selection(db: Session, account_id: str, snapshot_id: str,
                               source_refs: list[str], *, now: datetime) -> tuple[str, list[dict], list[str]]:
    """Resolve only verified credits, or recover the same already booked selection."""
    snapshot = read_transfer_snapshot(db, snapshot_id)
    account_ref = snapshot["account_ref"]
    link = db.get(RealMexcAccountLink, account_ref)
    if not snapshot["identity_verified"] or link is None or link.account_id != account_id:
        raise PortfolioError("Selecciona una captura MEXC View verificada de esta cuenta")
    if not 1 <= len(source_refs) <= 20 or len(set(source_refs)) != len(source_refs):
        raise PortfolioError("Selecciona de uno a veinte ingresos distintos")
    sources, booked = [], []
    for ref in sorted(source_refs):
        row = _source_row(db, account_ref, snapshot_id, "deposit", ref, check_history=True)
        occurred_at = datetime.fromtimestamp(row["time_ms"] / 1000, timezone.utc)
        if (row["asset"] != "USDT" or not row["network"] or occurred_at > now
                or occurred_at > datetime.fromisoformat(snapshot["observed_at"])):
            raise PortfolioError("Se requiere un ingreso USDT con red y fecha verificadas")
        match = db.query(RealMexcTransferMatch).filter_by(
            account_ref=account_ref, direction="deposit", source_ref=ref).one_or_none()
        if match is not None:
            verify_mexc_transfer_match(db, match, check_history=True)
            booked.append(match.event_id)
        sources.append(row)
    if booked and len(booked) != len(sources):
        raise PortfolioError("Retira de la selección los ingresos ya registrados")
    return account_ref, sources, booked


def external_deposit_review(db: Session, account_id: str, snapshot_id: str, source_refs: list[str],
                            parity: dict, *, now: datetime, expected_hash: str | None = None,
                            current_snapshot_id: str | None = None) -> dict:
    """Validate the exact balance difference and atomically write its evidence and entries."""
    account_ref, sources, booked = external_deposit_selection(db, account_id, snapshot_id, source_refs, now=now)
    if booked:
        return {"status": "ALREADY_RECORDED", "event_ids": booked}
    if current_snapshot_id is not None:
        current_ref, _, _ = external_deposit_selection(db, account_id, current_snapshot_id, source_refs, now=now)
        observed = datetime.fromisoformat(read_transfer_snapshot(db, current_snapshot_id)["observed_at"])
        if current_ref != account_ref or not timedelta(0) <= now - observed <= timedelta(seconds=120):
            raise PortfolioError("La comprobación actual del ingreso MEXC caducó o cambió de cuenta")
    if (parity["account_id"] != account_id or set(parity["blockers"]) != {"QUANTITY_DIFFERENCE"}
            or parity["book_revision"] != account_book_revision(db, account_id)
            or not timedelta(0) <= now - datetime.fromisoformat(parity["checked_at"]) <= timedelta(seconds=30)):
        raise PortfolioError("El saldo cambió o ya está registrado. Vuelve a comparar MEXC y la cartera")
    differences = [row for row in parity["assets"] if not row["quantity_equal"]]
    if len(differences) != 1 or differences[0]["asset"] != "USDT":
        raise PortfolioError("Revisa primero las diferencias en otros activos")
    with localcontext() as context:
        context.prec = 300
        amount = sum((Decimal(row["amount"]) for row in sources), Decimal(0))
    balance = differences[0]
    if amount != Decimal(balance["difference_mexc_minus_book"]):
        raise PortfolioError("Los ingresos elegidos deben cubrir exactamente la diferencia en USDT")

    verify_account_event_digests(db, account_id)
    if account_snapshot(db, account_id).cash.get("USDT", Decimal(0)) != Decimal(balance["book_quantity"]):
        raise PortfolioError("El USDT de esta cuenta debe estar registrado como efectivo")
    events = list_events(db, account_id)
    linked_ids = {event_id for event_id, in db.query(RealMexcTransferMatch.event_id).filter_by(account_id=account_id)}
    linked_ids.update(event_id for event_id, in db.query(RealWalletFundingBooking.event_id))
    usdt_cash = [event for event in events if event.input.kind in ("CONTRIBUTION", "CASH_TRANSFER_IN")
                 and event.input.quote_currency == "USDT"]
    opening = max((event.occurred_at for event in usdt_cash if event.request_id == str(uuid5(
        NAMESPACE_URL, f"orbis:mexc-opening:{account_id}:{event.occurred_at.isoformat()}:USDT"))), default=None)
    lines = []
    for row in sources:
        at = datetime.fromtimestamp(row["time_ms"] / 1000, timezone.utc)
        if opening is not None and at <= opening:
            raise PortfolioError("Un ingreso es anterior al saldo inicial MEXC. Revisa el historial")
        if db.query(RealWalletFundingCredit).filter_by(account_ref=account_ref, source_ref=row["source_ref"]).first():
            raise PortfolioError("Este ingreso tiene seguimiento Wallet ↔ MEXC. Regístralo desde ese recorrido")
        if any(event.id not in linked_ids and event.input.amount == Decimal(row["amount"])
               and abs(event.occurred_at - at) <= timedelta(hours=24) for event in usdt_cash):
            raise PortfolioError("Hay un posible asiento anterior para este ingreso. Revisa el historial")
        lines.append({"amount": row["amount"], "network": row["network"], "occurred_at": at.isoformat()})

    with localcontext() as context:
        context.prec = 300
        preview_hash = _digest({"account_id": account_id, "snapshot_id": snapshot_id, "sources": sources,
                                "revision": parity["book_revision"],
                                "cash_before": str(Decimal(balance["book_quantity"]).normalize()),
                                "cash_after": str(Decimal(balance["mexc_spot_quantity"]).normalize())})
    result = {"status": "PREVIEW", "amount": format(amount, "f"), "currency": "USDT", "lines": lines,
              "book_before": balance["book_quantity"], "book_after": balance["mexc_spot_quantity"],
              "preview_hash": preview_hash, "orders_enabled": False}
    if expected_hash is None:
        return result
    if current_snapshot_id is None or expected_hash != preview_hash:
        raise PortfolioError("La vista previa cambió. Revisa de nuevo los ingresos")
    event_ids = []
    for row, line in zip(sources, lines, strict=True):
        ref = row["source_ref"]
        item = EventInput("CONTRIBUTION", "USDT", datetime.fromisoformat(line["occurred_at"]),
                          amount=Decimal(row["amount"]), note=EXTERNAL_DEPOSIT_NOTE)
        event = append_event_in_transaction(db, account_id, external_deposit_request_id(account_id, ref), item)
        stored = db.get(RealEvent, event.id)
        db.add(RealMexcTransferMatch(id=str(uuid4()), account_ref=account_ref, account_id=account_id,
            snapshot_id=snapshot_id, event_id=event.id, direction="deposit", source_ref=ref,
            source_digest=_digest(row), event_digest=stored.payload_digest, confirmed_at=now))
        event_ids.append(event.id)
    return {**result, "status": "RECORDED", "event_ids": event_ids}
