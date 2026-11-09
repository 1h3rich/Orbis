"""Recepción BEP-20 observada: nunca confunde acuse, gas del exchange y neto."""

from datetime import datetime,timedelta,timezone
from typing import Literal
import json
import re

from sqlalchemy.orm import Session

from app.portfolio.models import RealMexcWithdrawalReceipt
from app.portfolio.mexc_withdrawal_journal import WithdrawalError,identity,recipient_matches,withdrawal_record
from app.portfolio.mexc_withdrawal_observation import consistent_source,observation_record,instant
from app.portfolio.wallet_funding import _decoded,_encoded,_time,_uint
from app.portfolio.wallet_funding_receipt import _hex,TRANSFER
from app.portfolio.wallet_proof import canonical_wallet_address


ReceiptSource = Literal["WALLET_RPC_OBSERVED", "ORBIS_RPC_OBSERVED"]


def received_amounts(terms: dict,source: dict,received: int) -> dict:
    requested,fee = int(terms["amount_units"]),int(source["fee_units"])
    debit = received+fee
    if (received <= 0 or debit >= 2**256 or received not in (requested,requested-fee)
            or int(source["amount_units"]) not in (requested,received,debit)):
        raise WithdrawalError("El neto recibido, la comisión y el importe MEXC no explican la retirada solicitada")
    return {"received_units":str(received),"fee_units":str(fee),"debit_units":str(debit),
        "fee_model":"NO_FEE" if fee == 0 else "ADDED" if received == requested else "DEDUCTED",
        "fee_filter_exceeded":fee > int(terms["max_fee_units"]) or debit > int(terms["max_debit_units"]) or received < int(terms["min_receive_units"])}


def receipt_record(db: Session,identifier: str,*,check_history: bool = True):
    row = db.get(RealMexcWithdrawalReceipt,identifier)
    if row is None:
        return None
    plan,terms,_ = withdrawal_record(db,identifier)
    observation,source = consistent_source(db,row.observation_id,require_success=True,check_history=check_history)
    data = _decoded(row.payload_json,row.payload_digest)
    try:
        keys = {"withdrawal_id","plan_digest","observation_id","observation_digest","source_ref","chain_source",
            "network_evidence","tx_digest","recipient_digest","contract","decimals","received_units","fee_units","debit_units",
            "fee_model","fee_filter_exceeded","block_number","block_hash","block_at","finalized_number","finalized_hash","rpc_digest","checked_at"}
        if (set(data) != keys or data["withdrawal_id"] != identifier or data["plan_digest"] != plan.payload_digest
                or observation.withdrawal_id != identifier or source["status"] != 7
                or data["observation_id"] != row.observation_id or data["observation_digest"] != observation.payload_digest
                or row.account_ref != plan.account_ref or row.source_ref != source["source_ref"] or data["source_ref"] != row.source_ref
                or data["chain_source"] not in ("WALLET_RPC_OBSERVED", "ORBIS_RPC_OBSERVED") or data["network_evidence"] not in ("MEXC_AND_RPC","PLAN_AND_RPC_HISTORY_NETWORK_ABSENT")
                or data["network_evidence"] != ("MEXC_AND_RPC" if source["network"] is not None else "PLAN_AND_RPC_HISTORY_NETWORK_ABSENT")
                or data["tx_digest"] != source["chain_digest"] or data["recipient_digest"] != terms["recipient_digest"]
                or data["contract"] != terms["contract"] or type(data["decimals"]) is not int or data["decimals"] != terms["decimals"]
                or any(not isinstance(data[key],str) or not re.fullmatch(r"[0-9a-f]{64}",data[key]) for key in ("tx_digest","rpc_digest"))
                or any(not isinstance(data[key],str) or not re.fullmatch(r"0x[0-9a-f]{64}",data[key]) for key in ("block_hash","finalized_hash"))
                or type(data["fee_filter_exceeded"]) is not bool
                or _uint(data["block_number"],"Bloque") > _uint(data["finalized_number"],"Bloque finalizado")
                or data["block_number"] == data["finalized_number"] and data["block_hash"] != data["finalized_hash"]
                or _time(datetime.fromisoformat(data["block_at"])) > _time(datetime.fromisoformat(data["checked_at"]))+timedelta(seconds=15)):
            raise ValueError
        amounts = received_amounts(terms,source,_uint(data["received_units"],"Unidades recibidas"))
        if any(data[key] != value for key,value in amounts.items()):
            raise ValueError
    except (KeyError,TypeError,ValueError,OverflowError):
        raise WithdrawalError("La evidencia de recepción cambió o ya no coincide con MEXC") from None
    return row,data,source


def incoming_receipt(db: Session,identifier: str,account_id: str,address: str,
                      observation_id: str, evidence: dict, *, now: datetime,
                      chain_source: ReceiptSource = "WALLET_RPC_OBSERVED") -> dict:
    plan,terms,state = withdrawal_record(db,identifier)
    observed,source = consistent_source(db,observation_id,require_success=True)
    if plan.account_id != account_id or observed.withdrawal_id != identifier:
        raise WithdrawalError("La recepción no corresponde a esta solicitud/cuenta")
    recipient = canonical_wallet_address(address)
    if not recipient_matches(db, terms, recipient):
        raise WithdrawalError("Conecta la wallet de destino original de la retirada")
    try:
        encoded = json.dumps(evidence,sort_keys=True,separators=(",",":"),allow_nan=False)
        if len(encoded)>1_000_000 or set(evidence) != {"chain_id","tx_hash","transaction","receipt","block","finalized"}:
            raise ValueError
        tx_hash = _hex(evidence["tx_hash"],word=True)
        if evidence["chain_id"] != "0x38" or identity(db,"tx",tx_hash) != source["chain_digest"]:
            raise ValueError
        tx,receipt,block,finalized = (evidence[key] for key in ("transaction","receipt","block","finalized"))
        if not all(isinstance(item,dict) for item in (tx,receipt,block,finalized)):
            raise WithdrawalError("Transacción pendiente o RPC sin bloque finalizado")
        if (_hex(tx["hash"],word=True) != tx_hash or _hex(receipt["transactionHash"],word=True) != tx_hash
                or _hex(tx["chainId"]) != 56 or _hex(receipt["status"]) != 1
                or canonical_wallet_address(tx["from"]) != canonical_wallet_address(receipt["from"])
                or canonical_wallet_address(tx["to"]) != canonical_wallet_address(receipt["to"])):
            raise ValueError
        height,block_hash = _hex(tx["blockNumber"]),_hex(tx["blockHash"],word=True)
        if (height == 0 or _hex(receipt["blockNumber"]) != height or _hex(block["number"]) != height
                or _hex(receipt["blockHash"],word=True) != block_hash or _hex(block["hash"],word=True) != block_hash
                or _hex(receipt["transactionIndex"]) != _hex(tx["transactionIndex"])):
            raise ValueError
        if chain_source == "ORBIS_RPC_OBSERVED":
            index = _hex(tx["transactionIndex"])
            hashes = block["transactions"]
            if type(hashes) is not list or index >= len(hashes) or _hex(hashes[index], word=True) != tx_hash:
                raise WithdrawalError("El bloque BNB no contiene la transacción en su índice")
        final_height,final_hash = _hex(finalized["number"]),_hex(finalized["hash"],word=True)
        at = datetime.fromtimestamp(_hex(block["timestamp"]),timezone.utc)
        final_at = datetime.fromtimestamp(_hex(finalized["timestamp"]),timezone.utc)
        if (height > final_height or height == final_height and block_hash != final_hash
                or final_at < at or final_at > _time(now)+timedelta(seconds=15)
                or at < datetime.fromisoformat(state["history"][1]["at"])-timedelta(seconds=15)
                or at > _time(now)+timedelta(seconds=15) or at < instant(source["time_ms"])-timedelta(seconds=15)):
            raise WithdrawalError("El recibo no está finalizado o su fecha no concuerda con la retirada")
        logs = receipt["logs"]
        if not isinstance(logs,list) or not 1 <= len(logs) <= 2000:
            raise ValueError
        matches,indices = [],set()
        for log in logs:
            if not isinstance(log,dict):
                raise ValueError
            index = _hex(log["logIndex"])
            if index in indices:
                raise ValueError
            indices.add(index)
            topics = log.get("topics")
            if (isinstance(log.get("address"),str) and log["address"].lower() == terms["contract"]
                    and isinstance(topics,list) and topics and isinstance(topics[0],str) and topics[0].lower() == TRANSFER):
                if len(topics) != 3:
                    raise ValueError
                if _hex(topics[1],word=True) == "0x"+recipient[2:].zfill(64):
                    raise WithdrawalError("El lote también contiene salidas de esta wallet; recepción neta ambigua")
                if _hex(topics[2],word=True) == "0x"+recipient[2:].zfill(64):
                    matches.append(log)
        if len(matches) != 1:
            raise WithdrawalError("No hay una única transferencia USDT inequívoca a la wallet; revisa el lote")
        log = matches[0]
        sender_word = _hex(log["topics"][1],word=True)
        if (log.get("removed") is not False or int(sender_word,16) in (0,int(recipient,16)) or int(sender_word[2:26],16) != 0
                or _hex(log["transactionHash"],word=True) != tx_hash or _hex(log["blockHash"],word=True) != block_hash
                or _hex(log["blockNumber"]) != height or _hex(log["transactionIndex"]) != _hex(tx["transactionIndex"])):
            raise ValueError
        received = int(_hex(log["data"],word=True),16)
    except WithdrawalError:
        raise
    except (KeyError,TypeError,ValueError,OverflowError):
        raise WithdrawalError("RPC de recepción incoherente con el hash, contrato, destino o bloque de MEXC") from None
    amounts = received_amounts(terms,source,received)
    return {"withdrawal_id":identifier,"plan_digest":plan.payload_digest,"observation_id":observed.id,"observation_digest":observed.payload_digest,
        "source_ref":source["source_ref"],"chain_source":chain_source,
        "network_evidence":"MEXC_AND_RPC" if source["network"] is not None else "PLAN_AND_RPC_HISTORY_NETWORK_ABSENT",
        "tx_digest":source["chain_digest"],"recipient_digest":terms["recipient_digest"],"contract":terms["contract"],"decimals":terms["decimals"],
        **amounts,"block_number":str(height),"block_hash":block_hash,"block_at":at.isoformat(),"finalized_number":str(final_height),
        "finalized_hash":final_hash,"rpc_digest":identity(db,"receipt",encoded),"checked_at":_time(now).isoformat()}


def confirm_receipt(db: Session,identifier: str,account_id: str,address: str,
                    observation_id: str, evidence: dict, *, now: datetime,
                    chain_source: ReceiptSource = "WALLET_RPC_OBSERVED") -> dict:
    try:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        payload = incoming_receipt(db,identifier,account_id,address,observation_id,evidence,now=now,chain_source=chain_source)
        existing = receipt_record(db,identifier)
        if existing:
            if any(existing[1][key] != payload[key] for key in ("block_number","block_hash","tx_digest","source_ref","received_units","fee_units","debit_units")):
                raise WithdrawalError("El recibo finalizado comunicado anteriormente cambió")
        else:
            plan,_,_ = withdrawal_record(db,identifier)
            raw,digest = _encoded(payload)
            db.add(RealMexcWithdrawalReceipt(withdrawal_id=identifier,observation_id=observation_id,account_ref=plan.account_ref,
                source_ref=payload["source_ref"],payload_json=raw,payload_digest=digest))
            db.flush()
            receipt_record(db,identifier)
        from app.portfolio.mexc_withdrawal_booking import settlement_status
        result = settlement_status(db,identifier)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
