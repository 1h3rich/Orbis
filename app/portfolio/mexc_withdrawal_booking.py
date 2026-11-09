"""Resolución contable explícita de retiradas: neto, comisión y traslado pareado."""

from datetime import datetime,timedelta
from decimal import Decimal,localcontext
import hashlib
import hmac
from uuid import UUID,uuid5

from sqlalchemy.orm import Session

from app.live_execution.mexc_uid_guard import MexcTransferRecoveryView
from app.portfolio.domain import CALC_PRECISION,EventInput,decimal_value,fold_events
from app.portfolio.models import (
    RealAccount,RealEvent,RealLiveStop,RealMexcAccountLink,RealMexcTransferMatch,
    RealMexcWithdrawalBooking,RealWalletLink,
    RealMexcWithdrawalObservation,
)
from app.portfolio.mexc_transfers import _verified_uid_digest
from app.portfolio.mexc_withdrawal_journal import WithdrawalError,amount_text,recipient_matches,withdrawal_record
from app.portfolio.mexc_withdrawal_observation import consistent_source,observation_record,instant,has_chain_observation
from app.portfolio.mexc_withdrawal_receipt import receipt_record
from app.portfolio.repository import (
    account_book_revision,account_snapshot,append_event_in_transaction,list_events,
    transfer_cash_in_transaction,verify_account_event_digests,verify_paired_transfers,
)
from app.portfolio.wallet_funding import _decoded,_encoded,_time
from app.portfolio.wallet_links import _salt
from app.portfolio.wallet_proof import canonical_wallet_address


def _wallet_account(db: Session,address: str) -> str | None:
    digest = hmac.new(_salt(db),f"0x38:{address}".encode("ascii"),hashlib.sha256).hexdigest()
    row = db.query(RealWalletLink).filter_by(chain_id="0x38",address_digest=digest).one_or_none()
    return row.account_id if row else None


def _source_event(db: Session,plan,receipt: dict,event_id: str):
    verify_account_event_digests(db,plan.account_id)
    row = db.get(RealEvent,event_id)
    if (row is None or row.account_id != plan.account_id or row.kind not in ("WITHDRAWAL","CASH_TRANSFER_OUT")
            or row.quote_currency != "USDT" or row.asset is not None
            or row.gross_amount != Decimal(amount_text(int(receipt["received_units"]),receipt["decimals"]))
            or row.fee != Decimal(amount_text(int(receipt["fee_units"]),receipt["decimals"]))
            or row.recorded_at < plan.created_at
            or row.occurred_at < plan.created_at-timedelta(seconds=15)
            or row.occurred_at > datetime.fromisoformat(receipt["block_at"])+timedelta(seconds=15)):
        raise WithdrawalError("El asiento REAL no coincide con neto, comisión y fecha de esta retirada")
    target = None
    if row.kind == "CASH_TRANSFER_OUT":
        verify_paired_transfers(db)
        target = db.query(RealEvent).filter_by(request_id=row.request_id,kind="CASH_TRANSFER_IN").one_or_none()
        if target is None:
            raise WithdrawalError("El traslado no tiene un lado de entrada válido")
    return row,target


def _view_balance(db: Session,plan,view: dict,uid: str,now: datetime,
                  recovery: MexcTransferRecoveryView | None) -> tuple[Decimal,str]:
    if recovery is not None:
        if (recovery.account_id, recovery.archive_ref) != (plan.account_id, plan.account_ref):
            raise WithdrawalError("La recuperación no corresponde a esta retirada")
        recovery.verify(db, uid)
    uid_digest = _verified_uid_digest(db,plan.account_ref,uid)
    if uid_digest is None or not 0 <= (_time(now)-_time(datetime.fromisoformat(view["observed_at"]))).total_seconds() <= 30:
        raise WithdrawalError("Saldo View caducado o identidad de la retirada no verificada")
    balances = view["balances"]
    if any(not isinstance(balances.get(key),str) or len(balances[key]) > 100 for key in ("free","locked")):
        raise WithdrawalError("Saldo View no decimal")
    free,locked = Decimal(balances["free"]),Decimal(balances["locked"])
    decimal_value(free)
    decimal_value(locked)
    if free < 0 or locked != 0:
        raise WithdrawalError("Hay saldo USDT negativo o bloqueado; revisa MEXC antes de conciliar")
    stop = db.get(RealLiveStop,plan.account_ref)
    if stop is None or not stop.stopped:
        raise WithdrawalError("Mantén STOP durante la conciliación de la retirada")
    return free,uid_digest


def booking_record(db: Session,identifier: str,*,check_history: bool = True):
    row = db.get(RealMexcWithdrawalBooking,identifier)
    if row is None:
        return None
    plan,_,_ = withdrawal_record(db,identifier)
    data = _decoded(row.payload_json,row.payload_digest)
    try:
        if data.get("withdrawal_id") != identifier or data.get("plan_digest") != plan.payload_digest:
            raise ValueError
        observed,source = consistent_source(db,data["observation_id"],check_history=check_history)
        if (observed.withdrawal_id != identifier or observed.payload_digest != data["observation_digest"]
                or data["source_ref"] != source["source_ref"] or data["uid_digest"] != source["uid_digest"]
                or _time(datetime.fromisoformat(data["booked_at"])) < plan.created_at):
            raise ValueError
        free = Decimal(data["view_free"])
        decimal_value(free)
        if free < 0 or not 0 <= (_time(datetime.fromisoformat(data["booked_at"]))-_time(datetime.fromisoformat(data["view_observed_at"]))).total_seconds() <= 30:
            raise ValueError
        if data["mode"] == "REJECTED_NO_DEBIT":
            if (set(data) != {"withdrawal_id","plan_digest","observation_id","observation_digest","source_ref","uid_digest","mode",
                    "book_revision","view_free","view_observed_at","booked_at"}
                    or row.source_event_id is not None or row.target_event_id is not None
                    or source["status"] not in (8,9) or source["chain_digest"] is not None or int(source["fee_units"]) != 0
                    or check_history and has_chain_observation(db,identifier)
                    or data["book_revision"] != withdrawal_record(db,identifier)[1]["book_revision"]):
                raise ValueError
            return row,data
        receipt = receipt_record(db,identifier,check_history=check_history)
        if receipt is None:
            raise ValueError
        event,target = _source_event(db,plan,receipt[1],row.source_event_id)
        if (set(data) != {"withdrawal_id","plan_digest","observation_id","observation_digest","source_ref","uid_digest","mode",
                "receipt_digest","source_event_digest","target_event_digest","target_account_id","view_free","view_observed_at",
                "accepted_fee_override","booked_at"}
                or data["mode"] not in ("NEW_EXTERNAL","NEW_INTERNAL","EXISTING_EVENT")
                or data["receipt_digest"] != receipt[0].payload_digest or data["source_event_digest"] != event.payload_digest
                or row.target_event_id != (target.id if target else None)
                or data["target_event_digest"] != (target.payload_digest if target else None)
                or data["target_account_id"] != (target.account_id if target else None)
                or data["mode"] == "NEW_EXTERNAL" and event.kind != "WITHDRAWAL"
                or data["mode"] == "NEW_INTERNAL" and target is None
                or type(data["accepted_fee_override"]) is not bool
                or receipt[1]["fee_filter_exceeded"] and not data["accepted_fee_override"]):
            raise ValueError
        for linked in db.query(RealMexcTransferMatch).filter_by(account_ref=plan.account_ref,direction="withdrawal",source_ref=source["source_ref"]).all():
            if linked.event_id != row.source_event_id:
                raise ValueError
    except (KeyError,ValueError,TypeError,OverflowError):
        raise WithdrawalError("La resolución contable de la retirada cambió o perdió su evidencia") from None
    return row,data


def settlement_status(db: Session,identifier: str) -> dict:
    receipt = receipt_record(db,identifier)
    booked = booking_record(db,identifier)
    rejected = bool(booked and booked[1]["mode"] == "REJECTED_NO_DEBIT")
    result = {"settlement_status":"REJECTED_CONFIRMED" if rejected else "BOOKED" if booked else "RECEIVED" if receipt else "UNCONFIRMED",
        "wallet_received":bool(receipt),"booked":bool(booked and not rejected),"resolved":bool(booked),"automatic_retry":False,
        "can_request_new_withdrawal":bool(booked),"trading_reopened":False}
    latest = db.query(RealMexcWithdrawalObservation).filter_by(withdrawal_id=identifier).order_by(
        RealMexcWithdrawalObservation.observed_at.desc(),RealMexcWithdrawalObservation.id.desc()).first()
    if latest:
        _,source = consistent_source(db,latest.id)
        result["mexc_status"] = source["status"]
        result["can_review_rejected"] = not receipt and not booked and source["status"] in (8,9) and not has_chain_observation(db,identifier) and int(source["fee_units"]) == 0
    if receipt:
        data = receipt[1]
        result.update({key:data[key] for key in ("chain_source","network_evidence","fee_model","fee_filter_exceeded")})
        result.update({"received":amount_text(int(data["received_units"]),data["decimals"]),
            "actual_fee":amount_text(int(data["fee_units"]),data["decimals"]),"actual_debit":amount_text(int(data["debit_units"]),data["decimals"])})
    return result


def booking_options(db: Session,identifier: str,account_id: str,address: str) -> dict:
    plan,terms,_ = withdrawal_record(db,identifier)
    address = canonical_wallet_address(address)
    if plan.account_id != account_id or not recipient_matches(db, terms, address):
        raise WithdrawalError("Conecta la wallet original de esta retirada")
    receipt = receipt_record(db,identifier)
    if receipt is None:
        raise WithdrawalError("Comprueba primero la recepción en la wallet")
    linked = _wallet_account(db,address)
    exchange_accounts = {link.account_id for link in db.query(RealMexcAccountLink).all()}
    accounts = [{"id":row.id,"name":row.name} for row in db.query(RealAccount).all()
                if row.id != account_id and row.id not in exchange_accounts and (linked is None or linked == row.id)]
    events = []
    for row in db.query(RealEvent).filter(RealEvent.account_id == account_id,RealEvent.kind.in_(("WITHDRAWAL","CASH_TRANSFER_OUT")),
                                        RealEvent.recorded_at >= plan.created_at).all():
        try:
            event,target = _source_event(db,plan,receipt[1],row.id)
        except ValueError:
            continue
        if linked is None or target and target.account_id == linked:
            events.append({"id":event.id,"kind":event.kind,"occurred_at":event.occurred_at.isoformat(),
                           "target_account_id":target.account_id if target else None})
    return {"accounts":accounts,"events":events,"external_available":linked is None,
            "linked_to_source_account":linked == account_id,"fee_filter_exceeded":receipt[1]["fee_filter_exceeded"]}


def book_withdrawal(db: Session,identifier: str,account_id: str,*,address: str,mode: str,
                    view: dict,uid: str,observation_id: str,now: datetime,target_account_id: str | None = None,
                    event_id: str | None = None,accept_fee_override: bool = False,
                    recovery: MexcTransferRecoveryView | None = None) -> dict:
    try:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        plan,terms,_ = withdrawal_record(db,identifier)
        address = canonical_wallet_address(address)
        if plan.account_id != account_id or not recipient_matches(db, terms, address):
            raise WithdrawalError("Cuenta o wallet original de la retirada no coinciden")
        previous = booking_record(db,identifier)
        if previous:
            data = previous[1]
            if (data["mode"] != mode or mode == "EXISTING_EVENT" and previous[0].source_event_id != event_id
                    or mode != "EXISTING_EVENT" and event_id is not None
                    or mode == "NEW_INTERNAL" and data.get("target_account_id") != target_account_id
                    or mode == "NEW_EXTERNAL" and target_account_id is not None
                    or mode == "EXISTING_EVENT" and target_account_id is not None and data.get("target_account_id") != target_account_id
                    or data.get("accepted_fee_override") != bool(accept_fee_override)):
                raise WithdrawalError("La retirada ya se resolvió de otra forma")
            result = settlement_status(db,identifier)
            db.commit()
            return result
        receipt = receipt_record(db,identifier)
        observed,source = consistent_source(db,observation_id,require_success=True)
        if receipt is None or observed.withdrawal_id != identifier:
            raise WithdrawalError("Comprueba primero la recepción de esta retirada")
        if receipt[1]["tx_digest"] != source["chain_digest"] or any(receipt[1][key] != source[key] for key in ("source_ref","fee_units")):
            raise WithdrawalError("MEXC cambió después de la comprobación de recepción")
        free,uid_digest = _view_balance(db,plan,view,uid,now,recovery)
        if receipt[1]["fee_filter_exceeded"] and not accept_fee_override:
            raise WithdrawalError("La comisión real superó el filtro previo; acepta registrar el exceso observado, sin ocultarlo")
        linked_wallet = _wallet_account(db,address)
        existing = db.query(RealMexcTransferMatch).filter_by(account_ref=plan.account_ref,direction="withdrawal",source_ref=source["source_ref"]).first()
        if existing is not None and (mode != "EXISTING_EVENT" or event_id != existing.event_id):
            raise WithdrawalError("Esta retirada ya tiene un asiento vinculado: selecciona ese mismo asiento")
        with localcontext() as context:
            context.prec = CALC_PRECISION
            net = Decimal(amount_text(int(receipt[1]["received_units"]),terms["decimals"]))
            fee = Decimal(amount_text(int(receipt[1]["fee_units"]),terms["decimals"]))
            debit = net+fee
            cash = account_snapshot(db,account_id).cash.get("USDT",Decimal(0))
            expected_free = cash-debit
        if mode in ("NEW_EXTERNAL","NEW_INTERNAL"):
            if event_id is not None or account_book_revision(db,account_id) != terms["book_revision"] or expected_free != free or cash < debit:
                raise WithdrawalError("El libro o saldo MEXC no explican exactamente neto + comisión; revisa otros movimientos")
            claim_at = datetime.fromisoformat(withdrawal_record(db,identifier)[2]["history"][1]["at"])
            occurred_at = max(datetime.fromisoformat(receipt[1]["block_at"]),instant(source["time_ms"]),plan.created_at,claim_at)
            preceding = [event for event in list_events(db,account_id) if event.occurred_at <= occurred_at]
            if fold_events(account_id,preceding).cash.get("USDT",Decimal(0)) < debit:
                raise WithdrawalError("El libro no tenía efectivo suficiente en la fecha de la retirada")
            request_id = str(uuid5(UUID(identifier),"mexc-withdrawal-booking-v1"))
            if mode == "NEW_EXTERNAL":
                if linked_wallet is not None or target_account_id is not None:
                    raise WithdrawalError("La wallet está registrada: usa traslado interno, sin retirada de capital ficticia")
                event_id = append_event_in_transaction(db,account_id,request_id,EventInput("WITHDRAWAL","USDT",occurred_at,
                    amount=net,fee=fee,note="Retirada USDT/BSC a wallet confirmada; comisión USDT separada")).id
            else:
                if (target_account_id is None or target_account_id == account_id or db.get(RealAccount,target_account_id) is None
                        or linked_wallet is not None and linked_wallet != target_account_id
                        or db.query(RealMexcAccountLink).filter_by(account_id=target_account_id).first()):
                    raise WithdrawalError("Elige una cuenta wallet separada y coherente con su vínculo local")
                pair = transfer_cash_in_transaction(db,account_id,target_account_id,request_id,currency="USDT",
                    amount=net,fee=fee,occurred_at=occurred_at,note="Traslado MEXC a wallet USDT/BSC confirmado")
                event_id = pair["source_event_id"]
        elif mode == "EXISTING_EVENT":
            if cash != free:
                raise WithdrawalError("El libro con el asiento existente no coincide con el saldo libre MEXC")
            event_id = str(UUID(event_id))
        else:
            raise WithdrawalError("Modo contable de retirada no admitido")
        event,target = _source_event(db,plan,receipt[1],event_id)
        if (linked_wallet is not None and (target is None or target.account_id != linked_wallet)
                or target_account_id is not None and (target is None or target.account_id != target_account_id)):
            raise WithdrawalError("El asiento no corresponde a la cuenta wallet de destino")
        if db.query(RealMexcWithdrawalBooking).filter(RealMexcWithdrawalBooking.source_event_id == event_id).first():
            raise WithdrawalError("El asiento ya representa otra retirada")
        for linked in db.query(RealMexcTransferMatch).filter(RealMexcTransferMatch.event_id.in_([event_id]+([target.id] if target else []))).all():
            if linked.account_ref != plan.account_ref or linked.direction != "withdrawal" or linked.source_ref != source["source_ref"] or linked.event_id != event_id:
                raise WithdrawalError("Un asiento ya representa otro traslado MEXC")
        raw,digest = _encoded({"withdrawal_id":identifier,"plan_digest":plan.payload_digest,"observation_id":observation_id,
            "observation_digest":observed.payload_digest,"source_ref":source["source_ref"],"uid_digest":uid_digest,"mode":mode,
            "receipt_digest":receipt[0].payload_digest,"source_event_digest":event.payload_digest,
            "target_event_digest":target.payload_digest if target else None,"target_account_id":target.account_id if target else None,
            "view_free":str(free),"view_observed_at":view["observed_at"],"accepted_fee_override":bool(accept_fee_override),"booked_at":_time(now).isoformat()})
        db.add(RealMexcWithdrawalBooking(withdrawal_id=identifier,source_event_id=event_id,target_event_id=target.id if target else None,
            payload_json=raw,payload_digest=digest))
        db.flush()
        result = settlement_status(db,identifier)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def resolve_rejected(db: Session,identifier: str,account_id: str,observation_id: str,view: dict,uid: str,*,now: datetime,
                     recovery: MexcTransferRecoveryView | None = None) -> dict:
    try:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        plan,terms,_ = withdrawal_record(db,identifier)
        if plan.account_id != account_id:
            raise WithdrawalError("La retirada pertenece a otra cuenta")
        previous = booking_record(db,identifier)
        if previous:
            if previous[1]["mode"] != "REJECTED_NO_DEBIT":
                raise WithdrawalError("La retirada ya tiene recepción o asiento")
            result = settlement_status(db,identifier)
            db.commit()
            return result
        observed,source = consistent_source(db,observation_id)
        free,uid_digest = _view_balance(db,plan,view,uid,now,recovery)
        if (observed.withdrawal_id != identifier or source["status"] not in (8,9) or source["chain_digest"] is not None
                or has_chain_observation(db,identifier)
                or int(source["fee_units"]) != 0 or receipt_record(db,identifier) is not None
                or account_book_revision(db,account_id) != terms["book_revision"]
                or account_snapshot(db,account_id).cash.get("USDT",Decimal(0)) != free):
            raise WithdrawalError("MEXC no demuestra rechazo sin hash, comisión ni cargo; mantén la revisión")
        raw,digest = _encoded({"withdrawal_id":identifier,"plan_digest":plan.payload_digest,"observation_id":observed.id,
            "observation_digest":observed.payload_digest,"source_ref":source["source_ref"],"uid_digest":uid_digest,"mode":"REJECTED_NO_DEBIT",
            "book_revision":terms["book_revision"],"view_free":str(free),"view_observed_at":view["observed_at"],"booked_at":_time(now).isoformat()})
        db.add(RealMexcWithdrawalBooking(withdrawal_id=identifier,source_event_id=None,target_event_id=None,payload_json=raw,payload_digest=digest))
        db.flush()
        result = settlement_status(db,identifier)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
