"""USDT/BSC: aritmética, una concesión, incertidumbre y privacidad durables."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4

import pytest

from app.portfolio.models import RealMexcAccountLink, RealWalletFundingIntent
from app.portfolio.repository import create_account
from app.portfolio.vault import RealVault, VaultError
from app.portfolio.wallet_funding import (
    WalletFundingError, _encoded, claim_funding, exact_units, funding_record, list_funding, prepare_funding, report_funding,
)


PASSWORD = "isolated funding vault password"
WALLET,DESTINATION,CONTRACT = ["0x"+value*40 for value in ("1","2","3")]
ACCOUNT_REF = "a"*64
TX_HASH = "0x"+"b"*64


def setup(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSWORD,create=True)
    with vault.session() as db:
        account = create_account(db,"MEXC","USDT").id
        db.add(RealMexcAccountLink(account_ref=ACCOUNT_REF,account_id=account,created_at=datetime.now(timezone.utc)))
        db.commit()
    return vault,account,datetime.now(timezone.utc)


def destination(now):
    return {"source":"MEXC_SPOT_PRIVATE_READONLY","network":"BNB_SMART_CHAIN_MAINNET","asset":"USDT",
            "token_contract":CONTRACT,"deposit_address":DESTINATION,"observed_at":now.isoformat(),
            "stored":False,"transfer_sent":False}


def prepare(vault,account,now,identifier=None,**kwargs):
    values = dict(identifier=identifier or str(uuid4()),account_id=account,account_ref=ACCOUNT_REF,
        wallet_address=WALLET,destination=destination(now),amount="12.345678901234567891",
        minimum_deposit_usdt="0.000000000000000001",decimals=18,
        nonce="3",gas="90000",gas_price="1000000000",max_gas_bnb="0.0001",now=now)
    values.update(kwargs)
    with vault.session() as db:
        return prepare_funding(db,**values)


def claim(vault,account,now,plan,**kwargs):
    values = dict(account_id=account,account_ref=ACCOUNT_REF,wallet_address=WALLET,
                  destination=destination(now),plan_digest=plan["plan_digest"],now=now)
    values.update(kwargs)
    with vault.session() as db:
        return claim_funding(db,plan["id"],**values)


def test_exact_bep20_amounts_and_gas_never_round_through_float():
    assert exact_units("12.345678901234567891",18) == 12345678901234567891
    assert exact_units("0.000000000000000001",18) == 1
    assert exact_units("0.00",18,allow_zero=True) == 0
    for value,decimals in (("0.0000001",6),("0",18),("-1",18),("NaN",18),("1e100",18),("1",True)):
        with pytest.raises(WalletFundingError):
            exact_units(value,decimals)


def test_prepare_is_immutable_ephemeral_and_requires_no_wallet_or_exchange_write(tmp_path):
    vault,account,now = setup(tmp_path)
    plan = prepare(vault,account,now)
    assert plan["status"] == "PREPARED" and not plan["exchange_credited"] and not plan["booked"]
    assert plan["transaction"]["data"] == "0xa9059cbb"+DESTINATION[2:].zfill(64)+hex(12345678901234567891)[2:].zfill(64)
    assert plan["transaction"]["chainId"] == "0x38" and plan["transaction"]["value"] == "0x0"
    replay = prepare(vault,account,now+timedelta(seconds=1),identifier=plan["id"])
    assert replay["plan_digest"] == plan["plan_digest"] and replay["expires_at"] == plan["expires_at"]
    with pytest.raises(WalletFundingError,match="otros datos"):
        prepare(vault,account,now,identifier=plan["id"],amount="13")
    with vault.session() as db:
        row,_,_ = funding_record(db,plan["id"])
        assert WALLET not in row.payload_json and DESTINATION not in row.payload_json
        assert WALLET not in str(list_funding(db,account,now=now))
    vault.lock()


def test_funding_minimum_uses_exact_units_and_is_bound_to_the_plan(tmp_path):
    vault, account, now = setup(tmp_path)
    minimum = "12.345678901234567891"
    with pytest.raises(WalletFundingError, match="inferior al mínimo"):
        prepare(vault, account, now, amount="12.345678901234567890", minimum_deposit_usdt=minimum)
    with vault.session() as db:
        assert list_funding(db, account, now=now) == []
    plan = prepare(vault, account, now, minimum_deposit_usdt=minimum)
    assert plan["amount_units"] == plan["minimum_deposit_units"] == "12345678901234567891"
    with pytest.raises(WalletFundingError, match="otros datos"):
        prepare(vault, account, now, identifier=plan["id"], minimum_deposit_usdt="1")
    assert claim(vault, account, now, plan)["claim_granted"]
    vault.lock()


def test_legacy_funding_plan_requires_new_minimum_but_claimed_plan_can_recover(tmp_path):
    vault, account, now = setup(tmp_path)

    def legacy_plan(plan):
        with vault.session() as db:
            row, terms, _ = funding_record(db, plan["id"])
            terms.pop("minimum_deposit_units")
            row.payload_json, row.payload_digest = _encoded(terms)
            db.commit()
            return {**plan, "plan_digest": row.payload_digest}

    unclaimed = legacy_plan(prepare(vault, account, now))
    with pytest.raises(WalletFundingError, match="nuevo ingreso con el mínimo"):
        claim(vault, account, now, unclaimed)
    claimed = prepare(vault, account, now, nonce="4")
    assert claim(vault, account, now, claimed)["claim_granted"]
    claimed = legacy_plan(claimed)
    assert not claim(vault, account, now, claimed)["claim_granted"]
    with vault.session() as db:
        recovered = report_funding(db, claimed["id"], account_id=account, tx_hash=TX_HASH, now=now)
        assert recovered["status"] == "HASH_REPORTED"
        assert recovered["minimum_deposit_units"] is None and not recovered["exchange_credited"]
    vault.lock()


def test_one_claim_and_hash_reporting_do_not_credit_money_or_repeat_wallet_request(tmp_path):
    vault,account,now = setup(tmp_path)
    plan = prepare(vault,account,now)
    assert claim(vault,account,now,plan)["claim_granted"]
    assert not claim(vault,account,now,plan)["claim_granted"]
    second = prepare(vault,account,now)
    with pytest.raises(WalletFundingError,match="sin resultado"):
        claim(vault,account,now,second)
    with vault.session() as db:
        report = report_funding(db,plan["id"],account_id=account,tx_hash=TX_HASH,now=now)
        assert report["status"] == "HASH_REPORTED" and not report["exchange_credited"] and not report["booked"]
    with vault.session() as db:
        assert report_funding(db,plan["id"],account_id=account,tx_hash=TX_HASH,now=now) == report
    with vault.session() as db:
        with pytest.raises(WalletFundingError,match="otro hash"):
            report_funding(db,plan["id"],account_id=account,tx_hash="0x"+"c"*64,now=now)
    assert not claim(vault,account,now,plan)["claim_granted"]
    with pytest.raises(WalletFundingError,match="nonce ya se utilizó"):
        claim(vault,account,now,second)
    third = prepare(vault,account,now,nonce="4")
    assert claim(vault,account,now,third)["claim_granted"]
    vault.lock()
    vault.unlock(PASSWORD)
    assert not claim(vault,account,now,plan)["claim_granted"]
    with vault.session() as db:
        row,_,_ = funding_record(db,plan["id"])
        assert TX_HASH not in row.state_json
    vault.lock()


def test_expired_or_changed_destination_or_gas_limit_never_grants_claim(tmp_path):
    vault,account,now = setup(tmp_path)
    with pytest.raises(WalletFundingError,match="comisión"):
        prepare(vault,account,now,max_gas_bnb="0.000000001")
    plan = prepare(vault,account,now)
    with pytest.raises(WalletFundingError,match="caducó"):
        claim(vault,account,now+timedelta(minutes=3),plan)
    with pytest.raises(WalletFundingError,match="destino actual"):
        claim(vault,account,now,plan,destination={**destination(now),"deposit_address":"0x"+"4"*40})
    with pytest.raises(WalletFundingError,match="caducó"):
        prepare(vault,account,now,destination=destination(now-timedelta(minutes=1)))
    vault.lock()


def test_claim_serializes_across_two_vault_instances(tmp_path):
    vault,account,now = setup(tmp_path)
    plan = prepare(vault,account,now)
    other = RealVault(vault.path)
    other.unlock(PASSWORD)
    barrier = Barrier(2)
    def run(instance):
        barrier.wait()
        return claim(instance,account,now,plan)["claim_granted"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(run,(vault,other))).count(True) == 1
    other.lock()
    vault.lock()


def test_wallet_rejection_is_a_report_not_a_receipt_and_preserves_audit(tmp_path):
    vault,account,now = setup(tmp_path)
    plan = prepare(vault,account,now)
    claim(vault,account,now,plan)
    with vault.session() as db:
        result = report_funding(db,plan["id"],account_id=account,tx_hash=None,rejected=True,now=now)
        assert result["status"] == "WALLET_REJECTED" and not result["exchange_credited"]
    assert not claim(vault,account,now,plan)["claim_granted"]
    another = prepare(vault,account,now,nonce="3")
    assert claim(vault,account,now,another)["claim_granted"]
    vault.lock()


def test_migration_backup_and_corrupt_funding_evidence(tmp_path):
    vault,account,now = setup(tmp_path)
    with vault.session() as db:
        db.connection().exec_driver_sql("DROP TABLE real_wallet_funding_intents")
        db.connection().exec_driver_sql("PRAGMA user_version = 25")
        db.commit()
    vault.lock()
    vault.unlock(PASSWORD)
    assert vault.last_migration_backup and vault.last_migration_backup.exists()
    plan = prepare(vault,account,now)
    backup = vault.backup(tmp_path / "funding-backup.db")
    copied = RealVault(backup)
    copied.unlock(PASSWORD)
    with copied.session() as db:
        assert list_funding(db,account,now=now)[0]["id"] == plan["id"]
    copied.lock()
    with vault.session() as db:
        db.get(RealWalletFundingIntent,plan["id"]).state_digest = "0"*64
        db.commit()
    with pytest.raises(VaultError,match="funding evidence"):
        vault.backup(tmp_path / "tampered.db")
    vault.lock()
