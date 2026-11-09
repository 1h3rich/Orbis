"""Caídas antes de enviar: decisión/reserva atómicas y cero permisos durables."""

from dataclasses import replace
from datetime import datetime, timedelta
from copy import copy,deepcopy
import pickle
from decimal import Decimal, localcontext
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.live_execution import mexc_spot_bot as executor,spot_bot_store as store
from app.live_execution.journal import LiveJournalError,release_unsent_spot_intent,recover_spot_intent,reserve_spot_intent_in_transaction
from app.live_execution.mexc_order_lifecycle import synchronize_spot_order
from app.live_execution.spot_bot_legacy_review import (
    archive_legacy_cancel, legacy_spot_order_review, observe_legacy_spot_order, read_legacy_spot_decision,
)
from app.live_execution.spot_bot_legacy_fills import archive_legacy_fills, legacy_fills_replay, preview_legacy_fill_recovery
from app.live_execution.spot_bot_store import SpotBotStoreError,bot_record,owned_spot_bot_ledger,encoded
from app.portfolio.api import create_real_router
from app.portfolio.domain import EventInput
from app.portfolio.models import MexcObservedFill, RealLiveOrderObservation, RealLiveSpotIntent, RealSpotBotDecision, RealEvent
from app.portfolio.repository import account_snapshot, append_event
from app.portfolio.vault import RealVault,VaultError
from tests.test_mexc_spot_bot import bot_case,next_entry,tick,PASSWORD
from tests.test_mexc_execution_state import SOURCE
from tests.test_spot_bot_rearm import review


def crash_after_preparation(case,monkeypatch):
    original = executor.submit_anchored_spot_limit
    captured = {}
    def crash(*args,**kwargs):
        captured.update(args=args,kwargs=kwargs)
        with case[0].session() as db:
            assert db.query(RealSpotBotDecision).count() == db.query(RealLiveSpotIntent).count() == 1
            assert db.query(RealLiveSpotIntent).one().status == "RESERVED"
        raise RuntimeError("fixture crash after atomic preparation")
    next_entry(case)
    monkeypatch.setattr(executor,"submit_anchored_spot_limit",crash)
    with pytest.raises(RuntimeError,match="fixture crash"):
        tick(case)
    monkeypatch.setattr(executor,"submit_anchored_spot_limit",original)
    captured["original"] = original
    return captured


def test_crash_has_both_records_survives_reopen_and_can_discard_then_review_without_resending(tmp_path,monkeypatch):
    case = bot_case(tmp_path)
    with case[0].session() as db: before = account_snapshot(db,case[2])
    captured = crash_after_preparation(case,monkeypatch)
    with case[0].session() as db:
        row,_,state = bot_record(db,case[8])
        assert row.status == "PAUSED" and state["last_decision_id"]
        intent = db.query(RealLiveSpotIntent).one().id
        assert store.bot_decision_payload(db,db.query(RealSpotBotDecision).one())["preparation_protocol"] == store.ATOMIC_PREPARATION_PROTOCOL
        assert account_snapshot(db,case[2]) == before
    with pytest.raises(SpotBotStoreError): review(case)
    backup = case[0].backup(tmp_path / "prepared-backup.db")
    copied = RealVault(backup)
    copied.unlock(PASSWORD)
    with copied.session() as db: assert db.query(RealLiveSpotIntent).one().status == "RESERVED"
    copied.lock()
    case[0].lock()
    case[0].unlock(PASSWORD)
    with pytest.raises((SpotBotStoreError,VaultError)):
        captured["kwargs"]["prepared_bot_order"].take(case[0],captured["kwargs"]["bot_intent"],intent)
    assert release_unsent_spot_intent(case[0],intent,now=case[4](),account_id=case[2])["status"] == "RELEASED_UNSENT"
    assert release_unsent_spot_intent(case[0],intent,now=case[4](),account_id=case[2])["status"] == "ALREADY_RELEASED"
    review(case)
    assert tick(case)["action"] == "WAIT" and case[7].posts == []
    with case[0].session() as db: assert account_snapshot(db,case[2]) == before
    case[0].lock()


def test_failure_after_reservation_rolls_back_decision_cursor_and_reservation_together(tmp_path,monkeypatch):
    case = bot_case(tmp_path)
    original = store.reserve_spot_intent_in_transaction
    def fail(db,*args,**kwargs):
        result = original(db,*args,**kwargs)
        assert result.status == "RESERVED" and db.connection().connection.dbapi_connection.in_transaction
        assert db.query(RealSpotBotDecision).count() == db.query(RealLiveSpotIntent).count() == 1
        raise RuntimeError("fixture rollback after inserting reservation")
    monkeypatch.setattr(store,"reserve_spot_intent_in_transaction",fail)
    next_entry(case)
    with pytest.raises(RuntimeError,match="fixture rollback"): tick(case)
    with case[0].session() as db:
        assert db.query(RealSpotBotDecision).count() == db.query(RealLiveSpotIntent).count() == 0
        assert bot_record(db,case[8])[2]["last_decision_id"] is None
    assert case[7].posts == []
    case[0].lock()


def test_preparation_is_single_use_exact_and_bound_to_its_vault_instance(tmp_path,monkeypatch):
    case = bot_case(tmp_path)
    original = executor.submit_anchored_spot_limit
    saved = {}
    def inspect(*args,**kwargs):
        saved.update(args=args,kwargs=kwargs)
        prepared,intent = kwargs["prepared_bot_order"],kwargs["bot_intent"]
        for clone in (copy,deepcopy,pickle.dumps):
            with pytest.raises(TypeError): clone(prepared)
        with pytest.raises(SpotBotStoreError,match="otro intento"):
            prepared.take(case[0],replace(intent,quantity_base=intent.quantity_base*2),kwargs["bot_decision_id"])
        other = RealVault(case[0].path)
        other.unlock(PASSWORD)
        with pytest.raises(SpotBotStoreError,match="otra instancia"):
            prepared.take(other,intent,kwargs["bot_decision_id"])
        other.lock()
        return original(*args,**kwargs)
    monkeypatch.setattr(executor,"submit_anchored_spot_limit",inspect)
    next_entry(case)
    assert tick(case)["order"]["journal_status"] == "RESERVATION_CLOSED"
    repeated = {**saved["kwargs"],"assert_active":lambda:None,"assert_active_in_transaction":lambda db:None}
    with pytest.raises(SpotBotStoreError,match="otro intento"):
        original(*saved["args"],**repeated)
    assert len(case[7].posts) == 1
    case[0].lock()


def test_a_stored_reservation_without_live_preparation_is_only_a_read_replay(tmp_path,monkeypatch):
    case = bot_case(tmp_path)
    captured = crash_after_preparation(case,monkeypatch)
    arguments = {**captured["kwargs"],"assert_active":lambda:None,"assert_active_in_transaction":lambda db:None}
    arguments.pop("prepared_bot_order")
    result = captured["original"](*captured["args"],**arguments)
    assert result["replayed_without_post"] and not result["new_order_post_attempted"] and case[7].posts == []
    case[0].lock()


def test_another_vault_instance_cannot_prepare_a_second_order_for_the_same_cycle(tmp_path,monkeypatch):
    case = bot_case(tmp_path)
    other = RealVault(case[0].path); other.unlock(PASSWORD)
    original = executor.archive_spot_bot_decision
    def duplicate(*args,**kwargs):
        prepared = original(*args,**kwargs)
        second = (other,*args[1:3],replace(args[3],intent_id=str(uuid4())),*args[4:])
        with pytest.raises(SpotBotStoreError,match="ciclo ya preparó"):
            original(*second,**kwargs)
        with other.session() as db:
            assert db.query(RealSpotBotDecision).count() == db.query(RealLiveSpotIntent).count() == 1
        return prepared
    monkeypatch.setattr(executor,"archive_spot_bot_decision",duplicate)
    next_entry(case)
    assert tick(case)["order"]["journal_status"] == "RESERVATION_CLOSED"
    assert len(case[7].posts) == 1
    other.lock(); case[0].lock()


def test_missing_atomic_pair_is_not_recreated_and_backup_rejects_it(tmp_path,monkeypatch):
    case = bot_case(tmp_path)
    captured = crash_after_preparation(case,monkeypatch)
    with case[0].session() as db:
        row = db.query(RealLiveSpotIntent).one()
        original = row.request_json
        row.request_json = "{}"
        db.commit()
        with pytest.raises(SpotBotStoreError,match="pedido.*alterado"):
            store.bot_decision_payload(db,db.query(RealSpotBotDecision).one())
        row.request_json = original
        db.delete(row)
        db.commit()
        with pytest.raises(SpotBotStoreError,match="perdió su reserva"):
            store.bot_decision_payload(db,db.query(RealSpotBotDecision).one())
    arguments = {**captured["kwargs"],"assert_active":lambda:None}
    with pytest.raises(RuntimeError,match="no tiene una reserva preparada"):
        captured["original"](*captured["args"],**arguments)
    with pytest.raises(VaultError,match="bot evidence"): case[0].backup(tmp_path / "invalid.db")
    assert case[7].posts == []
    case[0].lock()


def remove_reservation_as_legacy_fixture(vault, identifier=None):
    with vault.session() as db:
        row = db.get(RealSpotBotDecision, identifier) if identifier else db.query(RealSpotBotDecision).one()
        payload = store.bot_decision_payload(db,row)
        payload.pop("preparation_protocol"); payload.pop("preparation_cycle_id")
        row.payload_json,row.payload_digest = encoded(payload)
        db.delete(db.get(RealLiveSpotIntent,row.intent_id))
        db.commit()
        assert "preparation_protocol" not in store.bot_decision_payload(db,row)
        return row.intent_id


def add_external_cancel_as_legacy_fixture(trader, intent):
    client_id = intent.intent_id.replace("-", "")
    trader.orders[client_id] = {
        **SOURCE, "symbol":intent.symbol, "client_order_id":client_id, "order_id":"order-"+intent.intent_id,
        "type":"LIMIT", "side":intent.side, "price":str(intent.limit_price_quote),
        "quantity":str(intent.quantity_base), "status":"CANCELED", "executed_quantity":"0", "cumulative_quote":"0",
    }
    return trader.orders[client_id]


def test_legacy_orphan_is_preserved_but_never_treated_as_executed_or_ready_to_resend(tmp_path,monkeypatch):
    case = bot_case(tmp_path)
    crash_after_preparation(case,monkeypatch)
    remove_reservation_as_legacy_fixture(case[0])
    with pytest.raises(SpotBotStoreError,match="seguimiento"): owned_spot_bot_ledger(case[0],case[8])
    with pytest.raises(SpotBotStoreError,match="seguimiento"): review(case)
    case[0].backup(tmp_path / "legacy-preserved.db")
    assert case[7].posts == []
    case[0].lock()


def test_legacy_decision_reads_exact_mexc_order_and_fills_without_mutating_journal(tmp_path,monkeypatch):
    case = bot_case(tmp_path)
    captured = crash_after_preparation(case,monkeypatch)
    decision_id = remove_reservation_as_legacy_fixture(case[0])
    vault, _, account, venue, clock, _, _, trader, bot_id, _ = case
    with vault.session() as db:
        before = account_snapshot(db,account)
        decision = read_legacy_spot_decision(db,account,bot_id,decision_id)
        with pytest.raises(SpotBotStoreError,match="no pertenece"):
            read_legacy_spot_decision(db,str(uuid4()),bot_id,decision_id)
    assert legacy_spot_order_review(vault,decision,None,now=clock()+timedelta(days=7))["state"] == "OUTSIDE_LOOKUP_WINDOW"
    assert legacy_spot_order_review(vault,decision,venue,now=clock())["state"] == "UNVERIFIED"
    trader.place_limit_order(captured["kwargs"]["bot_intent"])  # Venue evidence inserted by the fixture only.
    result = legacy_spot_order_review(vault,decision,venue,now=clock())
    assert result["state"] == "MATCHED" and result["external_status"] == "FILLED" and result["fill_count"] == 1
    assert "resolution_digest" not in result
    assert Decimal(result["executed_base"]) == decision.order.quantity_base
    order = trader.orders[result["client_order_id"]]
    venue_fill = venue.trades.pop()
    assert legacy_spot_order_review(vault,decision,venue,now=clock())["state"] == "UNVERIFIED"
    venue.trades.append(venue_fill)
    original_uid = venue.uid

    def switch_account(symbol, client_id):
        venue.uid = "1234567890"
        return trader.order_by_client_id(symbol, client_id)

    with monkeypatch.context() as changed_view:
        changed_view.setattr(venue,"order_by_client_id",switch_account)
        with pytest.raises(SpotBotStoreError,match="cambió durante"):
            legacy_spot_order_review(vault,decision,venue,now=clock())
    with pytest.raises(SpotBotStoreError,match="no acredita"):
        legacy_spot_order_review(vault,decision,venue,now=clock())
    venue.uid = original_uid
    order["price"] = "999"
    assert legacy_spot_order_review(vault,decision,venue,now=clock())["state"] == "UNVERIFIED"
    with vault.session() as db:
        assert db.get(RealLiveSpotIntent,decision_id) is None and account_snapshot(db,account) == before
    with pytest.raises(SpotBotStoreError,match="seguimiento"):
        owned_spot_bot_ledger(vault,bot_id)
    assert trader.posts == [decision_id]
    vault.lock()


def test_verified_legacy_cancel_survives_backup_and_explicit_rearm_without_replay(tmp_path,monkeypatch):
    case = bot_case(tmp_path)
    captured = crash_after_preparation(case,monkeypatch)
    vault, _, account, venue, clock, _, _, trader, bot_id, _ = case
    identifier = remove_reservation_as_legacy_fixture(vault)
    order = add_external_cancel_as_legacy_fixture(trader, captured["kwargs"]["bot_intent"])
    with vault.session() as db:
        decision = read_legacy_spot_decision(db,account,bot_id,identifier)
        raw = db.get(RealSpotBotDecision,identifier).payload_json
        book = account_snapshot(db,account)
    preview = legacy_spot_order_review(vault,decision,venue,now=clock())
    digest = preview["resolution_digest"]
    order["status"] = "NEW"
    changed = legacy_spot_order_review(vault,decision,venue,now=clock())
    assert changed["external_status"] == "NEW" and "resolution_digest" not in changed
    with vault.session() as db:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        with pytest.raises(SpotBotStoreError,match="Repite la consulta"):
            archive_legacy_cancel(db,account,bot_id,decision,changed,digest)
    order["status"] = "CANCELED"
    with vault.session() as db:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        archived = archive_legacy_cancel(db,account,bot_id,decision,preview,digest)
        assert archived == {"state":store.LEGACY_CANCEL_STATE,"decision_id":identifier,"replayed":False}
        db.commit()
    with vault.session() as db:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        assert archive_legacy_cancel(db,account,bot_id,decision,preview,digest)["replayed"]
        assert bot_record(db,bot_id)[0].status == "PAUSED"
        assert db.get(RealLiveSpotIntent,identifier) is None
        assert db.get(RealSpotBotDecision,identifier).payload_json == raw
        assert account_snapshot(db,account) == book
    opening = owned_spot_bot_ledger(vault,bot_id)
    assert opening[0].position.units == opening[0].fill_count == 0
    backup = RealVault(vault.backup(tmp_path/"legacy-cancel-backup.db"))
    backup.unlock(PASSWORD)
    assert owned_spot_bot_ledger(backup,bot_id) == opening
    backup.lock()
    vault.lock()
    vault.unlock(PASSWORD)
    assert owned_spot_bot_ledger(vault,bot_id) == opening
    assert not review(case)[0]["replayed"]
    assert tick(case)["action"] == "WAIT" and trader.posts == []
    with vault.session() as db:
        row, config, _ = bot_record(db,bot_id)
        config[store.LEGACY_CANCEL_REVIEWS][identifier]["fill_count"] = 1
        row.config_json,row.config_digest = encoded(config)
        db.commit()
    with pytest.raises(SpotBotStoreError,match="evidencia de cancelación"):
        owned_spot_bot_ledger(vault,bot_id)
    with pytest.raises(VaultError,match="bot evidence"):
        vault.backup(tmp_path/"altered-cancel-backup.db")
    vault.lock()


@pytest.mark.parametrize("mode", ["filled", "partial", "manual_duplicate", "missing_original_fee"])
def test_legacy_fills_restore_exact_intent_and_book_atomically(tmp_path,monkeypatch,mode):
    case = bot_case(tmp_path)
    captured = crash_after_preparation(case,monkeypatch)
    vault, _, account, venue, clock, _, _, trader, bot_id, _ = case
    identifier = remove_reservation_as_legacy_fixture(vault)
    original_intent = captured["kwargs"]["bot_intent"]
    trader.place_limit_order(original_intent)  # The venue already executed in this controlled fixture.
    venue_fill = venue.trades[0]
    quantity, price = Decimal(venue_fill["quantity"]), Decimal(venue_fill["price"])
    fee = Decimal(venue_fill["commission"])
    if mode == "partial":
        quantity /= 2
        venue.base = str(quantity)
        venue.quote = str(Decimal("700") - quantity * price - fee)
        venue_fill.update(quantity=str(quantity),quote_quantity=str(quantity*price))
        trader.orders[identifier.replace("-", "")].update(status="PARTIALLY_CANCELED",
            executed_quantity=str(quantity),cumulative_quote=str(quantity*price))
    elif mode == "manual_duplicate":
        with vault.session() as db:
            append_event(db,account,str(uuid4()),EventInput(kind="BUY",asset="BTC",quote_currency="USDT",
                quantity=quantity,unit_price=price,fee=fee,occurred_at=clock(),note="Registro manual anterior"))
    elif mode == "missing_original_fee":
        with vault.session() as db:
            row = db.get(RealSpotBotDecision,identifier)
            payload = store.bot_decision_payload(db,row)
            payload["request_digest"] = store.intent_request_digest(replace(original_intent,estimated_fee_quote=Decimal("0.07")))
            row.payload_json,row.payload_digest = encoded(payload)
            db.commit()

    with vault.session() as db:
        before = account_snapshot(db,account)
        decision = read_legacy_spot_decision(db,account,bot_id,identifier)
    result, observed = observe_legacy_spot_order(vault,decision,venue,now=clock())
    assert result["state"] == "MATCHED"
    with vault.session() as db:
        proposal = preview_legacy_fill_recovery(db,account,bot_id,decision,observed,now=clock())
    if mode == "missing_original_fee":
        assert proposal is None
        vault.lock()
        return
    assert proposal and proposal.intent == original_intent
    observed_at = clock()
    bad_balances = venue.balances()
    if mode != "manual_duplicate":
        bad_balances["balances"][0].update(free="3",total="3")
    with vault.session() as db:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        with pytest.raises(SpotBotStoreError,match="duplicado manual" if mode == "manual_duplicate" else "no cuadran"):
            archive_legacy_fills(db,account,bot_id,proposal,bad_balances,observed_at=observed_at,now=clock())
        db.rollback()
    with vault.session() as db:
        assert db.get(RealLiveSpotIntent,identifier) is None
        assert db.query(MexcObservedFill).count() == db.query(RealLiveOrderObservation).count() == 0
        assert account_snapshot(db,account) == before
    if mode == "manual_duplicate":
        assert trader.posts == [identifier]
        vault.lock()
        return

    with vault.session() as db:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        archived = archive_legacy_fills(db,account,bot_id,proposal,venue.balances(),observed_at=observed_at,now=clock())
        assert archived["state"] == "LEGACY_FILLS_RECONCILED" and not archived["replayed"]
        db.commit()
    vault.lock()
    vault.unlock(PASSWORD)
    assert recover_spot_intent(vault,identifier) == original_intent
    with vault.session() as db:
        assert legacy_fills_replay(db,account,bot_id,identifier,proposal.digest)["replayed"]
        assert db.query(RealEvent).filter_by(kind="BUY").count() == 1
        assert bot_record(db,bot_id)[0].status == "PAUSED"
    ledger, _ = owned_spot_bot_ledger(vault,bot_id)
    assert ledger.fill_count == 1 and ledger.position.units == quantity
    assert ledger.position.cost_quote == quantity*price+fee
    assert not review(case)[0]["replayed"]
    assert tick(case)["action"] == "WAIT" and trader.posts == [identifier]
    vault.lock()


def test_legacy_sale_recovers_base_fee_and_owned_profit(tmp_path,monkeypatch):
    case = bot_case(tmp_path)
    vault, _, account, venue, clock, _, _, trader, bot_id, _ = case
    next_entry(case)
    assert tick(case)["action"] == "BUY"
    opening, _ = owned_spot_bot_ledger(vault,bot_id)
    venue.quote_get = lambda symbol: {"symbol":symbol,"closeTime":int(clock().timestamp()*1000),
                                      "bidPrice":"72000","askPrice":"72001","lastPrice":"72000"}
    captured = {}

    def interrupt_sale(*args, **kwargs):
        captured["order"] = kwargs["bot_intent"]
        raise RuntimeError("controlled legacy sale")

    with monkeypatch.context() as interruption:
        interruption.setattr(executor,"submit_anchored_spot_limit",interrupt_sale)
        with pytest.raises(RuntimeError,match="controlled legacy sale"):
            tick(case)
    sale = captured["order"]
    assert sale.side == "SELL"
    identifier = remove_reservation_as_legacy_fixture(vault,sale.intent_id)
    trader.place_limit_order(sale)
    fill = venue.trades[-1]
    base_fee = Decimal("0.0000001")
    venue.base = str(Decimal(venue.base)-base_fee)
    venue.quote = str(Decimal(venue.quote)+Decimal(fill["commission"]))
    fill.update(commission=str(base_fee),commission_asset="BTC")
    with vault.session() as db:
        decision = read_legacy_spot_decision(db,account,bot_id,identifier)
    _, observed = observe_legacy_spot_order(vault,decision,venue,now=clock())
    observed_at = clock()
    balances = venue.balances()
    with vault.session() as db:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        proposal = preview_legacy_fill_recovery(db,account,bot_id,decision,observed,now=observed_at)
        assert proposal and proposal.intent == sale
        archive_legacy_fills(db,account,bot_id,proposal,balances,observed_at=observed_at,now=clock())
        db.commit()
    ledger, _ = owned_spot_bot_ledger(vault,bot_id)
    with localcontext() as context:
        context.prec = 300
        consumed = sale.quantity_base + base_fee
        assert ledger.fill_count == 2
        assert ledger.position.units == opening.position.units - consumed
        cost = opening.position.cost_quote * consumed / opening.position.units
        assert ledger.position.cost_quote == opening.position.cost_quote - cost
        assert ledger.realized_pnl_quote == Decimal(fill["quote_quantity"]) - cost
    assert len(trader.posts) == 2
    vault.lock()


def test_rejected_atomic_preparation_has_no_execution_or_durable_permission(tmp_path,monkeypatch):
    case = bot_case(tmp_path)
    original = store.reserve_spot_intent_in_transaction
    monkeypatch.setattr(store,"reserve_spot_intent_in_transaction",lambda db,intent,state,**kwargs:
        original(db,intent,replace(state,stopped=True),**kwargs))
    next_entry(case)
    result = tick(case)
    assert result["action"] == "PAUSE" and result["journal_status"] == "REJECTED"
    assert case[7].posts == [] and owned_spot_bot_ledger(case[0],case[8])[0].fill_count == 0
    case[0].lock()


def test_unknown_submission_cannot_be_discarded_and_recovers_only_by_view_without_new_post(tmp_path):
    case = bot_case(tmp_path)
    next_entry(case); case[7].timeout_after_accepting = True
    assert tick(case)["order"]["journal_status"] == "SUBMISSION_UNKNOWN"
    with case[0].session() as db: intent = db.query(RealLiveSpotIntent).one().id
    with pytest.raises(LiveJournalError,match="nunca inició"): release_unsent_spot_intent(case[0],intent,now=case[4]())
    result = synchronize_spot_order(case[0],case[3],intent,case[2],clock=case[4])
    assert result["reservation_closed"] and owned_spot_bot_ledger(case[0],case[8])[0].fill_count == 1
    synchronize_spot_order(case[0],case[3],intent,case[2],clock=case[4])
    assert len(case[7].posts) == 1
    with case[0].session() as db: assert db.query(RealEvent).filter_by(kind="BUY").count() == 1
    review(case)
    assert tick(case)["action"] == "WAIT" and len(case[7].posts) == 1
    case[0].lock()


def test_discard_api_is_explicit_account_scoped_and_idempotent_without_exchange_keys(tmp_path,monkeypatch):
    case = bot_case(tmp_path)
    crash_after_preparation(case,monkeypatch)
    with case[0].session() as db: intent = db.query(RealLiveSpotIntent).one().id
    from app.portfolio import api as real_api
    class ScenarioDatetime(datetime):
        @classmethod
        def now(cls,tz=None):
            value = case[4]()
            return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)
    monkeypatch.setattr(real_api,"datetime",ScenarioDatetime)
    app = FastAPI(); app.include_router(create_real_router(case[0].path,mexc_bot_clock=case[4]))
    client = TestClient(app)
    url = f"/real/execution/mexc/intents/{intent}/release-unsent"
    body = {"account_id":case[2],"confirmation":"LIBERAR_RESERVA_NO_ENVIADA"}
    assert client.post(url,json=body).status_code == 401
    token = client.post("/real/vault/unlock",json={"passphrase":PASSWORD}).json()["token"]
    headers = {"X-Orbis-Vault-Token":token}
    assert client.post(url,headers={**headers,"Origin":"https://outside.invalid"},json=body).status_code == 403
    assert client.post(url,headers=headers,json={**body,"account_id":str(uuid4())}).status_code == 409
    released = client.post(url,headers=headers,json=body)
    assert released.status_code == 200 and released.json()["status"] == "RELEASED_UNSENT",released.text
    assert client.post(url,headers=headers,json=body).json()["status"] == "ALREADY_RELEASED"
    assert case[7].posts == []
    client.post("/real/vault/lock",headers=headers)
    case[0].lock()
