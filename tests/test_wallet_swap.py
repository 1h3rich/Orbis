"""Intercambio manual BSC: contrato exacto, permiso único y recibos observados."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from eth_abi import decode
from app.portfolio.vault import SCHEMA_VERSION
import pytest

from app.portfolio.repository import create_account
from app.portfolio.vault import RealVault
from app.portfolio.wallet_swap import prepare_swap, list_swaps, claim_swap, report_swap, check_swap, WalletSwapError
from app.portfolio.wallet_funding_receipt import TRANSFER


PASSWORD = "isolated swap vault password"
WALLET = "0x" + "12" * 20


def setup(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSWORD, create=True)
    with vault.session() as db:
        account = create_account(db, "Wallet BNB", "USDT").id
    return vault, account, datetime.now(timezone.utc)


def prepare(vault, account, now, **changes):
    values = dict(identifier=str(uuid4()), account_id=account, wallet_address=WALLET,
                  action="SWAP", token_in="USDT", amount="12.345678901234567891",
                  quote_units="20000000000000000", fee=500, slippage_bps=50,
                  nonce="3", gas="200000", gas_price="1000000000", max_gas_bnb="0.001", now=now)
    values.update(changes)
    with vault.session() as db:
        return prepare_swap(db, **values)


def test_swap_compiles_minimum_deadline_recipient_and_exact_amount_without_storing_wallet(tmp_path):
    vault, account, now = setup(tmp_path)
    plan = prepare(vault, account, now)
    tx = plan["transaction"]
    assert tx["chainId"] == "0x38" and tx["value"] == "0x0"
    assert tx["to"] == "0xb971ef87ede563556b2ed4b1c0b0019111dd85d2"
    assert tx["data"][:10] == "0x5ae401dc"
    deadline, calls = decode(["uint256", "bytes[]"], bytes.fromhex(tx["data"][10:]))
    assert deadline == int(now.timestamp()) + 120 and len(calls) == 1
    assert calls[0][:4].hex() == "04e45aaf"
    token_in, token_out, fee, recipient, amount, minimum, limit = decode(
        ["address", "address", "uint24", "address", "uint256", "uint256", "uint160"], calls[0][4:])
    assert token_in == "0x55d398326f99059ff775485246999027b3197955"
    assert token_out == "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
    assert fee == 500 and recipient == WALLET and amount == 12345678901234567891
    assert minimum == 19900000000000000 and limit == 0
    with vault.session() as db:
        history = list_swaps(db, account, now=now)
        assert WALLET not in str(history) and "transaction" not in str(history)
    assert plan["status"] == "PREPARED" and not plan["booked"]
    vault.lock()


def evidence(plan, now, *, out=20000000000000000, success=True):
    tx_hash, block_hash = "0x"+"ab"*32, "0x"+"cd"*32
    tx = {**plan["transaction"], "input": plan["transaction"]["data"], "hash": tx_hash, "type": "0x0",
          "blockNumber": "0x100", "blockHash": block_hash, "transactionIndex": "0x0"}
    pool = "0x"+"34"*20
    def transfer(token, sender, target, amount, index):
        return {"address": token, "topics": [TRANSFER, "0x"+sender[2:].zfill(64), "0x"+target[2:].zfill(64)],
                "data": "0x"+hex(amount)[2:].zfill(64), "removed": False, "transactionHash": tx_hash,
                "blockNumber": "0x100", "blockHash": block_hash, "transactionIndex": "0x0", "logIndex": hex(index)}
    logs = [transfer("0x55d398326f99059ff775485246999027b3197955", tx["from"], pool, int(plan["amount_units"]), 1),
            transfer("0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c", pool, tx["from"], out, 2)] if success else []
    receipt = {"transactionHash": tx_hash, "from": tx["from"], "to": tx["to"], "type": "0x0",
               "blockNumber": "0x100", "blockHash": block_hash, "transactionIndex": "0x0",
               "status": "0x1" if success else "0x0", "gasUsed": "0x186a0", "effectiveGasPrice": tx["gasPrice"], "logs": logs}
    block = {"number": "0x100", "hash": block_hash, "timestamp": hex(int(now.timestamp()))}
    return {"chain_id": "0x38", "tx_hash": tx_hash, "transaction": tx, "receipt": receipt,
            "block": block, "finalized": block, "pool": pool, "post_allowance": None}


def test_finalized_swap_requires_exact_plan_and_minimum_and_records_no_automatic_money(tmp_path):
    vault, account, now = setup(tmp_path)
    plan = prepare(vault, account, now)
    claim(vault, account, now, plan)
    proof = evidence(plan, now)
    with vault.session() as db:
        report_swap(db, plan["id"], account_id=account, tx_hash=proof["tx_hash"], now=now)
    with vault.session() as db:
        result = check_swap(db, plan["id"], account_id=account, evidence=proof, now=now)
        assert result["status"] == "FINALIZED" and not result["booked"]
        assert result["receipt"]["amount_out_units"] == "20000000000000000"
        assert result["receipt"]["gas_fee_wei"] == "100000000000000"
        assert result["receipt"]["source"] == "WALLET_RPC_OBSERVED"
        assert proof["tx_hash"] not in str(list_swaps(db, account, now=now))
    vault.lock()


def claim(vault, account, now, plan, **changes):
    values = dict(account_id=account, wallet_address=WALLET, plan_digest=plan["plan_digest"], now=now)
    values.update(changes)
    with vault.session() as db:
        return claim_swap(db, plan["id"], **values)


def test_claim_is_once_and_unknown_or_pending_result_blocks_new_send_across_reopen(tmp_path):
    vault, account, now = setup(tmp_path)
    plan = prepare(vault, account, now)
    assert claim(vault, account, now, plan)["claim_granted"] is True
    assert claim(vault, account, now, plan)["claim_granted"] is False
    other = prepare(vault, account, now, nonce="4")
    with pytest.raises(WalletSwapError, match="pendiente"):
        claim(vault, account, now, other)
    tx_hash = "0x" + "ab" * 32
    with vault.session() as db:
        result = report_swap(db, plan["id"], account_id=account, tx_hash=tx_hash, now=now)
        assert result["status"] == "HASH_REPORTED" and not result["booked"]
    vault.lock(); vault.unlock(PASSWORD)
    with pytest.raises(WalletSwapError, match="pendiente"):
        claim(vault, account, now, other)
    with vault.session() as db:
        assert tx_hash not in str(list_swaps(db, account, now=now))
        with pytest.raises(WalletSwapError):
            report_swap(db, plan["id"], account_id=account, tx_hash="0x"+"cd"*32, now=now)
    vault.lock()


@pytest.mark.parametrize("action,approved", [("APPROVE", 12345678901234567891), ("REVOKE", 0)])
def test_approval_never_grants_unlimited_and_requires_matching_event_and_readback(tmp_path, action, approved):
    from app.portfolio.wallet_swap_receipt import APPROVAL
    vault, account, now = setup(tmp_path)
    plan = prepare(vault, account, now, action=action)
    spender, allowance = decode(["address", "uint256"], bytes.fromhex(plan["transaction"]["data"][10:]))
    assert spender == "0xb971ef87ede563556b2ed4b1c0b0019111dd85d2" and allowance == approved
    claim(vault, account, now, plan)
    proof = evidence(plan, now)
    proof["pool"] = None
    proof["post_allowance"] = "0x"+hex(approved)[2:].zfill(64)
    log = proof["receipt"]["logs"][0]
    log["topics"] = [APPROVAL, "0x"+WALLET[2:].zfill(64), "0x"+spender[2:].zfill(64)]
    log["data"] = proof["post_allowance"]
    proof["receipt"]["logs"] = [log]
    with vault.session() as db:
        report_swap(db, plan["id"], account_id=account, tx_hash=proof["tx_hash"], now=now)
    with vault.session() as db:
        assert check_swap(db, plan["id"], account_id=account, evidence=proof, now=now)["receipt"]["approved_units"] == str(approved)
    vault.lock()


@pytest.mark.parametrize("mutation", ["minimum", "calldata", "sender", "chain", "nonce", "gas", "block", "finalized", "removed", "duplicate", "type"])
def test_inconsistent_rpc_never_finalizes_or_changes_book(tmp_path, mutation):
    from app.portfolio.domain import PortfolioError
    vault, account, now = setup(tmp_path)
    plan = prepare(vault, account, now)
    claim(vault, account, now, plan)
    proof = evidence(plan, now)
    if mutation == "minimum": proof["receipt"]["logs"][1]["data"] = "0x"+"1".zfill(64)
    if mutation == "calldata": proof["transaction"]["input"] = "0x12345678"
    if mutation == "sender": proof["transaction"]["from"] = "0x"+"56"*20
    if mutation == "chain": proof["transaction"]["chainId"] = "0x1"
    if mutation == "nonce": proof["transaction"]["nonce"] = "0x4"
    if mutation == "gas": proof["receipt"]["gasUsed"] = "0xffffff"
    if mutation == "block": proof["block"]["hash"] = "0x"+"ee"*32
    if mutation == "finalized": proof["finalized"] = {**proof["block"], "number": "0xff"}
    if mutation == "removed": proof["receipt"]["logs"][0]["removed"] = True
    if mutation == "duplicate": proof["receipt"]["logs"].append(proof["receipt"]["logs"][0].copy())
    if mutation == "type": proof["transaction"]["type"] = "0x2"
    with vault.session() as db:
        report_swap(db, plan["id"], account_id=account, tx_hash=proof["tx_hash"], now=now)
    with vault.session() as db:
        with pytest.raises(PortfolioError):
            check_swap(db, plan["id"], account_id=account, evidence=proof, now=now)
    with vault.session() as db:
        assert list_swaps(db, account, now=now)[0]["status"] == "HASH_REPORTED"
    vault.lock()


def test_reverted_swap_after_deadline_records_gas_and_allows_new_plan_but_never_reuses_hash(tmp_path):
    vault, account, now = setup(tmp_path)
    plan = prepare(vault, account, now)
    claim(vault, account, now, plan)
    proof = evidence(plan, now+timedelta(seconds=200), success=False)
    with vault.session() as db:
        report_swap(db, plan["id"], account_id=account, tx_hash=proof["tx_hash"], now=now)
    with vault.session() as db:
        result = check_swap(db, plan["id"], account_id=account, evidence=proof, now=now+timedelta(seconds=200))
        assert result["status"] == "REVERTED" and result["receipt"]["amount_out_units"] is None
    other = prepare(vault, account, now+timedelta(seconds=200), nonce="4")
    assert claim(vault, account, now+timedelta(seconds=200), other)["claim_granted"]
    with vault.session() as db:
        with pytest.raises(WalletSwapError, match="otro"):
            report_swap(db, other["id"], account_id=account, tx_hash=proof["tx_hash"], now=now+timedelta(seconds=200))
    vault.lock()


def test_backup_rejects_semantically_changed_receipt_even_if_json_digest_is_recomputed(tmp_path):
    from app.portfolio.models import RealWalletSwapIntent
    from app.portfolio.vault import VaultError
    from app.portfolio.wallet_funding import _encoded
    vault, account, now = setup(tmp_path)
    plan = prepare(vault, account, now)
    claim(vault, account, now, plan)
    proof = evidence(plan, now)
    with vault.session() as db:
        report_swap(db, plan["id"], account_id=account, tx_hash=proof["tx_hash"], now=now)
    with vault.session() as db:
        check_swap(db, plan["id"], account_id=account, evidence=proof, now=now)
    with vault.session() as db:
        import json
        row = db.get(RealWalletSwapIntent, plan["id"])
        state = json.loads(row.state_json)
        state["receipt"]["amount_out_units"] = "1"
        row.state_json, row.state_digest = _encoded(state)
        db.commit()
    with pytest.raises(VaultError, match="wallet swap"):
        vault.backup(tmp_path / "invalid-backup.db")
    vault.lock()


@pytest.mark.parametrize("changes", [{"amount": "0.0000000000000000001"}, {"amount": "NaN"}, {"amount": "-1"},
    {"token_in": "BNB"}, {"action": "TRANSFER"}, {"fee": True}, {"fee": 2500}, {"slippage_bps": 501},
    {"slippage_bps": 0}, {"slippage_bps": True}, {"quote_units": "1"}, {"nonce": "01"},
    {"gas": "2000001"}, {"max_gas_bnb": "0.0000001"}])
def test_invalid_financial_limits_never_create_a_plan(tmp_path, changes):
    from app.portfolio.domain import PortfolioError
    vault, account, now = setup(tmp_path)
    with pytest.raises(PortfolioError):
        prepare(vault, account, now, **changes)
    with vault.session() as db:
        assert list_swaps(db, account, now=now) == []
    vault.lock()


def test_v31_migrates_on_encrypted_copy_without_changing_real_account(tmp_path):
    vault, account, now = setup(tmp_path)
    with vault.session() as db:
        db.connection().exec_driver_sql("DROP TABLE real_wallet_swap_intents")
        db.connection().exec_driver_sql("PRAGMA user_version = 31")
        db.commit()
    vault.lock(); vault.unlock(PASSWORD)
    assert vault.last_migration_backup and vault.last_migration_backup.exists()
    with vault.session() as db:
        assert db.connection().exec_driver_sql("PRAGMA user_version").scalar_one() == SCHEMA_VERSION
        assert list_swaps(db, account, now=now) == []
    vault.lock()


@pytest.mark.parametrize("first", ["funding", "swap"])
def test_wallet_request_uncertainty_and_nonce_conflicts_are_shared_with_mexc_funding(tmp_path, first):
    from app.portfolio.domain import PortfolioError
    from tests import test_wallet_funding as funding
    vault, account, now = funding.setup(tmp_path)
    deposit = funding.prepare(vault, account, now)
    swap = prepare(vault, account, now, wallet_address=funding.WALLET)
    if first == "funding":
        assert funding.claim(vault, account, now, deposit)["claim_granted"]
        with pytest.raises(PortfolioError):
            claim(vault, account, now, swap, wallet_address=funding.WALLET)
        with vault.session() as db:
            funding.report_funding(db, deposit["id"], account_id=account, tx_hash=funding.TX_HASH, now=now)
        with pytest.raises(PortfolioError):
            claim(vault, account, now, swap, wallet_address=funding.WALLET)
    else:
        assert claim(vault, account, now, swap, wallet_address=funding.WALLET)["claim_granted"]
        with pytest.raises(PortfolioError):
            funding.claim(vault, account, now, deposit)
        with vault.session() as db:
            report_swap(db, swap["id"], account_id=account, tx_hash=funding.TX_HASH, now=now)
        with pytest.raises(PortfolioError):
            funding.claim(vault, account, now, deposit)
    vault.lock()
