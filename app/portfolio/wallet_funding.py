"""Registro local idempotente de solicitudes de transferencia BEP-20.

No posee claves, no transmite a RPC y no declara un depósito acreditado.
Direcciones y hashes de tx se reciben transitoriamente; sólo se guardan HMAC
con la sal secreta de la bóveda. El contrato del token es metadato público.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import hmac
import json
import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.portfolio.domain import MAX_INPUT_DIGITS, PortfolioError
from app.portfolio.mexc_transfers import BNB_NETWORKS, read_transfer_snapshot
from app.portfolio.models import RealMexcAccountLink, RealWalletFundingIntent
from app.portfolio.wallet_links import _salt
from app.portfolio.wallet_proof import canonical_wallet_address


CHAIN_ID = "0x38"
NETWORK = "BNB_SMART_CHAIN_MAINNET"
STATES = {"PREPARED", "WALLET_REQUESTED", "HASH_REPORTED", "WALLET_REJECTED", "CHAIN_CLOSED"}


class WalletFundingError(PortfolioError):
    pass


def _encoded(value: dict) -> tuple[str, str]:
    raw = json.dumps(value,sort_keys=True,separators=(",", ":"),allow_nan=False,ensure_ascii=True)
    return raw, hashlib.sha256(raw.encode()).hexdigest()


def _decoded(raw: str, digest: str) -> dict:
    try:
        result = json.loads(raw)
        if not isinstance(result,dict) or _encoded(result) != (raw,digest):
            raise ValueError
        return result
    except (ValueError,TypeError):
        raise WalletFundingError("El registro de envío cambió o no es legible") from None


def _time(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise WalletFundingError("Reloj de envío no válido")
    return now.astimezone(timezone.utc)


def _uint(value: str, name: str, *, maximum: int = 2**256-1, positive: bool = True) -> int:
    if not isinstance(value,str) or not re.fullmatch(r"0|[1-9][0-9]{0,77}",value):
        raise WalletFundingError(f"{name}: entero exacto no válido")
    number = int(value)
    if number > maximum or positive and number == 0:
        raise WalletFundingError(f"{name}: fuera de rango")
    return number


def exact_units(value: str, decimals: int, *, allow_zero: bool = False) -> int:
    if type(decimals) is not int or not 0 <= decimals <= 36 or not isinstance(value,str) or len(value) > 100:
        raise WalletFundingError("Importe o decimales no válidos")
    try:
        amount = Decimal(value)
        if (not amount.is_finite() or amount < 0 or (amount == 0 and not allow_zero)
                or len(amount.as_tuple().digits) > MAX_INPUT_DIGITS
                or abs(amount.as_tuple().exponent) > 100):
            raise ValueError
        with localcontext() as context:
            context.prec = 150
            units = amount * 10**decimals
            if units != units.to_integral_value() or units >= 2**256:
                raise ValueError
            return int(units)
    except (ValueError,InvalidOperation):
        raise WalletFundingError("El importe no cabe exactamente en las unidades del token") from None


def _identity(db: Session, kind: str, value: str) -> str:
    return hmac.new(_salt(db),f"wallet-funding-v1:{kind}:{CHAIN_ID}:{value.lower()}".encode(),hashlib.sha256).hexdigest()


def _verify_mexc_observation(db: Session, row, terms: dict, state: dict) -> None:
    proof = state["mexc_deposit"]
    if (not isinstance(proof, dict) or set(proof) != {"snapshot_id", "source_ref", "source_digest",
            "code", "observed_at", "tx_digest", "chain_source"}
            or proof["tx_digest"] != state["tx_digest"]
            or proof["chain_source"] not in ("WALLET_RPC_OBSERVED", "ORBIS_RPC_OBSERVED")
            or type(proof["code"]) is not int or proof["code"] == 5):
        raise ValueError("mexc observation terms")
    snapshot = read_transfer_snapshot(db, str(UUID(proof["snapshot_id"])))
    sources = [item for item in snapshot["deposits"] if item.get("source_ref") == proof["source_ref"]]
    if (snapshot["account_ref"] != row.account_ref or not snapshot["identity_verified"]
            or snapshot["observed_at"] != proof["observed_at"]
            or _time(datetime.fromisoformat(proof["observed_at"])) < row.created_at
            or len(sources) != 1):
        raise ValueError("mexc observation snapshot")
    source = sources[0]
    if (_encoded(source)[1] != proof["source_digest"] or source["status"] != proof["code"]
            or source["network"] not in BNB_NETWORKS or source["asset"] != terms["asset"]
            or source.get("token_contract") != terms["token_contract"] or not source.get("destination_ref")
            or Decimal(source["fee"]) != 0
            or exact_units(source["amount"], terms["decimals"]) != int(terms["amount_units"])):
        raise ValueError("mexc observation deposit")


def observed_mexc_deposit(state: dict) -> dict:
    proof = state.get("mexc_deposit")
    if proof is None:
        return {}
    return {"mexc_deposit": {key: proof[key] for key in ("code", "observed_at", "chain_source")}}


def funding_record(db: Session, identifier: str):
    row = db.get(RealWalletFundingIntent,str(UUID(identifier)))
    if row is None:
        raise WalletFundingError("Solicitud de envío no encontrada")
    terms, state = _decoded(row.payload_json,row.payload_digest), _decoded(row.state_json,row.state_digest)
    if (terms.get("id") != row.id or terms.get("account_id") != row.account_id
            or terms.get("account_ref") != row.account_ref or terms.get("wallet_digest") != row.wallet_digest
            or terms.get("chain_id") != CHAIN_ID or terms.get("asset") != "USDT"
            or row.status not in STATES or state.get("status") != row.status):
        raise WalletFundingError("El ámbito de la solicitud cambió")
    try:
        state_keys = set(state)
        if row.status == "HASH_REPORTED":
            state_keys.discard("mexc_deposit")
        if terms.keys() - {"minimum_deposit_units"} != {"id","account_id","account_ref","chain_id","asset","wallet_digest",
                "destination_digest","token_contract","amount_units","decimals","nonce","gas",
                "gas_price","max_gas_wei","expires_at"} or state_keys != (
                    {"status","tx_digest","history","chain_close"} if row.status == "CHAIN_CLOSED" else
                    {"status","tx_digest","history"}):
            raise ValueError
        for key in ("account_ref","wallet_digest","destination_digest"):
            if not isinstance(terms[key],str) or not re.fullmatch(r"[0-9a-f]{64}",terms[key]):
                raise ValueError
        if (not re.fullmatch(r"0x[0-9a-f]{40}",terms["token_contract"]) or int(terms["token_contract"][2:],16) == 0
                or type(terms["decimals"]) is not int or not 0 <= terms["decimals"] <= 36):
            raise ValueError
        amount = _uint(terms["amount_units"],"Unidades")
        if ("minimum_deposit_units" in terms
                and _uint(terms["minimum_deposit_units"],"Mínimo MEXC",positive=False) > amount):
            raise ValueError
        _uint(terms["nonce"],"Nonce",maximum=2**64-1,positive=False)
        gas = _uint(terms["gas"],"Gas",maximum=2_000_000)
        price = _uint(terms["gas_price"],"Gas price",maximum=10**18)
        if gas*price > _uint(terms["max_gas_wei"],"Límite de gas"):
            raise ValueError
        history = state["history"]
        expected = ["PREPARED"] + (["WALLET_REQUESTED"] if row.status != "PREPARED" else [])
        if row.status == "CHAIN_CLOSED" and state["tx_digest"] is not None:
            expected.append("HASH_REPORTED")
        if row.status in ("HASH_REPORTED","WALLET_REJECTED","CHAIN_CLOSED"):
            expected.append(row.status)
        if (not isinstance(history,list) or [item["status"] for item in history] != expected
                or any(set(item) != {"status","at"} for item in history)):
            raise ValueError
        stamps = [_time(datetime.fromisoformat(item["at"])) for item in history]
        if (stamps != sorted(stamps) or stamps[0] != row.created_at or stamps[-1] != row.updated_at
                or datetime.fromisoformat(terms["expires_at"]) != row.created_at+timedelta(minutes=2)
                or (("HASH_REPORTED" in expected) != (state["tx_digest"] is not None))
                or (state["tx_digest"] is not None and
                    not re.fullmatch(r"[0-9a-f]{64}",state["tx_digest"]))):
            raise ValueError
        if row.status == "CHAIN_CLOSED":
            validate_chain_close(state["chain_close"],terms,state["tx_digest"],stamps[1],row.updated_at)
        if "mexc_deposit" in state:
            _verify_mexc_observation(db, row, terms, state)
    except (ValueError,TypeError,KeyError):
        raise WalletFundingError("La evidencia del envío no es coherente") from None
    return row, terms, state


def validate_chain_close(proof, terms, original_hash, claimed_at, checked_at) -> None:
    if (not isinstance(proof,dict) or set(proof) != {"kind","tx_digest","block_number","block_hash","block_at",
            "finalized_number","finalized_hash","gas_used","gas_price","gas_fee_wei","evidence_digest"}
            or proof["kind"] not in ("CANCELLED","REVERTED")
            or any(not re.fullmatch(r"[0-9a-f]{64}",proof[key]) for key in ("tx_digest","evidence_digest"))
            or any(not re.fullmatch(r"0x[0-9a-f]{64}",proof[key]) for key in ("block_hash","finalized_hash"))):
        raise ValueError("funding closure")
    height = _uint(proof["block_number"],"Bloque",maximum=2**64-1)
    final = _uint(proof["finalized_number"],"Bloque finalizado",maximum=2**64-1)
    stamp = _time(datetime.fromisoformat(proof["block_at"]))
    gas, price = _uint(proof["gas_used"],"Gas",maximum=2_000_000), _uint(proof["gas_price"],"Gas price",maximum=10**18)
    if (final < height or final == height and proof["finalized_hash"] != proof["block_hash"]
            or not claimed_at-timedelta(seconds=15) <= stamp <= checked_at+timedelta(seconds=15)
            or proof["gas_fee_wei"] != str(gas*price)
            or (proof["kind"] == "CANCELLED" and gas != 21_000)
            or (proof["kind"] == "REVERTED" and (gas > int(terms["gas"]) or price != int(terms["gas_price"])
                or original_hash is not None and proof["tx_digest"] != original_hash))):
        raise ValueError("funding closure terms")


def _public(db: Session, row, terms, state, *, now: datetime) -> dict:
    from app.portfolio.wallet_funding_credit import settlement_status
    settlement = settlement_status(db,row.id)
    settlement["settlement_status"] = settlement.pop("status")
    if row.status == "CHAIN_CLOSED":
        proof = state["chain_close"]
        settlement.update(chain_close=proof["kind"],gas_fee_wei=proof["gas_fee_wei"])
    return {"id":row.id,"plan_digest":row.payload_digest,"status":row.status,
            "asset":terms["asset"],"chain_id":CHAIN_ID,"amount_units":terms["amount_units"],
            "minimum_deposit_units":terms.get("minimum_deposit_units"),
            "decimals":terms["decimals"],"gas_wei":str(int(terms["gas"])*int(terms["gas_price"])),
            "max_gas_wei":terms["max_gas_wei"],"expires_at":terms["expires_at"],
            "expired":now >= datetime.fromisoformat(terms["expires_at"]),
            "transaction_hash_reported":bool(state.get("tx_digest")),
            "created_at":row.created_at.isoformat(),**settlement,**observed_mexc_deposit(state)}


def _destination(destination: dict) -> tuple[str,str]:
    if (destination.get("network") != NETWORK or destination.get("asset") != "USDT"
            or destination.get("source") != "MEXC_SPOT_PRIVATE_READONLY"
            or destination.get("stored") is not False or destination.get("transfer_sent") is not False):
        raise WalletFundingError("Destino USDT/BNB Chain no verificable")
    contract = canonical_wallet_address(destination.get("token_contract"))
    target = canonical_wallet_address(destination.get("deposit_address"))
    if int(contract[2:],16) == 0 or int(target[2:],16) == 0:
        raise WalletFundingError("Contrato o destino nulo")
    return contract,target


def _fresh_destination(destination: dict, now: datetime) -> None:
    try:
        stamp = _time(datetime.fromisoformat(destination["observed_at"]))
        if not 0 <= (now-stamp).total_seconds() <= 30:
            raise ValueError
    except (ValueError,TypeError,KeyError):
        raise WalletFundingError("La consulta del destino caducó") from None


def prepare_funding(db: Session, *, identifier: str, account_id: str, account_ref: str,
                    wallet_address: str, destination: dict, amount: str, minimum_deposit_usdt: str, decimals: int,
                    nonce: str, gas: str, gas_price: str, max_gas_bnb: str, now: datetime) -> dict:
    """Guarda el plan antes de pedir firma; la petición canónica nunca lleva una dirección."""
    now, identifier = _time(now), str(UUID(identifier))
    wallet = canonical_wallet_address(wallet_address)
    contract, target = _destination(destination)
    _fresh_destination(destination,now)
    if wallet == target or contract in (wallet,target):
        raise WalletFundingError("Wallet, token y destino deben ser distintos")
    units, max_gas = exact_units(amount,decimals), exact_units(max_gas_bnb,18)
    minimum = exact_units(minimum_deposit_usdt,decimals,allow_zero=True)
    if units < minimum:
        raise WalletFundingError("El importe es inferior al mínimo de depósito indicado para MEXC")
    nonce_int = _uint(nonce,"Nonce",maximum=2**64-1,positive=False)
    gas_int = _uint(gas,"Gas",maximum=2_000_000)
    price = _uint(gas_price,"Precio de gas",maximum=10**18)
    if gas_int * price > max_gas:
        raise WalletFundingError("La comisión máxima de red supera el límite indicado")
    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    link = db.get(RealMexcAccountLink,account_ref)
    if link is None or link.account_id != account_id:
        raise WalletFundingError("MEXC no está vinculada con esta cuenta REAL")
    wallet_digest = _identity(db,"wallet",wallet)
    terms = {"id":identifier,"account_id":account_id,"account_ref":account_ref,"chain_id":CHAIN_ID,
             "asset":"USDT","wallet_digest":wallet_digest,"destination_digest":_identity(db,"destination",target),
             "token_contract":contract,"amount_units":str(units),"minimum_deposit_units":str(minimum),
             "decimals":decimals,"nonce":str(nonce_int),
             "gas":str(gas_int),"gas_price":str(price),"max_gas_wei":str(max_gas)}
    existing = db.get(RealWalletFundingIntent,identifier)
    if existing is not None:
        row, previous, state = funding_record(db,identifier)
        if {key:value for key,value in previous.items() if key not in ("expires_at",)} != terms:
            raise WalletFundingError("El ID de envío ya se usó con otros datos")
    else:
        terms["expires_at"] = (now+timedelta(minutes=2)).isoformat()
        raw,digest = _encoded(terms)
        state_raw,state_digest = _encoded({"status":"PREPARED","tx_digest":None,
                                          "history":[{"status":"PREPARED","at":now.isoformat()}]})
        row = RealWalletFundingIntent(id=identifier,account_id=account_id,account_ref=account_ref,
            wallet_digest=wallet_digest,payload_json=raw,payload_digest=digest,state_json=state_raw,
            state_digest=state_digest,status="PREPARED",created_at=now,updated_at=now)
        db.add(row)
        db.flush()
        previous,state = terms,_decoded(state_raw,state_digest)
    result = _public(db,row,previous,state,now=now)
    # Los datos de envío sólo están en esta respuesta no-cacheada.
    result["transaction"] = {"from":wallet,"to":contract,"value":"0x0","chainId":CHAIN_ID,
        "data":"0xa9059cbb"+target[2:].lower().zfill(64)+hex(units)[2:].zfill(64),
        "nonce":hex(nonce_int),"gas":hex(gas_int),"gasPrice":hex(price)}
    result["destination"] = target
    db.commit()
    return result


def claim_funding(db: Session, identifier: str, *, account_id: str, account_ref: str,
                  wallet_address: str, destination: dict, plan_digest: str, now: datetime) -> dict:
    """Una sola concesión durable: perder su respuesta no autoriza otro envío."""
    now = _time(now)
    contract,target = _destination(destination)
    _fresh_destination(destination,now)
    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    row,terms,state = funding_record(db,identifier)
    if (row.account_id != account_id or row.account_ref != account_ref or row.payload_digest != plan_digest
            or terms["wallet_digest"] != _identity(db,"wallet",canonical_wallet_address(wallet_address))
            or terms["destination_digest"] != _identity(db,"destination",target) or terms["token_contract"] != contract):
        raise WalletFundingError("El permiso no corresponde al plan, wallet o destino actual")
    if row.status != "PREPARED":
        result = _public(db,row,terms,state,now=now)
        return {**result,"claim_granted":False}
    if "minimum_deposit_units" not in terms:
        raise WalletFundingError("Prepara un nuevo ingreso con el mínimo indicado en MEXC")
    if now >= datetime.fromisoformat(terms["expires_at"]) or now < row.updated_at:
        raise WalletFundingError("El plan caducó o el reloj retrocedió; prepara otro envío")
    from app.portfolio.wallet_nonce_guard import assert_wallet_nonce_available
    try:
        assert_wallet_nonce_available(db, canonical_wallet_address(wallet_address), terms["nonce"])
    except PortfolioError as exc:
        raise WalletFundingError(str(exc)) from None
    unknown = db.scalar(select(RealWalletFundingIntent.id).where(
        RealWalletFundingIntent.wallet_digest == row.wallet_digest,
        RealWalletFundingIntent.status == "WALLET_REQUESTED").limit(1))
    if unknown is not None:
        raise WalletFundingError("Hay un envío de esta wallet sin resultado; revísalo antes de continuar")
    for prior in db.scalars(select(RealWalletFundingIntent).where(
            RealWalletFundingIntent.wallet_digest == row.wallet_digest,
            RealWalletFundingIntent.status == "HASH_REPORTED")).all():
        if funding_record(db,prior.id)[1]["nonce"] == terms["nonce"]:
            raise WalletFundingError("Este nonce ya se utilizó; revisa la actividad de la wallet")
    _transition(row,state,"WALLET_REQUESTED",now)
    db.commit()
    return {**_public(db,row,terms,state,now=now),"claim_granted":True}


def _transition(row, state: dict, status: str, now: datetime) -> None:
    if now < row.updated_at:
        raise WalletFundingError("El reloj del envío retrocedió")
    state["status"] = status
    state["history"].append({"status":status,"at":now.isoformat()})
    row.state_json,row.state_digest = _encoded(state)
    row.status,row.updated_at = status,now


def report_funding(db: Session, identifier: str, *, account_id: str, tx_hash: str | None,
                   rejected: bool = False, now: datetime) -> dict:
    """Informe de la wallet, no prueba on-chain ni aportación al libro."""
    now = _time(now)
    if tx_hash is not None and (not isinstance(tx_hash,str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}",tx_hash)):
        raise WalletFundingError("Hash de transacción no válido")
    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    row,terms,state = funding_record(db,identifier)
    if row.account_id != account_id:
        raise WalletFundingError("El envío no pertenece a esta cuenta")
    _apply_funding_report(db,row,state,tx_hash,rejected=rejected,now=now)
    db.commit()
    return _public(db,row,terms,state,now=now)


def _apply_funding_report(db, row, state, tx_hash, *, rejected=False, now):
    digest = _identity(db,"tx",tx_hash) if tx_hash is not None else None
    status = "WALLET_REJECTED" if rejected and tx_hash is None else "HASH_REPORTED" if digest else None
    if status is None or row.status not in ("WALLET_REQUESTED",status):
        raise WalletFundingError("No se puede cambiar el resultado de este envío")
    if row.status == status:
        if state.get("tx_digest") != digest:
            raise WalletFundingError("La misma solicitud informa otro hash")
    else:
        state["tx_digest"] = digest
        _transition(row,state,status,now)


def list_funding(db: Session, account_id: str, *, now: datetime, focus_id: str | None = None) -> list[dict]:
    query = select(RealWalletFundingIntent).where(RealWalletFundingIntent.account_id == account_id)
    if focus_id is not None:
        query = query.order_by((RealWalletFundingIntent.id == focus_id).desc())
    rows = db.scalars(query.order_by(RealWalletFundingIntent.created_at.desc(),
                                    RealWalletFundingIntent.id.desc()).limit(100)).all()
    return [_public(db,row,*funding_record(db,row.id)[1:],now=_time(now)) for row in rows]
