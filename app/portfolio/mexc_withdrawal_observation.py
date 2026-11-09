"""Capturas View de retiradas propias: identidades HMAC y evolución auditable."""

from datetime import datetime,timedelta,timezone
import hashlib
import hmac
import re
from uuid import UUID,uuid4

from sqlalchemy.orm import Session

from app.live_execution.mexc_uid_guard import MexcTransferRecoveryView
from app.portfolio.models import RealMexcWithdrawalObservation,RealMexcUidLink
from app.portfolio.mexc_transfers import BNB_NETWORKS,_verified_uid_digest
from app.portfolio.mexc_withdrawal_journal import WithdrawalError,identity,withdrawal_record,_units
from app.portfolio.wallet_funding import _decoded,_encoded,_time,_uint
from app.portfolio.wallet_links import _salt
from app.portfolio.wallet_proof import canonical_wallet_address


def _hash(value) -> bool:
    return isinstance(value,str) and re.fullmatch(r"[0-9a-f]{64}",value) is not None


def instant(milliseconds: int) -> datetime:
    return datetime.fromtimestamp(milliseconds//1000,timezone.utc)+timedelta(milliseconds=milliseconds%1000)


def observation_record(db: Session,identifier: str) -> tuple[RealMexcWithdrawalObservation,dict]:
    row = db.get(RealMexcWithdrawalObservation,str(UUID(identifier)))
    if row is None:
        raise WithdrawalError("Observación de retirada no encontrada")
    data = _decoded(row.payload_json,row.payload_digest)
    plan,terms,state = withdrawal_record(db,row.withdrawal_id)
    try:
        expected = {"withdrawal_id","plan_digest","account_ref","uid_digest","source_ref","external_ref","request_ref",
                    "recipient_digest","chain_digest","asset","network","amount_units","fee_units","status","time_ms",
                    "transfer_type","observed_at","start_ms","end_ms","requests"}
        link = db.get(RealMexcUidLink,row.account_ref)
        if (set(data) != expected or data["withdrawal_id"] != plan.id or data["plan_digest"] != plan.payload_digest
                or row.account_ref != plan.account_ref or data["account_ref"] != row.account_ref
                or data["source_ref"] != row.source_ref or data["status"] != row.status
                or plan.status not in ("SUBMISSION_UNKNOWN","ACKNOWLEDGED")
                or link is None or data["uid_digest"] != link.uid_digest
                or any(not _hash(data[key]) for key in ("uid_digest","source_ref","external_ref"))
                or any(data[key] is not None and not _hash(data[key]) for key in ("request_ref","recipient_digest","chain_digest"))
                or not isinstance(data["asset"],str) or not re.fullmatch(r"[A-Z0-9]{2,24}",data["asset"])
                or data["network"] is not None and (not isinstance(data["network"],str) or not re.fullmatch(r"[A-Za-z0-9()._-]{1,40}",data["network"]))
                or type(data["status"]) is not int or not 0 <= data["status"] <= 20
                or data["transfer_type"] is not None and (type(data["transfer_type"]) is not int or data["transfer_type"] not in (0,1))
                or type(data["time_ms"]) is not int or type(data["start_ms"]) is not int or type(data["end_ms"]) is not int
                or not 0 < data["start_ms"] <= data["time_ms"] <= data["end_ms"]
                or data["end_ms"]-data["start_ms"] > 90*86400000
                or type(data["requests"]) is not int or not 2 <= data["requests"] <= 128
                or _time(datetime.fromisoformat(data["observed_at"])) != row.observed_at
                or instant(data["time_ms"]) < datetime.fromisoformat(state["history"][1]["at"])-timedelta(seconds=15)
                or instant(data["time_ms"]) > row.observed_at+timedelta(seconds=15)):
            raise ValueError
        _uint(data["amount_units"],"Importe MEXC")
        _uint(data["fee_units"],"Comisión MEXC",positive=False)
        request_match = data["request_ref"] == identity(db,"request",UUID(plan.id).hex)
        acknowledged_match = state["external_id_digest"] is not None and data["external_ref"] == state["external_id_digest"]
        if not request_match and not acknowledged_match:
            raise ValueError
    except (KeyError,ValueError,TypeError,OverflowError):
        raise WithdrawalError("La observación de retirada cambió o perdió su vínculo View") from None
    return row,data


def consistent_source(db: Session,identifier: str,*,require_success: bool = False,check_history: bool = True) -> tuple[RealMexcWithdrawalObservation,dict]:
    row,data = observation_record(db,identifier)
    plan,terms,state = withdrawal_record(db,row.withdrawal_id)
    all_rows = (db.query(RealMexcWithdrawalObservation).filter_by(withdrawal_id=row.withdrawal_id).order_by(
        RealMexcWithdrawalObservation.observed_at,RealMexcWithdrawalObservation.id).all() if check_history else [row])
    terminal = None
    previous_at = previous_status = None
    for item in all_rows:
        item,facts = observation_record(db,item.id)
        if (facts["asset"] != "USDT" or facts["network"] is not None and facts["network"] not in BNB_NETWORKS
                or facts["request_ref"] is not None and facts["request_ref"] != identity(db,"request",UUID(plan.id).hex)
                or state["external_id_digest"] is not None and facts["external_ref"] != state["external_id_digest"]
                or facts["recipient_digest"] != terms["recipient_digest"] or facts["transfer_type"] != 0
                or any(facts[key] != data[key] for key in ("source_ref","external_ref","time_ms"))
                or previous_at == item.observed_at and previous_status != facts["status"]):
            raise WithdrawalError("MEXC informa identidad, red, destino o estados contradictorios de la retirada")
        if terminal is not None:
            if (facts["status"] != terminal["status"] or any(facts[key] != terminal[key] for key in ("amount_units","fee_units"))
                    or terminal["chain_digest"] is not None and facts["chain_digest"] is not None and facts["chain_digest"] != terminal["chain_digest"]):
                raise WithdrawalError("El estado final, importe, comisión o hash de retirada cambió")
            if terminal["chain_digest"] is None and facts["chain_digest"] is not None:
                terminal = {**terminal,"chain_digest":facts["chain_digest"]}
        if facts["status"] in (7,8,9) and terminal is None:
            terminal = facts
        previous_at,previous_status = item.observed_at,facts["status"]
    if require_success and (data["status"] != 7 or data["chain_digest"] is None or previous_status != 7):
        raise WithdrawalError("MEXC aún no confirma una retirada externa correcta con hash")
    return row,data


def has_chain_observation(db: Session,identifier: str) -> bool:
    return any(observation_record(db,row.id)[1]["chain_digest"] is not None
               for row in db.query(RealMexcWithdrawalObservation).filter_by(withdrawal_id=identifier).all())


def save_withdrawal_observation(db: Session,identifier: str,account_id: str,account_ref: str,
                                snapshot: dict,uid: str,*,now: datetime,
                                recovery: MexcTransferRecoveryView | None = None) -> tuple[dict,str | None]:
    """Devuelve hash sólo transitorio; no inventa que ausencia equivalga a rechazo."""
    now = _time(now)
    try:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        plan,terms,state = withdrawal_record(db,identifier)
        if plan.account_id != account_id or plan.account_ref != account_ref or plan.status not in ("SUBMISSION_UNKNOWN","ACKNOWLEDGED"):
            raise WithdrawalError("La solicitud no corresponde a esta cuenta o no se intentó")
        if recovery is not None:
            if (recovery.account_id, recovery.archive_ref) != (account_id, account_ref):
                raise WithdrawalError("La recuperación no corresponde a esta retirada")
            recovery.verify(db, uid)
        uid_digest = _verified_uid_digest(db,account_ref,uid)
        if uid_digest is None:
            raise WithdrawalError("Identidad View de la retirada no verificada")
        stamp = _time(datetime.fromisoformat(snapshot["observed_at"]))
        rows = snapshot["withdrawals"]
        if (snapshot.get("source") != "MEXC_SPOT_PRIVATE_READONLY" or snapshot.get("coverage") != "RECENT_WINDOW_BELOW_PAGE_LIMIT"
                or not 0 <= (now-stamp).total_seconds() <= 30 or not isinstance(rows,list) or len(rows)>10000):
            raise WithdrawalError("Captura View de retirada incompleta o caducada")
        candidates = []
        for raw in rows:
            source_id,request_id = raw.get("source_identity"),raw.get("request_identity")
            if not isinstance(source_id,str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}",source_id):
                if request_id == UUID(identifier).hex:
                    raise WithdrawalError("MEXC no identifica la retirada propia")
                continue
            external = identity(db,"external-id",source_id)
            if (isinstance(request_id,str) and request_id.lower() == UUID(identifier).hex
                    or state["external_id_digest"] is not None and external == state["external_id_digest"]):
                candidates.append(raw)
        if not candidates:
            db.rollback()
            return {"status":"NOT_FOUND_IN_RECENT_VIEW","wallet_received":False,"booked":False},None
        if len(candidates) != 1:
            raise WithdrawalError("MEXC devuelve más de una coincidencia para la solicitud")
        raw = candidates[0]
        source_id,request_id = raw["source_identity"],raw.get("request_identity")
        recipient = raw.get("destination_identity")
        recipient_digest = identity(db,"recipient",canonical_wallet_address(recipient)) if recipient else None
        chain_hash = raw.get("chain_identity")
        if chain_hash is not None and (not isinstance(chain_hash,str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}",chain_hash)):
            raise WithdrawalError("Hash MEXC de retirada inválido")
        source_ref = hmac.new(_salt(db),f"mexc-transfer-v1:{account_ref}:withdrawal:{source_id}".encode("ascii"),hashlib.sha256).hexdigest()
        if db.query(RealMexcWithdrawalObservation).filter(RealMexcWithdrawalObservation.account_ref == account_ref,
                RealMexcWithdrawalObservation.source_ref == source_ref,
                RealMexcWithdrawalObservation.withdrawal_id != identifier).first():
            raise WithdrawalError("La retirada externa ya se asoció a otra solicitud")
        for previous in db.query(RealMexcWithdrawalObservation).filter(RealMexcWithdrawalObservation.account_ref == account_ref,
                RealMexcWithdrawalObservation.withdrawal_id != identifier).all():
            if observation_record(db,previous.id)[1]["external_ref"] == identity(db,"external-id",source_id):
                raise WithdrawalError("La referencia externa ya corresponde a otra solicitud")
        data = {"withdrawal_id":identifier,"plan_digest":plan.payload_digest,"account_ref":account_ref,"uid_digest":uid_digest,
            "source_ref":source_ref,"external_ref":identity(db,"external-id",source_id),
            "request_ref":identity(db,"request",request_id) if request_id else None,"recipient_digest":recipient_digest,
            "chain_digest":identity(db,"tx",chain_hash) if chain_hash else None,"asset":raw["asset"],"network":raw["network"],
            "amount_units":str(_units(raw["amount"],terms["decimals"])),"fee_units":str(_units(raw["fee"],terms["decimals"])),
            "status":raw["status"],"time_ms":raw["time_ms"],"transfer_type":raw.get("transfer_type"),
            "observed_at":stamp.isoformat(),"start_ms":snapshot["start_ms"],"end_ms":snapshot["end_ms"],"requests":snapshot.get("requests",2)}
        raw_json,digest = _encoded(data)
        existing = db.query(RealMexcWithdrawalObservation).filter_by(withdrawal_id=identifier,payload_digest=digest).first()
        observation_id = existing.id if existing else str(uuid4())
        if existing is None:
            db.add(RealMexcWithdrawalObservation(id=observation_id,withdrawal_id=identifier,account_ref=account_ref,
                source_ref=source_ref,status=data["status"],observed_at=stamp,payload_json=raw_json,payload_digest=digest))
        db.flush()
        observation_record(db,observation_id)
        try:
            consistent_source(db,observation_id)
        except WithdrawalError:
            from app.live_execution.journal import _stop_spot_account_in_transaction
            _stop_spot_account_in_transaction(db,account_ref,now)
            db.commit()  # Conservar el conflicto y volver a detener la operativa, sin borrar hechos.
            raise
        db.commit()
        return {"status":"MEXC_SUCCESS_PENDING_RECEIPT" if data["status"] == 7 else
            "MEXC_REJECTED_NEEDS_REVIEW" if data["status"] in (8,9) else "MEXC_PENDING",
            "observation_id":observation_id,"mexc_status":data["status"],"wallet_received":False,"booked":False,
            "network_reported_by_mexc":data["network"],"token_contract":terms["contract"]},chain_hash
    except Exception:
        db.rollback()
        raise
