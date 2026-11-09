"""Resolve a finalized receipt for a durable wallet-to-MEXC request."""

from datetime import datetime

from eth_keys.exceptions import BadSignature
from eth_utils import ValidationError

from app.live_execution.evm_transaction import quantity, recover_reverted_call, recover_wallet_cancellation, word
from app.portfolio.dex_deployment_receipt import finalized_block
from app.portfolio.models import RealWalletFundingBooking, RealWalletFundingCredit
from app.portfolio.wallet_funding import (
    WalletFundingError, _apply_funding_report, _encoded, _identity, _public, _transition,
    funding_record, validate_chain_close,
)
from app.portfolio.wallet_funding_receipt import validate_funding_receipt
from app.portfolio.wallet_proof import canonical_wallet_address


def _cancel_proof(db, row, terms, wallet, evidence, at):
    tx, receipt, block, final = (evidence[key] for key in ("transaction","receipt","block","finalized"))
    sender, tx_type, limit, cap, priority = recover_wallet_cancellation(tx,expected_owner=wallet)
    height, block_hash, stamp = finalized_block(block,final,created_at=row.created_at,now=at)
    tx_hash = word(evidence["tx_hash"])
    price = quantity(receipt["effectiveGasPrice"])
    gas = quantity(receipt["gasUsed"])
    base = quantity(block["baseFeePerGas"]) if tx_type == 2 else None
    if (sender != wallet or quantity(tx["nonce"]) != int(terms["nonce"])
            or word(tx["hash"]) != tx_hash or word(receipt["transactionHash"]) != tx_hash
            or canonical_wallet_address(receipt["from"]) != wallet or canonical_wallet_address(receipt["to"]) != wallet
            or any(quantity(item["blockNumber"]) != height or word(item["blockHash"]) != block_hash for item in (tx,receipt))
            or quantity(tx["transactionIndex"]) != quantity(receipt["transactionIndex"])
            or quantity(receipt["type"]) != tx_type or quantity(receipt["status"]) != 1
            or receipt["logs"] != [] or receipt.get("contractAddress") is not None
            or gas != 21_000 or not gas <= limit <= 2_000_000 or not 0 < price <= 10**18
            or (price != cap if base is None else cap < base or price != min(cap,base+priority))):
        raise ValueError("cancellation evidence")
    return {"tx_digest":_identity(db,"tx",tx_hash),"block_number":str(height),"block_hash":block_hash,
            "block_at":stamp.isoformat(),"finalized_number":str(quantity(final["number"])),
            "finalized_hash":word(final["hash"]),"gas_used":str(gas),"gas_price":str(price),
            "gas_fee_wei":str(gas*price),"evidence_digest":_identity(db,"receipt",_encoded(evidence)[0])}


def settle_discovered_funding(db, identifier, account, wallet, plan_digest, evidence, *,
                              cancellation_code, now):
    """Write the verified outcome once, without crediting a balance."""
    try:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        row, terms, state = funding_record(db,identifier)
        if (row.account_id != account or row.payload_digest != plan_digest
                or _identity(db,"wallet",wallet) != terms["wallet_digest"]):
            raise WalletFundingError("El envío cambió durante la búsqueda BNB")
        if row.status == "CHAIN_CLOSED":
            result = _public(db,row,terms,state,now=now)
            db.commit()
            return result
        if row.status not in ("WALLET_REQUESTED","HASH_REPORTED"):
            raise WalletFundingError("El envío cambió durante la búsqueda BNB")

        tx_hash = word(evidence["tx_hash"])
        status = quantity(evidence["receipt"]["status"])
        if cancellation_code == "0x":
            proof = {"kind":"CANCELLED",**_cancel_proof(db,row,terms,wallet,evidence,now)}
        elif status == 0:
            sender, *_ = recover_reverted_call(evidence["transaction"],receipt_status=status)
            if sender != wallet:
                raise ValueError("revert signer")
            proof = {"kind":"REVERTED",**validate_funding_receipt(
                db,row,terms,evidence,now=now,expected_tx_digest=state["tx_digest"],reverted=True)}
        else:
            validate_funding_receipt(db,row,terms,evidence,now=now,expected_tx_digest=state["tx_digest"])
            _apply_funding_report(db,row,state,tx_hash,now=now)
            result = {**_public(db,row,terms,state,now=now),"discovered_tx_hash":tx_hash}
            db.commit()
            return result

        if db.get(RealWalletFundingCredit,identifier) or db.get(RealWalletFundingBooking,identifier):
            raise WalletFundingError("El ingreso ya tiene evidencia de acreditación")
        if "mexc_deposit" in state:
            raise WalletFundingError("El cierre contradice el envío finalizado que MEXC ya observó")
        claimed_at = datetime.fromisoformat(state["history"][1]["at"])
        validate_chain_close(proof,terms,state["tx_digest"],claimed_at,now)
        state["chain_close"] = proof
        _transition(row,state,"CHAIN_CLOSED",now)
        result = _public(db,row,terms,state,now=now)
        db.commit()
        return result
    except WalletFundingError:
        db.rollback()
        raise
    except (KeyError,ValueError,TypeError,OverflowError,BadSignature,ValidationError):
        db.rollback()
        raise WalletFundingError("El recibo no demuestra el envío original ni su cierre en BNB") from None
