"""Planes DEX BSC de un solo uso. No firma, no envía, no contabiliza saldos.

Registry deliberadamente cerrado: USDT Binance-Peg y WBNB, SwapRouter02 v3.
La transacción con destinatario sólo se construye en memoria. El diario cifrado
retiene importes y huellas HMAC, nunca direcciones de wallets ni txIds en claro.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import hmac
import re
from uuid import UUID

from eth_abi import encode
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.portfolio.domain import PortfolioError
from app.portfolio.models import RealAccount, RealDexCallResolution, RealWalletSwapIntent
from app.portfolio.wallet_funding import _encoded, _decoded, _time, _uint, exact_units
from app.portfolio.wallet_links import _salt
from app.portfolio.wallet_proof import canonical_wallet_address


# Registro V1 inmutable: otro despliegue/token exige otro contrato de datos.
PROTOCOL = "UNISWAP_V3_BSC_ROUTER02_V1"
ROUTER = "0xb971ef87ede563556b2ed4b1c0b0019111dd85d2"
QUOTER = "0x78d78e420da98ad378d7799be8f4af69033eb077"
TOKENS = {"USDT": "0x55d398326f99059ff775485246999027b3197955",
          "WBNB": "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"}
STATES = {"PREPARED", "WALLET_REQUESTED", "HASH_REPORTED", "WALLET_REJECTED", "FINALIZED", "REVERTED"}


class WalletSwapError(PortfolioError):
    pass


def _identity(db: Session, kind: str, value: str) -> str:
    return hmac.new(_salt(db), f"wallet-swap-v1:{kind}:0x38:{value.lower()}".encode(), hashlib.sha256).hexdigest()


def _validate(terms: dict) -> None:
    try:
        if set(terms) != {"id", "account_id", "wallet_digest", "chain_id", "action", "token_in", "amount_units",
                          "quote_units", "minimum_units", "fee", "slippage_bps", "nonce", "gas", "gas_price",
                          "max_gas_wei", "deadline", "protocol", "router", "token_in_contract", "token_out_contract"}:
            raise ValueError
        if (terms["protocol"] != PROTOCOL or terms["router"] != ROUTER
                or terms["token_in_contract"] != TOKENS[terms["token_in"]]
                or terms["token_out_contract"] != TOKENS["WBNB" if terms["token_in"] == "USDT" else "USDT"]):
            raise ValueError
        UUID(terms["id"]); UUID(terms["account_id"])
        if (terms["chain_id"] != "0x38" or terms["action"] not in {"SWAP", "APPROVE", "REVOKE"}
                or terms["token_in"] not in TOKENS or not re.fullmatch(r"[0-9a-f]{64}", terms["wallet_digest"])
                or type(terms["fee"]) is not int or terms["fee"] not in {100, 500, 3000, 10000}
                or type(terms["slippage_bps"]) is not int or not 1 <= terms["slippage_bps"] <= 500
                or type(terms["deadline"]) is not int or terms["deadline"] <= 0):
            raise ValueError
        amount = _uint(terms["amount_units"], "Cantidad")
        quote = _uint(terms["quote_units"], "Cotización")
        minimum = _uint(terms["minimum_units"], "Mínimo")
        if minimum != quote * (10000 - terms["slippage_bps"]) // 10000 or amount == 2**256 - 1:
            raise ValueError
        _uint(terms["nonce"], "Nonce", maximum=2**64-1, positive=False)
        gas = _uint(terms["gas"], "Gas", maximum=2_000_000)
        price = _uint(terms["gas_price"], "Gas price", maximum=10**18)
        if gas * price > _uint(terms["max_gas_wei"], "Límite de gas"):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise WalletSwapError("Plan DEX no válido") from None


def transaction(terms: dict, wallet: str) -> dict:
    """Compila el único método permitido; jamás acepta calldata del navegador."""
    _validate(terms)
    sender = canonical_wallet_address(wallet)
    token = terms["token_in_contract"]
    if terms["action"] == "SWAP":
        output = terms["token_out_contract"]
        inner = bytes.fromhex("04e45aaf") + encode(
            ["address", "address", "uint24", "address", "uint256", "uint256", "uint160"],
            [token, output, terms["fee"], sender, int(terms["amount_units"]), int(terms["minimum_units"]), 0])
        data = "0x5ae401dc" + encode(["uint256", "bytes[]"], [terms["deadline"], [inner]]).hex()
        target = terms["router"]
    else:
        approved = 0 if terms["action"] == "REVOKE" else int(terms["amount_units"])
        data = "0x095ea7b3" + encode(["address", "uint256"], [terms["router"], approved]).hex()
        target = token
    return {"from": sender, "to": target, "data": data, "chainId": "0x38", "value": "0x0", "type": "0x0",
            "nonce": hex(int(terms["nonce"])), "gas": hex(int(terms["gas"])), "gasPrice": hex(int(terms["gas_price"]))}


def swap_record(db: Session, identifier: str) -> tuple[RealWalletSwapIntent, dict, dict]:
    row = db.get(RealWalletSwapIntent, str(UUID(identifier)))
    if row is None:
        raise WalletSwapError("Acción DEX no encontrada")
    terms, state = _decoded(row.payload_json, row.payload_digest), _decoded(row.state_json, row.state_digest)
    _validate(terms)
    try:
        if (terms["id"] != row.id or terms["account_id"] != row.account_id or terms["wallet_digest"] != row.wallet_digest
                or row.status not in STATES or state["status"] != row.status
                or set(state) != {"status", "tx_digest", "history", "receipt"}
                or terms["deadline"] != int(row.created_at.timestamp()) + 120):
            raise ValueError
        expected = ["PREPARED"]
        if row.status != "PREPARED": expected += ["WALLET_REQUESTED"]
        if row.status in {"HASH_REPORTED", "FINALIZED", "REVERTED"}: expected += ["HASH_REPORTED"]
        if row.status not in {"PREPARED", "WALLET_REQUESTED", "HASH_REPORTED"}: expected += [row.status]
        history = state["history"]
        if ([entry["status"] for entry in history] != expected
                or any(set(entry) != {"status", "at"} for entry in history)):
            raise ValueError
        stamps = [_time(datetime.fromisoformat(item["at"])) for item in history]
        if stamps != sorted(stamps) or stamps[0] != row.created_at or stamps[-1] != row.updated_at:
            raise ValueError
        hashed = row.status in {"HASH_REPORTED", "FINALIZED", "REVERTED"}
        if (hashed and not re.fullmatch(r"[0-9a-f]{64}", state["tx_digest"])
                or not hashed and state["tx_digest"] is not None):
            raise ValueError
        if row.status not in {"FINALIZED", "REVERTED"} and state["receipt"] is not None:
            raise ValueError
        if row.status in {"FINALIZED", "REVERTED"}:
            receipt = state["receipt"]
            if (not isinstance(receipt, dict) or set(receipt) != {"status", "source", "block_number", "block_hash", "block_at",
                    "gas_used", "gas_price", "gas_fee_wei", "amount_out_units", "approved_units", "observation_digest"}
                    or receipt["status"] != row.status or receipt["source"] != "WALLET_RPC_OBSERVED"
                    or not re.fullmatch(r"0x[0-9a-f]{64}", receipt["block_hash"])
                    or not re.fullmatch(r"[0-9a-f]{64}", receipt["observation_digest"])):
                raise ValueError
            _uint(receipt["block_number"], "Bloque")
            stamp = _time(datetime.fromisoformat(receipt["block_at"]))
            gas, price = _uint(receipt["gas_used"], "Gas"), _uint(receipt["gas_price"], "Gas price")
            if (gas > int(terms["gas"]) or price != int(terms["gas_price"]) or receipt["gas_fee_wei"] != str(gas*price)
                    or stamp < row.created_at-timedelta(seconds=15) or stamp > row.updated_at+timedelta(seconds=15)):
                raise ValueError
            if row.status == "REVERTED":
                if receipt["amount_out_units"] is not None or receipt["approved_units"] is not None:
                    raise ValueError
            elif terms["action"] == "SWAP":
                if (_uint(receipt["amount_out_units"], "Salida") < int(terms["minimum_units"])
                        or receipt["approved_units"] is not None or int(stamp.timestamp()) > terms["deadline"]):
                    raise ValueError
            elif (receipt["amount_out_units"] is not None or receipt["approved_units"] !=
                    ("0" if terms["action"] == "REVOKE" else terms["amount_units"])):
                raise ValueError
    except (KeyError, TypeError, ValueError):
        raise WalletSwapError("El historial DEX no es coherente") from None
    return row, terms, state


def _public(row: RealWalletSwapIntent, terms: dict, state: dict, now: datetime, *, db: Session) -> dict:
    from app.portfolio.wallet_swap_booking import _status
    extra={};booked=_status(db,row.id)
    if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()>=71:
        from app.portfolio.dex_call_resolution import status as call_status
        resolved=call_status(db,'WALLET_SWAP',row.id)
        if resolved is not None:
            from app.portfolio.dex_receipt_booking import status as booking_status
            booked=booking_status(db,'CALL_RESOLUTION',resolved['id'])
            extra={'resolution':resolved,'effective_outcome':resolved['outcome'],
                'booking_source_kind':'CALL_RESOLUTION','booking_source_id':resolved['id']}
        elif db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()>=78:
            from app.portfolio.dex_external_outcomes import external_outcome,public_outcome
            from app.portfolio.economic_external_booking import booking_record,identifier as external_booking_id
            external=external_outcome(db,'WALLET_SWAP',row.id)
            if external is not None:
                record=booking_record(db,external_booking_id(external.observation_id))
                booked={'booked':True,'target_account_id':record[0].account_id,'events_created':0,
                    'target_account_name':db.get(RealAccount,record[0].account_id).name,
                    'events_count':len(record[1]['plan']['review']['selected_events']),'booked_at':record[1]['booked_at']}
                extra={'external_review':public_outcome(external),'effective_outcome':'EXTERNAL_REVIEWED',
                    'booking_source_kind':'EXTERNAL_OBSERVATION','booking_source_id':external.observation_id}
    return {"id": row.id, "plan_digest": row.payload_digest, "status": row.status,
            "chain_id": "0x38", "action": terms["action"], "token_in": terms["token_in"],
            "fee": terms["fee"],
            "amount_units": terms["amount_units"], "minimum_units": terms["minimum_units"],
            "gas_wei": str(int(terms["gas"]) * int(terms["gas_price"])),
            "expires_at": datetime.fromtimestamp(terms["deadline"], now.tzinfo).isoformat(),
            "expired": int(now.timestamp()) >= terms["deadline"], "created_at": row.created_at.isoformat(),
            "receipt": state["receipt"], "booking_supported": True, "automatic_execution": False, **booked,**extra}


def prepare_swap(db: Session, *, identifier: str, account_id: str, wallet_address: str, action: str,
                 token_in: str, amount: str, quote_units: str, fee: int, slippage_bps: int,
                 nonce: str, gas: str, gas_price: str, max_gas_bnb: str, now: datetime) -> dict:
    now, identifier = _time(now), str(UUID(identifier))
    wallet = canonical_wallet_address(wallet_address)
    if int(wallet, 16) == 0 or wallet in {*TOKENS.values(), ROUTER, QUOTER}:
        raise WalletSwapError("Wallet EOA no válida")
    units, max_gas = exact_units(amount, 18), exact_units(max_gas_bnb, 18)
    quote = _uint(quote_units, "Cotización")
    if type(slippage_bps) is not int or not 1 <= slippage_bps <= 500:
        raise WalletSwapError("Deslizamiento fuera del límite (0,01–5 %)")
    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    if db.get(RealAccount, account_id) is None:
        raise WalletSwapError("Cuenta REAL no encontrada")
    terms = {"id": identifier, "account_id": account_id, "wallet_digest": _identity(db, "wallet", wallet),
             "chain_id": "0x38", "action": action, "token_in": token_in, "amount_units": str(units),
             "quote_units": quote_units, "minimum_units": str(quote * (10000-slippage_bps) // 10000),
             "fee": fee, "slippage_bps": slippage_bps, "nonce": nonce, "gas": gas, "gas_price": gas_price,
             "max_gas_wei": str(max_gas), "deadline": int(now.timestamp())+120,
             "protocol": PROTOCOL, "router": ROUTER, "token_in_contract": TOKENS.get(token_in),
             "token_out_contract": TOKENS["WBNB" if token_in == "USDT" else "USDT"]}
    _validate(terms)
    row = db.get(RealWalletSwapIntent, identifier)
    if row is not None:
        row, prior, state = swap_record(db, identifier)
        if {k: v for k, v in prior.items() if k != "deadline"} != {k: v for k, v in terms.items() if k != "deadline"}:
            raise WalletSwapError("El ID DEX ya se usó con otros datos")
        terms = prior
    else:
        raw, digest = _encoded(terms)
        state = {"status": "PREPARED", "tx_digest": None, "history": [{"status": "PREPARED", "at": now.isoformat()}], "receipt": None}
        state_raw, state_digest = _encoded(state)
        row = RealWalletSwapIntent(id=identifier, account_id=account_id, wallet_digest=terms["wallet_digest"],
            payload_json=raw, payload_digest=digest, state_json=state_raw, state_digest=state_digest,
            status="PREPARED", created_at=now, updated_at=now)
        db.add(row); db.flush()
    result = _public(row, terms, state, now, db=db)
    result["transaction"] = transaction(terms, wallet)
    db.commit()
    return result


def list_swaps(db: Session, account_id: str, *, now: datetime) -> list[dict]:
    if db.get(RealAccount, account_id) is None:
        raise WalletSwapError("Cuenta REAL no encontrada")
    pending = RealWalletSwapIntent.status.in_(["WALLET_REQUESTED", "HASH_REPORTED"])
    if db.connection().exec_driver_sql("PRAGMA user_version").scalar_one() >= 71:
        resolved = select(RealDexCallResolution.id).where(RealDexCallResolution.swap_id == RealWalletSwapIntent.id).exists()
        pending = pending & ~resolved
    rows = db.scalars(select(RealWalletSwapIntent).where(RealWalletSwapIntent.account_id == account_id)
                      .order_by(pending.desc(), RealWalletSwapIntent.created_at.desc(), RealWalletSwapIntent.id).limit(100)).all()
    return [_public(*swap_record(db, row.id), _time(now), db=db) for row in rows]


def _advance(db: Session, row: RealWalletSwapIntent, state: dict, status: str, now: datetime) -> None:
    if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()>=71:
        from app.portfolio.dex_call_resolution import require_unresolved
        require_unresolved(db,'WALLET_SWAP',row.id)
    now = _time(now)
    if now < row.updated_at:
        raise WalletSwapError("El reloj DEX retrocedió")
    state["status"] = row.status = status
    state["history"].append({"status": status, "at": now.isoformat()})
    row.updated_at = now
    row.state_json, row.state_digest = _encoded(state)
    db.flush(); db.commit()


def claim_swap(db: Session, identifier: str, *, account_id: str, wallet_address: str,
               plan_digest: str, now: datetime) -> dict:
    now = _time(now)
    wallet = canonical_wallet_address(wallet_address)
    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    row, terms, state = swap_record(db, identifier)
    if (row.account_id != account_id or _identity(db, "wallet", wallet) != row.wallet_digest
            or not hmac.compare_digest(plan_digest, row.payload_digest)):
        raise WalletSwapError("La cuenta, wallet o plan DEX cambió")
    if row.status != "PREPARED":
        return {**_public(row, terms, state, now, db=db), "claim_granted": False}
    if int(now.timestamp()) >= terms["deadline"]:
        raise WalletSwapError("El plan DEX caducó")
    from app.portfolio.wallet_nonce_guard import assert_wallet_nonce_available
    try:
        assert_wallet_nonce_available(db, wallet, terms["nonce"])
    except PortfolioError as exc:
        raise WalletSwapError(str(exc)) from None
    pending = list(db.scalars(select(RealWalletSwapIntent.id).where(RealWalletSwapIntent.wallet_digest == row.wallet_digest,
        RealWalletSwapIntent.status.in_(["WALLET_REQUESTED", "HASH_REPORTED"]))))
    version=db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()
    for other in pending:
        if version>=71:
            from app.portfolio.dex_call_resolution import resolution_record,identifier as resolution_id
            record=resolution_record(db,resolution_id('WALLET_SWAP',other))
            if record is not None and record[0].created_at<=now:continue
        if version>=78:
            from app.portfolio.dex_external_outcomes import external_outcome
            if external_outcome(db,'WALLET_SWAP',other,as_of=now)is not None:continue
        raise WalletSwapError("Hay una acción DEX pendiente; revisa su recibo antes de otro envío")
    _advance(db, row, state, "WALLET_REQUESTED", now)
    return {**_public(row, terms, state, now, db=db), "claim_granted": True}


def report_swap(db: Session, identifier: str, *, account_id: str, tx_hash: str | None = None,
                rejected: bool = False, now: datetime) -> dict:
    now = _time(now)
    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    row, terms, state = swap_record(db, identifier)
    if row.account_id != account_id:
        raise WalletSwapError("Cuenta DEX distinta")
    if type(rejected) is not bool or rejected == (tx_hash is not None):
        raise WalletSwapError("Comunica un hash o un rechazo, no ambos")
    if tx_hash is not None and (not isinstance(tx_hash, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", tx_hash)):
        raise WalletSwapError("Hash DEX no válido")
    digest = _identity(db, "tx", tx_hash) if tx_hash is not None else None
    target = "WALLET_REJECTED" if rejected else "HASH_REPORTED"
    if row.status == "WALLET_REQUESTED":
        if digest is not None:
            for other in db.scalars(select(RealWalletSwapIntent).where(RealWalletSwapIntent.id != row.id)):
                if swap_record(db, other.id)[2]["tx_digest"] == digest:
                    raise WalletSwapError("El hash ya pertenece a otro plan DEX")
        state["tx_digest"] = digest
        _advance(db, row, state, target, now)
    elif row.status != target or state["tx_digest"] != digest:
        raise WalletSwapError("La acción DEX ya tiene otro resultado; no se reenvía")
    return _public(row, terms, state, now, db=db)


def check_swap(db: Session, identifier: str, *, account_id: str, evidence: dict, now: datetime) -> dict:
    from app.portfolio.wallet_swap_receipt import observe_receipt
    now = _time(now)
    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    row, terms, state = swap_record(db, identifier)
    if row.account_id != account_id or row.status not in {"HASH_REPORTED", "FINALIZED", "REVERTED"}:
        raise WalletSwapError("Registra primero el hash original de la acción DEX")
    observed = observe_receipt(db, row, terms, state, evidence, now)
    if row.status in {"FINALIZED", "REVERTED"}:
        if {k: v for k, v in state["receipt"].items() if k != "observation_digest"} != {k: v for k, v in observed.items() if k != "observation_digest"}:
            raise WalletSwapError("El recibo DEX cambió; no se modifica el historial")
    else:
        state["receipt"] = observed
        _advance(db, row, state, observed["status"], now)
    return _public(row, terms, state, now, db=db)
