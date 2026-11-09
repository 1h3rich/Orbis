"""Evidencia causal archivada; ninguna revisión concede autoridad de trading."""

from uuid import uuid4
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest

from app.live_execution.mexc_continuity_review import MexcContinuityError, record_continuity_review, read_continuity_review
from app.live_execution.journal import _stored_policy, register_spot_policy
from app.live_execution.mexc_execution_state import fetch_spot_execution_inputs,MexcExecutionStateError
from app.portfolio.domain import EventInput
from app.portfolio.repository import account_book_revision, account_snapshot, append_event
from app.portfolio.mexc_parity import assess_mexc_spot_parity
from app.portfolio.mexc_parity_archive import _canonical_report, _digest, add_observation
from app.portfolio.models import RealMexcParityObservation
from app.portfolio.vault import VaultError
from tests.test_mexc_execution_state import setup, PASSWORD


def test_review_is_causal_encrypted_replayable_and_does_not_extend_policy(tmp_path):
    vault, policy, account, venue, clock = setup(tmp_path)
    identifier = str(uuid4())
    with vault.session() as db:
        before = account_snapshot(db, account)
    result = record_continuity_review(vault, venue, policy.policy_id, review_id=identifier,
        assert_active=lambda:None, clock=clock, quote_get=venue.quote_get, rules_fetch=venue.rules_fetch)
    assert result["causal"]["protocol"] == "MEXC_CAUSAL_REVIEW_V1"
    assert result["causal"]["opening_id"] == policy.reconciliation_anchor_id
    assert result["causal"]["authorizes_renewal"] is False and result["orders_enabled"] is False
    assert result["causal"]["checks"][0]["started_at"] <= result["causal"]["checks"][1]["started_at"]
    assert result["causal"]["boundary_at"] == result["causal"]["checks"][0]["started_at"]
    with vault.session() as db:
        assert _stored_policy(db, policy.policy_id) == policy
        assert account_snapshot(db, account) == before
    vault.backup(tmp_path / "review-copy.db")
    vault.lock(); vault.unlock(PASSWORD)
    assert read_continuity_review(vault, identifier) == result
    venue.calls.clear()
    assert record_continuity_review(vault, venue, policy.policy_id, review_id=identifier,
        assert_active=lambda:None, clock=clock, quote_get=venue.quote_get, rules_fetch=venue.rules_fetch) == result
    assert venue.calls == []
    vault.lock()


def test_next_policy_checks_transfers_since_first_sweep_not_review_completion(tmp_path):
    vault,parent,_,venue,clock = setup(tmp_path)
    report = record_continuity_review(vault,venue,parent.policy_id,review_id=str(uuid4()),
        assert_active=lambda:None,clock=clock,quote_get=venue.quote_get,rules_fetch=venue.rules_fetch)
    boundary = datetime.fromisoformat(report["causal"]["boundary_at"])
    # Una transferencia que sólo aparece en la siguiente lectura, ocurrida
    # DURANTE la revisión, no puede quedar fuera del nuevo intervalo vigilado.
    child = replace(parent,policy_id=str(uuid4()),reconciliation_anchor_id=report["id"],
                    expires_at=boundary+timedelta(days=1))
    register_spot_policy(vault,child,now=clock())
    assert fetch_spot_execution_inputs(vault,venue,child,clock=clock,
        quote_get=venue.quote_get,rules_fetch=venue.rules_fetch).state.reconciled
    transfer_at = boundary+timedelta(milliseconds=1)
    assert transfer_at < datetime.fromisoformat(report["checked_at"])
    venue.deposits = [{"direction":"deposit","asset":"USDT","network":"BSC",
        "amount":"1","fee":"0","status":5,"time_ms":int(transfer_at.timestamp()*1000)}]
    with pytest.raises(MexcExecutionStateError):
        fetch_spot_execution_inputs(vault,venue,child,clock=clock,
            quote_get=venue.quote_get,rules_fetch=venue.rules_fetch)
    vault.lock()


@pytest.mark.parametrize("fault",["boundary","risk","coverage","private_time","authority","policy","opening","balances","missing_check","unknown_field","nested_secret","future_transfer_end","cached_second_read"])
def test_backup_rejects_rehashed_review_with_changed_proof(tmp_path,fault):
    vault, policy, _, venue, clock = setup(tmp_path)
    identifier = str(uuid4())
    record_continuity_review(vault, venue, policy.policy_id, review_id=identifier,
        assert_active=lambda:None, clock=clock, quote_get=venue.quote_get, rules_fetch=venue.rules_fetch)
    with vault.session() as db:
        row = db.get(RealMexcParityObservation, identifier)
        report = json.loads(row.report_json)
        causal = report["causal"]
        if fault == "boundary": causal["boundary_at"] = causal["checks"][1]["checked_at"]
        elif fault == "risk": causal["checks"][0]["risk"]["turnover_quote"] = "99999"
        elif fault == "coverage": causal["checks"][0]["trades"]["start_ms"] += 1
        elif fault == "private_time": causal["checks"][0]["balances"]["observed_at"] = "2020-01-01T00:00:00+00:00"
        elif fault == "authority": causal["authorizes_renewal"] = True
        elif fault == "policy": causal["policy_digest"] = "0"*64
        elif fault == "opening": causal["opening_digest"] = "0"*64
        elif fault == "balances": causal["checks"][0]["balances"]["balances"][1].update(free="701",total="701")
        elif fault == "missing_check": causal["checks"].pop()
        elif fault == "unknown_field": causal["checks"][0]["secret"] = "unrecognized fixture column"
        elif fault == "nested_secret": causal["checks"][0]["trades"]["secret"] = "unrecognized fixture column"
        elif fault == "future_transfer_end": causal["checks"][0]["transfers"]["observed_at"] = causal["checks"][0]["started_at"]
        elif fault == "cached_second_read":
            stamp = causal["checks"][0]["balances"]["observed_at"]
            causal["checks"][1]["balances"]["observed_at"] = stamp
            report["second_observed_at"] = stamp
        row.report_json = _canonical_report(report)
        row.report_digest = _digest(row)
        db.commit()
    with pytest.raises(VaultError, match="causal"):
        vault.backup(tmp_path / "invalid-overlap.db")
    vault.lock()


def test_opening_cannot_be_replaced_during_causal_review(tmp_path):
    vault, policy, _, venue, clock = setup(tmp_path)
    identifier = str(uuid4())
    def replace_opening():
        venue.on_window = None
        with vault.session() as db:
            row = db.get(RealMexcParityObservation, policy.reconciliation_anchor_id)
            report = json.loads(row.report_json)
            report["changed_during_review"] = True
            row.report_json = _canonical_report(report)
            row.report_digest = _digest(row)
            db.commit()
    venue.on_window = replace_opening
    with pytest.raises(MexcContinuityError, match="apertura"):
        record_continuity_review(vault, venue, policy.policy_id, review_id=identifier,
            assert_active=lambda:None, clock=clock, quote_get=venue.quote_get, rules_fetch=venue.rules_fetch)
    with pytest.raises(MexcContinuityError, match="encontrada"):
        read_continuity_review(vault, identifier)
    vault.lock()


def test_network_error_cannot_create_a_review_or_disclose_provider_text(tmp_path):
    vault, policy, _, venue, clock = setup(tmp_path)
    identifier = str(uuid4())
    def unavailable(): raise TimeoutError("fixture provider secret must stay private")
    venue.on_window = unavailable
    with pytest.raises(MexcContinuityError) as error:
        record_continuity_review(vault, venue, policy.policy_id, review_id=identifier,
            assert_active=lambda:None, clock=clock, quote_get=venue.quote_get, rules_fetch=venue.rules_fetch)
    assert "fixture provider secret" not in str(error.value)
    with pytest.raises(MexcContinuityError, match="encontrada"):
        read_continuity_review(vault, identifier)
    vault.lock()


def test_day_boundary_preserves_position_cost_and_recomputes_only_daily_flow(tmp_path):
    vault, previous, account, venue, clock = setup(tmp_path)
    day = (datetime.now(timezone.utc)+timedelta(days=1)).date()
    clock.value = datetime.combine(day, datetime.min.time(), timezone.utc)+timedelta(hours=23,minutes=50)
    # Saldo inicial revisado con coste registrado: no se afirma ejecución MEXC
    # de esa compra anterior a la apertura ni se importa un fill ficticio.
    with vault.session() as db:
        append_event(db, account, str(uuid4()), EventInput("BUY","USDT",clock.value-timedelta(minutes=10),
            "BTC",Decimal("0.001"),Decimal("65000"),fee=Decimal("0.1")))
    venue.base, venue.quote = "0.001", "634.9"
    first, second = venue.balances(), venue.balances()
    opening = str(uuid4())
    with vault.session() as db:
        revision = account_book_revision(db, account)
        report = assess_mexc_spot_parity(account_snapshot(db, account),first,second,
            book_revision_before=revision,book_revision_after=revision,now=clock())
        add_observation(db,opening,account,previous.account_ref,report); db.commit()
    policy = replace(previous,policy_id=str(uuid4()),reconciliation_anchor_id=opening,
                     expires_at=clock.value+timedelta(hours=1))
    register_spot_policy(vault,policy,now=clock())
    def review():
        return record_continuity_review(vault, venue, policy.policy_id, review_id=str(uuid4()),
            assert_active=lambda:None, clock=clock, quote_get=venue.quote_get, rules_fetch=venue.rules_fetch)
    before = review()
    clock.value += timedelta(minutes=20)
    after = review()
    old_risk, new_risk = (item["causal"]["checks"][-1]["risk"] for item in (before,after))
    assert Decimal(old_risk["turnover_quote"]) == Decimal("65")
    assert Decimal(new_risk["turnover_quote"]) == 0
    assert old_risk["position_cost_basis_quote"] == new_risk["position_cost_basis_quote"]
    assert Decimal(new_risk["position_cost_basis_quote"]) == Decimal("65.1")
    assert old_risk["position_quantity_base"] == new_risk["position_quantity_base"] == "0.001"
    assert read_continuity_review(vault,before["id"]) == before
    with vault.session() as db: assert _stored_policy(db,policy.policy_id) == policy
    vault.backup(tmp_path/"day-boundary.db")
    vault.lock()


@pytest.mark.parametrize("fault",["partial_window","transfer","foreign_roundtrip","cached_private","manual_movement","stop","expiry"])
def test_unusable_or_changed_evidence_never_creates_a_checkpoint(tmp_path,fault):
    vault, policy, account, venue, clock = setup(tmp_path)
    identifier = str(uuid4())
    if fault == "partial_window":
        original = venue.trades_window
        def partial(*args,**kwargs):
            result = original(*args,**kwargs)
            result["coverage"] = "RECENT_MONTH_SINGLE_PAGE_INCOMPLETE"
            return result
        venue.trades_window = partial
    elif fault == "cached_private":
        original = venue.balances
        def cached():
            result = original()
            result["observed_at"] = (datetime.fromisoformat(result["observed_at"])-timedelta(seconds=10)).isoformat()
            return result
        venue.balances = cached
    elif fault == "transfer":
        venue.withdrawals = [{"direction":"withdrawal","asset":"USDT","network":"BSC",
            "amount":"1","fee":"0","status":7,"time_ms":int(clock.value.timestamp()*1000)}]
    elif fault == "foreign_roundtrip":
        venue.trades = [{"id":str(index),"order_id":str(index),"side":side,"time_ms":int(clock.value.timestamp()*1000),
            "price":"65000","quantity":"0.001","quote_quantity":"65","commission":"0","commission_asset":"USDT"}
            for index,side in ((1,"BUY"),(2,"SELL"))]
    else:
        def changed():
            venue.on_window = None
            if fault == "expiry": clock.value = policy.expires_at+timedelta(seconds=1)
            elif fault == "stop":
                from app.live_execution.journal import stop_spot_account
                stop_spot_account(vault,policy.account_ref,now=clock())
            else:
                with vault.session() as db:
                    append_event(db,account,str(uuid4()),EventInput("CONTRIBUTION","USDT",clock(),amount=Decimal("1")))
                venue.quote = "701"
        venue.on_window = changed
    with pytest.raises(MexcContinuityError):
        record_continuity_review(vault, venue, policy.policy_id, review_id=identifier,
            assert_active=lambda:None, clock=clock, quote_get=venue.quote_get, rules_fetch=venue.rules_fetch)
    with pytest.raises(MexcContinuityError, match="encontrada"):
        read_continuity_review(vault, identifier)
    with vault.session() as db: assert _stored_policy(db,policy.policy_id) == policy
    vault.lock()


@pytest.mark.parametrize("before_read",[False,True])
def test_revocation_wins_without_writing_a_review(tmp_path,before_read):
    vault, policy, _, venue, clock = setup(tmp_path)
    identifier, revoked = str(uuid4()), {"value":before_read}
    def authorized():
        if revoked["value"]: raise PermissionError("review permission revoked")
    venue.on_window = lambda:revoked.update(value=True)
    with pytest.raises(PermissionError, match="revoked"):
        record_continuity_review(vault, venue, policy.policy_id, review_id=identifier,
            assert_active=authorized, clock=clock, quote_get=venue.quote_get, rules_fetch=venue.rules_fetch)
    with pytest.raises(MexcContinuityError, match="encontrada"):
        read_continuity_review(vault, identifier)
    if before_read: assert venue.calls == []
    vault.lock()


def test_review_uses_real_owned_fills_with_view_protected_without_touching_permission(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from tests.test_spot_bot_permission import scenario, grant
    from tests.test_mexc_bot_api import authorize
    from tests.mexc_bot_fixture import advance_market
    from app.live_execution.spot_bot_store import owned_spot_bot_ledger

    env,workers,app = scenario(tmp_path,monkeypatch)
    with TestClient(app) as client:
        headers = authorize(client); created,url,body = grant(client,env,headers)
        assert client.post("/real/vault/protect-view",headers=headers).json()["operating"]
        env["paper"].stop(env["session"]["id"]); env["paper"].passphrase = None
        worker = workers[0]; deadline = worker._deadline
        advance_market(env); assert worker.run_once()["action"] == "BUY"
        before,_ = owned_spot_bot_ledger(worker.vault,body["request_id"])
        original = env["venue"].trades_window
        def unknown_provider_columns(*args,**kwargs):
            raw = original(*args,**kwargs)
            raw["api_key"] = "fixture-private-provider-column"
            raw["trades"] = [{**item,"address":"0x1111111111111111111111111111111111111111"} for item in raw["trades"]]
            return raw
        env["venue"].trades_window = unknown_provider_columns
        result = record_continuity_review(worker.vault,env["venue"],created.json()["policy_id"],review_id=str(uuid4()),
            assert_active=worker._authorized,clock=env["clock"],quote_get=env["venue"].quote_get,rules_fetch=env["venue"].rules_fetch)
        risk = result["causal"]["checks"][-1]["risk"]
        assert "fixture-private-provider-column" not in json.dumps(result)
        assert "0x1111111111111111111111111111111111111111" not in json.dumps(result)
        assert Decimal(risk["position_quantity_base"]) == before.position.units
        assert Decimal(risk["position_cost_basis_quote"]) == before.position.cost_quote
        assert owned_spot_bot_ledger(worker.vault,body["request_id"])[0] == before
        assert worker._deadline == deadline and len(env["trader"].posts) == 1
        assert client.get(url,headers=headers).status_code == 401
        assert client.get("/real/vault/status").json()["view_protected"]
        worker.vault.backup(tmp_path/"protected-review.db")


def test_replaying_old_evidence_is_not_a_new_or_extended_permission(tmp_path):
    vault, policy, _, venue, clock = setup(tmp_path)
    identifier = str(uuid4())
    def review(target=identifier,policy_id=policy.policy_id):
        return record_continuity_review(vault,venue,policy_id,review_id=target,assert_active=lambda:None,
            clock=clock,quote_get=venue.quote_get,rules_fetch=venue.rules_fetch)
    original = review()
    clock.value = policy.expires_at+timedelta(days=1)
    venue.calls.clear()
    assert review() == read_continuity_review(vault,identifier) == original
    assert original["causal"]["authorizes_renewal"] is False
    with pytest.raises(MexcContinuityError): review(str(uuid4()))
    with pytest.raises(MexcContinuityError,match="otra política"): review(policy_id=str(uuid4()))
    assert venue.calls == []
    vault.lock()


def test_historical_review_keeps_its_prefix_but_does_not_approve_later_manual_money(tmp_path,monkeypatch):
    from app.portfolio import repository
    vault, policy, account, venue, clock = setup(tmp_path)
    result = record_continuity_review(vault,venue,policy.policy_id,review_id=str(uuid4()),assert_active=lambda:None,
        clock=clock,quote_get=venue.quote_get,rules_fetch=venue.rules_fetch)
    later = datetime.fromisoformat(result["checked_at"])+timedelta(seconds=1)
    class Later(datetime):
        @classmethod
        def now(cls,tz=None): return later if tz else later.replace(tzinfo=None)
    monkeypatch.setattr(repository,"datetime",Later)
    with vault.session() as db:
        append_event(db,account,str(uuid4()),EventInput("CONTRIBUTION","USDT",later,amount=Decimal("1")))
    venue.quote, clock.value = "701", later+timedelta(seconds=1)
    assert read_continuity_review(vault,result["id"]) == result
    with pytest.raises(MexcContinuityError):
        record_continuity_review(vault,venue,policy.policy_id,review_id=str(uuid4()),assert_active=lambda:None,
            clock=clock,quote_get=venue.quote_get,rules_fetch=venue.rules_fetch)
    vault.backup(tmp_path/"historical-review.db")
    vault.lock()
