"""Ingreso USDT/BSC: corroboración View y contabilidad explícita, nunca ejecución."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import hmac
import re
from uuid import UUID, uuid5
from sqlalchemy.orm import Session

from app.portfolio.domain import EventInput, PortfolioError
from app.portfolio.mexc_transfer_matches import _digest, _source_row
from app.portfolio.mexc_transfers import (
    BNB_NETWORKS, destination_message, find_mexc_deposit_by_evm_tx_hash, read_transfer_snapshot,
)
from app.portfolio.models import (
    RealAccount, RealEvent, RealMexcAccountLink, RealMexcTransferMatch, RealWalletFundingBooking, RealWalletFundingCredit, RealWalletLink,
)
from app.portfolio.repository import (
    account_book_revision, append_event_in_transaction, list_events, transfer_cash_in_transaction,
    verify_account_event_digests, verify_paired_transfers,
)
from app.portfolio.wallet_funding import (
    WalletFundingError, _decoded, _encoded, _identity, _time, _uint, exact_units, funding_record, observed_mexc_deposit,
)
from app.portfolio.wallet_funding_receipt import FundingReceiptSource, receipt_observation
from app.portfolio.wallet_links import _salt, inspect_wallet_link
from app.portfolio.wallet_proof import canonical_wallet_address


def _instant(milliseconds: int) -> datetime:
    return datetime.fromtimestamp(milliseconds//1000,timezone.utc)+timedelta(milliseconds=milliseconds%1000)


def credit_record(db: Session, identifier: str, *, check_history: bool = True
                  ) -> tuple[RealWalletFundingCredit, dict, dict] | None:
    funding, terms, state = funding_record(db,identifier)
    row = db.get(RealWalletFundingCredit,identifier)
    if row is None:
        return None
    data = _decoded(row.payload_json,row.payload_digest)
    try:
        snapshot = read_transfer_snapshot(db,row.snapshot_id)
        link = db.get(RealMexcAccountLink,row.account_ref)
        source = _source_row(db,row.account_ref,row.snapshot_id,"deposit",row.source_ref,check_history=check_history)
        if (set(data) != {"funding_id","plan_digest","tx_digest","chain_id","chain_source","block_number",
                "block_hash","block_at","finalized_number","finalized_hash","gas_used","effective_gas_price",
                "gas_fee_wei","rpc_digest","snapshot_id","source_ref","source_digest","checked_at"}
                or not snapshot["identity_verified"] or link is None or link.account_id != funding.account_id
                or row.account_ref != funding.account_ref or funding.status != "HASH_REPORTED"
                or data["funding_id"] != identifier or data["plan_digest"] != funding.payload_digest
                or data["tx_digest"] != state["tx_digest"] or data["snapshot_id"] != row.snapshot_id
                or data["source_ref"] != row.source_ref or data["source_digest"] != _digest(source)
                or data["chain_id"] != "0x38" or data["chain_source"] not in ("WALLET_RPC_OBSERVED", "ORBIS_RPC_OBSERVED")
                or source["network"] not in BNB_NETWORKS or source["asset"] != "USDT"
                or source.get("token_contract") != terms["token_contract"]
                or exact_units(source["amount"],terms["decimals"]) != int(terms["amount_units"])
                or int(data["finalized_number"]) < int(data["block_number"])
                or any(not isinstance(data[key],str) or not re.fullmatch(r"0x[0-9a-f]{64}",data[key])
                       for key in ("block_hash","finalized_hash"))
                or not isinstance(data["rpc_digest"],str) or not re.fullmatch(r"[0-9a-f]{64}",data["rpc_digest"])
                or data["finalized_number"] == data["block_number"] and data["finalized_hash"] != data["block_hash"]
                or _uint(data["effective_gas_price"],"Gas price") != int(terms["gas_price"])
                or _uint(data["gas_used"],"Gas usado") > int(terms["gas"])
                or _uint(data["block_number"],"Bloque") > _uint(data["finalized_number"],"Bloque finalizado")
                or int(data["gas_used"])*int(data["effective_gas_price"]) != int(data["gas_fee_wei"])
                or not 0 < int(data["gas_fee_wei"]) <= int(terms["max_gas_wei"])
                or _time(datetime.fromisoformat(data["checked_at"])) < funding.created_at
                or datetime.fromisoformat(data["block_at"]) > datetime.fromisoformat(data["checked_at"])+timedelta(seconds=15)
                or abs(_instant(source["time_ms"])-_time(datetime.fromisoformat(data["block_at"]))) > timedelta(hours=24)):
            raise ValueError
    except (KeyError,TypeError,ValueError,OverflowError):
        raise WalletFundingError("La evidencia de acreditación cambió; revisa el ingreso") from None
    return row, data, source


def _verify_deposit_terms(db: Session, source: dict, funding, terms: dict, destination: str,
                          block_at: str) -> None:
    expected = hmac.new(_salt(db), destination_message(funding.account_ref, "deposit", source["network"], destination),
                        hashlib.sha256).hexdigest()
    if (source["network"] not in BNB_NETWORKS or source["asset"] != "USDT"
            or source.get("token_contract") != terms["token_contract"]
            or source.get("destination_ref") != expected or Decimal(source["fee"]) != 0
            or exact_units(source["amount"], terms["decimals"]) != int(terms["amount_units"])
            or abs(_instant(source["time_ms"]) - datetime.fromisoformat(block_at)) > timedelta(hours=24)):
        raise WalletFundingError("Contrato, destino, red, importe u hora de MEXC no coinciden con el envío")


def confirm_credit(db: Session, identifier: str, account_id: str, evidence: dict,
                   snapshot_id: str, *, now: datetime,
                   chain_source: FundingReceiptSource = "WALLET_RPC_OBSERVED") -> dict:
    try:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        observed = receipt_observation(db,identifier,account_id,evidence,now=now,chain_source=chain_source)
        funding,terms,state = funding_record(db,identifier)
        destination = "0x"+evidence["transaction"]["input"][34:74].lower()
        match = find_mexc_deposit_by_evm_tx_hash(db,account_id,evidence["tx_hash"],[destination])
        snapshot = read_transfer_snapshot(db,snapshot_id)
        if (not snapshot["identity_verified"] or snapshot["account_ref"] != funding.account_ref
                or not 0 <= (_time(now)-datetime.fromisoformat(snapshot["observed_at"])).total_seconds() <= 60):
            raise WalletFundingError("La consulta View de MEXC caducó o no pertenece a esta cuenta")
        pending = {"status":"PENDING_MEXC","chain_source":chain_source,"exchange_credited":False,"booked":False}
        if match["status"] == "NOT_FINAL_MEXC_DEPOSIT":
            # A fresh response must itself contain the match, with destination and token.
            source = next((item for item in snapshot["deposits"] if item.get("source_ref") == match["source_ref"]), None)
            if (source and source["status"] == match["mexc_status_code"]
                    and source.get("destination_ref") and source.get("token_contract")):
                _verify_deposit_terms(db, source, funding, terms, destination, observed["block_at"])
                previous = state.get("mexc_deposit")
                if not previous or datetime.fromisoformat(snapshot["observed_at"]) > datetime.fromisoformat(previous["observed_at"]):
                    state["mexc_deposit"] = {"snapshot_id":snapshot_id,"source_ref":source["source_ref"],
                        "source_digest":_digest(source),"code":source["status"],"observed_at":snapshot["observed_at"],
                        "tx_digest":observed["tx_digest"],"chain_source":chain_source}
                    funding.state_json,funding.state_digest = _encoded(state)
                    funding_record(db,identifier)
                db.commit()
                return pending | observed_mexc_deposit(state)
        if match["status"] in ("NOT_FOUND_IN_VERIFIED_ARCHIVE", "NOT_FINAL_MEXC_DEPOSIT"):
            db.rollback()
            return pending
        if match["status"] != "FINAL_MEXC_DEPOSIT":
            raise WalletFundingError("MEXC comunica datos contradictorios del ingreso")
        source_ref = match["source_ref"]
        source = _source_row(db,funding.account_ref,snapshot_id,"deposit",source_ref,check_history=True)
        _verify_deposit_terms(db, source, funding, terms, destination, observed["block_at"])
        existing = credit_record(db,identifier)
        if existing:
            previous = existing[1]
            if (previous["source_ref"] != source_ref or any(previous[key] != observed[key]
                    for key in ("block_number","block_hash","gas_used","effective_gas_price","gas_fee_wei"))):
                raise WalletFundingError("La evidencia del ingreso acreditado cambió")
        else:
            if db.query(RealWalletFundingCredit).filter_by(account_ref=funding.account_ref,source_ref=source_ref).first():
                raise WalletFundingError("Este depósito ya corresponde a otro envío")
            payload = {**observed,"snapshot_id":snapshot_id,"source_ref":source_ref,
                       "source_digest":_digest(source),"checked_at":_time(now).isoformat()}
            raw,digest = _encoded(payload)
            db.add(RealWalletFundingCredit(funding_id=identifier,account_ref=funding.account_ref,
                source_ref=source_ref,snapshot_id=snapshot_id,payload_json=raw,payload_digest=digest))
            db.flush()
            credit_record(db,identifier)
        if "mexc_deposit" in state:
            state.pop("mexc_deposit")
            funding.state_json,funding.state_digest = _encoded(state)
        result = settlement_status(db,identifier)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def _book_event(db: Session, account_id: str, event_id: str, source: dict) -> RealEvent:
    verify_account_event_digests(db,account_id)
    event = next((event for event in list_events(db,account_id) if event.id == event_id),None)
    if (event is None or event.input.kind not in ("CONTRIBUTION","CASH_TRANSFER_IN")
            or event.input.quote_currency != "USDT" or event.input.amount != Decimal(source["amount"])
            or event.input.fee != 0 or abs(event.occurred_at-_instant(source["time_ms"])) > timedelta(hours=24)):
        raise WalletFundingError("El asiento no coincide con este depósito USDT")
    if event.input.kind == "CASH_TRANSFER_IN":
        verify_paired_transfers(db)
    row = db.get(RealEvent,event_id)
    if row is None:
        raise WalletFundingError("El asiento REAL ya no está disponible")
    return row


def booking_record(db: Session, identifier: str, *, check_history: bool = True
                   ) -> tuple[RealWalletFundingBooking, dict] | None:
    row = db.get(RealWalletFundingBooking,identifier)
    if row is None:
        return None
    credit = credit_record(db,identifier,check_history=check_history)
    if credit is None:
        raise WalletFundingError("El asiento perdió la evidencia de acreditación")
    funding,_,_ = funding_record(db,identifier)
    event = _book_event(db,funding.account_id,row.event_id,credit[2])
    data = _decoded(row.payload_json,row.payload_digest)
    internal = data.get("mode") == "NEW_INTERNAL"
    keys = {"funding_id","credit_digest","event_id","event_digest","mode","booked_at"}
    if internal:
        keys |= {"source_account_id","source_event_id","source_event_digest"}
    if (set(data) != keys
            or data["funding_id"] != identifier or data["credit_digest"] != credit[0].payload_digest
            or data["event_id"] != row.event_id or data["event_digest"] != event.payload_digest
            or data["mode"] not in ("NEW_CONTRIBUTION","NEW_INTERNAL","EXISTING_EVENT")
            or data["mode"] == "NEW_CONTRIBUTION" and event.kind != "CONTRIBUTION"
            or _time(datetime.fromisoformat(data["booked_at"])) < datetime.fromisoformat(credit[1]["checked_at"])):
        raise WalletFundingError("La relación contable del ingreso cambió")
    if internal:
        outgoing = db.get(RealEvent,data["source_event_id"])
        if (event.kind != "CASH_TRANSFER_IN" or outgoing is None or outgoing.kind != "CASH_TRANSFER_OUT"
                or outgoing.account_id != data["source_account_id"] or outgoing.request_id != event.request_id
                or outgoing.payload_digest != data["source_event_digest"] or outgoing.fee != 0):
            raise WalletFundingError("El ingreso perdió su traslado desde la wallet")
        verify_account_event_digests(db,outgoing.account_id)
    other = db.query(RealMexcTransferMatch).filter_by(account_ref=credit[0].account_ref,
        direction="deposit",source_ref=credit[0].source_ref).first()
    if other is not None and other.event_id != row.event_id:
        raise WalletFundingError("El depósito está asociado a otro asiento")
    return row,data


def settlement_status(db: Session, identifier: str) -> dict:
    credit = credit_record(db,identifier)
    if credit is None:
        return {"status":"UNCONFIRMED","exchange_credited":False,"booked":False}
    booked = booking_record(db,identifier)
    return {"status":"BOOKED" if booked else "MEXC_CREDITED","exchange_credited":True,"booked":bool(booked),
        "chain_source":credit[1]["chain_source"],"gas_fee_wei":credit[1]["gas_fee_wei"],
        "credit_checked_at":credit[1]["checked_at"],"event_id":booked[0].event_id if booked else None,
        "ready_for_execution":False}


def book_credit(db: Session, identifier: str, account_id: str, *, mode: str,
                wallet_address: str | None, event_id: str | None = None, now: datetime,
                source_account_id: str | None = None, source_revision: str | None = None) -> dict:
    """El asiento y su vínculo se confirman juntos; jamás hay una orden externa."""
    try:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        funding,terms,_ = funding_record(db,identifier)
        if (funding.account_id != account_id or mode not in ("NEW_CONTRIBUTION","NEW_INTERNAL","EXISTING_EVENT")
                or mode != "NEW_INTERNAL" and (source_account_id is not None or source_revision is not None)):
            raise WalletFundingError("Cuenta o modo contable inválido")
        if mode == "NEW_INTERNAL":
            wallet = canonical_wallet_address(wallet_address)
            if event_id is not None or _identity(db,"wallet",wallet) != terms["wallet_digest"]:
                raise WalletFundingError("La wallet de origen no coincide con el envío")
        credit = credit_record(db,identifier)
        if credit is None:
            raise WalletFundingError("Confirma primero la acreditación en MEXC")
        previous = booking_record(db,identifier)
        if previous:
            if (previous[1]["mode"] != mode or mode == "EXISTING_EVENT" and event_id != previous[0].event_id
                    or mode == "NEW_INTERNAL" and previous[1]["source_account_id"] != source_account_id):
                raise WalletFundingError("El ingreso ya se contabilizó de otra forma")
            result = settlement_status(db,identifier)
            db.commit()
            return result
        linked = db.query(RealMexcTransferMatch).filter_by(account_ref=funding.account_ref,
            direction="deposit",source_ref=credit[0].source_ref).first()
        if linked is not None and (mode != "EXISTING_EVENT" or linked.event_id != event_id):
            raise WalletFundingError("El depósito ya tiene un asiento vinculado; selecciona ese mismo asiento")
        outgoing = None
        if mode == "NEW_INTERNAL":
            if booking_candidates(db,identifier,account_id):
                raise WalletFundingError("Ya hay asientos compatibles: enlaza el existente antes de crear otro traslado")
            owner = _wallet_owner(db,wallet)
            if (owner is None or owner.id != source_account_id or owner.id == account_id
                    or db.query(RealMexcAccountLink).filter_by(account_id=owner.id).first()):
                raise WalletFundingError("Elige la cuenta wallet vinculada, separada de MEXC")
            verify_account_event_digests(db,owner.id)
            if source_revision is None or account_book_revision(db,owner.id) != source_revision:
                raise WalletFundingError("El saldo de la wallet cambió; vuelve a revisar el ingreso")
            request = str(uuid5(UUID(identifier),"wallet-funding-internal-v1"))
            pair = transfer_cash_in_transaction(db,owner.id,account_id,request,currency="USDT",
                amount=Decimal(credit[2]["amount"]),occurred_at=_instant(credit[2]["time_ms"]),
                note="Traslado wallet a MEXC USDT/BSC acreditado")
            outgoing = db.get(RealEvent,pair["source_event_id"])
            event_id = pair["target_event_id"]
        elif mode == "NEW_CONTRIBUTION":
            wallet = canonical_wallet_address(wallet_address)
            if event_id is not None or _identity(db,"wallet",wallet) != terms["wallet_digest"]:
                raise WalletFundingError("La wallet de origen no coincide con el envío")
            if inspect_wallet_link(db,account_id,wallet,"0x38")["status"] != "UNLINKED":
                raise WalletFundingError("Wallet vinculada a ORBIS: registra el traslado interno y enlaza su asiento, sin nueva aportación")
            request = str(uuid5(UUID(identifier),"wallet-funding-contribution-v1"))
            event = append_event_in_transaction(db,account_id,request,EventInput("CONTRIBUTION","USDT",
                _instant(credit[2]["time_ms"]),amount=Decimal(credit[2]["amount"]),note="Ingreso wallet USDT/BSC acreditado por MEXC"))
            event_id = event.id
        else:
            event_id = str(UUID(event_id))
        event = _book_event(db,account_id,event_id,credit[2])
        used = db.query(RealMexcTransferMatch).filter_by(event_id=event_id).first()
        if used and (used.account_ref != funding.account_ref or used.direction != "deposit" or used.source_ref != credit[0].source_ref):
            raise WalletFundingError("El asiento ya representa otro traslado")
        if db.query(RealWalletFundingBooking).filter_by(event_id=event_id).first():
            raise WalletFundingError("El asiento ya representa otro ingreso")
        payload = {"funding_id":identifier,"credit_digest":credit[0].payload_digest,
            "event_id":event_id,"event_digest":event.payload_digest,"mode":mode,"booked_at":_time(now).isoformat()}
        if outgoing is not None:
            payload.update(source_account_id=outgoing.account_id,source_event_id=outgoing.id,source_event_digest=outgoing.payload_digest)
        raw,digest = _encoded(payload)
        db.add(RealWalletFundingBooking(funding_id=identifier,event_id=event_id,payload_json=raw,payload_digest=digest))
        db.flush()
        result = settlement_status(db,identifier)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def booking_candidates(db: Session, identifier: str, account_id: str) -> list[dict]:
    funding,_,_ = funding_record(db,identifier)
    if funding.account_id != account_id:
        raise WalletFundingError("El ingreso pertenece a otra cuenta")
    credit = credit_record(db,identifier)
    if credit is None:
        return []
    candidates = []
    for event in list_events(db,account_id):
        try:
            row = _book_event(db,account_id,event.id,credit[2])
        except PortfolioError:
            continue
        used = db.query(RealWalletFundingBooking).filter_by(event_id=row.id).first()
        linked = db.query(RealMexcTransferMatch).filter_by(event_id=row.id).first()
        if (used is not None or linked is not None and (linked.account_ref != funding.account_ref
                or linked.direction != "deposit" or linked.source_ref != credit[0].source_ref)):
            continue
        candidates.append({"id":row.id,"kind":row.kind,"amount":str(row.gross_amount),"occurred_at":row.occurred_at.isoformat()})
    return candidates


def _wallet_owner(db: Session,address: str) -> RealAccount | None:
    digest = hmac.new(_salt(db),f"0x38:{address}".encode("ascii"),hashlib.sha256).hexdigest()
    link = db.query(RealWalletLink).filter_by(chain_id="0x38",address_digest=digest).one_or_none()
    if link is None:
        return None
    owner = db.get(RealAccount,link.account_id)
    if owner is None:
        raise WalletFundingError("El vínculo wallet perdió su cuenta REAL")
    return owner


def booking_options(db: Session,identifier: str,account_id: str,wallet_address: str | None) -> dict:
    """Vista previa local exacta. No crea asientos ni concede permiso de gasto."""
    try:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        funding,terms,_ = funding_record(db,identifier)
        if funding.account_id != account_id:
            raise WalletFundingError("El ingreso pertenece a otra cuenta")
        credit = credit_record(db,identifier)
        if credit is None:
            raise WalletFundingError("Confirma primero la acreditación en MEXC")
        result = {"events":booking_candidates(db,identifier,account_id),"external_available":False,
                  "internal":None,"booked":bool(booking_record(db,identifier)),
                  "reason":"Conecta y verifica la wallet de origen para registrar un traslado nuevo"}
        if result["booked"]:
            result["reason"] = "Este ingreso ya está contabilizado"
            return result
        if wallet_address is None:
            return result
        address = canonical_wallet_address(wallet_address)
        if _identity(db,"wallet",address) != terms["wallet_digest"]:
            raise WalletFundingError("La wallet de origen no coincide con el envío")
        owner = _wallet_owner(db,address)
        if owner is None:
            result.update(external_available=True,reason=None)
            return result
        if owner.id == account_id or db.query(RealMexcAccountLink).filter_by(account_id=owner.id).first():
            result["reason"] = "La wallet necesita una cuenta REAL separada del exchange"
            return result
        if result["events"]:
            result["reason"] = "Ya hay asientos compatibles: selecciona el existente, sin duplicar el traslado"
            return result
        verify_account_event_digests(db,owner.id)
        revision = account_book_revision(db,owner.id)
        try:
            preview = transfer_cash_in_transaction(db,owner.id,account_id,str(uuid5(UUID(identifier),"wallet-funding-internal-v1")),
                currency="USDT",amount=Decimal(credit[2]["amount"]),occurred_at=_instant(credit[2]["time_ms"]),
                note="Traslado wallet a MEXC USDT/BSC acreditado",dry_run=True)
        except PortfolioError:
            result["reason"] = "Revisa el saldo USDT y el historial de la cuenta wallet; no se convierten otros activos ni divisas"
            return result
        result.update(internal={"source_account_id":owner.id,"name":owner.name,"source_revision":revision,
            "available_at_time":preview["available_at_time"],"remaining_at_time":preview["remaining_at_time"],
            "amount":credit[2]["amount"]},reason=None)
        return result
    finally:
        db.rollback()
