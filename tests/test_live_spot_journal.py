from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from app.portfolio.vault import SCHEMA_VERSION
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.live_execution.journal import (
    LiveJournalError, begin_spot_submission, claim_spot_order_post, register_spot_policy,
    recover_spot_intent, recover_spot_signal_source, reserve_spot_intent,
    stop_spot_account, stop_spot_account_for_intent,
)
from app.live_execution.policy import LiveSpotIntent, LiveSpotPolicy, LiveSpotState
from app.portfolio.api import create_real_router
from app.portfolio.vault import RealVault, VaultError


NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
ACCOUNT = "a" * 64
PASSWORD = "isolated test vault password"


def case(symbol="BTCUSDT", base="BTC", quote="USDT", *, price="100000", quantity="0.001"):
    policy = LiveSpotPolicy(
        policy_id=str(uuid4()), strategy_version_id="strategy-v1", account_ref=ACCOUNT,
        symbol=symbol, base_asset=base, quote_asset=quote,
        max_order_quote=Decimal("150"), max_daily_turnover_quote=Decimal("500"),
        max_exposure_quote=Decimal("1000"), max_daily_loss_quote=Decimal("50"),
        max_fee_bps=Decimal("20"), max_slippage_bps=Decimal("50"),
        max_data_age_seconds=60, expires_at=NOW + timedelta(hours=1), enabled=True,
    )
    intent = LiveSpotIntent(
        intent_id=str(uuid4()), policy_id=policy.policy_id,
        strategy_version_id=policy.strategy_version_id, account_ref=ACCOUNT,
        symbol=symbol, side="BUY", quantity_base=Decimal(quantity),
        limit_price_quote=Decimal(price), estimated_fee_quote=Decimal("0.1"),
        signal_at=NOW - timedelta(seconds=1),
    )
    state = LiveSpotState(
        account_ref=ACCOUNT, symbol=symbol, reconciled=True, stopped=False,
        account_verified=True, instrument_verified=True,
        observed_at=NOW, quote_at=NOW, best_bid=Decimal(price) - 1,
        best_ask=Decimal(price), free_base=Decimal("0.0015"),
        free_quote=Decimal("200"), pending_base=Decimal(0),
        pending_quote=Decimal(0), daily_turnover_quote=Decimal(0),
        pending_turnover_quote=Decimal(0), exposure_quote=Decimal(0),
        daily_realized_pnl_quote=Decimal(0),
        average_cost_quote_per_base=Decimal(price),
    )
    return policy, intent, state


def new_vault(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSWORD, create=True)
    return vault


def test_reservation_is_idempotent_encrypted_and_survives_restart(tmp_path):
    vault = new_vault(tmp_path)
    policy, intent, state = case()
    assert register_spot_policy(vault, policy, now=NOW) == policy.policy_id
    assert register_spot_policy(vault, policy, now=NOW) == policy.policy_id
    first = reserve_spot_intent(vault, intent, state, now=NOW)
    assert first.status == "RESERVED"
    assert first.reserved_quote == Decimal("100.100")
    assert first.reserved_base == 0
    assert reserve_spot_intent(vault, intent, state, now=NOW) == first
    with pytest.raises(LiveJournalError, match="Intent ID was reused"):
        reserve_spot_intent(vault, replace(intent, quantity_base=Decimal("0.0009")), state, now=NOW)
    with pytest.raises(LiveJournalError, match="Policy ID was reused"):
        register_spot_policy(vault, replace(policy, max_order_quote=Decimal("120")), now=NOW)
    vault.lock()
    raw = (tmp_path / "real.db").read_bytes()
    assert raw[:16] != b"SQLite format 3\x00"
    assert b"strategy-v1" not in raw and ACCOUNT.encode() not in raw
    vault.unlock(PASSWORD)
    assert reserve_spot_intent(vault, intent, state, now=NOW) == first
    vault.lock()


def test_exact_intent_request_recovers_after_restart_and_rejects_tampering(tmp_path):
    vault = new_vault(tmp_path)
    policy, intent, state = case()
    register_spot_policy(vault, policy, now=NOW)
    reserve_spot_intent(vault, intent, state, now=NOW)
    vault.lock()
    vault.unlock(PASSWORD)
    assert recover_spot_intent(vault, intent.intent_id) == intent
    with vault.session() as db:
        db.connection().exec_driver_sql(
            "UPDATE real_live_spot_intents SET request_json=? WHERE id=?",
            ('{}', intent.intent_id),
        )
        db.commit()
    with pytest.raises(LiveJournalError, match="integrity"):
        recover_spot_intent(vault, intent.intent_id)
    vault.lock()


def test_v14_migrates_exact_intent_request_without_inventing_legacy_fields(tmp_path):
    vault = new_vault(tmp_path)
    policy, intent, state = case()
    register_spot_policy(vault, policy, now=NOW)
    reserve_spot_intent(vault, intent, state, now=NOW)
    assert vault._engine is not None
    with vault._engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE real_live_spot_intents DROP COLUMN request_json")
        connection.exec_driver_sql("PRAGMA user_version = 14")
    vault.lock()
    vault.unlock(PASSWORD)
    with vault.session() as db:
        assert db.connection().exec_driver_sql("PRAGMA user_version").scalar_one() == SCHEMA_VERSION
        row = db.connection().exec_driver_sql(
            "SELECT request_json,status FROM real_live_spot_intents WHERE id=?",
            (intent.intent_id,),
        ).one()
        assert row == (None, "RESERVED")
    with pytest.raises(LiveJournalError, match="Legacy intent"):
        recover_spot_intent(vault, intent.intent_id)
    vault.lock()
    backups = list((tmp_path / "backups").glob("pre-migrate-v14-*.db"))
    assert len(backups) == 1
    assert backups[0].read_bytes()[:16] != b"SQLite format 3\x00"


def test_v16_migrates_signal_source_columns_without_inventing_legacy_origin(tmp_path):
    vault = new_vault(tmp_path)
    policy, intent, state = case()
    register_spot_policy(vault, policy, now=NOW)
    assert reserve_spot_intent(vault, intent, state, now=NOW).status == "RESERVED"
    assert vault._engine is not None
    with vault._engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE real_live_spot_intents DROP COLUMN signal_source_json")
        connection.exec_driver_sql("ALTER TABLE real_live_spot_intents DROP COLUMN signal_source_digest")
        connection.exec_driver_sql("PRAGMA user_version = 16")
    vault.lock()
    vault.unlock(PASSWORD)
    with vault.session() as db:
        assert db.connection().exec_driver_sql("PRAGMA user_version").scalar_one() == SCHEMA_VERSION
        row = db.connection().exec_driver_sql(
            "SELECT signal_source_json,signal_source_digest,status "
            "FROM real_live_spot_intents WHERE id=?", (intent.intent_id,),
        ).one()
        assert row == (None, None, "RESERVED")
    assert recover_spot_intent(vault, intent.intent_id) == intent
    with pytest.raises(LiveJournalError, match="no archived signal source"):
        recover_spot_signal_source(vault, intent.intent_id)
    vault.lock()
    backups = list((tmp_path / "backups").glob("pre-migrate-v16-*.db"))
    assert len(backups) == 1
    assert backups[0].read_bytes()[:16] != b"SQLite format 3\x00"


def test_quote_reservation_covers_other_symbols_using_same_quote(tmp_path):
    vault = new_vault(tmp_path)
    btc_policy, btc_intent, btc_state = case()
    eth_policy, eth_intent, eth_state = case("ETHUSDT", "ETH", "USDT", price="2000", quantity="0.05")
    register_spot_policy(vault, btc_policy, now=NOW)
    register_spot_policy(vault, eth_policy, now=NOW)
    assert reserve_spot_intent(vault, btc_intent, btc_state, now=NOW).status == "RESERVED"
    second = reserve_spot_intent(vault, eth_intent, eth_state, now=NOW)
    assert second.status == "REJECTED" and second.reason == "INSUFFICIENT_QUOTE"
    assert second.reserved_quote == 0
    vault.lock()


def test_daily_turnover_limit_aggregates_policies_with_same_quote(tmp_path):
    vault = new_vault(tmp_path)
    btc_policy, btc_intent, btc_state = case()
    eth_policy, eth_intent, eth_state = case("ETHUSDT", "ETH", "USDT", price="2000", quantity="0.05")
    btc_policy = replace(btc_policy, max_daily_turnover_quote=Decimal("150"))
    eth_policy = replace(eth_policy, max_daily_turnover_quote=Decimal("150"))
    register_spot_policy(vault, btc_policy, now=NOW)
    register_spot_policy(vault, eth_policy, now=NOW)
    assert reserve_spot_intent(vault, btc_intent, replace(btc_state, free_quote=Decimal("500")),
                               now=NOW).status == "RESERVED"
    second = reserve_spot_intent(vault, eth_intent, replace(eth_state, free_quote=Decimal("500")), now=NOW)
    assert second.status == "REJECTED" and second.reason == "DAILY_TURNOVER_LIMIT"
    assert second.reserved_quote == 0
    vault.lock()


def test_submission_rechecks_other_policy_pending_turnover(tmp_path):
    vault = new_vault(tmp_path)
    btc_policy, btc_intent, btc_state = case()
    eth_policy, eth_intent, eth_state = case("ETHUSDT", "ETH", "USDT", price="2000", quantity="0.05")
    btc_policy = replace(btc_policy, max_daily_turnover_quote=Decimal("250"))
    eth_policy = replace(eth_policy, max_daily_turnover_quote=Decimal("250"))
    register_spot_policy(vault, btc_policy, now=NOW)
    register_spot_policy(vault, eth_policy, now=NOW)
    assert reserve_spot_intent(vault, btc_intent, replace(btc_state, free_quote=Decimal("500")),
                               now=NOW).status == "RESERVED"
    assert reserve_spot_intent(vault, eth_intent, replace(eth_state, free_quote=Decimal("500")),
                               now=NOW).status == "RESERVED"
    fresh = replace(eth_state, free_quote=Decimal("500"), daily_turnover_quote=Decimal("60"),
                    observed_at=NOW + timedelta(seconds=1), quote_at=NOW + timedelta(seconds=1))
    with pytest.raises(LiveJournalError, match="DAILY_TURNOVER_LIMIT"):
        begin_spot_submission(vault, eth_intent, fresh, now=NOW + timedelta(seconds=1))
    vault.lock()


def test_rejected_intent_is_recorded_without_reserving_and_idempotent(tmp_path):
    vault = new_vault(tmp_path)
    policy, intent, state = case()
    register_spot_policy(vault, replace(policy, enabled=False), now=NOW)
    denied = reserve_spot_intent(vault, intent, state, now=NOW)
    assert denied.status == "REJECTED" and denied.reason == "POLICY_DISABLED"
    assert denied.reserved_quote == 0 and denied.reserved_base == 0
    assert reserve_spot_intent(vault, intent, state, now=NOW) == denied
    with pytest.raises(LiveJournalError, match="Policy ID was reused"):
        register_spot_policy(vault, policy, now=NOW)
    vault.lock()


def test_missing_or_tampered_policy_fails_closed(tmp_path):
    vault = new_vault(tmp_path)
    policy, intent, state = case()
    with pytest.raises(LiveJournalError, match="not registered"):
        reserve_spot_intent(vault, intent, state, now=NOW)
    register_spot_policy(vault, policy, now=NOW)
    with vault.session() as db:
        db.connection().exec_driver_sql(
            "UPDATE real_live_spot_policies SET payload_json = '{}' WHERE id = ?", (policy.policy_id,)
        )
        db.commit()
    with pytest.raises(LiveJournalError, match="integrity"):
        reserve_spot_intent(vault, intent, state, now=NOW)
    vault.lock()


def test_base_reservation_covers_sell_on_other_quote_market(tmp_path):
    vault = new_vault(tmp_path)
    a_policy, a_intent, a_state = case()
    b_policy, b_intent, b_state = case("BTCEUR", "BTC", "EUR", price="100000")
    register_spot_policy(vault, a_policy, now=NOW)
    register_spot_policy(vault, b_policy, now=NOW)
    a_intent = replace(a_intent, side="SELL")
    b_intent = replace(b_intent, side="SELL")
    assert reserve_spot_intent(vault, a_intent, a_state, now=NOW).status == "RESERVED"
    second = reserve_spot_intent(vault, b_intent, b_state, now=NOW)
    assert second.status == "REJECTED" and second.reason == "INSUFFICIENT_BASE"
    vault.lock()


def test_multiple_pending_sells_share_daily_loss_budget(tmp_path):
    vault = new_vault(tmp_path)
    policy, template, state = case(quantity="0.0005")
    state = replace(state, average_cost_quote_per_base=Decimal("140000"), free_base=Decimal("0.002"))
    register_spot_policy(vault, policy, now=NOW)
    sells = [replace(template, intent_id=str(uuid4()), side="SELL") for _ in range(3)]
    first = reserve_spot_intent(vault, sells[0], state, now=NOW)
    assert first.status == "RESERVED" and first.reserved_base == Decimal("0.000501")
    assert reserve_spot_intent(vault, sells[1], state, now=NOW).status == "RESERVED"
    third = reserve_spot_intent(vault, sells[2], state, now=NOW)
    assert third.status == "REJECTED" and third.reason == "PROJECTED_DAILY_LOSS_LIMIT"
    vault.lock()


def test_pending_sell_with_unverifiable_request_fails_closed(tmp_path):
    vault = new_vault(tmp_path)
    policy, template, state = case(quantity="0.0005")
    register_spot_policy(vault, policy, now=NOW)
    sell = replace(template, side="SELL")
    assert reserve_spot_intent(vault, sell, state, now=NOW).status == "RESERVED"
    with vault.session() as db:
        db.connection().exec_driver_sql(
            "UPDATE real_live_spot_intents SET request_json = '{}' WHERE id = ?", (sell.intent_id,)
        )
        db.commit()
    with pytest.raises(LiveJournalError, match="not verifiable"):
        reserve_spot_intent(vault, replace(template, intent_id=str(uuid4())), state, now=NOW)
    vault.lock()


def test_persistent_stop_releases_unsent_reservations_and_blocks_new_ones(tmp_path):
    vault = new_vault(tmp_path)
    policy, intent, state = case()
    register_spot_policy(vault, policy, now=NOW)
    assert reserve_spot_intent(vault, intent, state, now=NOW).status == "RESERVED"
    stopped_at = NOW + timedelta(seconds=1)
    assert stop_spot_account(vault, ACCOUNT, now=stopped_at) == 1
    assert stop_spot_account(vault, ACCOUNT, now=stopped_at) == 0
    vault.lock()
    vault.unlock(PASSWORD)
    stopped = reserve_spot_intent(vault, replace(intent, intent_id=str(uuid4())), state, now=stopped_at)
    assert stopped.status == "REJECTED" and stopped.reason == "STOPPED"
    original = reserve_spot_intent(vault, intent, state, now=NOW)
    assert original.status == "STOPPED" and original.reserved_quote == 0
    with vault.session() as db:
        row = db.connection().exec_driver_sql(
            "SELECT updated_at FROM real_live_spot_intents WHERE id = ?", (intent.intent_id,)
        ).one()
        assert row[0].startswith("2026-10-13T12:00:01")
    vault.lock()


def test_stop_by_known_intent_releases_only_unsent_and_keeps_unknown_reserved(tmp_path):
    vault = new_vault(tmp_path)
    policy, first, state = case()
    state = replace(state, free_quote=Decimal("500"))
    second = replace(first, intent_id=str(uuid4()))
    register_spot_policy(vault, policy, now=NOW)
    reserve_spot_intent(vault, first, state, now=NOW)
    begin_spot_submission(vault, first, state, now=NOW)
    assert reserve_spot_intent(vault, second, state, now=NOW).status == "RESERVED"
    result = stop_spot_account_for_intent(vault, first.intent_id,
                                          now=NOW + timedelta(seconds=1))
    assert result == {"stopped": True, "released_unsent": 1,
                      "unresolved_sent_or_unknown": 1,
                      "exchange_orders_cancelled": False}
    with vault.session() as db:
        rows = db.connection().exec_driver_sql(
            "SELECT id,status,reserved_quote FROM real_live_spot_intents ORDER BY id"
        ).all()
        by_id = {row[0]: (row[1], Decimal(row[2])) for row in rows}
    assert by_id[first.intent_id][0] == "SUBMISSION_UNKNOWN"
    assert by_id[first.intent_id][1] > 0
    assert by_id[second.intent_id] == ("STOPPED", Decimal(0))
    assert stop_spot_account_for_intent(vault, second.intent_id,
                                        now=NOW + timedelta(seconds=2))["released_unsent"] == 0
    with pytest.raises(LiveJournalError, match="no encontrada"):
        stop_spot_account_for_intent(vault, str(uuid4()), now=NOW)
    vault.lock()


def test_local_stop_route_requires_vault_and_confirmation_without_trade_key(tmp_path):
    vault = new_vault(tmp_path)
    policy, intent, state = case()
    register_spot_policy(vault, policy, now=NOW)
    reserve_spot_intent(vault, intent, state, now=NOW)
    vault.lock()
    app = FastAPI()
    app.include_router(create_real_router(vault.path))
    client = TestClient(app)
    route = f"/real/execution/mexc/intents/{intent.intent_id}/stop-account"
    assert client.post(route, json={"confirmation": "DETENER"}).status_code == 401
    token = client.post("/real/vault/unlock", json={"passphrase": PASSWORD}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    assert client.post(route, headers=headers,
                       json={"confirmation": "CERRAR"}).status_code == 422
    response = client.post(route, headers=headers,
                           json={"confirmation": "DETENER"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"stopped": True, "released_unsent": 1,
                               "unresolved_sent_or_unknown": 0,
                               "exchange_orders_cancelled": False}
    status = client.get("/real/execution/mexc/intents", headers=headers).json()[0]
    assert status["account_stopped"] is True
    assert status["journal_status"] == "STOPPED"
    assert status["reserved_quote"] == "0"
    assert client.post(route, headers=headers,
                       json={"confirmation": "DETENER"}).json()["released_unsent"] == 0
    client.post("/real/vault/lock", headers=headers)


def test_submission_unknown_survives_restart_stop_and_blocks_resend(tmp_path):
    vault = new_vault(tmp_path)
    policy, intent, state = case()
    register_spot_policy(vault, policy, now=NOW)
    reserved = reserve_spot_intent(vault, intent, state, now=NOW)
    submitting = begin_spot_submission(vault, intent, state,
                                       now=NOW + timedelta(milliseconds=1))
    assert submitting.status == "SUBMISSION_UNKNOWN"
    assert submitting.reserved_quote == reserved.reserved_quote
    with pytest.raises(LiveJournalError, match="not reserved"):
        begin_spot_submission(vault, intent, state, now=NOW)
    other = replace(intent, intent_id=str(uuid4()))
    assert reserve_spot_intent(vault, other, state, now=NOW).reason == "INSUFFICIENT_QUOTE"
    vault.lock()
    vault.unlock(PASSWORD)
    assert reserve_spot_intent(vault, intent, state, now=NOW).status == "SUBMISSION_UNKNOWN"
    assert stop_spot_account(vault, ACCOUNT, now=NOW + timedelta(seconds=1)) == 0
    after_stop = reserve_spot_intent(vault, intent, state, now=NOW)
    assert after_stop.status == "SUBMISSION_UNKNOWN"
    assert after_stop.reserved_quote == reserved.reserved_quote
    with pytest.raises(LiveJournalError, match="not reserved"):
        begin_spot_submission(vault, intent, state, now=NOW + timedelta(seconds=2))
    vault.lock()


def test_post_claim_is_durable_single_use_and_stays_uncertain_after_restart(tmp_path):
    vault = new_vault(tmp_path)
    policy, intent, state = case()
    register_spot_policy(vault, policy, now=NOW)
    reserve_spot_intent(vault, intent, state, now=NOW)
    begin_spot_submission(vault, intent, state, now=NOW)
    claim = claim_spot_order_post(vault, intent, now=NOW + timedelta(milliseconds=1))
    assert claim.status == "SUBMISSION_UNKNOWN"
    assert claim.reason == "POST_ATTEMPTED_OUTCOME_UNKNOWN"
    with pytest.raises(LiveJournalError, match="first POST"):
        claim_spot_order_post(vault, intent, now=NOW + timedelta(milliseconds=2))
    vault.lock()
    vault.unlock(PASSWORD)
    with pytest.raises(LiveJournalError, match="first POST"):
        claim_spot_order_post(vault, intent, now=NOW + timedelta(milliseconds=3))
    assert stop_spot_account(vault, ACCOUNT, now=NOW + timedelta(seconds=1)) == 0
    assert reserve_spot_intent(vault, intent, state, now=NOW).reserved_quote > 0
    vault.lock()


def test_post_claim_rejects_stale_preflight_and_stop(tmp_path):
    vault = new_vault(tmp_path)
    policy, intent, state = case()
    register_spot_policy(vault, policy, now=NOW)
    reserve_spot_intent(vault, intent, state, now=NOW)
    begin_spot_submission(vault, intent, state, now=NOW)
    with pytest.raises(LiveJournalError, match="no longer fresh"):
        claim_spot_order_post(vault, intent, now=NOW + timedelta(seconds=3))
    stop_spot_account(vault, ACCOUNT, now=NOW + timedelta(seconds=1))
    with pytest.raises(LiveJournalError, match="stopped"):
        claim_spot_order_post(vault, intent, now=NOW + timedelta(seconds=1))
    with vault.session() as db:
        assert db.connection().exec_driver_sql(
            "SELECT reason FROM real_live_spot_intents WHERE id=?", (intent.intent_id,),
        ).scalar_one() == "SUBMISSION_OUTCOME_UNKNOWN"
    vault.lock()


def test_submission_cannot_begin_without_reservation_or_after_stop(tmp_path):
    vault = new_vault(tmp_path)
    policy, intent, state = case()
    with pytest.raises(LiveJournalError, match="not reserved"):
        begin_spot_submission(vault, intent, state, now=NOW)
    with pytest.raises(ValueError, match="Fresh intent"):
        begin_spot_submission(vault, "bad-id", state, now=NOW)
    register_spot_policy(vault, policy, now=NOW)
    reserve_spot_intent(vault, intent, state, now=NOW)
    stop_spot_account(vault, ACCOUNT, now=NOW)
    with pytest.raises(LiveJournalError, match="not reserved"):
        begin_spot_submission(vault, intent, state, now=NOW)
    vault.lock()


@pytest.mark.parametrize("change,reason", [
    ({"reconciled": False}, "NOT_RECONCILED"),
    ({"account_verified": False}, "UNVERIFIED_ACCOUNT"),
    ({"instrument_verified": False}, "UNVERIFIED_INSTRUMENT"),
    ({"free_quote": Decimal("50")}, "INSUFFICIENT_QUOTE"),
    ({"best_bid": Decimal("89999"), "best_ask": Decimal("90000")}, "PRICE_BOUND"),
    ({"daily_realized_pnl_quote": Decimal("-50")}, "DAILY_LOSS_LIMIT"),
])
def test_submission_rechecks_current_account_risk_without_mutating_reservation(
        tmp_path, change, reason):
    vault = new_vault(tmp_path)
    policy, intent, state = case()
    register_spot_policy(vault, policy, now=NOW)
    reserved = reserve_spot_intent(vault, intent, state, now=NOW)
    fresh = replace(state, observed_at=NOW + timedelta(seconds=1),
                    quote_at=NOW + timedelta(seconds=1), **change)
    with pytest.raises(LiveJournalError, match=reason):
        begin_spot_submission(vault, intent, fresh, now=NOW + timedelta(seconds=1))
    assert reserve_spot_intent(vault, intent, state, now=NOW) == reserved
    vault.lock()


def test_submission_rejects_changed_intent_expired_signal_and_expired_policy(tmp_path):
    vault = new_vault(tmp_path)
    policy, intent, state = case()
    register_spot_policy(vault, policy, now=NOW)
    reserved = reserve_spot_intent(vault, intent, state, now=NOW)
    with pytest.raises(LiveJournalError, match="differs from reservation"):
        begin_spot_submission(vault, replace(intent, quantity_base=Decimal("0.0009")),
                              state, now=NOW)
    later = NOW + timedelta(seconds=61)
    fresh = replace(state, observed_at=later, quote_at=later)
    with pytest.raises(LiveJournalError, match="STALE_DATA_OR_SIGNAL"):
        begin_spot_submission(vault, intent, fresh, now=later)
    assert reserve_spot_intent(vault, intent, state, now=NOW) == reserved
    vault.lock()

    another = new_vault(tmp_path / "another")
    short_policy, short_intent, short_state = case()
    short_policy = replace(short_policy, expires_at=NOW + timedelta(seconds=5))
    register_spot_policy(another, short_policy, now=NOW)
    assert reserve_spot_intent(another, short_intent, short_state, now=NOW).status == "RESERVED"
    later = NOW + timedelta(seconds=6)
    fresh = replace(short_state, observed_at=later, quote_at=later)
    with pytest.raises(LiveJournalError, match="POLICY_OR_SIGNAL_TIME_INVALID"):
        begin_spot_submission(another, short_intent, fresh, now=later)
    another.lock()


def test_submission_rejects_stale_quote_even_with_fresh_signal(tmp_path):
    vault = new_vault(tmp_path)
    policy, intent, state = case()
    register_spot_policy(vault, policy, now=NOW)
    assert reserve_spot_intent(vault, intent, state, now=NOW).status == "RESERVED"
    later = NOW + timedelta(seconds=5)
    stale_quote = replace(state, observed_at=later,
                          quote_at=NOW - timedelta(seconds=61))
    with pytest.raises(LiveJournalError, match="STALE_DATA_OR_SIGNAL"):
        begin_spot_submission(vault, intent, stale_quote, now=later)
    with vault.session() as db:
        row = db.connection().exec_driver_sql(
            "SELECT status, reserved_quote FROM real_live_spot_intents WHERE id = ?",
            (intent.intent_id,),
        ).one()
        assert row[0] == "RESERVED" and Decimal(row[1]) == Decimal("100.100")
    vault.lock()


def test_two_vault_instances_cannot_reserve_same_balance_twice(tmp_path):
    first_vault = new_vault(tmp_path)
    second_vault = RealVault(tmp_path / "real.db")
    second_vault.unlock(PASSWORD)
    policy, intent, state = case()
    register_spot_policy(first_vault, policy, now=NOW)
    other_intent = replace(intent, intent_id=str(uuid4()))

    def reserve(vault, item):
        return reserve_spot_intent(vault, item, state, now=NOW).status

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(reserve, first_vault, intent),
                   pool.submit(reserve, second_vault, other_intent)]
        assert sorted(f.result() for f in futures) == ["REJECTED", "RESERVED"]
    first_vault.lock()
    second_vault.lock()


def test_v4_migrates_on_encrypted_copy_and_backup_contains_journal(tmp_path):
    vault = new_vault(tmp_path)
    with vault.session() as db:
        connection = db.connection()
        for name in ("real_live_spot_intents", "real_live_spot_policies", "real_live_stops"):
            connection.exec_driver_sql(f"DROP TABLE {name}")
        connection.exec_driver_sql("PRAGMA user_version = 4")
        db.commit()
    vault.lock()
    vault.unlock(PASSWORD)
    with vault.session() as db:
        assert db.connection().exec_driver_sql("PRAGMA user_version").scalar_one() == SCHEMA_VERSION
    policy, intent, state = case()
    register_spot_policy(vault, policy, now=NOW)
    assert reserve_spot_intent(vault, intent, state, now=NOW).status == "RESERVED"
    backup = vault.backup(tmp_path / "journal-backup.db")
    vault.lock()
    migrated_from = list((tmp_path / "backups").glob("pre-migrate-v4-*.db"))
    assert len(migrated_from) == 1
    assert migrated_from[0].read_bytes()[:16] != b"SQLite format 3\x00"
    restored = RealVault(backup)
    restored.unlock(PASSWORD)
    assert reserve_spot_intent(restored, intent, state, now=NOW).status == "RESERVED"
    restored.lock()


def test_failed_v4_migration_preserves_original_and_preimage(tmp_path, monkeypatch):
    vault = new_vault(tmp_path)
    with vault.session() as db:
        connection = db.connection()
        for name in ("real_live_spot_intents", "real_live_spot_policies", "real_live_stops"):
            connection.exec_driver_sql(f"DROP TABLE {name}")
        connection.exec_driver_sql("PRAGMA user_version = 4")
        db.commit()
    vault.lock()
    original = (tmp_path / "real.db").read_bytes()

    def fail_replace(*_):
        raise OSError("simulated replacement failure")

    monkeypatch.setattr("app.portfolio.vault.os.replace", fail_replace)
    with pytest.raises(VaultError, match="original file was preserved"):
        vault.unlock(PASSWORD)
    assert (tmp_path / "real.db").read_bytes() == original
    assert not vault.unlocked
    assert len(list((tmp_path / "backups").glob("pre-migrate-v4-*.db"))) == 1
