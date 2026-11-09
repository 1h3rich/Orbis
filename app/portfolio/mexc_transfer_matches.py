"""Vínculos explícitos entre un traslado MEXC y un asiento REAL pareado.

La referencia externa es HMAC de un identificador privado, nunca la dirección
ni el txid. Un vínculo no prueba titularidad de la wallet ni conciliación de
toda la cuenta; únicamente fija una correspondencia revisada y reproducible.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import re
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy.orm import Session

from app.portfolio.domain import PortfolioError, utc_datetime
from app.portfolio.mexc_transfers import read_transfer_snapshot
from app.portfolio.models import (
    MexcTransferSnapshot, RealEvent, RealMexcAccountLink, RealMexcTransferMatch,
)
from app.portfolio.repository import list_events, verify_paired_transfers


_REF = re.compile(r"[0-9a-f]{64}\Z")
_WINDOW = timedelta(hours=24)
EXTERNAL_DEPOSIT_NOTE = "Ingreso externo MEXC observado en Spot"


def external_deposit_request_id(account_id: str, source_ref: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"orbis:mexc-external-deposit:{account_id}:{source_ref}"))


def _uuid(value: str, name: str) -> str:
    try:
        return str(UUID(value))
    except (TypeError, ValueError):
        raise PortfolioError(f"Invalid {name}") from None


def _digest(row: dict) -> str:
    return hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode("ascii")).hexdigest()


def _source_rows(snapshot: dict, direction: str, source_ref: str) -> list[dict]:
    if direction not in ("deposit", "withdrawal"):
        raise PortfolioError("Invalid MEXC transfer direction")
    field = "deposits" if direction == "deposit" else "withdrawals"
    return [row for row in snapshot[field] if row.get("source_ref") == source_ref]


def _source_row(db: Session, account_ref: str, snapshot_id: str,
                direction: str, source_ref: str, *, check_history: bool = False) -> dict:
    snapshot = read_transfer_snapshot(db, snapshot_id)
    if snapshot["account_ref"] != account_ref:
        raise PortfolioError("MEXC transfer belongs to another connection")
    rows = _source_rows(snapshot, direction, source_ref)
    if len(rows) != 1:
        raise PortfolioError("MEXC transfer reference is absent or ambiguous")
    row = rows[0]
    if row["status"] != (5 if direction == "deposit" else 7):
        raise PortfolioError("MEXC transfer is not final")
    if direction == "withdrawal" and row.get("transfer_type") != 0:
        raise PortfolioError("MEXC withdrawal route is not external and verified")
    if Decimal(row["fee"]) != 0:
        raise PortfolioError("MEXC transfer fee needs an explicit accounting model")
    if not check_history:
        return row
    # Una referencia que cambió de importe, activo, red, destino u hora/estado final
    # entre consultas no puede fijarse silenciosamente a un asiento.
    observations = [(datetime.fromisoformat(snapshot["observed_at"]), row["status"])]
    known_destinations = {row["destination_ref"]} if row.get("destination_ref") else set()
    known_contracts = {row["token_contract"]} if row.get("token_contract") else set()
    known_chain_refs = {row["chain_ref"]} if row.get("chain_ref") else set()
    for archived in db.query(MexcTransferSnapshot).filter(
            MexcTransferSnapshot.account_ref == account_ref).all():
        if archived.id == snapshot_id:
            continue
        observed = read_transfer_snapshot(db, archived.id)
        matching = _source_rows(observed, direction, source_ref)
        if len(matching) > 1:
            raise PortfolioError("MEXC transfer reference is ambiguous across snapshots")
        if not matching:
            continue
        other = matching[0]
        if any(other.get(field) != row.get(field) for field in (
                "asset", "network", "amount", "fee", "time_ms", "transfer_type")):
            raise PortfolioError("MEXC transfer changed across snapshots")
        if other.get("destination_ref"):
            known_destinations.add(other["destination_ref"])
        if other.get("token_contract"):
            known_contracts.add(other["token_contract"])
        if other.get("chain_ref"):
            known_chain_refs.add(other["chain_ref"])
        observations.append((datetime.fromisoformat(observed["observed_at"]), other["status"]))
    if len(known_destinations) > 1:
        raise PortfolioError("MEXC deposit destination changed across snapshots")
    if len(known_contracts) > 1:
        raise PortfolioError("MEXC token contract changed across snapshots")
    if len(known_chain_refs) > 1:
        raise PortfolioError("MEXC chain transaction changed across snapshots")
    observations.sort()
    final_seen = False
    previous_time = None
    previous_status = None
    for stamp, status in observations:
        if stamp == previous_time and status != previous_status:
            raise PortfolioError("MEXC transfer finality is ambiguous across snapshots")
        if status == row["status"]:
            final_seen = True
        elif final_seen:
            raise PortfolioError("MEXC transfer finality regressed across snapshots")
        previous_time, previous_status = stamp, status
    if observations[-1][1] != row["status"]:
        raise PortfolioError("MEXC transfer finality changed across snapshots")
    return row


def _event(db: Session, account_id: str, event_id: str,
           direction: str, source: dict, *, external: bool = False) -> RealEvent:
    event = next((item for item in list_events(db, account_id)
                  if item.id == event_id), None)
    if event is None:
        raise PortfolioError("Real transfer event not found in linked account")
    item = event.input
    expected = (("TRANSFER_IN", "CASH_TRANSFER_IN") if direction == "deposit"
                else ("TRANSFER_OUT", "CASH_TRANSFER_OUT"))
    amount = item.quantity if item.asset else item.amount
    asset = item.asset or item.quote_currency
    observed_at = datetime.fromtimestamp(source["time_ms"] / 1000, timezone.utc)
    if external and direction == "deposit" and item.kind == "CONTRIBUTION":
        if (item.asset is not None or asset != "USDT" or asset != source["asset"]
                or amount != Decimal(source["amount"]) or item.fee != 0
                or event.occurred_at != observed_at or item.note != EXTERNAL_DEPOSIT_NOTE
                or event.request_id != external_deposit_request_id(account_id, source["source_ref"])):
            raise PortfolioError("El aporte MEXC no coincide con su evidencia")
    elif (item.kind not in expected or asset != source["asset"]
            or amount != Decimal(source["amount"])
            or abs(event.occurred_at - observed_at) > _WINDOW
            or item.fee != 0):
        raise PortfolioError("Real transfer event does not match MEXC evidence")
    else:
        verify_paired_transfers(db)
    row = db.get(RealEvent, event_id)
    if row is None or row.account_id != account_id:
        raise PortfolioError("Real transfer event is unavailable")
    return row


def confirm_mexc_transfer_match(db: Session, account_id: str, snapshot_id: str,
                                direction: str, source_ref: str, event_id: str,
                                *, confirmation: str, now: datetime | None = None) -> dict:
    """Fija una relación 1:1 tras confirmación literal; no crea asientos."""
    if confirmation != "VINCULAR TRASLADO":
        raise PortfolioError("Confirm transfer match explicitly")
    snapshot_id = _uuid(snapshot_id, "snapshot id")
    event_id = _uuid(event_id, "event id")
    if direction not in ("deposit", "withdrawal"):
        raise PortfolioError("Invalid MEXC transfer direction")
    if not isinstance(source_ref, str) or not _REF.fullmatch(source_ref):
        raise PortfolioError("Invalid MEXC transfer reference")
    confirmed_at = utc_datetime(now or datetime.now(timezone.utc))
    try:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        snapshot = read_transfer_snapshot(db, snapshot_id)
        account_ref = snapshot["account_ref"]
        if not snapshot["identity_verified"]:
            raise PortfolioError("MEXC transfer snapshot lacks verified View identity")
        link = db.get(RealMexcAccountLink, account_ref)
        if link is None or link.account_id != account_id:
            raise PortfolioError("MEXC connection is not linked to this real account")
        source = _source_row(db, account_ref, snapshot_id, direction, source_ref,
                             check_history=True)
        local = _event(db, account_id, event_id, direction, source)
        from app.portfolio.models import RealWalletFundingCredit, RealWalletFundingBooking
        funding_links = db.query(RealWalletFundingBooking,RealWalletFundingCredit).join(
            RealWalletFundingCredit,RealWalletFundingCredit.funding_id == RealWalletFundingBooking.funding_id).all()
        for booking,credit in funding_links:
            same_source = direction == "deposit" and credit.account_ref == account_ref and credit.source_ref == source_ref
            if (same_source and booking.event_id != event_id or booking.event_id == event_id and not same_source):
                raise PortfolioError("Wallet funding deposit or event is already linked differently")
        from app.portfolio.models import RealMexcWithdrawalBooking, RealMexcWithdrawalReceipt
        withdrawal_links = db.query(RealMexcWithdrawalBooking,RealMexcWithdrawalReceipt).join(
            RealMexcWithdrawalReceipt,RealMexcWithdrawalReceipt.withdrawal_id == RealMexcWithdrawalBooking.withdrawal_id).all()
        for booking,receipt in withdrawal_links:
            same_source = direction == "withdrawal" and receipt.account_ref == account_ref and receipt.source_ref == source_ref
            if (same_source and booking.source_event_id != event_id
                    or event_id in (booking.source_event_id,booking.target_event_id) and not (same_source and booking.source_event_id == event_id)):
                raise PortfolioError("Wallet withdrawal or event is already linked differently")
        existing = db.query(RealMexcTransferMatch).filter(
            RealMexcTransferMatch.account_ref == account_ref,
            RealMexcTransferMatch.direction == direction,
            RealMexcTransferMatch.source_ref == source_ref,
        ).one_or_none()
        if existing is not None:
            if (existing.event_id != event_id or existing.account_id != account_id
                    or existing.source_digest != _digest(source)
                    or existing.event_digest != local.payload_digest):
                raise PortfolioError("MEXC transfer reference is already linked differently")
            db.commit()
            return {"id": existing.id, "status": "ALREADY_LINKED",
                    "account_id": account_id, "event_id": event_id,
                    "direction": direction, "source_ref": source_ref,
                    "conclusion": "EXPLICIT_MATCH_NOT_ACCOUNT_RECONCILIATION"}
        if db.query(RealMexcTransferMatch).filter(
                RealMexcTransferMatch.event_id == event_id).first() is not None:
            raise PortfolioError("Real transfer event is already linked")
        result = RealMexcTransferMatch(
            id=str(uuid4()), account_ref=account_ref, account_id=account_id,
            snapshot_id=snapshot_id, event_id=event_id, direction=direction,
            source_ref=source_ref, source_digest=_digest(source),
            event_digest=local.payload_digest, confirmed_at=confirmed_at,
        )
        db.add(result)
        db.commit()
        return {"id": result.id, "status": "LINKED", "account_id": account_id,
                "event_id": event_id, "direction": direction,
                "source_ref": source_ref,
                "conclusion": "EXPLICIT_MATCH_NOT_ACCOUNT_RECONCILIATION"}
    except Exception:
        db.rollback()
        raise


def verify_mexc_transfer_match(db: Session, row: RealMexcTransferMatch, *,
                               check_history: bool = False) -> None:
    """Reabre ambos lados; una copia no se invalida por observaciones futuras."""
    if db.get(RealMexcAccountLink, row.account_ref) is None:
        raise PortfolioError("MEXC transfer link lost its account connection")
    source = _source_row(db, row.account_ref, row.snapshot_id,
                         row.direction, row.source_ref, check_history=check_history)
    local = _event(db, row.account_id, row.event_id, row.direction, source, external=True)
    if (db.get(RealMexcAccountLink, row.account_ref).account_id != row.account_id
            or _digest(source) != row.source_digest
            or local.payload_digest != row.event_digest
            or local.kind == "CONTRIBUTION" and not read_transfer_snapshot(db, row.snapshot_id)["identity_verified"]):
        raise PortfolioError("MEXC transfer match differs from archived evidence")
