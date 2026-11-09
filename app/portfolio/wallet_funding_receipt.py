"""Validación conservadora de observaciones RPC en BNB.

Las direcciones y el hash de transacción sólo viven durante esta llamada.
MEXC debe corroborar el abono por su canal View independiente.
"""

from datetime import datetime, timedelta, timezone
import json
import re
from typing import Literal
from sqlalchemy.orm import Session

from app.portfolio.wallet_funding import WalletFundingError, _identity, _time, funding_record
from app.portfolio.wallet_proof import canonical_wallet_address


FundingReceiptSource = Literal["WALLET_RPC_OBSERVED", "ORBIS_RPC_OBSERVED"]

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _hex(value, *, word=False):
    pattern = r"0x[0-9a-fA-F]{64}" if word else r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]{0,63})"
    if not isinstance(value,str) or not re.fullmatch(pattern,value):
        raise WalletFundingError("RPC: dato hexadecimal inválido")
    return value.lower() if word else int(value,16)


def receipt_observation(db: Session, identifier: str, account_id: str,
                        evidence: dict, *, now: datetime,
                        chain_source: FundingReceiptSource = "WALLET_RPC_OBSERVED") -> dict:
    """Reabre el plan; exige tx legacy exacta y Transfer único en bloque finalizado."""
    row, terms, state = funding_record(db,identifier)
    if row.account_id != account_id or row.status != "HASH_REPORTED":
        raise WalletFundingError("Registra primero el txId original de este envío")
    observed = validate_funding_receipt(db,row,terms,evidence,now=now,expected_tx_digest=state["tx_digest"])
    return {**observed, "chain_source": chain_source}


def validate_funding_receipt(db: Session, row, terms: dict, evidence: dict, *, now: datetime,
                             expected_tx_digest: str | None, reverted: bool = False) -> dict:
    """Check plan terms and finalized transfer independently of recording its hash."""
    try:
        encoded = json.dumps(evidence,sort_keys=True,separators=(",",":"),allow_nan=False)
        if len(encoded) > 300_000 or set(evidence) != {"chain_id","tx_hash","transaction","receipt","block","finalized"}:
            raise ValueError
        tx_hash = _hex(evidence["tx_hash"],word=True)
        tx_digest = _identity(db,"tx",tx_hash)
        if evidence["chain_id"] != "0x38" or (expected_tx_digest is not None and tx_digest != expected_tx_digest):
            raise ValueError
        tx, receipt, block, finalized = (evidence[key] for key in ("transaction","receipt","block","finalized"))
        if not all(isinstance(item,dict) for item in (tx,receipt,block,finalized)):
            raise WalletFundingError("Transacción pendiente o finalización no disponible en el RPC")
        sender, token = canonical_wallet_address(tx["from"]), canonical_wallet_address(tx["to"])
        if (_identity(db,"wallet",sender) != terms["wallet_digest"] or token != terms["token_contract"]
                or _hex(tx["hash"],word=True) != tx_hash or _hex(receipt["transactionHash"],word=True) != tx_hash
                or canonical_wallet_address(receipt["from"]) != sender
                or canonical_wallet_address(receipt["to"]) != token or _hex(tx["chainId"]) != 56
                or _hex(tx["type"]) != 0 or _hex(receipt["type"]) != 0 or _hex(tx["value"]) != 0
                or _hex(tx["nonce"]) != int(terms["nonce"]) or _hex(tx["gas"]) != int(terms["gas"])
                or _hex(tx["gasPrice"]) != int(terms["gas_price"])):
            raise ValueError
        data = tx["input"]
        if not isinstance(data,str) or not re.fullmatch(r"0xa9059cbb[0-9a-fA-F]{128}",data):
            raise ValueError
        if int(data[10:34],16) != 0 or int(data[74:],16) != int(terms["amount_units"]):
            raise ValueError
        destination = "0x"+data[34:74].lower()
        if _identity(db,"destination",destination) != terms["destination_digest"]:
            raise ValueError
        height = _hex(tx["blockNumber"])
        block_hash = _hex(tx["blockHash"],word=True)
        if (height == 0 or _hex(receipt["blockNumber"]) != height
                or _hex(receipt["blockHash"],word=True) != block_hash
                or _hex(block["number"]) != height or _hex(block["hash"],word=True) != block_hash
                or _hex(receipt["transactionIndex"]) != _hex(tx["transactionIndex"])):
            raise ValueError
        if _hex(receipt["status"]) != (0 if reverted else 1):
            raise WalletFundingError("El recibo comunica ejecución fallida; puede haberse gastado gas. No se acredita")
        final_height, final_hash = _hex(finalized["number"]), _hex(finalized["hash"],word=True)
        stamp, final_stamp = _hex(block["timestamp"]), _hex(finalized["timestamp"])
        at = datetime.fromtimestamp(stamp,timezone.utc)
        if (final_height < height or final_height == height and final_hash != block_hash
                or final_stamp < stamp or final_stamp > int(_time(now).timestamp())+15
                or at < row.created_at-timedelta(seconds=15)
                or at > _time(now)+timedelta(seconds=15)):
            raise WalletFundingError("Bloque no finalizado, obsoleto o incoherente con el envío")
        gas, price = _hex(receipt["gasUsed"]), _hex(receipt["effectiveGasPrice"])
        if not 0 < gas <= int(terms["gas"]) or price != int(terms["gas_price"]) or gas*price > int(terms["max_gas_wei"]):
            raise ValueError
        logs = receipt["logs"]
        if reverted:
            if logs != [] or receipt.get("contractAddress") is not None:
                raise ValueError
            return {"tx_digest":tx_digest,"block_number":str(height),"block_hash":block_hash,
                    "block_at":at.isoformat(),"finalized_number":str(final_height),"finalized_hash":final_hash,
                    "gas_used":str(gas),"gas_price":str(price),
                    "gas_fee_wei":str(gas*price),"evidence_digest":_identity(db,"receipt",encoded)}
        if not isinstance(logs,list) or not 1 <= len(logs) <= 500:
            raise ValueError
        token_transfers = []
        for log in logs:
            if not isinstance(log,dict):
                raise ValueError
            topics = log.get("topics")
            if (canonical_wallet_address(log.get("address")) == token and isinstance(topics,list)
                    and topics and isinstance(topics[0],str) and topics[0].lower() == TRANSFER):
                token_transfers.append(log)
        if len(token_transfers) != 1:
            raise WalletFundingError("Transfer ambiguo: se necesita revisar el movimiento del token")
        log = token_transfers[0]
        if (log.get("removed") is not False or len(log["topics"]) != 3
                or _hex(log["topics"][1],word=True) != "0x"+sender[2:].zfill(64)
                or _hex(log["topics"][2],word=True) != "0x"+destination[2:].zfill(64)
                or int(_hex(log["data"],word=True),16) != int(terms["amount_units"])
                or _hex(log["transactionHash"],word=True) != tx_hash
                or _hex(log["blockHash"],word=True) != block_hash or _hex(log["blockNumber"]) != height
                or _hex(log["transactionIndex"]) != _hex(tx["transactionIndex"])):
            raise ValueError
        _hex(log["logIndex"])
    except WalletFundingError:
        raise
    except (KeyError,ValueError,TypeError,OverflowError):
        raise WalletFundingError("El recibo RPC no coincide exactamente con el plan. Revisión necesaria, sin reenvío") from None
    return {"funding_id":row.id,"plan_digest":row.payload_digest,"tx_digest":tx_digest,
            "chain_id":"0x38","chain_source":"WALLET_RPC_OBSERVED","block_number":str(height),
            "block_hash":block_hash,"block_at":at.isoformat(),"finalized_number":str(final_height),
            "finalized_hash":final_hash,"gas_used":str(gas),"effective_gas_price":str(price),
            "gas_fee_wei":str(gas*price),"rpc_digest":_identity(db,"receipt",encoded)}
