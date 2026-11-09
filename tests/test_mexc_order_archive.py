"""Orden/fills observados: bóveda cifrada, vínculo a intención y no contabilidad."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from uuid import uuid4

from app.portfolio.vault import SCHEMA_VERSION
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.live_execution.journal import (
    begin_spot_submission, register_spot_policy, reserve_spot_intent,
)
from app.live_execution.mexc_order_observer import (
    MexcOrderObservationError, ObservedMexcOrder, archive_observed_spot_order,
    observe_spot_intent,
)
from app.live_execution import mexc_order_observer
from app.live_execution.mexc_booking import MexcBookingError, book_observed_spot_order
from app.live_execution.mexc_settlement import MexcSettlementError, close_final_spot_reservation
from app.live_execution.mexc_uid_guard import bind_uid_for_spot_submission
from app.live_execution.reconciliation_view import list_spot_execution_intents
from app.live_execution.policy import LiveSpotIntent, LiveSpotPolicy, LiveSpotState
from app.portfolio.models import MexcObservedFill, RealEvent, RealLiveOrderObservation, RealLiveSpotIntent, RealLiveSpotSettlement, RealMexcFillBooking
from app.portfolio.api import create_real_router
from app.portfolio.domain import EventInput
from app.portfolio.repository import append_event, create_account, account_snapshot
from app.portfolio.vault import RealVault


NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
ACCOUNT = "a" * 64
PASSWORD = "isolated order archive password"


def prepared(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSWORD, create=True)
    policy = LiveSpotPolicy(
        policy_id=str(uuid4()), strategy_version_id="strategy-v1", account_ref=ACCOUNT,
        symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT",
        max_order_quote=Decimal("1000"), max_daily_turnover_quote=Decimal("5000"),
        max_exposure_quote=Decimal("5000"), max_daily_loss_quote=Decimal("500"),
        max_fee_bps=Decimal("20"), max_slippage_bps=Decimal("50"),
        max_data_age_seconds=60, expires_at=NOW + timedelta(hours=1), enabled=True,
    )
    intent = LiveSpotIntent(
        intent_id=str(uuid4()), policy_id=policy.policy_id,
        strategy_version_id="strategy-v1", account_ref=ACCOUNT, symbol="BTCUSDT",
        side="BUY", quantity_base=Decimal("0.01"), limit_price_quote=Decimal("65000"),
        estimated_fee_quote=Decimal("0.65"), signal_at=NOW - timedelta(seconds=1),
    )
    state = LiveSpotState(
        account_ref=ACCOUNT, symbol="BTCUSDT", reconciled=True, stopped=False,
        account_verified=True, instrument_verified=True,
        observed_at=NOW, quote_at=NOW, best_bid=Decimal("64999"),
        best_ask=Decimal("65000"), free_base=Decimal("0"),
        free_quote=Decimal("1000"), pending_base=Decimal(0),
        pending_quote=Decimal(0), daily_turnover_quote=Decimal(0),
        pending_turnover_quote=Decimal(0), exposure_quote=Decimal(0),
        daily_realized_pnl_quote=Decimal(0),
    )
    register_spot_policy(vault, policy, now=NOW)
    assert reserve_spot_intent(vault, intent, state, now=NOW).status == "RESERVED"
    return vault, intent, state


def snapshot(intent):
    return ObservedMexcOrder(
        intent_id=intent.intent_id, external_order_id="external-1",
        status="PARTIALLY_FILLED", executed_base=Decimal("0.002"),
        cumulative_quote=Decimal("130"),
        fills=({"id": "fill-1", "order_id": "external-1", "side": "BUY",
                "time_ms": 1_800_000_000_000, "price": "65000", "quantity": "0.002",
                "quote_quantity": "130", "commission": "0.01",
                "commission_asset": "USDT"},),
        fees_by_asset=(("USDT", Decimal("0.01")),), final=False,
    )


def test_order_snapshot_requires_submission_and_survives_restart(tmp_path):
    vault, intent, state = prepared(tmp_path)
    observed = snapshot(intent)
    with pytest.raises(MexcOrderObservationError, match="pendiente"):
        archive_observed_spot_order(vault, intent, observed, now=NOW)
    begin_spot_submission(vault, intent, state, now=NOW)
    first = archive_observed_spot_order(vault, intent, observed, now=NOW)
    assert first["new_snapshot"] is True and first["new_fills"] == 1
    again = archive_observed_spot_order(vault, intent, observed, now=NOW)
    assert again["observation_id"] == first["observation_id"]
    assert again["new_snapshot"] is False and again["new_fills"] == 0
    with pytest.raises(MexcOrderObservationError, match="otra orden"):
        archive_observed_spot_order(vault, intent,
                                    replace(observed, external_order_id="external-2",
                                            fills=({**observed.fills[0], "order_id": "external-2"},)),
                                    now=NOW)
    with vault.session() as db:
        assert db.query(RealLiveOrderObservation).count() == 1
        assert db.connection().exec_driver_sql("SELECT count(*) FROM mexc_observed_fills").scalar_one() == 1
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_events").scalar_one() == 0
    vault.lock()
    raw = (tmp_path / "real.db").read_bytes()
    assert raw[:16] != b"SQLite format 3\x00"
    assert b"external-1" not in raw and b"fill-1" not in raw
    vault.unlock(PASSWORD)
    with vault.session() as db:
        row = db.query(RealLiveOrderObservation).one()
        assert row.executed_base == Decimal("0.002") and row.cumulative_quote == Decimal("130")
        assert row.status == "PARTIALLY_FILLED" and row.intent_id == intent.intent_id
    vault.lock()


def test_duplicate_order_snapshot_revalidates_fills_and_refreshes_last_seen(tmp_path):
    vault, intent, state = prepared(tmp_path)
    begin_spot_submission(vault, intent, state, now=NOW)
    observed = snapshot(intent)
    first = archive_observed_spot_order(vault, intent, observed, now=NOW)
    changed = replace(observed, fills=({**observed.fills[0], "price": "65001"},))
    with pytest.raises(MexcOrderObservationError, match="fills MEXC cambiaron"):
        archive_observed_spot_order(vault, intent, changed, now=NOW + timedelta(seconds=1))
    with vault.session() as db:
        assert db.query(RealLiveOrderObservation).one().observed_at == NOW
    again = archive_observed_spot_order(vault, intent, observed, now=NOW + timedelta(seconds=2))
    assert again["observation_id"] == first["observation_id"]
    assert again["new_snapshot"] is False and again["new_fills"] == 0
    with vault.session() as db:
        assert db.query(RealLiveOrderObservation).one().observed_at == NOW + timedelta(seconds=2)
        assert db.query(RealLiveOrderObservation).count() == 1
    with pytest.raises(MexcOrderObservationError, match="retrocedió"):
        archive_observed_spot_order(vault, intent, observed, now=NOW + timedelta(seconds=1))
    vault.lock()


def test_order_archive_rejects_wrong_intent_and_incoherent_fills(tmp_path):
    vault, intent, state = prepared(tmp_path)
    begin_spot_submission(vault, intent, state, now=NOW)
    observed = snapshot(intent)
    with pytest.raises(MexcOrderObservationError):
        archive_observed_spot_order(vault, replace(intent, quantity_base=Decimal("0.02")),
                                    observed, now=NOW)
    with pytest.raises(MexcOrderObservationError):
        archive_observed_spot_order(vault, intent,
                                    replace(observed, executed_base=Decimal("0.003")), now=NOW)
    with pytest.raises(MexcOrderObservationError):
        archive_observed_spot_order(vault, intent,
                                    replace(observed, external_order_id="wrong-order"), now=NOW)
    with vault.session() as db:
        assert db.query(RealLiveOrderObservation).count() == 0
        assert db.connection().exec_driver_sql("SELECT count(*) FROM mexc_observed_fills").scalar_one() == 0
    vault.lock()


def test_order_archive_does_not_lose_tiny_commission_in_fill_total(tmp_path):
    vault, intent, state = prepared(tmp_path)
    begin_spot_submission(vault, intent, state, now=NOW)
    first = {**snapshot(intent).fills[0], "quantity": "0.001",
             "quote_quantity": "65", "commission": "1"}
    second = {**first, "id": "fill-2", "commission": "1e-100"}
    inaccurate = replace(snapshot(intent), fills=(first, second),
                         fees_by_asset=(("USDT", Decimal("1")),))
    with pytest.raises(MexcOrderObservationError, match="Snapshot MEXC no verificable"):
        archive_observed_spot_order(vault, intent, inaccurate, now=NOW)
    with vault.session() as db:
        assert db.query(RealLiveOrderObservation).count() == 0
    vault.lock()


def test_order_reader_keeps_tiny_commission_in_aggregated_evidence(tmp_path):
    vault, intent, _state = prepared(tmp_path)
    first = {**snapshot(intent).fills[0], "quantity": "0.001",
             "quote_quantity": "65", "commission": "1"}
    second = {**first, "id": "fill-2", "commission": "1e-100"}

    class FakeView:
        def order_by_client_id(self, symbol, client_order_id):
            return {"source": "MEXC_SPOT_PRIVATE_READONLY",
                    "accounting": "OBSERVED_NOT_IMPORTED", "type": "LIMIT",
                    "symbol": symbol, "client_order_id": client_order_id,
                    "side": "BUY", "price": "65000", "quantity": "0.01",
                    "executed_quantity": "0.002", "cumulative_quote": "130",
                    "order_id": "external-1", "status": "PARTIALLY_FILLED"}

        def trades_for_order(self, symbol, order_id):
            return {"source": "MEXC_SPOT_PRIVATE_READONLY",
                    "accounting": "OBSERVED_NOT_IMPORTED",
                    "coverage": "ORDER_ID_BELOW_PAGE_LIMIT",
                    "symbol": symbol, "order_id": order_id,
                    "trades": [first, second]}

    observed = observe_spot_intent(FakeView(), intent)
    assert observed.fees_by_asset[0][1] > Decimal("1")
    with localcontext() as context:
        context.prec = 300
        assert observed.fees_by_asset[0][1] - Decimal("1") == Decimal("1e-100")
    vault.lock()


def test_order_archive_never_moves_backward_or_reopens_final_order(tmp_path):
    vault, intent, state = prepared(tmp_path)
    begin_spot_submission(vault, intent, state, now=NOW)
    partial = snapshot(intent)
    archive_observed_spot_order(vault, intent, partial, now=NOW)
    older = replace(partial, status="NEW", executed_base=Decimal(0),
                    cumulative_quote=Decimal(0), fills=(), fees_by_asset=())
    with pytest.raises(MexcOrderObservationError, match="retrocedió"):
        archive_observed_spot_order(vault, intent, older, now=NOW + timedelta(seconds=1))
    with pytest.raises(MexcOrderObservationError, match="hora de observación"):
        archive_observed_spot_order(vault, intent,
                                    replace(partial, status="PARTIALLY_CANCELED", final=True),
                                    now=NOW - timedelta(seconds=1))
    second_fill = {**partial.fills[0], "id": "fill-2", "quantity": "0.008",
                   "quote_quantity": "520", "commission": "0.04"}
    filled = replace(partial, status="FILLED", executed_base=Decimal("0.01"),
                     cumulative_quote=Decimal("650"),
                     fills=(*partial.fills, second_fill),
                     fees_by_asset=(("USDT", Decimal("0.05")),), final=True)
    archive_observed_spot_order(vault, intent, filled, now=NOW + timedelta(seconds=2))
    with pytest.raises(MexcOrderObservationError, match="estado final"):
        archive_observed_spot_order(vault, intent,
                                    replace(partial, status="PARTIALLY_CANCELED", final=True),
                                    now=NOW + timedelta(seconds=3))
    with pytest.raises(MexcOrderObservationError, match="retrocedió"):
        archive_observed_spot_order(vault, intent, partial, now=NOW + timedelta(seconds=3))
    again = archive_observed_spot_order(vault, intent, filled, now=NOW + timedelta(seconds=4))
    assert again["new_snapshot"] is False and again["new_fills"] == 0
    with vault.session() as db:
        assert db.query(RealLiveOrderObservation).count() == 2
        latest = db.query(RealLiveOrderObservation).filter_by(status="FILLED").one()
        assert latest.observed_at == NOW + timedelta(seconds=4)
        assert db.connection().exec_driver_sql("SELECT count(*) FROM mexc_observed_fills").scalar_one() == 2
    vault.lock()


def test_order_archive_rejects_impossible_execution_amount(tmp_path):
    vault, intent, state = prepared(tmp_path)
    begin_spot_submission(vault, intent, state, now=NOW)
    partial = snapshot(intent)
    impossible = replace(partial, status="FILLED", final=True)
    with pytest.raises(MexcOrderObservationError, match="progreso"):
        archive_observed_spot_order(vault, intent, impossible, now=NOW)
    with vault.session() as db:
        assert db.query(RealLiveOrderObservation).count() == 0
    vault.lock()


def test_order_archive_requires_all_prior_fills_and_rolls_back_together(tmp_path, monkeypatch):
    vault, intent, state = prepared(tmp_path)
    begin_spot_submission(vault, intent, state, now=NOW)
    partial = snapshot(intent)
    archive_observed_spot_order(vault, intent, partial, now=NOW)
    replacement_fill = {**partial.fills[0], "id": "replacement-1"}
    omitted = replace(partial, fills=(replacement_fill,))
    with pytest.raises(MexcOrderObservationError, match="omitió fills"):
        archive_observed_spot_order(vault, intent, omitted, now=NOW + timedelta(seconds=1))

    second_fill = {**partial.fills[0], "id": "fill-2", "quantity": "0.001",
                   "quote_quantity": "65", "commission": "0.005"}
    progressed = replace(partial, executed_base=Decimal("0.003"),
                         cumulative_quote=Decimal("195"), fills=(*partial.fills, second_fill),
                         fees_by_asset=(("USDT", Decimal("0.015")),))
    real_save = mexc_order_observer.save_observation_in_transaction

    def fail_after_fill(db, account_ref, data):
        real_save(db, account_ref, data)
        raise RuntimeError("Injected after fill staging")

    monkeypatch.setattr(mexc_order_observer, "save_observation_in_transaction", fail_after_fill)
    with pytest.raises(MexcOrderObservationError, match="archivar los fills"):
        archive_observed_spot_order(vault, intent, progressed, now=NOW + timedelta(seconds=2))
    with vault.session() as db:
        assert db.query(RealLiveOrderObservation).count() == 1
        assert db.connection().exec_driver_sql("SELECT count(*) FROM mexc_observed_fills").scalar_one() == 1
        assert db.connection().exec_driver_sql("SELECT count(*) FROM mexc_observation_batches").scalar_one() == 1
    vault.lock()


def test_execution_view_distinguishes_reservation_observation_and_unbooked_final(tmp_path):
    vault, intent, state = prepared(tmp_path)
    before = list_spot_execution_intents(vault)
    assert len(before) == 1
    assert before[0]["stage"] == "RESERVED_NOT_SUBMITTED"
    assert before[0]["reserved_quote"] == "650.65"
    assert before[0]["external_order_observed"] is False
    assert before[0]["booked_to_portfolio"] is False
    assert before[0]["reconciliation"]["status"] == "ORDER_NOT_OBSERVED"
    assert ACCOUNT not in str(before)

    begin_spot_submission(vault, intent, state, now=NOW)
    assert list_spot_execution_intents(vault)[0]["stage"] == "ORDER_UNVERIFIED"
    partial = snapshot(intent)
    archive_observed_spot_order(vault, intent, partial, now=NOW)
    observed = list_spot_execution_intents(vault)[0]
    assert observed["stage"] == "ORDER_OBSERVED_OPEN"
    assert observed["executed_base_observed"] == "0.002"
    assert observed["reconciliation"]["status"] == "ORDER_NOT_FINAL"
    second_fill = {**partial.fills[0], "id": "fill-2", "quantity": "0.008",
                   "quote_quantity": "520", "commission": "0.04"}
    complete = replace(partial, status="FILLED", executed_base=Decimal("0.01"),
                       cumulative_quote=Decimal("650"), fills=(*partial.fills, second_fill),
                       fees_by_asset=(("USDT", Decimal("0.05")),), final=True)
    archive_observed_spot_order(vault, intent, complete, now=NOW + timedelta(seconds=1))
    final = list_spot_execution_intents(vault)[0]
    assert final["stage"] == "FINAL_OBSERVED_UNBOOKED"
    assert final["external_status"] == "FILLED"
    assert final["reserved_quote"] == "650.65"
    assert final["booked_to_portfolio"] is False
    assert final["reconciliation"] == {
        "status": "NEEDS_ACCOUNT_LINK_AND_BOOKING",
        "deltas": {"BTC": "0.010", "USDT": "-650.05"},
        "fee_by_asset": {"USDT": "0.05"}, "fill_count": 2,
        "fee_model_compatible": True, "quote_fee_model_compatible": True,
    }
    vault.lock()
    vault.unlock(PASSWORD)
    assert list_spot_execution_intents(vault)[0] == final
    vault.lock()


def test_reconciliation_blocks_third_asset_fee_without_fabricating_ledger_event(tmp_path):
    vault, intent, state = prepared(tmp_path)
    begin_spot_submission(vault, intent, state, now=NOW)
    fill = {"id": "base-fee-1", "order_id": "external-1", "side": "BUY",
            "time_ms": 1_800_000_000_000, "price": "65000", "quantity": "0.01",
            "quote_quantity": "650", "commission": "0.00001",
            "commission_asset": "BNB"}
    final = ObservedMexcOrder(intent.intent_id, "external-1", "FILLED",
                              Decimal("0.01"), Decimal("650"), (fill,),
                              (("BNB", Decimal("0.00001")),), True)
    archive_observed_spot_order(vault, intent, final, now=NOW)
    projection = list_spot_execution_intents(vault)[0]["reconciliation"]
    assert projection == {
        "status": "MULTI_ASSET_FEE_UNSUPPORTED",
        "deltas": {"BNB": "-0.00001", "BTC": "0.01", "USDT": "-650"},
        "fee_by_asset": {"BNB": "0.00001"}, "fill_count": 1,
        "fee_model_compatible": False, "quote_fee_model_compatible": False,
    }
    with vault.session() as db:
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_events").scalar_one() == 0
    app = FastAPI()
    app.include_router(create_real_router(vault.path))
    client = TestClient(app)
    assert client.get("/real/execution/mexc/intents").status_code == 401
    token = client.post("/real/vault/unlock", json={"passphrase": PASSWORD}).json()["token"]
    response = client.get("/real/execution/mexc/intents", headers={"X-Orbis-Vault-Token": token})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()[0]["reconciliation"] == projection
    assert ACCOUNT not in response.text
    with vault.session() as db:
        account_id = create_account(db, "Incompatible", "USDT").id
    with pytest.raises(MexcBookingError, match="MULTI_ASSET_FEE_UNSUPPORTED"):
        book_observed_spot_order(vault, intent.intent_id, account_id)
    with vault.session() as db:
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_mexc_fill_bookings").scalar_one() == 0
    vault.lock()


def test_verified_fills_book_once_atomically_and_survive_restart(tmp_path):
    vault, intent, state = prepared(tmp_path)
    begin_spot_submission(vault, intent, state, now=NOW)
    partial = snapshot(intent)
    extra = {**partial.fills[0], "id": "fill-2", "quantity": "0.008",
             "quote_quantity": "520", "commission": "0.04"}
    complete = replace(partial, status="FILLED", executed_base=Decimal("0.01"),
                       cumulative_quote=Decimal("650"), fills=(*partial.fills, extra),
                       fees_by_asset=(("USDT", Decimal("0.05")),), final=True)
    archive_observed_spot_order(vault, intent, complete, now=NOW)
    with vault.session() as db:
        account = create_account(db, "MEXC", "USDT")
        account_id = account.id
        append_event(db, account_id, str(uuid4()), EventInput(
            kind="CONTRIBUTION", quote_currency="USDT", amount=Decimal("700"),
            occurred_at=NOW - timedelta(days=1),
        ))
    first = book_observed_spot_order(vault, intent.intent_id, account_id, now=NOW)
    assert first["status"] == "BOOKED" and first["fill_count"] == 2
    second = book_observed_spot_order(vault, intent.intent_id, account_id, now=NOW)
    assert second["status"] == "ALREADY_BOOKED"
    view = list_spot_execution_intents(vault)[0]
    assert view["stage"] == "FINAL_BOOKED_RESERVED"
    assert view["booked_to_portfolio"] is True
    with vault.session() as db:
        archived = db.query(RealLiveOrderObservation).filter_by(intent_id=intent.intent_id).one()
        original_executed = archived.executed_base
        archived.executed_base += Decimal("0.001")
        db.commit()
    inconsistent = list_spot_execution_intents(vault)[0]
    assert inconsistent["stage"] == "RECONCILIATION_INCONSISTENT"
    assert inconsistent["reconciliation"]["status"] == "ORDER_ARCHIVE_TAMPERED"
    with vault.session() as db:
        archived = db.query(RealLiveOrderObservation).filter_by(intent_id=intent.intent_id).one()
        archived.executed_base = original_executed
        db.commit()
    assert list_spot_execution_intents(vault)[0]["stage"] == "FINAL_BOOKED_RESERVED"
    with vault.session() as db:
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_mexc_fill_bookings").scalar_one() == 2
        value = account_snapshot(db, account_id)
        assert value.cash["USDT"] == Decimal("49.95")
        assert value.positions[("BTC", "USDT")].quantity == Decimal("0.01")
        assert value.positions[("BTC", "USDT")].cost_basis == Decimal("650.05")
    vault.lock()
    vault.unlock(PASSWORD)
    assert book_observed_spot_order(vault, intent.intent_id, account_id)["status"] == "ALREADY_BOOKED"
    app = FastAPI()
    app.include_router(create_real_router(vault.path))
    client = TestClient(app)
    assert client.post(f"/real/execution/mexc/intents/{intent.intent_id}/book",
                       json={"account_id": account_id, "confirmation": "IMPORTAR"}).status_code == 401
    token = client.post("/real/vault/unlock", json={"passphrase": PASSWORD}).json()["token"]
    response = client.post(f"/real/execution/mexc/intents/{intent.intent_id}/book",
                           headers={"X-Orbis-Vault-Token": token},
                           json={"account_id": account_id, "confirmation": "IMPORTAR"})
    assert response.status_code == 200 and response.json()["status"] == "ALREADY_BOOKED"
    assert response.headers["cache-control"] == "no-store"
    vault.lock()


def test_booking_rejects_missing_cash_wrong_account_and_non_quote_fee(tmp_path):
    vault, intent, state = prepared(tmp_path)
    begin_spot_submission(vault, intent, state, now=NOW)
    partial = snapshot(intent)
    complete = replace(partial, status="PARTIALLY_CANCELED", final=True)
    archive_observed_spot_order(vault, intent, complete, now=NOW)
    with vault.session() as db:
        account = create_account(db, "MEXC", "USDT")
        other = create_account(db, "Otro", "USDT")
        account_id, other_id = account.id, other.id
    with pytest.raises(MexcBookingError, match="efectivo"):
        book_observed_spot_order(vault, intent.intent_id, account_id)
    with vault.session() as db:
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_mexc_account_links").scalar_one() == 0
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_events").scalar_one() == 0
        append_event(db, account_id, str(uuid4()), EventInput(
            kind="CONTRIBUTION", quote_currency="USDT", amount=Decimal("200"),
            occurred_at=datetime(2027, 1, 17, tzinfo=timezone.utc),
        ))
    with pytest.raises(MexcBookingError, match="fecha de compra"):
        book_observed_spot_order(vault, intent.intent_id, account_id)
    with vault.session() as db:
        append_event(db, account_id, str(uuid4()), EventInput(
            kind="CONTRIBUTION", quote_currency="USDT", amount=Decimal("200"),
            occurred_at=NOW - timedelta(days=1),
        ))
    assert book_observed_spot_order(vault, intent.intent_id, account_id)["status"] == "BOOKED"
    with pytest.raises(MexcBookingError, match="otra cartera"):
        book_observed_spot_order(vault, intent.intent_id, other_id)
    with vault.session() as db:
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_mexc_fill_bookings").scalar_one() == 1
    vault.lock()


def test_reconciliation_books_exact_reported_quote(tmp_path):
    vault, intent, state = prepared(tmp_path)
    begin_spot_submission(vault, intent, state, now=NOW)
    fill = {"id": "rounded-1", "order_id": "external-1", "side": "BUY",
            "time_ms": 1_800_000_000_000, "price": "65000", "quantity": "0.01",
            "quote_quantity": "650.00000001", "commission": "0",
            "commission_asset": "USDT"}
    final = ObservedMexcOrder(intent.intent_id, "external-1", "FILLED",
                              Decimal("0.01"), Decimal("650.00000001"), (fill,),
                              (("USDT", Decimal(0)),), True)
    archive_observed_spot_order(vault, intent, final, now=NOW)
    projection = list_spot_execution_intents(vault)[0]["reconciliation"]
    assert projection["status"] == "NEEDS_ACCOUNT_LINK_AND_BOOKING"
    assert projection["fee_model_compatible"] is True
    assert projection["quote_fee_model_compatible"] is True
    with vault.session() as db:
        account_id = create_account(db, "MEXC", "USDT").id
        append_event(db, account_id, str(uuid4()), EventInput(
            kind="CONTRIBUTION", quote_currency="USDT", amount=Decimal("700"),
            occurred_at=NOW - timedelta(days=1),
        ))
    assert book_observed_spot_order(vault, intent.intent_id, account_id)["status"] == "BOOKED"
    with vault.session() as db:
        event = db.query(RealEvent).filter_by(kind="BUY").one()
        assert event.unit_price == Decimal("65000")
        assert event.gross_amount == Decimal("650.00000001")
        value = account_snapshot(db, account_id)
        assert value.cash["USDT"] == Decimal("49.99999999")
        assert value.positions[("BTC", "USDT")].cost_basis == Decimal("650.00000001")
    vault.lock()
    vault.unlock(PASSWORD)
    with vault.session() as db:
        assert account_snapshot(db, account_id).cash["USDT"] == Decimal("49.99999999")
    client = _VerifiedReadOnlyMexc(intent, final, balance_at=NOW + timedelta(seconds=1))
    changed_fill = {**fill, "quote_quantity": "650"}
    with pytest.raises(MexcSettlementError, match="fills"):
        close_final_spot_reservation(
            vault, _VerifiedReadOnlyMexc(intent, final, balance_at=NOW + timedelta(seconds=1),
                                          fills=(changed_fill,)),
            ACCOUNT, intent.intent_id, now=NOW + timedelta(seconds=2),
        )
    assert list_spot_execution_intents(vault)[0]["stage"] == "FINAL_BOOKED_RESERVED"
    with vault.session() as db:
        event = db.query(RealEvent).filter_by(kind="BUY").one()
        event.gross_amount = Decimal("650")
        db.commit()
    with pytest.raises(MexcSettlementError, match="asiento REAL no coincide"):
        close_final_spot_reservation(vault, client, ACCOUNT, intent.intent_id,
                                     now=NOW + timedelta(seconds=2))
    assert list_spot_execution_intents(vault)[0]["stage"] == "RECONCILIATION_INCONSISTENT"
    assert list_spot_execution_intents(vault)[0]["booked_to_portfolio"] is False
    with vault.session() as db:
        event = db.query(RealEvent).filter_by(kind="BUY").one()
        event.gross_amount = Decimal("650.00000001")
        db.commit()
    assert close_final_spot_reservation(
        vault, client, ACCOUNT, intent.intent_id, now=NOW + timedelta(seconds=2),
    )["status"] == "RESERVATION_CLOSED"
    assert close_final_spot_reservation(
        vault, client, ACCOUNT, intent.intent_id, now=NOW + timedelta(seconds=3),
    )["status"] == "ALREADY_CLOSED"
    assert book_observed_spot_order(vault, intent.intent_id, account_id)["status"] == "ALREADY_BOOKED"
    assert list_spot_execution_intents(vault)[0]["stage"] == "FINAL_RESERVATION_CLOSED"
    vault.lock()


def test_reconciliation_blocks_incomplete_fill_archive(tmp_path):
    vault, intent, state = prepared(tmp_path)
    begin_spot_submission(vault, intent, state, now=NOW)
    fill = {"id": "rounded-1", "order_id": "external-1", "side": "BUY",
            "time_ms": 1_800_000_000_000, "price": "65000", "quantity": "0.01",
            "quote_quantity": "650.00000001", "commission": "0",
            "commission_asset": "USDT"}
    final = ObservedMexcOrder(intent.intent_id, "external-1", "FILLED",
                              Decimal("0.01"), Decimal("650.00000001"), (fill,),
                              (("USDT", Decimal(0)),), True)
    archive_observed_spot_order(vault, intent, final, now=NOW)
    with vault.session() as db:
        db.connection().exec_driver_sql(
            "DELETE FROM mexc_observed_fills WHERE trade_id='rounded-1'")
        db.commit()
    assert list_spot_execution_intents(vault)[0]["reconciliation"]["status"] == "FILL_ARCHIVE_INCOMPLETE"
    vault.lock()


@pytest.mark.parametrize("field,change", [
    ("time_ms", lambda fill: fill.time_ms + 1),
    ("price", lambda fill: fill.price + Decimal("1")),
])
def test_reconciliation_rejects_tampered_fill_before_booking(tmp_path, field, change):
    vault, intent, state = prepared(tmp_path)
    begin_spot_submission(vault, intent, state, now=NOW)
    final = replace(snapshot(intent), status="PARTIALLY_CANCELED", final=True)
    archive_observed_spot_order(vault, intent, final, now=NOW)
    with vault.session() as db:
        account_id = create_account(db, "MEXC", "USDT").id
        append_event(db, account_id, str(uuid4()), EventInput(
            kind="CONTRIBUTION", quote_currency="USDT", amount=Decimal("700"),
            occurred_at=NOW - timedelta(days=1),
        ))
        fill = db.query(MexcObservedFill).one()
        setattr(fill, field, change(fill))  # totales intactos; huella original ya no coincide
        db.commit()
    view = list_spot_execution_intents(vault)[0]
    assert view["reconciliation"]["status"] == "FILL_ARCHIVE_TAMPERED"
    assert view["stage"] == "RECONCILIATION_INCONSISTENT"
    with pytest.raises(MexcBookingError, match="FILL_ARCHIVE_TAMPERED"):
        book_observed_spot_order(vault, intent.intent_id, account_id)
    with vault.session() as db:
        assert db.query(RealMexcFillBooking).count() == 0
        assert db.query(RealEvent).filter_by(kind="BUY").count() == 0
    vault.lock()


def test_reconciliation_rejects_tampered_order_status_before_booking(tmp_path):
    vault, intent, state = prepared(tmp_path)
    begin_spot_submission(vault, intent, state, now=NOW)
    final = replace(snapshot(intent), status="PARTIALLY_CANCELED", final=True)
    archive_observed_spot_order(vault, intent, final, now=NOW)
    with vault.session() as db:
        account_id = create_account(db, "MEXC", "USDT").id
        append_event(db, account_id, str(uuid4()), EventInput(
            kind="CONTRIBUTION", quote_currency="USDT", amount=Decimal("700"),
            occurred_at=NOW - timedelta(days=1),
        ))
        db.query(RealLiveOrderObservation).one().status = "CANCELED"
        db.commit()
    view = list_spot_execution_intents(vault)[0]
    assert view["reconciliation"]["status"] == "ORDER_ARCHIVE_TAMPERED"
    assert view["stage"] == "RECONCILIATION_INCONSISTENT"
    with pytest.raises(MexcBookingError, match="ORDER_ARCHIVE_TAMPERED"):
        book_observed_spot_order(vault, intent.intent_id, account_id)
    with vault.session() as db:
        assert db.query(RealMexcFillBooking).count() == 0
        assert db.query(RealEvent).filter_by(kind="BUY").count() == 0
    vault.lock()


def test_zero_fill_order_status_still_requires_original_digest(tmp_path):
    vault, intent, state = prepared(tmp_path)
    begin_spot_submission(vault, intent, state, now=NOW)
    canceled = ObservedMexcOrder(intent.intent_id, "external-1", "CANCELED",
                                 Decimal(0), Decimal(0), (), (), True)
    archive_observed_spot_order(vault, intent, canceled, now=NOW)
    with vault.session() as db:
        db.query(RealLiveOrderObservation).one().status = "FILLED"
        db.commit()
    view = list_spot_execution_intents(vault)[0]
    assert view["reconciliation"]["status"] == "ORDER_ARCHIVE_TAMPERED"
    assert view["stage"] == "RECONCILIATION_INCONSISTENT"
    vault.lock()


def test_base_asset_fee_books_net_units_and_closes_only_matching_reservation(tmp_path):
    vault, intent, state = prepared(tmp_path)
    begin_spot_submission(vault, intent, state, now=NOW)
    fill = {"id": "base-fee-1", "order_id": "external-1", "side": "BUY",
            "time_ms": 1_800_000_000_000, "price": "65000", "quantity": "0.01",
            "quote_quantity": "650", "commission": "0.00001",
            "commission_asset": "BTC"}
    final = ObservedMexcOrder(intent.intent_id, "external-1", "FILLED",
                              Decimal("0.01"), Decimal("650"), (fill,),
                              (("BTC", Decimal("0.00001")),), True)
    archive_observed_spot_order(vault, intent, final, now=NOW)
    projection = list_spot_execution_intents(vault)[0]["reconciliation"]
    assert projection["status"] == "NEEDS_ACCOUNT_LINK_AND_BOOKING"
    assert projection["fee_model_compatible"] is True
    assert projection["quote_fee_model_compatible"] is False
    with vault.session() as db:
        account_id = create_account(db, "MEXC", "USDT").id
        append_event(db, account_id, str(uuid4()), EventInput(
            kind="CONTRIBUTION", quote_currency="USDT", amount=Decimal("700"),
            occurred_at=NOW - timedelta(days=1),
        ))
    assert book_observed_spot_order(vault, intent.intent_id, account_id)["status"] == "BOOKED"
    with vault.session() as db:
        value = account_snapshot(db, account_id)
        assert value.cash["USDT"] == Decimal("50")
        assert value.positions[("BTC", "USDT")].quantity == Decimal("0.00999")
        assert value.positions[("BTC", "USDT")].cost_basis == Decimal("650")
        assert db.query(RealEvent).filter_by(kind="BUY").one().fee_asset == "BTC"
    client = _VerifiedReadOnlyMexc(intent, final, balance_at=NOW + timedelta(seconds=1))
    assert close_final_spot_reservation(vault, client, ACCOUNT, intent.intent_id,
                                        now=NOW + timedelta(seconds=2))["status"] == "RESERVATION_CLOSED"
    vault.lock()
    vault.unlock(PASSWORD)
    with vault.session() as db:
        assert account_snapshot(db, account_id).positions[("BTC", "USDT")].quantity == Decimal("0.00999")
    assert book_observed_spot_order(vault, intent.intent_id, account_id)["status"] == "ALREADY_BOOKED"
    vault.lock()


def test_base_fee_consuming_entire_buy_is_not_bookable(tmp_path):
    vault, intent, state = prepared(tmp_path)
    begin_spot_submission(vault, intent, state, now=NOW)
    fill = {"id": "all-fee-1", "order_id": "external-1", "side": "BUY",
            "time_ms": 1_800_000_000_000, "price": "65000", "quantity": "0.01",
            "quote_quantity": "650", "commission": "0.01", "commission_asset": "BTC"}
    final = ObservedMexcOrder(intent.intent_id, "external-1", "FILLED",
                              Decimal("0.01"), Decimal("650"), (fill,),
                              (("BTC", Decimal("0.01")),), True)
    archive_observed_spot_order(vault, intent, final, now=NOW)
    assert list_spot_execution_intents(vault)[0]["reconciliation"]["status"] == "BASE_FEE_EXCEEDS_FILL"
    with vault.session() as db:
        account_id = create_account(db, "MEXC", "USDT").id
    with pytest.raises(MexcBookingError, match="BASE_FEE_EXCEEDS_FILL"):
        book_observed_spot_order(vault, intent.intent_id, account_id)
    with vault.session() as db:
        assert db.query(RealEvent).count() == 0
    vault.lock()


def test_reconciliation_final_canceled_without_fills_is_not_a_purchase(tmp_path):
    vault, intent, state = prepared(tmp_path)
    begin_spot_submission(vault, intent, state, now=NOW)
    canceled = ObservedMexcOrder(intent.intent_id, "external-1", "CANCELED",
                                 Decimal(0), Decimal(0), (), (), True)
    archive_observed_spot_order(vault, intent, canceled, now=NOW)
    result = list_spot_execution_intents(vault)[0]
    assert result["stage"] == "FINAL_NO_EXECUTION"
    assert result["reconciliation"]["status"] == "FINAL_WITHOUT_EXECUTION"
    assert result["reconciliation"]["deltas"] == {}
    with vault.session() as db:
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_events").scalar_one() == 0
    vault.lock()


def test_v5_migrates_to_v9_with_encrypted_preimage(tmp_path):
    vault, intent, _state = prepared(tmp_path)
    vault.lock()
    vault.unlock(PASSWORD)
    with vault.session() as db:
        db.connection().exec_driver_sql("DROP TABLE real_live_order_observations")
        db.connection().exec_driver_sql("PRAGMA user_version = 5")
        db.commit()
    vault.lock()
    vault.unlock(PASSWORD)
    with vault.session() as db:
        assert db.connection().exec_driver_sql("PRAGMA user_version").scalar_one() == SCHEMA_VERSION
        assert db.query(RealLiveOrderObservation).count() == 0
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_live_spot_intents").scalar_one() == 1
    vault.lock()
    backups = list((tmp_path / "backups").glob("pre-migrate-v5-*.db"))
    assert len(backups) == 1
    assert backups[0].read_bytes()[:16] != b"SQLite format 3\x00"


def test_v6_migrates_booking_tables_on_encrypted_copy(tmp_path):
    vault, intent, _state = prepared(tmp_path)
    vault.lock()
    vault.unlock(PASSWORD)
    with vault.session() as db:
        db.connection().exec_driver_sql("DROP TABLE real_mexc_fill_bookings")
        db.connection().exec_driver_sql("DROP TABLE real_mexc_account_links")
        db.connection().exec_driver_sql("PRAGMA user_version = 6")
        db.commit()
    vault.lock()
    vault.unlock(PASSWORD)
    with vault.session() as db:
        assert db.connection().exec_driver_sql("PRAGMA user_version").scalar_one() == SCHEMA_VERSION
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_mexc_fill_bookings").scalar_one() == 0
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_live_spot_intents").scalar_one() == 1
    vault.lock()
    backups = list((tmp_path / "backups").glob("pre-migrate-v6-*.db"))
    assert len(backups) == 1
    assert backups[0].read_bytes()[:16] != b"SQLite format 3\x00"


class _VerifiedReadOnlyMexc:
    def __init__(self, intent, observed, *, balance_at=NOW, fills=None,
                 account_ref=ACCOUNT, uid="123456789"):
        self.intent = intent
        self.observed = observed
        self.balance_at = balance_at
        self.fills = observed.fills if fills is None else fills
        self.account_ref = account_ref
        self.uid = uid

    def account_reference(self):
        return self.account_ref

    def account_uid(self):
        return self.uid

    def order_by_client_id(self, symbol, client_order_id):
        assert symbol == self.intent.symbol
        assert client_order_id == self.intent.intent_id.replace("-", "")
        return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "type": "LIMIT", "client_order_id": client_order_id,
                "symbol": symbol, "side": self.intent.side,
                "price": format(self.intent.limit_price_quote, "f"),
                "quantity": format(self.intent.quantity_base, "f"),
                "order_id": self.observed.external_order_id, "status": self.observed.status,
                "executed_quantity": format(self.observed.executed_base, "f"),
                "cumulative_quote": format(self.observed.cumulative_quote, "f")}

    def trades_for_order(self, symbol, order_id):
        assert symbol == self.intent.symbol and order_id == self.observed.external_order_id
        return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "coverage": "ORDER_ID_BELOW_PAGE_LIMIT", "symbol": symbol,
                "order_id": order_id, "trades": list(self.fills)}

    def balances(self):
        return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "observed_at": self.balance_at.isoformat(), "balances": [
                    {"asset": "BTC", "free": "0.01", "locked": "0", "total": "0.01"},
                    {"asset": "USDT", "free": "349.95", "locked": "0", "total": "349.95"},
                ]}


def test_final_mexc_reservation_closes_only_after_reconfirmation_and_booking(tmp_path):
    vault, intent, state = prepared(tmp_path)
    begin_spot_submission(vault, intent, state, now=NOW)
    bind_uid_for_spot_submission(vault, ACCOUNT, "123456789", now=NOW)
    part = snapshot(intent)
    final = replace(part, status="PARTIALLY_CANCELED", final=True)
    archive_observed_spot_order(vault, intent, final, now=NOW)
    client = _VerifiedReadOnlyMexc(intent, final, balance_at=NOW + timedelta(seconds=1))
    with pytest.raises(MexcSettlementError, match="Asienta todos"):
        close_final_spot_reservation(vault, client, ACCOUNT, intent.intent_id,
                                     now=NOW + timedelta(seconds=2))
    with vault.session() as db:
        account = create_account(db, "MEXC", "USDT")
        account_id = account.id
        append_event(db, account_id, str(uuid4()), EventInput(
            kind="CONTRIBUTION", quote_currency="USDT", amount=Decimal("700"),
            occurred_at=NOW - timedelta(days=1),
        ))
    book_observed_spot_order(vault, intent.intent_id, account_id, now=NOW)
    result = close_final_spot_reservation(vault, client, ACCOUNT, intent.intent_id,
                                          now=NOW + timedelta(seconds=2))
    assert result["status"] == "RESERVATION_CLOSED"
    assert close_final_spot_reservation(vault, client, ACCOUNT, intent.intent_id,
                                        now=NOW + timedelta(seconds=3))["status"] == "ALREADY_CLOSED"
    assert book_observed_spot_order(vault, intent.intent_id, account_id)["status"] == "ALREADY_BOOKED"
    with vault.session() as db:
        row = db.get(RealLiveSpotSettlement, intent.intent_id)
        assert row.external_status == "PARTIALLY_CANCELED"
        assert row.free_quote == Decimal("349.95")
        assert len(row.balance_digest) == 64
        journal = db.connection().exec_driver_sql(
            "SELECT status, reserved_quote, reserved_base FROM real_live_spot_intents WHERE id=?",
            (intent.intent_id,),
        ).one()
        assert journal == ("RESERVATION_CLOSED", "0", "0")
    assert list_spot_execution_intents(vault)[0]["stage"] == "FINAL_RESERVATION_CLOSED"
    vault.lock()
    vault.unlock(PASSWORD)
    with vault.session() as db:
        assert db.query(RealLiveSpotSettlement).count() == 1
    vault.lock()


def test_final_mexc_reservation_keeps_hold_on_mismatch_or_stale_balance(tmp_path):
    vault, intent, state = prepared(tmp_path)
    begin_spot_submission(vault, intent, state, now=NOW)
    final = replace(snapshot(intent), status="PARTIALLY_CANCELED", final=True)
    archive_observed_spot_order(vault, intent, final, now=NOW)
    with pytest.raises(MexcSettlementError, match="esta conexión"):
        close_final_spot_reservation(vault, _VerifiedReadOnlyMexc(intent, final), "b" * 64,
                                     intent.intent_id, now=NOW)
    stale = _VerifiedReadOnlyMexc(intent, final, balance_at=NOW - timedelta(seconds=20))
    with pytest.raises(MexcSettlementError, match="no es reciente"):
        close_final_spot_reservation(vault, stale, ACCOUNT, intent.intent_id, now=NOW)
    altered = {**final.fills[0], "quantity": "0.003"}
    with pytest.raises(MexcSettlementError, match="fills"):
        close_final_spot_reservation(vault,
                                     _VerifiedReadOnlyMexc(intent, final, fills=(altered,)),
                                     ACCOUNT, intent.intent_id, now=NOW + timedelta(seconds=1))
    with vault.session() as db:
        assert db.query(RealLiveSpotSettlement).count() == 0
        row = db.connection().exec_driver_sql(
            "SELECT status, reserved_quote FROM real_live_spot_intents WHERE id=?",
            (intent.intent_id,),
        ).one()
        assert row == ("SUBMISSION_UNKNOWN", "650.65")
    vault.lock()


def test_final_mexc_reservation_requires_same_view_key_and_known_uid(tmp_path):
    vault, intent, state = prepared(tmp_path)
    begin_spot_submission(vault, intent, state, now=NOW)
    final = replace(snapshot(intent), status="PARTIALLY_CANCELED", final=True)
    archive_observed_spot_order(vault, intent, final, now=NOW)
    bind_uid_for_spot_submission(vault, ACCOUNT, "123456789", now=NOW)

    foreign_key = _VerifiedReadOnlyMexc(intent, final, account_ref="b" * 64)
    with pytest.raises(MexcSettlementError, match="clave View"):
        close_final_spot_reservation(vault, foreign_key, ACCOUNT, intent.intent_id,
                                     now=NOW + timedelta(seconds=1))
    wrong_uid = _VerifiedReadOnlyMexc(intent, final, uid="987654321")
    with pytest.raises(MexcSettlementError, match="identidad"):
        close_final_spot_reservation(vault, wrong_uid, ACCOUNT, intent.intent_id,
                                     now=NOW + timedelta(seconds=1))
    with vault.session() as db:
        assert db.query(RealLiveSpotSettlement).count() == 0
        row = db.get(RealLiveSpotIntent, intent.intent_id)
        assert row.status == "SUBMISSION_UNKNOWN"
        assert row.reserved_quote == Decimal("650.65")
    vault.lock()


@pytest.mark.parametrize("rotated_field", ["uid", "reference"])
def test_final_mexc_reservation_keeps_hold_if_view_identity_changes_during_reads(
        tmp_path, rotated_field):
    vault, intent, state = prepared(tmp_path)
    begin_spot_submission(vault, intent, state, now=NOW)
    final = ObservedMexcOrder(intent.intent_id, "external-1", "CANCELED",
                              Decimal(0), Decimal(0), (), (), True)
    archive_observed_spot_order(vault, intent, final, now=NOW)
    bind_uid_for_spot_submission(vault, ACCOUNT, "123456789", now=NOW)

    class RotatingView(_VerifiedReadOnlyMexc):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.uid_checks = 0
            self.reference_checks = 0

        def account_reference(self):
            self.reference_checks += 1
            return ("b" * 64 if rotated_field == "reference" and self.reference_checks > 1
                    else ACCOUNT)

        def account_uid(self):
            self.uid_checks += 1
            return ("987654321" if rotated_field == "uid" and self.uid_checks > 1
                    else "123456789")

    client = RotatingView(intent, final, balance_at=NOW + timedelta(seconds=1))
    with pytest.raises(MexcSettlementError, match="identidad"):
        close_final_spot_reservation(vault, client, ACCOUNT, intent.intent_id,
                                     now=NOW + timedelta(seconds=2))
    assert (client.uid_checks if rotated_field == "uid" else client.reference_checks) >= 2
    with vault.session() as db:
        assert db.query(RealLiveSpotSettlement).count() == 0
        row = db.get(RealLiveSpotIntent, intent.intent_id)
        assert row.status == "SUBMISSION_UNKNOWN"
        assert row.reserved_quote == Decimal("650.65")
    vault.lock()


@pytest.mark.parametrize("field, altered", [
    ("price", "64999"),
    ("quantity", "0.02"),
])
def test_final_mexc_reservation_keeps_hold_if_order_terms_change(tmp_path, field, altered):
    vault, intent, state = prepared(tmp_path)
    begin_spot_submission(vault, intent, state, now=NOW)
    final = ObservedMexcOrder(intent.intent_id, "external-1", "CANCELED",
                              Decimal(0), Decimal(0), (), (), True)
    archive_observed_spot_order(vault, intent, final, now=NOW)

    class ChangedTerms(_VerifiedReadOnlyMexc):
        def order_by_client_id(self, symbol, client_order_id):
            order = super().order_by_client_id(symbol, client_order_id)
            order[field] = altered
            return order

    client = ChangedTerms(intent, final, balance_at=NOW + timedelta(seconds=1))
    with pytest.raises(MexcSettlementError, match="no concuerda"):
        close_final_spot_reservation(vault, client, ACCOUNT, intent.intent_id,
                                     now=NOW + timedelta(seconds=2))
    with vault.session() as db:
        assert db.query(RealLiveSpotSettlement).count() == 0
        row = db.connection().exec_driver_sql(
            "SELECT status, reserved_quote FROM real_live_spot_intents WHERE id=?",
            (intent.intent_id,),
        ).one()
        assert row == ("SUBMISSION_UNKNOWN", "650.65")
    vault.lock()


def test_final_mexc_reservation_rejects_reciprocal_terms_with_same_notional(tmp_path):
    vault, intent, state = prepared(tmp_path)
    begin_spot_submission(vault, intent, state, now=NOW)
    final = ObservedMexcOrder(intent.intent_id, "external-1", "CANCELED",
                              Decimal(0), Decimal(0), (), (), True)
    archive_observed_spot_order(vault, intent, final, now=NOW)

    class ReciprocalTerms(_VerifiedReadOnlyMexc):
        def order_by_client_id(self, symbol, client_order_id):
            order = super().order_by_client_id(symbol, client_order_id)
            order["price"] = "32500"
            order["quantity"] = "0.02"
            return order

    client = ReciprocalTerms(intent, final, balance_at=NOW + timedelta(seconds=1))
    with pytest.raises(MexcSettlementError, match="no concuerda"):
        close_final_spot_reservation(vault, client, ACCOUNT, intent.intent_id,
                                     now=NOW + timedelta(seconds=2))
    assert list_spot_execution_intents(vault)[0]["stage"] == "FINAL_NO_EXECUTION"
    vault.lock()


def test_v13_migrates_intent_terms_on_encrypted_copy(tmp_path):
    vault, intent, _state = prepared(tmp_path)
    assert vault._engine is not None
    with vault._engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE real_live_spot_intents DROP COLUMN limit_price_quote")
        connection.exec_driver_sql("ALTER TABLE real_live_spot_intents DROP COLUMN quantity_base")
        connection.exec_driver_sql("PRAGMA user_version = 13")
    vault.lock()

    vault.unlock(PASSWORD)
    with vault.session() as db:
        assert db.connection().exec_driver_sql("PRAGMA user_version").scalar_one() == SCHEMA_VERSION
        row = db.connection().exec_driver_sql(
            "SELECT limit_price_quote, quantity_base, status FROM real_live_spot_intents WHERE id=?",
            (intent.intent_id,),
        ).one()
        assert row == (None, None, "RESERVED")
        legacy = db.get(RealLiveSpotIntent, intent.intent_id)
        assert legacy.limit_price_quote is None and legacy.quantity_base is None
    vault.lock()
    backups = list((tmp_path / "backups").glob("pre-migrate-v13-*.db"))
    assert len(backups) == 1
    assert backups[0].read_bytes()[:16] != b"SQLite format 3\x00"


def test_canceled_mexc_order_without_fills_closes_without_portfolio_event(tmp_path):
    vault, intent, state = prepared(tmp_path)
    begin_spot_submission(vault, intent, state, now=NOW)
    final = ObservedMexcOrder(intent.intent_id, "external-1", "CANCELED",
                              Decimal(0), Decimal(0), (), (), True)
    archive_observed_spot_order(vault, intent, final, now=NOW)
    client = _VerifiedReadOnlyMexc(intent, final, balance_at=NOW + timedelta(seconds=1))
    result = close_final_spot_reservation(vault, client, ACCOUNT, intent.intent_id,
                                          now=NOW + timedelta(seconds=2))
    assert result["status"] == "RESERVATION_CLOSED"
    with vault.session() as db:
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_events").scalar_one() == 0
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_mexc_fill_bookings").scalar_one() == 0
        assert db.query(RealLiveSpotSettlement).count() == 1
    view = list_spot_execution_intents(vault)[0]
    assert view["stage"] == "FINAL_RESERVATION_CLOSED"
    assert view["booked_to_portfolio"] is False and view["reservation_closed"] is True
    vault.lock()


def test_v7_migrates_settlement_table_on_encrypted_copy(tmp_path):
    vault, intent, _state = prepared(tmp_path)
    with vault.session() as db:
        db.connection().exec_driver_sql("DROP TABLE real_live_spot_settlements")
        db.connection().exec_driver_sql("PRAGMA user_version = 7")
        db.commit()
    vault.lock()
    vault.unlock(PASSWORD)
    with vault.session() as db:
        assert db.connection().exec_driver_sql("PRAGMA user_version").scalar_one() == SCHEMA_VERSION
        assert db.query(RealLiveSpotSettlement).count() == 0
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_live_spot_intents").scalar_one() == 1
    vault.lock()
    backups = list((tmp_path / "backups").glob("pre-migrate-v7-*.db"))
    assert len(backups) == 1 and backups[0].read_bytes()[:16] != b"SQLite format 3\x00"
