"""Plan y permiso durable de retirada; nunca transmite ni modifica el libro."""

from datetime import datetime,timedelta
from decimal import Decimal,localcontext
import hashlib
import hmac
from math import lcm
import re
from uuid import UUID

from sqlalchemy.orm import Session

from app.portfolio.domain import MAX_INPUT_DIGITS,PortfolioError,decimal_value
from app.portfolio.models import (
    RealLiveStop, RealLiveSpotIntent, RealMexcAccountLink, RealMexcWithdrawal, RealSpotBot, RealWalletIdentitySalt,
)
from app.portfolio.repository import account_book_revision,account_snapshot
from app.portfolio.wallet_funding import _decoded,_encoded,_identity,_time,_uint,exact_units
from app.portfolio.wallet_links import _salt
from app.portfolio.wallet_proof import canonical_wallet_address


class WithdrawalError(PortfolioError):
    pass


STATES = {"PREPARED","CANCELLED","SUBMISSION_UNKNOWN","ACKNOWLEDGED"}


def identity(db: Session, kind: str, value: str) -> str:
    return hmac.new(_salt(db),f"mexc-withdrawal-v1:{kind}:0x38:{value.lower()}".encode(),hashlib.sha256).hexdigest()


def _roundtrip_binding(db: Session, terms: dict, funding_digest: str) -> str:
    salt = db.get(RealWalletIdentitySalt, 1)
    if salt is None:
        raise ValueError("Falta la identidad de la bóveda")
    scope = ":".join((terms["id"], terms["account_ref"], terms["recipient_digest"], funding_digest))
    return hmac.new(bytes.fromhex(salt.salt_hex), f"mexc-withdrawal-v1:roundtrip-wallet:0x38:{scope}".encode(),
                    hashlib.sha256).hexdigest()


def _verify_roundtrip_wallet(db: Session, terms: dict) -> None:
    entry = terms["roundtrip_wallet"]
    if (not isinstance(entry, dict) or set(entry) != {"funding_digest", "binding"}
            or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in entry.values())
            or not hmac.compare_digest(entry["binding"], _roundtrip_binding(db, terms, entry["funding_digest"]))):
        raise ValueError("Huella de regreso no verificable")


def recipient_matches(db: Session, terms: dict, address: str) -> bool:
    wallet = canonical_wallet_address(address)
    if not hmac.compare_digest(terms["recipient_digest"], identity(db, "recipient", wallet)):
        return False
    proof = terms.get("roundtrip_wallet")
    return proof is None or hmac.compare_digest(proof["funding_digest"], _identity(db, "wallet", wallet))



def amount_text(units: int,decimals: int) -> str:
    with localcontext() as context:
        context.prec = 300
        return format(Decimal(units).scaleb(-decimals),"f")


def route_terms(route: dict) -> dict:
    if (route.get("source") != "MEXC_SPOT_PRIVATE_READONLY" or route.get("chain_id") != "0x38"
            or route.get("asset") != "USDT" or route.get("network") != "BSC" or route.get("recipient_listed") is not True):
        raise WithdrawalError("Ruta USDT/BSC y destino MEXC no verificables")
    contract = canonical_wallet_address(route.get("token_contract"))
    if int(contract[2:],16) == 0:
        raise WithdrawalError("Contrato USDT/BSC nulo")
    return {"contract":contract,**{key:route[key] for key in ("fee","minimum","maximum","increment")}}


def _units(raw: str,decimals: int) -> int:
    return 0 if raw in ("0","0.0") or isinstance(raw,str) and re.fullmatch(r"0(?:\.0+)?",raw) else exact_units(raw,decimals)


def _route_for_wallet(route: dict, address: str) -> dict:
    terms = route_terms(route)
    if address == terms["contract"] or int(address[2:],16) == 0:
        raise WithdrawalError("El destino no puede ser el contrato del token")
    return terms


def _route_units(route: dict, decimals: int, max_fee: str | None) -> tuple[int, int, int, int, int]:
    fee = _units(route["fee"],decimals)
    limit = fee if max_fee is None else _units(max_fee,decimals)
    lower,upper = _units(route["minimum"],decimals),exact_units(route["maximum"],decimals)
    step = 1 if route["increment"] is None else exact_units(route["increment"],decimals)
    return fee,limit,lower,upper,step


def _largest_exact_multiple(capacity: int, step: int) -> int:
    # At each magnitude the amount must satisfy both the venue increment and
    # the decimal input limit. A coefficient with extra digits needs zeros.
    if capacity <= 0:
        return 0
    for digits in range(len(str(capacity)),0,-1):
        multiple = lcm(step,10**max(0,digits-MAX_INPUT_DIGITS))
        candidate = min(capacity,10**digits-1)//multiple*multiple
        if candidate >= 10**(digits-1):
            return candidate
    return 0


def _input_amount(units: int, decimals: int) -> str:
    with localcontext() as context:
        context.prec = 300
        value = Decimal(units).scaleb(-decimals).normalize()
        fixed = format(value,"f")
        return fixed if len(Decimal(fixed).as_tuple().digits) <= MAX_INPUT_DIGITS else str(value)


def _fresh(value: str,now: datetime) -> None:
    if not 0 <= (now-_time(datetime.fromisoformat(value))).total_seconds() <= 30:
        raise WithdrawalError("La lectura MEXC de retirada caducó")


def withdrawal_record(db: Session,identifier: str):
    row = db.get(RealMexcWithdrawal,str(UUID(identifier)))
    if row is None:
        raise WithdrawalError("Plan de retirada no encontrado")
    terms,state = _decoded(row.payload_json,row.payload_digest),_decoded(row.state_json,row.state_digest)
    try:
        expected_keys = {"id","account_id","account_ref","asset","chain_id","network","consent_version",
                         "recipient_digest","contract","decimals","amount_units","fee_units","max_fee_units",
                         "max_debit_units","min_receive_units","route_digest","book_revision","stop_at","expires_at"}
        if "roundtrip_wallet" in terms:
            expected_keys.add("roundtrip_wallet")
            _verify_roundtrip_wallet(db, terms)
        if (set(terms) != expected_keys
                or terms["id"] != row.id or terms["account_id"] != row.account_id or terms["account_ref"] != row.account_ref
                or terms["asset"] != "USDT" or terms["chain_id"] != "0x38" or terms["network"] != "BSC"
                or terms["consent_version"] != "MEXC_BSC_WITHDRAWAL_V1_FEE_QUOTE_ONLY"
                or row.status not in STATES or state.get("status") != row.status
                or set(state) != {"status","history","external_id_digest"}
                or type(terms["decimals"]) is not int or not 0 <= terms["decimals"] <= 36
                or canonical_wallet_address(terms["contract"]) != terms["contract"] or int(terms["contract"][2:],16) == 0):
            raise ValueError
        for key in ("account_ref","recipient_digest","route_digest","book_revision"):
            if not re.fullmatch(r"[0-9a-f]{64}",terms[key]):
                raise ValueError
        amount = _uint(terms["amount_units"],"Importe")
        fee = _uint(terms["fee_units"],"Comisión",positive=False)
        maximum = _uint(terms["max_fee_units"],"Límite de comisión",positive=False)
        if (fee > maximum or amount+maximum != _uint(terms["max_debit_units"],"Débito máximo")
                or amount-maximum != _uint(terms["min_receive_units"],"Recepción mínima")
                or datetime.fromisoformat(terms["expires_at"]) != row.created_at+timedelta(minutes=2)
                or _time(datetime.fromisoformat(terms["stop_at"])) > row.created_at):
            raise ValueError
        expected = ["PREPARED"] + (["CANCELLED"] if row.status == "CANCELLED" else
            ["SUBMISSION_UNKNOWN"]+(["ACKNOWLEDGED"] if row.status == "ACKNOWLEDGED" else []) if row.status != "PREPARED" else [])
        history = state["history"]
        stamps = [_time(datetime.fromisoformat(item["at"])) for item in history]
        if (any(set(item) != {"status","at"} for item in history) or [item["status"] for item in history] != expected
                or stamps != sorted(stamps) or stamps[0] != row.created_at or stamps[-1] != row.updated_at
                or row.status == "ACKNOWLEDGED" and not re.fullmatch(r"[0-9a-f]{64}",state["external_id_digest"] or "")
                or row.status != "ACKNOWLEDGED" and state["external_id_digest"] is not None):
            raise ValueError
    except (KeyError,ValueError,TypeError,IndexError):
        raise WithdrawalError("El registro de retirada cambió o no es coherente") from None
    return row,terms,state


def pending_withdrawals(db: Session,account_ref: str,now: datetime,*,excluding: str | None = None) -> bool:
    for row in db.query(RealMexcWithdrawal).filter_by(account_ref=account_ref).all():
        row,terms,_ = withdrawal_record(db,row.id)
        from app.portfolio.mexc_withdrawal_booking import booking_record
        if booking_record(db,row.id) is not None:
            continue
        if row.id != excluding and (row.status in ("SUBMISSION_UNKNOWN","ACKNOWLEDGED")
                or row.status == "PREPARED" and now < datetime.fromisoformat(terms["expires_at"])):
            return True
    return False


def assert_no_pending_withdrawals(db: Session,account_ref: str,now: datetime) -> None:
    if pending_withdrawals(db,account_ref,now):
        raise WithdrawalError("Resuelve o cancela primero el plan de retirada; no se puede reabrir la operativa")


def _scope(db: Session,account_id: str,account_ref: str,source: dict,now: datetime):
    link,stop = db.get(RealMexcAccountLink,account_ref),db.get(RealLiveStop,account_ref)
    if link is None or link.account_id != account_id or stop is None or not stop.stopped:
        raise WithdrawalError("Detén primero la operativa de esta cuenta MEXC")
    if (db.query(RealLiveSpotIntent).filter(RealLiveSpotIntent.account_ref == account_ref,
            RealLiveSpotIntent.status.in_(("RESERVED","SUBMISSION_UNKNOWN"))).first()
            or db.query(RealSpotBot).filter_by(account_ref=account_ref,status="ACTIVE").first()):
        raise WithdrawalError("Hay órdenes o bots pendientes de revisión antes de retirar")
    _fresh(source["observed_at"],now)
    _fresh(source["route"]["observed_at"],now)
    balances = source["balances"]
    if any(not isinstance(balances.get(key),str) or len(balances[key]) > 100 for key in ("free","locked")):
        raise WithdrawalError("Saldo MEXC no decimal")
    free,locked = Decimal(balances["free"]),Decimal(balances["locked"])
    decimal_value(free)
    decimal_value(locked)
    if free < 0 or locked < 0:
        raise WithdrawalError("Saldo MEXC negativo")
    snapshot = account_snapshot(db,account_id)
    if locked != 0 or snapshot.cash.get("USDT",Decimal(0)) != free:
        raise WithdrawalError("Revisa el saldo USDT del libro y MEXC antes de retirar; no se usa capital sin registrar")
    return stop,free,account_book_revision(db,account_id)


def estimate_max_withdrawal(db: Session, *, account_id: str, account_ref: str, address: str,
                            decimals: int, max_fee: str | None, source: dict, now: datetime) -> dict:
    now = _time(now)
    _,free,_ = _scope(db,account_id,account_ref,source,now)
    route = _route_for_wallet(source["route"],canonical_wallet_address(address))
    fee,limit,lower,upper,step = _route_units(route,decimals,max_fee)
    if fee > limit:
        raise WithdrawalError("La comisión observada supera el filtro")
    if pending_withdrawals(db,account_ref,now):
        raise WithdrawalError("Ya existe una retirada o un plan pendiente en esta cuenta")
    with localcontext() as context:
        context.prec = 300
        capacity = min(int(free.scaleb(decimals)),2**256-1)
    quantity = _largest_exact_multiple(min(upper,capacity-limit),step)
    if quantity < lower or quantity <= limit:
        raise WithdrawalError("El saldo no cubre el mínimo y la comisión con el múltiplo de MEXC")
    return {"account_id":account_id,"asset":"USDT","chain_id":"0x38","token_contract":route["contract"],
            "amount":_input_amount(quantity,decimals),"max_fee":_input_amount(limit,decimals),
            "observed_fee":amount_text(fee,decimals),"max_debit":amount_text(quantity+limit,decimals),
            "min_receive":amount_text(quantity-limit,decimals),"fee_limit_exchange_enforced":False}


def public_record(db: Session,identifier: str,now: datetime) -> dict:
    row,terms,_ = withdrawal_record(db,identifier)
    from app.portfolio.mexc_withdrawal_booking import settlement_status
    return {"id":row.id,"account_id":row.account_id,"status":row.status,"plan_digest":row.payload_digest,
        "asset":"USDT","chain_id":"0x38","token_contract":terms["contract"],"amount":amount_text(int(terms["amount_units"]),terms["decimals"]),
        "observed_fee":amount_text(int(terms["fee_units"]),terms["decimals"]),
        "max_fee":amount_text(int(terms["max_fee_units"]),terms["decimals"]),
        "max_debit":amount_text(int(terms["max_debit_units"]),terms["decimals"]),
        "min_receive":amount_text(int(terms["min_receive_units"]),terms["decimals"]),
        "expires_at":terms["expires_at"],"expired":now >= datetime.fromisoformat(terms["expires_at"]),
        "created_at":row.created_at.isoformat(),"exchange_request_id":UUID(row.id).hex,
        "automatic_retry":False,"fee_limit_exchange_enforced":False,**settlement_status(db,identifier)}


def prepare_withdrawal(db: Session,*,identifier: str,account_id: str,account_ref: str,address: str,
                       decimals: int,amount: str,max_fee: str,source: dict,now: datetime) -> dict:
    now,identifier = _time(now),str(UUID(identifier))
    try:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        stop,free,revision = _scope(db,account_id,account_ref,source,now)
        wallet = canonical_wallet_address(address)
        route = _route_for_wallet(source["route"],wallet)
        quantity = exact_units(amount,decimals)
        fee,limit,lower,upper,step = _route_units(route,decimals,max_fee)
        if fee > limit or quantity <= limit or not lower <= quantity <= upper or quantity%step != 0:
            raise WithdrawalError("Importe, mínimo, máximo, múltiplo publicado o comisión fuera de tus límites")
        if Decimal(amount_text(quantity+limit,decimals)) > free or quantity+limit >= 2**256:
            raise WithdrawalError("Saldo USDT insuficiente para cubrir importe y comisión máxima por separado")
        previous = db.get(RealMexcWithdrawal,identifier)
        previous_terms = withdrawal_record(db, identifier)[1] if previous else None
        created = previous.created_at if previous else now
        terms = {"id":identifier,"account_id":account_id,"account_ref":account_ref,"asset":"USDT","chain_id":"0x38","network":"BSC",
            "consent_version":"MEXC_BSC_WITHDRAWAL_V1_FEE_QUOTE_ONLY","recipient_digest":identity(db,"recipient",wallet),
            "contract":route["contract"],"decimals":decimals,"amount_units":str(quantity),"fee_units":str(fee),
            "max_fee_units":str(limit),"max_debit_units":str(quantity+limit),"min_receive_units":str(quantity-limit),
            "route_digest":_encoded(route)[1],"book_revision":revision,"stop_at":stop.updated_at.isoformat(),
            "expires_at":(created+timedelta(minutes=2)).isoformat()}
        if previous_terms is None or "roundtrip_wallet" in previous_terms:
            funding_digest = _identity(db, "wallet", wallet)
            terms["roundtrip_wallet"] = {"funding_digest": funding_digest,
                                        "binding": _roundtrip_binding(db, terms, funding_digest)}
        raw,digest = _encoded(terms)
        if previous:
            if previous.payload_digest != digest:
                raise WithdrawalError("El ID de retirada ya tiene otros términos")
        else:
            if pending_withdrawals(db,account_ref,now):
                raise WithdrawalError("Ya existe una retirada o un plan pendiente en esta cuenta")
            state,state_digest = _encoded({"status":"PREPARED","external_id_digest":None,
                "history":[{"status":"PREPARED","at":now.isoformat()}]})
            db.add(RealMexcWithdrawal(id=identifier,account_id=account_id,account_ref=account_ref,status="PREPARED",
                payload_json=raw,payload_digest=digest,state_json=state,state_digest=state_digest,created_at=now,updated_at=now))
            db.flush()
        result = public_record(db,identifier,now)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def _transition(row,state,status,now):
    if now < row.updated_at:
        raise WithdrawalError("El reloj de retirada retrocedió")
    state["status"] = row.status = status
    state["history"].append({"status":status,"at":now.isoformat()})
    row.updated_at = now
    row.state_json,row.state_digest = _encoded(state)


def prepared_review_terms(db: Session, identifier: str, *, account_id: str, account_ref: str,
                          address: str, contract: str, decimals: int, plan_digest: str, now: datetime) -> dict:
    row, terms, _ = withdrawal_record(db, identifier)
    if (row.account_id != account_id or row.account_ref != account_ref or row.payload_digest != plan_digest
            or terms["contract"] != contract or terms["decimals"] != decimals
            or not recipient_matches(db, terms, address)):
        raise WithdrawalError("Cuenta, wallet, token o plan de retirada no coinciden")
    if row.status != "PREPARED" or _time(now) >= datetime.fromisoformat(terms["expires_at"]):
        raise WithdrawalError("El plan ya se intentó, se descartó o caducó; consulta el historial")
    return terms


def check_current_plan(db: Session, terms: dict, source: dict, now: datetime) -> None:
    stop, free, revision = _scope(db, terms["account_id"], terms["account_ref"], source, now)
    if (now >= datetime.fromisoformat(terms["expires_at"])
            or pending_withdrawals(db, terms["account_ref"], now, excluding=terms["id"])
            or stop.updated_at.isoformat() != terms["stop_at"] or revision != terms["book_revision"]
            or _encoded(route_terms(source["route"]))[1] != terms["route_digest"]
            or Decimal(amount_text(int(terms["max_debit_units"]), terms["decimals"])) > free):
        raise WithdrawalError("Plan caducado o cambiaron comisión, saldo, parada o ruta; prepara otro")


def claim_withdrawal(db: Session,identifier: str,*,account_id: str,account_ref: str,address: str,
                     plan_digest: str,source: dict,now: datetime) -> dict:
    now = _time(now)
    try:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        row,terms,state = withdrawal_record(db,identifier)
        if (row.account_id != account_id or row.account_ref != account_ref or row.payload_digest != plan_digest
                or not recipient_matches(db, terms, address)):
            raise WithdrawalError("Cuenta, destino o plan de retirada no coinciden")
        if row.status != "PREPARED":
            result = {**public_record(db,identifier,now),"claim_granted":False}
            db.commit()
            return result
        check_current_plan(db,terms,source,now)
        _transition(row,state,"SUBMISSION_UNKNOWN",now)
        db.flush()
        result = {**public_record(db,identifier,now),"claim_granted":True}
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def acknowledge_withdrawal(db: Session,identifier: str,external_id: str,*,now: datetime) -> dict:
    now = _time(now)
    try:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        row,_,state = withdrawal_record(db,identifier)
        if not isinstance(external_id,str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}",external_id):
            raise WithdrawalError("Referencia de retirada no verificable")
        digest = identity(db,"external-id",external_id)
        for other in db.query(RealMexcWithdrawal).filter_by(account_ref=row.account_ref,status="ACKNOWLEDGED").all():
            if other.id != identifier and withdrawal_record(db,other.id)[2]["external_id_digest"] == digest:
                raise WithdrawalError("MEXC devolvió la referencia de otra retirada")
        if row.status not in ("SUBMISSION_UNKNOWN","ACKNOWLEDGED") or row.status == "ACKNOWLEDGED" and state["external_id_digest"] != digest:
            raise WithdrawalError("Respuesta de retirada contradictoria")
        if row.status == "SUBMISSION_UNKNOWN":
            state["external_id_digest"] = digest
            _transition(row,state,"ACKNOWLEDGED",now)
        db.flush()
        result = public_record(db,identifier,now)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def cancel_prepared(db: Session,identifier: str,account_id: str,*,now: datetime) -> dict:
    now = _time(now)
    try:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        row,_,state = withdrawal_record(db,identifier)
        if row.account_id != account_id or row.status not in ("PREPARED","CANCELLED"):
            raise WithdrawalError("No se puede cancelar localmente una retirada ya intentada")
        if row.status == "PREPARED":
            _transition(row,state,"CANCELLED",now)
        db.flush()
        result = public_record(db,identifier,now)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
