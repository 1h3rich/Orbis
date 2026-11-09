"""Cotejo RPC conservador, no una prueba independiente de consenso BSC."""

from datetime import datetime, timedelta, timezone
import json

from sqlalchemy.orm import Session

from app.portfolio.wallet_funding_receipt import _hex, TRANSFER
from app.portfolio.wallet_proof import canonical_wallet_address
from app.portfolio.wallet_swap import WalletSwapError, TOKENS, ROUTER, _identity, transaction


APPROVAL = "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"


def observe_receipt(db: Session, row, terms: dict, state: dict, evidence: dict, now: datetime) -> dict:
    try:
        raw = json.dumps(evidence, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(raw) > 300_000 or set(evidence) != {"chain_id", "tx_hash", "transaction", "receipt", "block", "finalized", "pool", "post_allowance"}:
            raise ValueError
        tx_hash = _hex(evidence["tx_hash"], word=True)
        if evidence["chain_id"] != "0x38" or _identity(db, "tx", tx_hash) != state["tx_digest"]:
            raise ValueError
        tx, receipt, block, finalized = [evidence[key] for key in ("transaction", "receipt", "block", "finalized")]
        if not all(isinstance(value, dict) for value in (tx, receipt, block, finalized)):
            raise WalletSwapError("Transacción pendiente o finalización no disponible")
        sender = canonical_wallet_address(tx["from"])
        if _identity(db, "wallet", sender) != row.wallet_digest:
            raise ValueError
        expected = transaction(terms, sender)
        if (canonical_wallet_address(tx["to"]) != expected["to"] or tx["input"].lower() != expected["data"]
                or any(_hex(tx[key]) != int(expected[key], 16) for key in ("chainId", "nonce", "gas", "gasPrice", "value"))
                or _hex(tx["type"]) != 0 or _hex(receipt["type"]) != 0
                or canonical_wallet_address(receipt["from"]) != sender or canonical_wallet_address(receipt["to"]) != expected["to"]
                or _hex(tx["hash"], word=True) != tx_hash or _hex(receipt["transactionHash"], word=True) != tx_hash):
            raise ValueError
        height, block_hash = _hex(tx["blockNumber"]), _hex(tx["blockHash"], word=True)
        index = _hex(tx["transactionIndex"])
        if (height <= 0 or _hex(receipt["blockNumber"]) != height or _hex(receipt["blockHash"], word=True) != block_hash
                or _hex(block["number"]) != height or _hex(block["hash"], word=True) != block_hash
                or _hex(receipt["transactionIndex"]) != index):
            raise ValueError
        stamp, final_stamp = _hex(block["timestamp"]), _hex(finalized["timestamp"])
        final_height, final_hash = _hex(finalized["number"]), _hex(finalized["hash"], word=True)
        at = datetime.fromtimestamp(stamp, timezone.utc)
        if (final_height < height or final_height == height and final_hash != block_hash or final_stamp < stamp
                or final_stamp > int(now.timestamp())+15 or at < row.created_at-timedelta(seconds=15)
                or at > now+timedelta(seconds=15)):
            raise WalletSwapError("Bloque no finalizado o incoherente con el plan DEX")
        gas, price = _hex(receipt["gasUsed"]), _hex(receipt["effectiveGasPrice"])
        if not 0 < gas <= int(terms["gas"]) or price != int(terms["gas_price"]):
            raise ValueError
        succeeded = _hex(receipt["status"])
        if succeeded not in {0, 1}:
            raise ValueError
        if succeeded and terms["action"] == "SWAP" and stamp > terms["deadline"]:
            raise ValueError
        amount_out, approved = wallet_effects(terms, sender, receipt["logs"], evidence["pool"], evidence["post_allowance"],
            height, block_hash, tx_hash, index, succeeded)
    except WalletSwapError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise WalletSwapError("El recibo DEX no coincide con el plan; revisión necesaria, sin reenvío") from None
    return {"status": "FINALIZED" if succeeded else "REVERTED", "source": "WALLET_RPC_OBSERVED",
            "block_number": str(height), "block_hash": block_hash, "block_at": at.isoformat(),
            "gas_used": str(gas), "gas_price": str(price), "gas_fee_wei": str(gas*price),
            "amount_out_units": amount_out, "approved_units": approved, "observation_digest": _identity(db, "receipt", raw)}


def wallet_effects(terms, sender, logs, pool_value, post_allowance, height, block_hash, tx_hash, index, succeeded):
    if not isinstance(logs, list) or len(logs) > 500 or not succeeded and logs:
        raise ValueError
    indices, transfers, approvals = set(), [], []
    for log in logs:
        log_index = _hex(log["logIndex"])
        if (log_index in indices or log["removed"] is not False or _hex(log["blockNumber"]) != height
                or _hex(log["blockHash"], word=True) != block_hash or _hex(log["transactionHash"], word=True) != tx_hash
                or _hex(log["transactionIndex"]) != index):
            raise ValueError
        indices.add(log_index)
        token = canonical_wallet_address(log["address"])
        topics = log["topics"]
        if not isinstance(topics, list):
            raise ValueError
        if token not in TOKENS.values() or not topics or topics[0].lower() not in {TRANSFER, APPROVAL}:
            continue
        if len(topics) != 3:
            raise ValueError
        source_word, target_word = _hex(topics[1], word=True), _hex(topics[2], word=True)
        if int(source_word[2:26], 16) or int(target_word[2:26], 16):
            raise ValueError
        source, target = "0x"+source_word[-40:], "0x"+target_word[-40:]
        value = int(_hex(log["data"], word=True), 16)
        item = (token, source, target, value)
        if topics[0].lower() == TRANSFER and sender in {source, target}: transfers.append(item)
        if topics[0].lower() == APPROVAL and source == sender: approvals.append(item)
    amount_out, approved = None, None
    token_in = TOKENS[terms["token_in"]]
    if succeeded and terms["action"] == "SWAP":
        pool = canonical_wallet_address(pool_value)
        if int(pool, 16) == 0 or pool in {*TOKENS.values(), ROUTER, sender} or post_allowance is not None:
            raise ValueError
        output = TOKENS["WBNB" if terms["token_in"] == "USDT" else "USDT"]
        sent = [item for item in transfers if item[:3] == (token_in, sender, pool)]
        received = [item for item in transfers if item[:3] == (output, pool, sender)]
        if (len(transfers) != 2 or len(sent) != 1 or len(received) != 1
                or sent[0][3] != int(terms["amount_units"]) or received[0][3] < int(terms["minimum_units"])):
            raise WalletSwapError("Las transferencias DEX no cumplen el importe y mínimo del plan")
        amount_out = str(received[0][3])
    elif succeeded:
        approved = "0" if terms["action"] == "REVOKE" else terms["amount_units"]
        if (transfers or approvals != [(token_in, sender, ROUTER, int(approved))]
                or pool_value is not None or int(_hex(post_allowance, word=True), 16) != int(approved)):
            raise WalletSwapError("El permiso ERC-20 no coincide con el importe exacto del plan")
    return amount_out, approved
