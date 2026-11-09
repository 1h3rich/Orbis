"""Retirada USDT/BSC: planes cifrados y credenciales/transportes ficticios."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest

from app.portfolio.domain import EventInput,PortfolioError
from app.portfolio.models import RealLiveStop,RealMexcWithdrawal
from app.portfolio.mexc_withdrawal_journal import (
    WithdrawalError,acknowledge_withdrawal,assert_no_pending_withdrawals,cancel_prepared,
    claim_withdrawal,estimate_max_withdrawal,prepare_withdrawal,public_record,withdrawal_record,
)
from app.portfolio.repository import append_event,account_snapshot
from app.portfolio.vault import RealVault,VaultError
from app.portfolio.wallet_funding import _encoded
from tests.test_wallet_funding import setup as base_setup,ACCOUNT_REF,WALLET,CONTRACT,PASSWORD


def source(now):
    return {"route":{"source":"MEXC_SPOT_PRIVATE_READONLY","chain_id":"0x38","asset":"USDT","network":"BSC",
        "token_contract":CONTRACT,"fee":"0.01","minimum":"1","maximum":"100000","increment":None,
        "recipient_listed":True,"observed_at":now.isoformat()},"balances":{"free":"100","locked":"0"},
        "observed_at":now.isoformat()}


def setup(tmp_path):
    vault,account,now = base_setup(tmp_path)
    with vault.session() as db:
        append_event(db,account,str(uuid4()),EventInput("CONTRIBUTION","USDT",now-timedelta(days=1),amount=Decimal("100")))
        db.add(RealLiveStop(account_ref=ACCOUNT_REF,stopped=True,updated_at=now))
        db.commit()
    return vault,account,now


def prepare(vault,account,now,**changes):
    values = dict(identifier=str(uuid4()),account_id=account,account_ref=ACCOUNT_REF,address=WALLET,
        decimals=18,amount="10.123456789012345678",max_fee="0.02",source=source(now),now=now)
    values.update(changes)
    with vault.session() as db:
        return prepare_withdrawal(db,**values)


def claim(vault,account,now,plan,**changes):
    values = dict(account_id=account,account_ref=ACCOUNT_REF,address=WALLET,plan_digest=plan["plan_digest"],
        source=source(now),now=now)
    values.update(changes)
    with vault.session() as db:
        return claim_withdrawal(db,plan["id"],**values)


def test_plan_exact_decimal_bounds_claim_once_ack_not_wallet_receipt_or_book(tmp_path):
    vault,account,now = setup(tmp_path)
    plan = prepare(vault,account,now)
    assert plan["amount"] == "10.123456789012345678"
    assert plan["max_debit"] == "10.143456789012345678"
    assert plan["min_receive"] == "10.103456789012345678"
    assert not plan["fee_limit_exchange_enforced"] and not plan["wallet_received"]
    assert prepare(vault,account,now,identifier=plan["id"])["plan_digest"] == plan["plan_digest"]
    assert claim(vault,account,now,plan)["claim_granted"]
    assert not claim(vault,account,now,plan)["claim_granted"]
    with pytest.raises(WithdrawalError,match="pendiente"):
        prepare(vault,account,now)
    with vault.session() as db:
        accepted = acknowledge_withdrawal(db,plan["id"],"fixture-withdrawal-id",now=now)
        assert accepted["status"] == "ACKNOWLEDGED" and not accepted["wallet_received"] and not accepted["booked"]
        assert acknowledge_withdrawal(db,plan["id"],"fixture-withdrawal-id",now=now) == accepted
        with pytest.raises(WithdrawalError,match="contradictoria"):
            acknowledge_withdrawal(db,plan["id"],"different-id",now=now)
        assert account_snapshot(db,account).cash["USDT"] == Decimal("100")
        row,_,_ = withdrawal_record(db,plan["id"])
        assert WALLET not in row.payload_json and "fixture-withdrawal-id" not in row.state_json
        with pytest.raises(WithdrawalError,match="reabrir"):
            assert_no_pending_withdrawals(db,ACCOUNT_REF,now+timedelta(days=20))
    backup = vault.backup(tmp_path / "withdrawal-backup.db")
    vault.lock()
    copied = RealVault(backup)
    copied.unlock(PASSWORD)
    with copied.session() as db:
        assert public_record(db,plan["id"],now)["status"] == "ACKNOWLEDGED"
    copied.lock()


def test_rehashed_roundtrip_binding_cannot_move_between_withdrawals(tmp_path):
    vault, account, now = setup(tmp_path)
    first = prepare(vault, account, now)
    with vault.session() as db:
        _, terms, _ = withdrawal_record(db, first["id"])
        copied_binding = terms["roundtrip_wallet"]
        cancel_prepared(db, first["id"], account, now=now)
    second = prepare(vault, account, now)
    with vault.session() as db:
        row, terms, _ = withdrawal_record(db, second["id"])
        assert copied_binding["funding_digest"] == terms["roundtrip_wallet"]["funding_digest"]
        terms["roundtrip_wallet"] = copied_binding
        row.payload_json, row.payload_digest = _encoded(terms)
        forged = {**second, "plan_digest":row.payload_digest}
        db.commit()
    with pytest.raises(WithdrawalError, match="no es coherente"):
        claim(vault, account, now, forged)
    with vault.session() as db:
        assert db.get(RealMexcWithdrawal, second["id"]).status == "PREPARED"
    vault.lock()


def test_legacy_withdrawal_preparation_retry_keeps_original_digest(tmp_path):
    vault, account, now = setup(tmp_path)
    plan = prepare(vault, account, now)
    with vault.session() as db:
        row, terms, _ = withdrawal_record(db, plan["id"])
        terms.pop("roundtrip_wallet")
        row.payload_json, row.payload_digest = _encoded(terms)
        legacy = {**plan, "plan_digest":row.payload_digest}
        db.commit()
    assert prepare(vault, account, now, identifier=plan["id"])["plan_digest"] == legacy["plan_digest"]
    assert claim(vault, account, now, legacy)["claim_granted"]
    vault.lock()


@pytest.mark.parametrize("changes",[
    {"amount":"100"},{"amount":"0.001"},{"amount":"1e100"},{"amount":"NaN"},
    {"max_fee":"0.001"},{"max_fee":"11"},{"decimals":True},{"address":CONTRACT},
])
def test_invalid_money_or_incompatible_token_terms_never_prepare(tmp_path,changes):
    vault,account,now = setup(tmp_path)
    with pytest.raises((WithdrawalError,PortfolioError)):
        prepare(vault,account,now,**changes)
    vault.lock()


def test_stop_freshness_route_balance_and_book_revision_are_revalidated(tmp_path):
    vault,account,now = setup(tmp_path)
    plan = prepare(vault,account,now)
    changed = source(now)
    changed["route"]["fee"] = "0.015"
    with pytest.raises(WithdrawalError,match="cambiaron"):
        claim(vault,account,now,plan,source=changed)
    with pytest.raises(WithdrawalError,match="caducado"):
        claim(vault,account,now+timedelta(minutes=3),plan)
    with vault.session() as db:
        db.get(RealLiveStop,ACCOUNT_REF).stopped = False
        db.commit()
    with pytest.raises(WithdrawalError,match="Detén"):
        claim(vault,account,now,plan)
    with vault.session() as db:
        db.get(RealLiveStop,ACCOUNT_REF).stopped = True
        db.get(RealLiveStop,ACCOUNT_REF).updated_at = now+timedelta(seconds=1)
        db.commit()
    with pytest.raises(WithdrawalError,match="cambiaron"):
        claim(vault,account,now+timedelta(seconds=1),plan)
    with vault.session() as db:
        cancel_prepared(db,plan["id"],account,now=now+timedelta(seconds=1))
    current = prepare(vault,account,now+timedelta(seconds=1))
    with vault.session() as db:
        append_event(db,account,str(uuid4()),EventInput("CONTRIBUTION","USDT",now,amount=Decimal("1")))
    with pytest.raises(WithdrawalError,match="saldo"):
        claim(vault,account,now+timedelta(seconds=1),current)
    vault.lock()


def test_published_increment_is_not_guessed_as_decimals_or_silently_rounded(tmp_path):
    vault,account,now = setup(tmp_path)
    observed = source(now)
    observed["route"]["increment"] = "0.25"
    with pytest.raises(WithdrawalError,match="múltiplo"):
        prepare(vault,account,now,source=observed,amount="10.13")
    assert prepare(vault,account,now,source=observed,amount="10.25")["amount"] == "10.250000000000000000"
    vault.lock()


@pytest.mark.parametrize("increment,maximum,fee,expected",[
    (None,"100000",None,"99.99"),
    ("0.125","100000",None,"99.875"),
    ("0.25","50.06","0.02","50"),
    ("0.000000000000000003","100000","0.02","99.979999999999999998"),
])
def test_maximum_uses_the_greatest_admissible_amount_and_prepares_separately(tmp_path,increment,maximum,fee,expected):
    vault,account,now = setup(tmp_path)
    observed = source(now)
    observed["route"].update(increment=increment,maximum=maximum)
    with vault.session() as db:
        quote = estimate_max_withdrawal(db,account_id=account,account_ref=ACCOUNT_REF,address=WALLET,
            decimals=18,max_fee=fee,source=observed,now=now)
        assert quote["amount"] == expected and quote["max_fee"] == (fee or "0.01")
        assert Decimal(quote["max_debit"]) == Decimal(expected)+Decimal(fee or "0.01")
        assert Decimal(quote["min_receive"]) == Decimal(expected)-Decimal(fee or "0.01")
        assert db.query(RealMexcWithdrawal).count() == 0 and account_snapshot(db,account).cash["USDT"] == 100
    plan = prepare(vault,account,now,source=observed,amount=quote["amount"],max_fee=quote["max_fee"])
    assert plan["max_debit"] == quote["max_debit"] and plan["min_receive"] == quote["min_receive"]
    vault.lock()


def test_maximum_respects_wallet_precision_and_rejects_unavailable_minimum(tmp_path):
    vault,account,now = setup(tmp_path)
    observed = source(now)
    observed["route"].update(fee="0.000000000000000001",minimum="99")
    with vault.session() as db:
        append_event(db,account,str(uuid4()),EventInput("CONTRIBUTION","USDT",now,amount=Decimal("0.0000000000000000009")))
        observed["balances"]["free"] = "100.0000000000000000009"
        arguments = dict(account_id=account,account_ref=ACCOUNT_REF,address=WALLET,decimals=18,
                         max_fee=None,source=observed,now=now)
        quote = estimate_max_withdrawal(db,**arguments)
        assert quote["amount"] == "99.999999999999999999" and Decimal(quote["max_debit"]) == 100
        with pytest.raises(WithdrawalError,match="supera el filtro"):
            estimate_max_withdrawal(db,**{**arguments,"max_fee":"0"})
        observed["route"]["minimum"] = "100"
        with pytest.raises(WithdrawalError,match="no cubre el mínimo"):
            estimate_max_withdrawal(db,**arguments)
    vault.lock()


def test_precision_rounding_respects_the_increment_across_decimal_magnitudes(monkeypatch):
    from app.portfolio import mexc_withdrawal_journal as journal

    monkeypatch.setattr(journal,"MAX_INPUT_DIGITS",3)
    for capacity in (0,1,999,1000,1017,1999,9999,10000,10007,10891):
        for step in (1,3,11,19,100,1001):
            expected = max(number for number in range(0,capacity+1,step)
                           if len(str(number).rstrip("0")) <= 3)
            assert journal._largest_exact_multiple(capacity,step) == expected


def test_cancel_never_undoes_started_withdrawal_and_unknown_survives_restart(tmp_path):
    vault,account,now = setup(tmp_path)
    plan = prepare(vault,account,now)
    with vault.session() as db:
        assert cancel_prepared(db,plan["id"],account,now=now)["status"] == "CANCELLED"
        assert_no_pending_withdrawals(db,ACCOUNT_REF,now)
    new = prepare(vault,account,now)
    claim(vault,account,now,new)
    with vault.session() as db:
        with pytest.raises(WithdrawalError,match="ya intentada"):
            cancel_prepared(db,new["id"],account,now=now)
    vault.lock()
    vault.unlock(PASSWORD)
    assert not claim(vault,account,now+timedelta(days=10),new)["claim_granted"]
    with vault.session() as db:
        with pytest.raises(WithdrawalError):
            assert_no_pending_withdrawals(db,ACCOUNT_REF,now+timedelta(days=10))
    vault.lock()


def test_concurrent_connections_grant_only_one_attempt(tmp_path):
    vault,account,now = setup(tmp_path)
    plan = prepare(vault,account,now)
    other = RealVault(vault.path)
    other.unlock(PASSWORD)
    barrier = Barrier(2)
    def perform(local):
        barrier.wait()
        return claim(local,account,now,plan)["claim_granted"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(perform,[vault,other])).count(True) == 1
    other.lock()
    vault.lock()


def test_v27_migration_preserves_book_and_backup_rejects_changed_withdrawal(tmp_path):
    vault,account,now = setup(tmp_path)
    with vault.session() as db:
        db.connection().exec_driver_sql("DROP TABLE real_mexc_withdrawals")
        db.connection().exec_driver_sql("PRAGMA user_version = 27")
        db.commit()
    vault.lock()
    vault.unlock(PASSWORD)
    assert vault.last_migration_backup and vault.last_migration_backup.exists()
    plan = prepare(vault,account,now)
    with vault.session() as db:
        db.get(RealMexcWithdrawal,plan["id"]).payload_digest = "0"*64
        db.commit()
    with pytest.raises(VaultError,match="withdrawal evidence"):
        vault.backup(tmp_path / "tampered.db")
    vault.lock()


@pytest.mark.parametrize("status",["PREPARED","SUBMISSION_UNKNOWN","ACKNOWLEDGED"])
def test_actual_trading_resume_keeps_stop_while_withdrawal_is_unresolved(tmp_path,status):
    from tests.test_mexc_execution_recovery import setup as execution_setup,draft,opening,resume,ExchangeOrders
    from app.live_execution.journal import stop_spot_account
    from app.live_execution.mexc_execution import MexcExecutionError
    vault,previous,account,venue,clock = execution_setup(tmp_path)
    limits = draft(vault,previous,clock)
    stop_spot_account(vault,previous.account_ref,now=clock())
    anchor = opening(vault,account,venue,clock)
    observed = source(clock())
    with vault.session() as db:
        observed["balances"]["free"] = str(account_snapshot(db,account).cash["USDT"])
        plan = prepare_withdrawal(db,identifier=str(uuid4()),account_id=account,account_ref=previous.account_ref,
            address=WALLET,decimals=18,amount="10",max_fee="0.02",source=observed,now=clock())
        if status != "PREPARED":
            claim_withdrawal(db,plan["id"],account_id=account,account_ref=previous.account_ref,address=WALLET,
                plan_digest=plan["plan_digest"],source=observed,now=clock())
        if status == "ACKNOWLEDGED":
            acknowledge_withdrawal(db,plan["id"],"fixture-external-id",now=clock())
    trader = ExchangeOrders(venue,clock)
    with pytest.raises(MexcExecutionError,match="retirada"):
        resume(vault,limits,anchor,venue,clock,trader)
    with vault.session() as db:
        assert db.get(RealLiveStop,previous.account_ref).stopped
    assert not trader.posts
    vault.lock()


def test_prepared_review_checks_exact_expiry_boundary_without_extending_it(tmp_path):
    from app.portfolio.mexc_withdrawal_journal import prepared_review_terms

    vault,account,now = setup(tmp_path)
    plan = prepare(vault,account,now)
    deadline = datetime.fromisoformat(plan["expires_at"])
    selection = {"account_id":account,"account_ref":ACCOUNT_REF,"address":WALLET,
                 "contract":CONTRACT,"decimals":18,"plan_digest":plan["plan_digest"]}
    with vault.session() as db:
        terms = prepared_review_terms(db,plan["id"],**selection,now=deadline-timedelta(microseconds=1))
        assert terms["expires_at"] == plan["expires_at"]
        with pytest.raises(WithdrawalError,match="caducó"):
            prepared_review_terms(db,plan["id"],**selection,now=deadline)
        assert public_record(db,plan["id"],deadline) == {**plan,"expired":True}
