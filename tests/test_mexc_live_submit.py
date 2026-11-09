"""El POST real sólo se ejerce contra transporte falso; nunca usa una clave real."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import hmac
import time
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest

from app.integrations.mexc_readonly import MexcReadOnlyError
from app.live_execution.journal import (
    LiveJournalError, recover_spot_intent, register_spot_policy, stop_spot_account,
)
from app.live_execution.mexc_instrument import MexcSpotRules
from app.live_execution.mexc_live_order import MexcOrderSendError, MexcSpotLiveClient
from app.live_execution.mexc_stop_supervisor import MexcStopSupervisor
from app.live_execution.mexc_live_cancel import (cancel_next_stopped_spot_limit_once,
                                                  cancel_stopped_spot_limit_once,
                                                  observe_next_stopped_spot_limit_once,
                                                  observe_stopped_spot_limit,
                                                  reobserve_next_stopped_spot_limit_once,
                                                  supervise_stopped_spot_account_once)
from app.live_execution.mexc_order_observer import MexcOrderObservationError
from app.live_execution.reconciliation_view import list_spot_execution_intents
from app.live_execution.mexc_live_submit import MexcLiveInputs, submit_spot_limit_once
from app.live_execution import mexc_live_submit
from app.live_execution.policy import LiveSpotIntent, LiveSpotPolicy, LiveSpotState
from app.portfolio.vault import RealVault
from app.portfolio.api import RealAccess
from app.portfolio.models import RealLiveOrderObservation, RealLiveSpotIntent


NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
ACCOUNT = "a" * 64
KEY = "fixture-trade-key"
SECRET = "fixture-trade-secret"
PASSWORD = "isolated live submit password"


def case():
    policy = LiveSpotPolicy(
        policy_id=str(uuid4()), strategy_version_id="v1", account_ref=ACCOUNT,
        symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT",
        max_order_quote=Decimal("150"), max_daily_turnover_quote=Decimal("500"),
        max_exposure_quote=Decimal("1000"), max_daily_loss_quote=Decimal("50"),
        max_fee_bps=Decimal("20"), max_slippage_bps=Decimal("50"),
        max_data_age_seconds=60, expires_at=NOW + timedelta(hours=1), enabled=True,
    )
    intent = LiveSpotIntent(
        intent_id=str(uuid4()), policy_id=policy.policy_id,
        strategy_version_id="v1", account_ref=ACCOUNT, symbol="BTCUSDT",
        side="BUY", quantity_base=Decimal("0.001"),
        limit_price_quote=Decimal("100000"), estimated_fee_quote=Decimal("0.1"),
        signal_at=NOW,
    )
    state = LiveSpotState(
        account_ref=ACCOUNT, symbol="BTCUSDT", reconciled=True, stopped=False,
        account_verified=True, instrument_verified=True, observed_at=NOW,
        quote_at=NOW, best_bid=Decimal("99999"), best_ask=Decimal("100000"),
        free_base=Decimal(0), free_quote=Decimal("200"), pending_base=Decimal(0),
        pending_quote=Decimal(0), daily_turnover_quote=Decimal(0),
        pending_turnover_quote=Decimal(0), exposure_quote=Decimal(0),
        daily_realized_pnl_quote=Decimal(0),
    )
    rules = MexcSpotRules(
        symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT", observed_at=NOW,
        buy_allowed=True, sell_allowed=True, base_precision=6, quote_precision=2,
        base_step=Decimal("0.000001"), min_quote=Decimal("1"),
        max_quote=Decimal("10000"), fee_rate_upper=Decimal("0.0005"),
        bid_multiplier_up=None, ask_multiplier_down=None,
    )
    return policy, intent, state, rules


def fake_client(intent, *, uid="123456789", fail_real=False, revoke_after_test=False):
    posts = []
    gets = []

    def get(url, headers):
        parsed = urlparse(url)
        assert parsed.scheme == "https" and parsed.netloc == "api.mexc.com"
        assert parsed.path in ("/api/v3/apiKeyInfo", "/api/v3/uid")
        assert headers == {"X-MEXC-APIKEY": KEY}
        unsigned, signature = parsed.query.rsplit("&signature=", 1)
        assert signature == hmac.new(SECRET.encode(), unsigned.encode(), hashlib.sha256).hexdigest()
        gets.append(parsed.path)
        if revoke_after_test and parsed.path.endswith("apiKeyInfo") and len(gets) > 2:
            return {"status": "VALID", "permissions": "SPOT_ACCOUNT_READ",
                    "apikey": KEY, "remainingValidity": 30}
        return ({"status": "VALID", "permissions": "SPOT_ACCOUNT_READ,SPOT_DEAL_WRITE",
                 "apikey": KEY, "remainingValidity": 30}
                if parsed.path.endswith("apiKeyInfo") else {"uid": uid})

    def post(url, body, headers):
        unsigned, signature = body.rsplit("&signature=", 1)
        assert signature == hmac.new(SECRET.encode(), unsigned.encode(), hashlib.sha256).hexdigest()
        assert headers["X-MEXC-APIKEY"] == KEY
        params = parse_qs(unsigned)
        assert params["symbol"] == [intent.symbol]
        assert params["newClientOrderId"] == [intent.intent_id.replace("-", "")]
        assert params["quantity"] == ["0.001"] and params["price"] == ["100000"]
        posts.append(urlparse(url).path)
        if url.endswith("/order/test"):
            return {}
        assert url == "https://api.mexc.com/api/v3/order"
        assert headers["Content-Type"] == "application/json"
        if fail_real:
            raise RuntimeError("private upstream response containing fixture-trade-secret")
        return {"symbol": "BTCUSDT", "orderId": "external-order-1",
                "price": "100000", "origQty": "0.001", "type": "LIMIT", "side": "BUY"}

    return MexcSpotLiveClient(KEY, SECRET, http_get=get, http_post=post,
                              clock_ms=lambda: 1_800_000_000_000), gets, posts


class FakeView:
    def __init__(self, account_ref=ACCOUNT, uid="123456789"):
        self._account_ref = account_ref
        self._uid = uid

    def account_reference(self):
        return self._account_ref

    def account_uid(self):
        return self._uid


def submit(vault, trade, policy, intent, state, rules):
    return submit_spot_limit_once(
        vault, trade, FakeView(account_ref=intent.account_ref), policy, intent, state, rules,
        last_price=Decimal("100000"), last_price_at=NOW,
        refresh_inputs=lambda: fresh_inputs(state, rules),
        clock=lambda: NOW + timedelta(seconds=1),
    )


def fresh_inputs(state, rules):
    later = NOW + timedelta(seconds=1)
    return MexcLiveInputs(
        replace(state, observed_at=later, quote_at=later),
        replace(rules, observed_at=later), Decimal("100000"), later,
    )


def test_real_transport_is_one_post_after_test_identity_and_durable_claim(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSWORD, create=True)
    policy, intent, state, rules = case()
    register_spot_policy(vault, policy, now=NOW)
    trade, gets, posts = fake_client(intent)
    result = submit(vault, trade, policy, intent, state, rules)
    assert result.external_order_id == "external-order-1"
    assert gets == ["/api/v3/apiKeyInfo", "/api/v3/uid",
                    "/api/v3/apiKeyInfo", "/api/v3/uid"]
    assert posts == ["/api/v3/order/test", "/api/v3/order"]
    with vault.session() as db:
        row = db.connection().exec_driver_sql(
            "SELECT status, reason, reserved_quote FROM real_live_spot_intents WHERE id=?",
            (intent.intent_id,),
        ).one()
        assert row == ("SUBMISSION_UNKNOWN", "POST_ACKNOWLEDGED", "100.100")
        assert db.connection().exec_driver_sql(
            "SELECT count(*) FROM real_mexc_uid_links WHERE account_ref=?", (ACCOUNT,)
        ).scalar_one() == 1
    with pytest.raises(MexcOrderSendError, match="Reserva REAL"):
        submit(vault, trade, policy, intent, state, rules)
    vault.lock()
    vault.unlock(PASSWORD)
    with pytest.raises(MexcOrderSendError, match="Reserva REAL"):
        submit(vault, trade, policy, intent, state, rules)
    assert posts.count("/api/v3/order") == 1
    vault.lock()


def test_final_authorization_is_checked_inside_the_post_lock(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSWORD, create=True)
    policy, intent, state, rules = case()
    register_spot_policy(vault, policy, now=NOW)
    trade, _, posts = fake_client(intent)
    checks = []

    def authorized():
        checks.append(True)
        if len(checks) == 3:
            raise MexcOrderSendError("Bot paused immediately before POST")

    with pytest.raises(MexcOrderSendError, match="immediately before POST"):
        submit_spot_limit_once(vault, trade, FakeView(), policy, intent, state, rules,
            last_price=Decimal("100000"), last_price_at=NOW,
            refresh_inputs=lambda: fresh_inputs(state, rules), clock=lambda: NOW + timedelta(seconds=1),
            assert_authorized=authorized)
    assert posts == ["/api/v3/order/test"]
    with vault.session() as db:
        assert db.get(RealLiveSpotIntent, intent.intent_id).status == "SUBMISSION_UNKNOWN"
    vault.lock()


def test_trade_permission_revoked_after_test_never_opens_real_post(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSWORD, create=True)
    policy, intent, state, rules = case()
    register_spot_policy(vault, policy, now=NOW)
    trade, gets, posts = fake_client(intent, revoke_after_test=True)
    with pytest.raises(MexcOrderSendError, match="permisos MEXC no verificables"):
        submit(vault, trade, policy, intent, state, rules)
    assert gets == ["/api/v3/apiKeyInfo", "/api/v3/uid", "/api/v3/apiKeyInfo"]
    assert posts == ["/api/v3/order/test"]
    with vault.session() as db:
        assert db.connection().exec_driver_sql(
            "SELECT status, reserved_quote FROM real_live_spot_intents WHERE id=?",
            (intent.intent_id,),
        ).one() == ("RESERVED", "100.100")
    vault.lock()


def test_clock_rollback_after_order_test_never_opens_real_post(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSWORD, create=True)
    policy, intent, state, rules = case()
    register_spot_policy(vault, policy, now=NOW)
    trade, _gets, posts = fake_client(intent)
    ticks = iter((NOW + timedelta(seconds=1), NOW + timedelta(seconds=1),
                  *(NOW + timedelta(milliseconds=500) for _ in range(5))))
    earlier = NOW + timedelta(milliseconds=250)

    with pytest.raises(MexcOrderSendError, match="Reloj local retrocedió"):
        submit_spot_limit_once(
            vault, trade, FakeView(), policy, intent, state, rules,
            last_price=Decimal("100000"), last_price_at=NOW,
            refresh_inputs=lambda: MexcLiveInputs(
                replace(state, observed_at=earlier, quote_at=earlier),
                replace(rules, observed_at=earlier), Decimal("100000"), earlier,
            ),
            clock=lambda: next(ticks),
        )
    assert posts == ["/api/v3/order/test"]
    with vault.session() as db:
        assert db.connection().exec_driver_sql(
            "SELECT status, reserved_quote FROM real_live_spot_intents WHERE id=?",
            (intent.intent_id,),
        ).one() == ("RESERVED", "100.100")
    vault.lock()


def test_clock_rollback_at_post_gate_keeps_uncertain_reservation_without_post(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSWORD, create=True)
    policy, intent, state, rules = case()
    register_spot_policy(vault, policy, now=NOW)
    trade, _gets, posts = fake_client(intent)
    ticks = iter((NOW + timedelta(seconds=1),) * 4
                 + (NOW + timedelta(milliseconds=500),))

    with pytest.raises(MexcOrderSendError, match="Reloj local retrocedió"):
        submit_spot_limit_once(
            vault, trade, FakeView(), policy, intent, state, rules,
            last_price=Decimal("100000"), last_price_at=NOW,
            refresh_inputs=lambda: fresh_inputs(state, rules),
            clock=lambda: next(ticks),
        )
    assert posts == ["/api/v3/order/test"]
    with vault.session() as db:
        assert db.connection().exec_driver_sql(
            "SELECT status, reason, reserved_quote FROM real_live_spot_intents WHERE id=?",
            (intent.intent_id,),
        ).one() == ("SUBMISSION_UNKNOWN", "POST_ATTEMPTED_OUTCOME_UNKNOWN", "100.100")
    vault.lock()


def test_clock_rollback_after_ack_does_not_erase_ack_or_backdate_journal(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSWORD, create=True)
    policy, intent, state, rules = case()
    register_spot_policy(vault, policy, now=NOW)
    trade, _gets, posts = fake_client(intent)
    ticks = iter((NOW + timedelta(seconds=1),) * 5
                 + (NOW + timedelta(milliseconds=500),))
    result = submit_spot_limit_once(
        vault, trade, FakeView(), policy, intent, state, rules,
        last_price=Decimal("100000"), last_price_at=NOW,
        refresh_inputs=lambda: fresh_inputs(state, rules),
        clock=lambda: next(ticks),
    )
    assert result.external_order_id == "external-order-1"
    assert posts == ["/api/v3/order/test", "/api/v3/order"]
    with vault.session() as db:
        row = db.connection().exec_driver_sql(
            "SELECT reason, updated_at FROM real_live_spot_intents WHERE id=?",
            (intent.intent_id,),
        ).one()
        assert row.reason == "POST_ACKNOWLEDGED"
        assert datetime.fromisoformat(row.updated_at) >= NOW + timedelta(seconds=1)
    vault.lock()


def test_view_identity_changed_after_test_never_opens_real_post(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSWORD, create=True)
    policy, intent, state, rules = case()
    register_spot_policy(vault, policy, now=NOW)
    trade, gets, posts = fake_client(intent)

    class ChangingView(FakeView):
        calls = 0

        def account_uid(self):
            self.calls += 1
            return "123456789" if self.calls == 1 else "987654321"

    with pytest.raises(MexcOrderSendError, match="Identidad o permisos MEXC"):
        submit_spot_limit_once(
            vault, trade, ChangingView(), policy, intent, state, rules,
            last_price=Decimal("100000"), last_price_at=NOW,
            refresh_inputs=lambda: fresh_inputs(state, rules),
            clock=lambda: NOW + timedelta(seconds=1),
        )
    assert gets == ["/api/v3/apiKeyInfo", "/api/v3/uid"]
    assert posts == ["/api/v3/order/test"]
    with vault.session() as db:
        assert db.connection().exec_driver_sql(
            "SELECT status, reserved_quote FROM real_live_spot_intents WHERE id=?",
            (intent.intent_id,),
        ).one() == ("RESERVED", "100.100")
    vault.lock()


def test_uncertain_post_failure_is_never_retried_and_stop_keeps_reservation(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSWORD, create=True)
    policy, intent, state, rules = case()
    register_spot_policy(vault, policy, now=NOW)
    trade, _gets, posts = fake_client(intent, fail_real=True)
    with pytest.raises(MexcOrderSendError, match="incierto") as error:
        submit(vault, trade, policy, intent, state, rules)
    assert SECRET not in str(error.value)
    with vault.session() as db:
        row = db.connection().exec_driver_sql(
            "SELECT reason, reserved_quote FROM real_live_spot_intents WHERE id=?",
            (intent.intent_id,),
        ).one()
        assert row == ("POST_ATTEMPTED_OUTCOME_UNKNOWN", "100.100")
    with pytest.raises(MexcOrderSendError, match="Reserva REAL"):
        submit(vault, trade, policy, intent, state, rules)
    assert posts.count("/api/v3/order") == 1
    assert stop_spot_account(vault, ACCOUNT, now=NOW + timedelta(seconds=2)) == 0
    with vault.session() as db:
        assert db.connection().exec_driver_sql(
            "SELECT reserved_quote FROM real_live_spot_intents WHERE id=?", (intent.intent_id,),
        ).scalar_one() == "100.100"
    vault.lock()


def test_wrong_uid_cannot_reserve_or_send(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSWORD, create=True)
    policy, intent, state, rules = case()
    register_spot_policy(vault, policy, now=NOW)
    trade, _gets, posts = fake_client(intent, uid="999999")
    with pytest.raises(MexcOrderSendError, match="no pertenece"):
        submit(vault, trade, policy, intent, state, rules)
    with vault.session() as db:
        assert db.connection().exec_driver_sql(
            "SELECT count(*) FROM real_live_spot_intents").scalar_one() == 0
    assert posts == []
    vault.lock()


def test_view_key_reference_must_match_intent_before_trade_call(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSWORD, create=True)
    policy, intent, state, rules = case()
    register_spot_policy(vault, policy, now=NOW)
    trade, gets, posts = fake_client(intent)
    with pytest.raises(MexcOrderSendError, match="clave View"):
        submit_spot_limit_once(
            vault, trade, FakeView(account_ref="b" * 64), policy, intent, state, rules,
            last_price=Decimal("100000"), last_price_at=NOW,
            refresh_inputs=lambda: fresh_inputs(state, rules),
            clock=lambda: NOW + timedelta(seconds=1),
        )
    assert gets == [] and posts == []
    with vault.session() as db:
        assert db.connection().exec_driver_sql(
            "SELECT count(*) FROM real_live_spot_intents"
        ).scalar_one() == 0
    vault.lock()


def test_rotated_key_same_uid_cannot_start_new_limit_budget(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSWORD, create=True)
    policy, intent, state, rules = case()
    register_spot_policy(vault, policy, now=NOW)
    first_trade, _first_gets, first_posts = fake_client(intent)
    submit(vault, first_trade, policy, intent, state, rules)
    assert first_posts == ["/api/v3/order/test", "/api/v3/order"]

    rotated_ref = "b" * 64
    rotated_policy = replace(policy, policy_id=str(uuid4()), account_ref=rotated_ref)
    rotated_intent = replace(intent, intent_id=str(uuid4()), policy_id=rotated_policy.policy_id,
                             account_ref=rotated_ref)
    rotated_state = replace(state, account_ref=rotated_ref)
    register_spot_policy(vault, rotated_policy, now=NOW)
    rotated_trade, _gets, rotated_posts = fake_client(rotated_intent)
    with pytest.raises(MexcOrderSendError, match="Otra clave"):
        submit(vault, rotated_trade, rotated_policy, rotated_intent, rotated_state, rules)
    assert rotated_posts == []
    with vault.session() as db:
        assert db.connection().exec_driver_sql(
            "SELECT count(*) FROM real_live_spot_intents WHERE account_ref=?", (rotated_ref,)
        ).scalar_one() == 0
    vault.lock()


def test_stop_after_durable_claim_blocks_the_live_post(tmp_path, monkeypatch):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSWORD, create=True)
    policy, intent, state, rules = case()
    register_spot_policy(vault, policy, now=NOW)
    trade, _gets, posts = fake_client(intent)
    original = mexc_live_submit.claim_spot_order_post

    def claim_then_stop(vault_arg, intent_arg, *, now):
        result = original(vault_arg, intent_arg, now=now)
        stop_spot_account(vault_arg, ACCOUNT, now=now + timedelta(milliseconds=1))
        return result

    monkeypatch.setattr(mexc_live_submit, "claim_spot_order_post", claim_then_stop)
    with pytest.raises(MexcOrderSendError, match="Parada"):
        submit(vault, trade, policy, intent, state, rules)
    assert posts == ["/api/v3/order/test"]
    with vault.session() as db:
        assert db.connection().exec_driver_sql(
            "SELECT reason FROM real_live_spot_intents WHERE id=?", (intent.intent_id,),
        ).scalar_one() == "POST_ATTEMPTED_OUTCOME_UNKNOWN"
    vault.lock()


def test_disabled_instrument_side_blocks_all_private_calls(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSWORD, create=True)
    policy, intent, state, rules = case()
    register_spot_policy(vault, policy, now=NOW)
    trade, gets, posts = fake_client(intent)
    with pytest.raises(MexcOrderSendError, match="SIDE_DISABLED"):
        submit(vault, trade, policy, intent, state, replace(rules, buy_allowed=False))
    assert gets == [] and posts == []
    with vault.session() as db:
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_live_spot_intents").scalar_one() == 0
    vault.lock()


def test_unverifiable_ack_is_not_treated_as_success_or_retried(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSWORD, create=True)
    policy, intent, state, rules = case()
    register_spot_policy(vault, policy, now=NOW)
    trade, _gets, posts = fake_client(intent)
    original_post = trade._http_post

    def mismatched_ack(url, body, headers):
        response = original_post(url, body, headers)
        if url.endswith("/order"):
            return {**response, "origQty": "0.002"}
        return response

    trade._http_post = mismatched_ack
    with pytest.raises(MexcOrderSendError, match="no verificable"):
        submit(vault, trade, policy, intent, state, rules)
    with vault.session() as db:
        assert db.connection().exec_driver_sql(
            "SELECT reason, reserved_quote FROM real_live_spot_intents WHERE id=?",
            (intent.intent_id,),
        ).one() == ("POST_ATTEMPTED_OUTCOME_UNKNOWN", "100.100")
    with pytest.raises(MexcOrderSendError, match="Reserva REAL"):
        submit(vault, trade, policy, intent, state, rules)
    assert posts == ["/api/v3/order/test", "/api/v3/order"]
    vault.lock()


def test_rules_and_price_expiring_during_test_order_prevent_live_post(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSWORD, create=True)
    policy, intent, state, rules = case()
    register_spot_policy(vault, policy, now=NOW)
    trade, _gets, posts = fake_client(intent)
    times = iter((NOW + timedelta(seconds=1), NOW + timedelta(seconds=1),
                  NOW + timedelta(seconds=62)))
    with pytest.raises(MexcOrderSendError, match="STALE_INSTRUMENT_OR_TRADE"):
        submit_spot_limit_once(
            vault, trade, FakeView(), policy, intent, state, rules,
            last_price=Decimal("100000"), last_price_at=NOW,
            refresh_inputs=lambda: fresh_inputs(state, rules),
            clock=lambda: next(times),
        )
    assert posts == ["/api/v3/order/test"]
    with vault.session() as db:
        assert db.connection().exec_driver_sql(
            "SELECT status FROM real_live_spot_intents WHERE id=?",
            (intent.intent_id,),
        ).scalar_one() == "RESERVED"
    vault.lock()


@pytest.mark.parametrize("failure", ("provider", "unchanged", "bad_result"))
def test_missing_new_observations_leave_reservation_without_live_post(tmp_path, failure):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSWORD, create=True)
    policy, intent, state, rules = case()
    register_spot_policy(vault, policy, now=NOW)
    trade, _gets, posts = fake_client(intent)

    def refresh():
        if failure == "provider":
            raise RuntimeError("private fixture-trade-secret upstream text")
        if failure == "bad_result":
            return None
        return MexcLiveInputs(state, rules, Decimal("100000"), NOW)

    with pytest.raises(MexcOrderSendError, match="observaciones nuevas") as error:
        submit_spot_limit_once(
            vault, trade, FakeView(), policy, intent, state, rules,
            last_price=Decimal("100000"), last_price_at=NOW,
            refresh_inputs=refresh, clock=lambda: NOW + timedelta(seconds=1),
        )
    assert SECRET not in str(error.value)
    assert posts == ["/api/v3/order/test"]
    with vault.session() as db:
        assert db.connection().exec_driver_sql(
            "SELECT status FROM real_live_spot_intents WHERE id=?", (intent.intent_id,),
        ).scalar_one() == "RESERVED"
    vault.lock()


@pytest.mark.parametrize("change", ("not_reconciled", "insufficient_balance", "side_disabled", "price_bound"))
def test_final_gate_uses_changed_account_rules_and_price(tmp_path, change):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSWORD, create=True)
    policy, intent, state, rules = case()
    register_spot_policy(vault, policy, now=NOW)
    trade, _gets, posts = fake_client(intent)
    fresh = fresh_inputs(state, rules)
    if change == "not_reconciled":
        fresh = replace(fresh, state=replace(fresh.state, reconciled=False))
    elif change == "insufficient_balance":
        fresh = replace(fresh, state=replace(fresh.state, free_quote=Decimal("10")))
    elif change == "side_disabled":
        fresh = replace(fresh, rules=replace(fresh.rules, buy_allowed=False))
    else:
        fresh = replace(fresh, rules=replace(fresh.rules, bid_multiplier_up=Decimal("0.01")),
                        last_price=Decimal("90000"))
    error_type = LiveJournalError if change in ("not_reconciled", "insufficient_balance") else MexcOrderSendError
    with pytest.raises(error_type):
        submit_spot_limit_once(
            vault, trade, FakeView(), policy, intent, state, rules,
            last_price=Decimal("100000"), last_price_at=NOW,
            refresh_inputs=lambda: fresh, clock=lambda: NOW + timedelta(seconds=1),
        )
    assert posts == ["/api/v3/order/test"]
    with vault.session() as db:
        assert db.connection().exec_driver_sql(
            "SELECT status FROM real_live_spot_intents WHERE id=?", (intent.intent_id,),
        ).scalar_one() == "RESERVED"
    vault.lock()


def test_cancel_transport_targets_only_its_own_client_order_id():
    _policy, intent, _state, _rules = case()
    trade, _gets, posts = fake_client(intent)
    deletes = []

    def delete(url, headers):
        parsed = urlparse(url)
        assert parsed.scheme == "https" and parsed.netloc == "api.mexc.com"
        assert parsed.path == "/api/v3/order"
        unsigned, signature = parsed.query.rsplit("&signature=", 1)
        assert signature == hmac.new(SECRET.encode(), unsigned.encode(), hashlib.sha256).hexdigest()
        params = parse_qs(unsigned)
        assert params["symbol"] == [intent.symbol]
        assert params["origClientOrderId"] == [intent.intent_id.replace("-", "")]
        assert "orderId" not in params
        assert headers == {"X-MEXC-APIKEY": KEY}
        deletes.append(parsed.path)
        return {"symbol": intent.symbol, "origClientOrderId": params["origClientOrderId"][0],
                "orderId": "external-order-1", "status": "PARTIALLY_CANCELED",
                "side": intent.side, "type": "LIMIT", "price": "100000", "origQty": "0.001"}

    trade._http_delete = delete
    with pytest.raises(MexcOrderSendError, match="ya no está verificada"):
        trade.cancel_limit_order(intent)
    trade.pair_view_account("123456789")
    result = trade.cancel_limit_order(intent)
    assert result.external_status == "PARTIALLY_CANCELED"
    assert result.external_order_id == "external-order-1"
    assert deletes == ["/api/v3/order"] and posts == []


def test_uncertain_or_mismatched_cancel_response_needs_observation():
    _policy, intent, _state, _rules = case()
    trade, _gets, _posts = fake_client(intent)
    trade.pair_view_account("123456789")

    def uncertain(_url, _headers):
        raise RuntimeError("fixture-trade-secret")

    trade._http_delete = uncertain
    with pytest.raises(MexcOrderSendError, match="incierto") as error:
        trade.cancel_limit_order(intent)
    assert SECRET not in str(error.value)
    trade._http_delete = lambda _url, _headers: {
        "symbol": intent.symbol, "origClientOrderId": "another-order",
        "orderId": "external-order-1", "status": "CANCELED",
        "side": intent.side, "type": "LIMIT", "price": "100000", "origQty": "0.001",
    }
    with pytest.raises(MexcOrderSendError, match="no verificable"):
        trade.cancel_limit_order(intent)


def _archive_open_order(vault, intent, *, observed_at=NOW + timedelta(seconds=2),
                        external_order_id="external-order-1"):
    with vault.session() as db:
        db.add(RealLiveOrderObservation(
            id=str(uuid4()), intent_id=intent.intent_id, account_ref=intent.account_ref,
            symbol=intent.symbol, external_order_id=external_order_id,
            status="NEW", executed_base=Decimal(0), cumulative_quote=Decimal(0),
            fee_json="[]", fill_count=0, payload_digest="b" * 64,
            observed_at=observed_at,
        ))
        db.commit()


def _cancel_response(intent):
    return {"symbol": intent.symbol, "origClientOrderId": intent.intent_id.replace("-", ""),
            "orderId": "external-order-1", "status": "CANCELED",
            "side": intent.side, "type": "LIMIT", "price": "100000", "origQty": "0.001"}


class FakeStoppedView:
    def __init__(self, *, account_ref=ACCOUNT, status="CANCELED", fails=False,
                 uid="123456789"):
        self.account_ref = account_ref
        self.status = status
        self.fails = fails
        self.uid = uid
        self.calls = []

    def account_reference(self):
        return self.account_ref

    def account_uid(self):
        return self.uid

    def order_by_client_id(self, symbol, client_order_id):
        self.calls.append(("order", symbol, client_order_id))
        if self.fails:
            raise MexcReadOnlyError("fixture View timeout")
        return {"source": "MEXC_SPOT_PRIVATE_READONLY",
                "accounting": "OBSERVED_NOT_IMPORTED", "type": "LIMIT",
                "symbol": symbol, "client_order_id": client_order_id,
                "side": "BUY", "price": "100000", "quantity": "0.001",
                "order_id": "external-order-1", "status": self.status,
                "executed_quantity": "0", "cumulative_quote": "0"}

    def trades_for_order(self, symbol, order_id):
        self.calls.append(("fills", symbol, order_id))
        return {"source": "MEXC_SPOT_PRIVATE_READONLY",
                "accounting": "OBSERVED_NOT_IMPORTED",
                "coverage": "ORDER_ID_BELOW_PAGE_LIMIT",
                "symbol": symbol, "order_id": order_id, "trades": []}


def test_stopped_intent_can_be_observed_with_view_without_cancel_or_release(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSWORD, create=True)
    policy, intent, state, rules = case()
    register_spot_policy(vault, policy, now=NOW)
    trade, _gets, posts = fake_client(intent)
    submit(vault, trade, policy, intent, state, rules)
    stop_spot_account(vault, ACCOUNT, now=NOW + timedelta(seconds=2))
    wrong = FakeStoppedView(account_ref="b" * 64)
    with pytest.raises(MexcOrderObservationError, match="no pertenece"):
        observe_stopped_spot_limit(vault, wrong, intent.intent_id,
                                   clock=lambda: NOW + timedelta(seconds=3))
    assert wrong.calls == []
    changed_uid = FakeStoppedView(uid="987654321")
    with pytest.raises(MexcOrderObservationError, match="UID MEXC"):
        observe_stopped_spot_limit(vault, changed_uid, intent.intent_id,
                                   clock=lambda: NOW + timedelta(seconds=3))
    assert changed_uid.calls == []
    unavailable = FakeStoppedView(fails=True)
    with pytest.raises(MexcOrderObservationError, match="no verificable"):
        observe_stopped_spot_limit(vault, unavailable, intent.intent_id,
                                   clock=lambda: NOW + timedelta(seconds=3))
    class LeakyReference(FakeStoppedView):
        def account_reference(self):
            raise RuntimeError("private upstream fixture-secret")

    class LeakyUid(FakeStoppedView):
        def account_uid(self):
            raise RuntimeError("private upstream fixture-secret")

    class LeakyOrder(FakeStoppedView):
        def order_by_client_id(self, symbol, client_order_id):
            raise RuntimeError("private upstream fixture-secret")

    class LeakyFills(FakeStoppedView):
        def trades_for_order(self, symbol, order_id):
            raise RuntimeError("private upstream fixture-secret")

    class MalformedOrder(FakeStoppedView):
        def order_by_client_id(self, symbol, client_order_id):
            return {"status": "FILLED"}

    class MalformedFills(FakeStoppedView):
        def trades_for_order(self, symbol, order_id):
            return {"trades": [{"side": "BUY"}]}

    class NonFiniteFills(FakeStoppedView):
        def trades_for_order(self, symbol, order_id):
            return {"trades": [{"side": "BUY", "quantity": "NaN",
                                "quote_quantity": "0", "commission_asset": "USDT",
                                "commission": "0"}]}

    for source in (LeakyReference(), LeakyUid(), LeakyOrder(), LeakyFills()):
        with pytest.raises(MexcOrderObservationError) as error:
            observe_stopped_spot_limit(vault, source, intent.intent_id,
                                       clock=lambda: NOW + timedelta(seconds=3))
        assert "fixture-secret" not in str(error.value)
    for source in (MalformedOrder(), MalformedFills(), NonFiniteFills()):
        with pytest.raises(MexcOrderObservationError, match="no verificable"):
            observe_stopped_spot_limit(vault, source, intent.intent_id,
                                       clock=lambda: NOW + timedelta(seconds=3))
    with vault.session() as db:
        assert db.query(RealLiveOrderObservation).count() == 0
    view = FakeStoppedView(status="NEW")
    step = observe_stopped_spot_limit(vault, view, intent.intent_id,
                                      clock=lambda: NOW + timedelta(seconds=3))
    assert step.status == "OPEN_ARCHIVED_NEEDS_RECHECK"
    assert step.external_status == "NEW" and step.new_snapshot is True
    assert step.unresolved_intents == 1
    assert view.calls == [("order", intent.symbol, intent.intent_id.replace("-", "")),
                          ("fills", intent.symbol, "external-order-1")]
    with vault.session() as db:
        row = db.connection().exec_driver_sql(
            "SELECT status,reserved_quote FROM real_live_spot_intents WHERE id=?",
            (intent.intent_id,),
        ).one()
        assert row == ("SUBMISSION_UNKNOWN", "100.100")
        assert db.connection().exec_driver_sql(
            "SELECT COUNT(*) FROM real_live_order_observations WHERE intent_id=?",
            (intent.intent_id,),
        ).scalar_one() == 1
    assert posts == ["/api/v3/order/test", "/api/v3/order"]


def test_stopped_observation_rejects_view_rotation_between_order_and_archive(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSWORD, create=True)
    policy, intent, state, rules = case()
    register_spot_policy(vault, policy, now=NOW)
    trade, _gets, _posts = fake_client(intent)
    submit(vault, trade, policy, intent, state, rules)
    stop_spot_account(vault, ACCOUNT, now=NOW + timedelta(seconds=2))

    class RotatingView(FakeStoppedView):
        def __init__(self):
            super().__init__(status="NEW")
            self.uid_checks = 0

        def account_uid(self):
            self.uid_checks += 1
            return "123456789" if self.uid_checks == 1 else "987654321"

    view = RotatingView()
    with pytest.raises(MexcOrderObservationError, match="identidad"):
        observe_stopped_spot_limit(vault, view, intent.intent_id,
                                  clock=lambda: NOW + timedelta(seconds=3))
    assert view.uid_checks >= 2
    assert [name for name, *_rest in view.calls] == ["order", "fills"]
    with vault.session() as db:
        assert db.query(RealLiveOrderObservation).count() == 0
        row = db.get(RealLiveSpotIntent, intent.intent_id)
        assert row.status == "SUBMISSION_UNKNOWN"
    vault.lock()


def test_stopped_supervision_refreshes_unchanged_open_order_before_single_cancel(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSWORD, create=True)
    policy, intent, state, rules = case()
    register_spot_policy(vault, policy, now=NOW)
    trade, _gets, _posts = fake_client(intent)
    submit(vault, trade, policy, intent, state, rules)
    stop_spot_account(vault, ACCOUNT, now=NOW + timedelta(seconds=2))
    wrong = FakeStoppedView(account_ref="b" * 64)
    with pytest.raises(MexcOrderObservationError, match="no pertenece"):
        observe_next_stopped_spot_limit_once(
            vault, wrong, ACCOUNT, clock=lambda: NOW + timedelta(seconds=3))
    assert wrong.calls == []
    view = FakeStoppedView(status="NEW")
    first = observe_next_stopped_spot_limit_once(
        vault, view, ACCOUNT, clock=lambda: NOW + timedelta(seconds=3))
    assert first.status == "OPEN_ARCHIVED_NEEDS_RECHECK" and first.new_snapshot is True
    due = observe_next_stopped_spot_limit_once(
        vault, view, ACCOUNT, clock=lambda: NOW + timedelta(seconds=4))
    assert due.status == "NO_OBSERVATION_DUE" and due.unresolved_intents == 1
    assert len(view.calls) == 2
    refreshed = observe_next_stopped_spot_limit_once(
        vault, view, ACCOUNT, clock=lambda: NOW + timedelta(seconds=35))
    assert refreshed.status == "OPEN_ARCHIVED_NEEDS_RECHECK"
    assert refreshed.new_snapshot is False
    assert len(view.calls) == 4
    with vault.session() as db:
        observation = db.query(RealLiveOrderObservation).one()
        assert observation.observed_at == NOW + timedelta(seconds=35)
    cancels = []
    trade._http_delete = lambda url, _headers: (cancels.append(url) or _cancel_response(intent))
    result = cancel_next_stopped_spot_limit_once(
        vault, trade, view, ACCOUNT, clock=lambda: NOW + timedelta(seconds=36))
    assert result.status == "CANCEL_ACKNOWLEDGED_REOBSERVE"
    assert len(cancels) == 1
    assert cancel_next_stopped_spot_limit_once(
        vault, trade, view, ACCOUNT, clock=lambda: NOW + timedelta(seconds=37)
    ).status == "NO_FRESH_OWN_OPEN_ORDER"
    assert len(cancels) == 1
    vault.lock()


def test_stopped_supervision_cycle_cancels_once_then_only_reobserves(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSWORD, create=True)
    policy, intent, state, rules = case()
    register_spot_policy(vault, policy, now=NOW)
    trade, _gets, _posts = fake_client(intent)
    submit(vault, trade, policy, intent, state, rules)
    stop_spot_account(vault, ACCOUNT, now=NOW + timedelta(seconds=2))
    calls = []
    trade._http_delete = lambda url, _headers: (calls.append(url) or _cancel_response(intent))
    unavailable = FakeStoppedView(fails=True)
    with pytest.raises(MexcOrderObservationError, match="no verificable"):
        supervise_stopped_spot_account_once(
            vault, trade, unavailable, ACCOUNT, clock=lambda: NOW + timedelta(seconds=3))
    assert calls == []
    view = FakeStoppedView(status="NEW")
    first = supervise_stopped_spot_account_once(
        vault, trade, view, ACCOUNT, clock=lambda: NOW + timedelta(seconds=3))
    assert first.status == "CANCEL_ACKNOWLEDGED_REOBSERVE"
    assert first.intent_id == intent.intent_id and len(calls) == 1
    second = supervise_stopped_spot_account_once(
        vault, trade, view, ACCOUNT, clock=lambda: NOW + timedelta(seconds=4))
    assert second.status == "STILL_OPEN_RECHECK" and len(calls) == 1
    view.status = "CANCELED"
    final = supervise_stopped_spot_account_once(
        vault, trade, view, ACCOUNT, clock=lambda: NOW + timedelta(seconds=5))
    assert final.status == "FINAL_ARCHIVED_NEEDS_SETTLEMENT"
    assert final.external_status == "CANCELED" and len(calls) == 1
    assert list_spot_execution_intents(vault)[0]["stage"] == "FINAL_NO_EXECUTION"
    with vault.session() as db:
        row = db.connection().exec_driver_sql(
            "SELECT status, reserved_quote FROM real_live_spot_intents WHERE id=?",
            (intent.intent_id,),
        ).one()
        assert row == ("SUBMISSION_UNKNOWN", "100.100")
    vault.lock()


def test_real_access_runs_stopped_supervisor_with_fake_mexc_transport(tmp_path):
    """API → trabajador → View → DELETE único → View final, sin red externa."""
    class ConnectedView(FakeStoppedView):
        def balances(self):
            return {"source": "fixture", "balances": []}

    class Probe:
        def __init__(self, _key, _secret):
            pass

        def account_uid(self):
            return "123456789"

    policy, intent, state, rules = case()
    view = ConnectedView(status="NEW")
    trade, gets, posts = fake_client(intent)
    deletes = []
    trade._http_delete = lambda url, _headers: (deletes.append(url) or _cancel_response(intent))
    access = RealAccess(
        tmp_path / "real.db",
        mexc_factory=lambda _key, _secret: view,
        mexc_trade_factory=Probe,
        mexc_live_factory=lambda _key, _secret: trade,
        mexc_stop_supervisor_factory=lambda vault, live, reader, account_ref:
            MexcStopSupervisor(vault, live, reader, account_ref, interval_seconds=0.25),
    )
    token = access.unlock(PASSWORD, create=True)
    register_spot_policy(access.vault, policy, now=NOW)
    submit(access.vault, trade, policy, intent, state, rules)
    stop_spot_account(access.vault, ACCOUNT, now=NOW + timedelta(seconds=2))
    posts_before = list(posts)
    access.connect_mexc(token, "fixture-view-key", "fixture-view-secret")
    access.connect_mexc_trade(token, KEY, SECRET)
    started = access.start_mexc_stop_supervisor(token, intent.intent_id)
    assert started["new_orders_enabled"] is False
    deadline = time.monotonic() + 3
    while len(deletes) < 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(deletes) == 1
    assert posts == posts_before
    with access.vault.session() as db:
        row = db.connection().exec_driver_sql(
            "SELECT status,reason,reserved_quote FROM real_live_spot_intents WHERE id=?",
            (intent.intent_id,),
        ).one()
        assert row == ("SUBMISSION_UNKNOWN", "CANCEL_ACKNOWLEDGED_REOBSERVE", "100.100")
    view.status = "CANCELED"
    while time.monotonic() < deadline:
        if access.mexc_stop_supervisor_status(token)["status"] == "FINAL_ARCHIVED_NEEDS_SETTLEMENT":
            break
        time.sleep(0.02)
    assert access.mexc_stop_supervisor_status(token)["status"] == "FINAL_ARCHIVED_NEEDS_SETTLEMENT"
    assert len(deletes) == 1 and posts == posts_before
    assert list_spot_execution_intents(access.vault)[0]["stage"] == "FINAL_NO_EXECUTION"
    access.stop_mexc_stop_supervisor(token)
    access.lock()


def test_stopped_reobservation_rotates_between_pending_cancel_outcomes(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSWORD, create=True)
    policy, first, state, rules = case()
    state = replace(state, free_quote=Decimal("400"))
    second = replace(first, intent_id=str(uuid4()))
    register_spot_policy(vault, policy, now=NOW)
    first_trade, _gets, _posts = fake_client(first)
    second_trade, _gets2, _posts2 = fake_client(second)
    submit(vault, first_trade, policy, first, state, rules)
    submit(vault, second_trade, policy, second, state, rules)
    _archive_open_order(vault, first, observed_at=NOW + timedelta(seconds=2),
                        external_order_id="external-order-1")
    _archive_open_order(vault, second, observed_at=NOW + timedelta(seconds=3),
                        external_order_id="external-order-2")
    stop_spot_account(vault, ACCOUNT, now=NOW + timedelta(seconds=3))
    with vault.session() as db:
        db.connection().exec_driver_sql(
            "UPDATE real_live_spot_intents SET reason='CANCEL_ACKNOWLEDGED_REOBSERVE' "
            "WHERE id IN (?, ?)", (first.intent_id, second.intent_id),
        )
        db.commit()

    class DistinctView(FakeStoppedView):
        def order_by_client_id(self, symbol, client_order_id):
            row = super().order_by_client_id(symbol, client_order_id)
            row["order_id"] = ("external-order-1" if client_order_id == first.intent_id.replace("-", "")
                               else "external-order-2")
            return row

    view = DistinctView(status="NEW")
    first_step = reobserve_next_stopped_spot_limit_once(
        vault, view, ACCOUNT, clock=lambda: NOW + timedelta(seconds=4))
    second_step = reobserve_next_stopped_spot_limit_once(
        vault, view, ACCOUNT, clock=lambda: NOW + timedelta(seconds=5))
    assert first_step.intent_id == first.intent_id
    assert second_step.intent_id == second.intent_id
    assert first_step.unresolved_intents == second_step.unresolved_intents == 2
    vault.lock()
    vault.lock()


def test_stopped_open_order_is_canceled_once_and_remains_reserved(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSWORD, create=True)
    policy, intent, state, rules = case()
    register_spot_policy(vault, policy, now=NOW)
    trade, _gets, _posts = fake_client(intent)
    submit(vault, trade, policy, intent, state, rules)
    _archive_open_order(vault, intent)
    calls = []

    def delete(url, _headers):
        calls.append(url)
        return _cancel_response(intent)

    trade._http_delete = delete
    with pytest.raises(MexcOrderSendError, match="parada"):
        cancel_stopped_spot_limit_once(
            vault, trade, FakeView(), intent, clock=lambda: NOW + timedelta(seconds=3),
        )
    assert calls == []
    stop_spot_account(vault, ACCOUNT, now=NOW + timedelta(seconds=2))
    vault.lock()
    vault.unlock(PASSWORD)
    restored_intent = recover_spot_intent(vault, intent.intent_id)
    assert restored_intent == intent
    ack = cancel_stopped_spot_limit_once(
        vault, trade, FakeView(), restored_intent, clock=lambda: NOW + timedelta(seconds=3),
    )
    assert ack.external_status == "CANCELED"
    assert len(calls) == 1
    with vault.session() as db:
        assert db.connection().exec_driver_sql(
            "SELECT reason, reserved_quote FROM real_live_spot_intents WHERE id=?",
            (intent.intent_id,),
        ).one() == ("CANCEL_ACKNOWLEDGED_REOBSERVE", "100.100")
    with pytest.raises(MexcOrderSendError, match="no autoriza"):
        cancel_stopped_spot_limit_once(
            vault, trade, FakeView(), intent, clock=lambda: NOW + timedelta(seconds=4),
        )
    assert len(calls) == 1
    vault.lock()


def test_stopped_cancel_rejects_foreign_view_and_changed_uid_before_claim(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSWORD, create=True)
    policy, intent, state, rules = case()
    register_spot_policy(vault, policy, now=NOW)
    trade, _gets, _posts = fake_client(intent)
    submit(vault, trade, policy, intent, state, rules)
    _archive_open_order(vault, intent)
    stop_spot_account(vault, ACCOUNT, now=NOW + timedelta(seconds=2))
    deletes = []
    trade._http_delete = lambda _url, _headers: deletes.append("DELETE")

    with pytest.raises(MexcOrderSendError, match="clave View"):
        cancel_next_stopped_spot_limit_once(
            vault, trade, FakeView(account_ref="b" * 64), ACCOUNT,
            clock=lambda: NOW + timedelta(seconds=3))
    changed_trade, _gets, _posts = fake_client(intent, uid="987654321")
    changed_trade._http_delete = lambda _url, _headers: deletes.append("DELETE")
    with pytest.raises(MexcOrderSendError, match="no coincide con el envío archivado"):
        cancel_stopped_spot_limit_once(
            vault, changed_trade, FakeView(uid="987654321"), intent,
            clock=lambda: NOW + timedelta(seconds=3))
    assert deletes == []
    with vault.session() as db:
        assert db.connection().exec_driver_sql(
            "SELECT reason FROM real_live_spot_intents WHERE id=?", (intent.intent_id,)
        ).scalar_one() == "POST_ACKNOWLEDGED"
    vault.lock()


def test_stopped_cancel_coordinator_never_uses_stale_order_or_retries(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSWORD, create=True)
    policy, intent, state, rules = case()
    register_spot_policy(vault, policy, now=NOW)
    trade, _gets, _posts = fake_client(intent)
    submit(vault, trade, policy, intent, state, rules)
    _archive_open_order(vault, intent)
    stop_spot_account(vault, ACCOUNT, now=NOW + timedelta(seconds=2))
    calls = []

    def delete(url, _headers):
        calls.append(url)
        return _cancel_response(intent)

    trade._http_delete = delete
    stale = cancel_next_stopped_spot_limit_once(
        vault, trade, FakeView(), ACCOUNT,
        clock=lambda: NOW + timedelta(seconds=40))
    assert stale.status == "NO_FRESH_OWN_OPEN_ORDER"
    assert stale.unresolved_intents == 1 and calls == []
    vault.lock()
    vault.unlock(PASSWORD)
    step = cancel_next_stopped_spot_limit_once(
        vault, trade, FakeView(), ACCOUNT,
        clock=lambda: NOW + timedelta(seconds=3))
    assert step.status == "CANCEL_ACKNOWLEDGED_REOBSERVE"
    assert step.intent_id == intent.intent_id and step.external_status == "CANCELED"
    assert len(calls) == 1
    again = cancel_next_stopped_spot_limit_once(
        vault, trade, FakeView(), ACCOUNT,
        clock=lambda: NOW + timedelta(seconds=4))
    assert again.status == "NO_FRESH_OWN_OPEN_ORDER"
    assert len(calls) == 1
    with vault.session() as db:
        row = db.connection().exec_driver_sql(
            "SELECT status,reason,reserved_quote FROM real_live_spot_intents WHERE id=?",
            (intent.intent_id,),
        ).one()
    assert row == ("SUBMISSION_UNKNOWN", "CANCEL_ACKNOWLEDGED_REOBSERVE", "100.100")
    vault.lock()


def test_stopped_cancel_reobserves_final_without_freeing_reservation(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSWORD, create=True)
    policy, intent, state, rules = case()
    register_spot_policy(vault, policy, now=NOW)
    trade, _gets, _posts = fake_client(intent)
    submit(vault, trade, policy, intent, state, rules)
    _archive_open_order(vault, intent)
    stop_spot_account(vault, ACCOUNT, now=NOW + timedelta(seconds=2))
    trade._http_delete = lambda _url, _headers: _cancel_response(intent)
    cancel_next_stopped_spot_limit_once(
        vault, trade, FakeView(), ACCOUNT, clock=lambda: NOW + timedelta(seconds=3))
    assert list_spot_execution_intents(vault)[0]["cancel_state"] == "ACKNOWLEDGED_NEEDS_REOBSERVATION"
    vault.lock()
    vault.unlock(PASSWORD)

    wrong = FakeStoppedView(account_ref="b" * 64)
    with pytest.raises(MexcOrderObservationError, match="no pertenece"):
        reobserve_next_stopped_spot_limit_once(
            vault, wrong, ACCOUNT, clock=lambda: NOW + timedelta(seconds=4))
    assert wrong.calls == []
    changed_uid = FakeStoppedView(uid="987654321")
    with pytest.raises(MexcOrderObservationError, match="UID MEXC"):
        reobserve_next_stopped_spot_limit_once(
            vault, changed_uid, ACCOUNT, clock=lambda: NOW + timedelta(seconds=4))
    assert changed_uid.calls == []
    unavailable = FakeStoppedView(fails=True)
    with pytest.raises(MexcOrderObservationError, match="no verificable"):
        reobserve_next_stopped_spot_limit_once(
            vault, unavailable, ACCOUNT, clock=lambda: NOW + timedelta(seconds=4))
    assert len(unavailable.calls) == 1
    assert list_spot_execution_intents(vault)[0]["cancel_state"] == "ACKNOWLEDGED_NEEDS_REOBSERVATION"
    with vault.session() as db:
        assert db.connection().exec_driver_sql(
            "SELECT count(*) FROM real_live_order_observations WHERE intent_id=?",
            (intent.intent_id,),
        ).scalar_one() == 1

    view = FakeStoppedView()
    step = reobserve_next_stopped_spot_limit_once(
        vault, view, ACCOUNT, clock=lambda: NOW + timedelta(seconds=4))
    assert step.status == "FINAL_ARCHIVED_NEEDS_SETTLEMENT"
    assert step.intent_id == intent.intent_id and step.external_status == "CANCELED"
    assert step.new_snapshot is True and step.unresolved_intents == 1
    assert list_spot_execution_intents(vault)[0]["cancel_state"] == "FINAL_OBSERVED_AFTER_CANCEL"
    assert view.calls == [("order", intent.symbol, intent.intent_id.replace("-", "")),
                          ("fills", intent.symbol, "external-order-1")]
    again = reobserve_next_stopped_spot_limit_once(
        vault, view, ACCOUNT, clock=lambda: NOW + timedelta(seconds=5))
    assert again.status == "NO_CANCEL_OUTCOME_TO_REOBSERVE" and again.unresolved_intents == 1
    assert len(view.calls) == 2
    with vault.session() as db:
        assert db.connection().exec_driver_sql(
            "SELECT status,reason,reserved_quote FROM real_live_spot_intents WHERE id=?",
            (intent.intent_id,),
        ).one() == ("SUBMISSION_UNKNOWN", "CANCEL_ACKNOWLEDGED_REOBSERVE", "100.100")
        assert db.connection().exec_driver_sql(
            "SELECT status FROM real_live_order_observations WHERE intent_id=? ORDER BY observed_at DESC LIMIT 1",
            (intent.intent_id,),
        ).scalar_one() == "CANCELED"
    vault.lock()


def test_stopped_cancel_reobservation_rejects_view_rotation(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSWORD, create=True)
    policy, intent, state, rules = case()
    register_spot_policy(vault, policy, now=NOW)
    trade, _gets, _posts = fake_client(intent)
    submit(vault, trade, policy, intent, state, rules)
    _archive_open_order(vault, intent)
    stop_spot_account(vault, ACCOUNT, now=NOW + timedelta(seconds=2))
    trade._http_delete = lambda _url, _headers: _cancel_response(intent)
    cancel_next_stopped_spot_limit_once(
        vault, trade, FakeView(), ACCOUNT, clock=lambda: NOW + timedelta(seconds=3))

    class RotatingView(FakeStoppedView):
        def __init__(self):
            super().__init__(status="CANCELED")
            self.uid_checks = 0

        def account_uid(self):
            self.uid_checks += 1
            return "123456789" if self.uid_checks == 1 else "987654321"

    view = RotatingView()
    with pytest.raises(MexcOrderObservationError, match="identidad"):
        reobserve_next_stopped_spot_limit_once(
            vault, view, ACCOUNT, clock=lambda: NOW + timedelta(seconds=4))
    assert view.uid_checks >= 2
    with vault.session() as db:
        assert db.query(RealLiveOrderObservation).count() == 1
        row = db.get(RealLiveSpotIntent, intent.intent_id)
        assert row.reason == "CANCEL_ACKNOWLEDGED_REOBSERVE"
    vault.lock()


def test_stopped_cancel_reobservation_can_remain_open_then_finish(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSWORD, create=True)
    policy, intent, state, rules = case()
    register_spot_policy(vault, policy, now=NOW)
    trade, _gets, _posts = fake_client(intent)
    submit(vault, trade, policy, intent, state, rules)
    _archive_open_order(vault, intent)
    stop_spot_account(vault, ACCOUNT, now=NOW + timedelta(seconds=2))
    deletes = []

    def delete(_url, _headers):
        deletes.append(intent.intent_id)
        return _cancel_response(intent)

    trade._http_delete = delete
    cancel_next_stopped_spot_limit_once(
        vault, trade, FakeView(), ACCOUNT, clock=lambda: NOW + timedelta(seconds=3))
    view = FakeStoppedView(status="NEW")
    pending = reobserve_next_stopped_spot_limit_once(
        vault, view, ACCOUNT, clock=lambda: NOW + timedelta(seconds=4))
    assert pending.status == "STILL_OPEN_RECHECK" and pending.external_status == "NEW"
    assert list_spot_execution_intents(vault)[0]["cancel_state"] == "OPEN_OBSERVED_AFTER_CANCEL"
    with vault.session() as db:
        assert db.connection().exec_driver_sql(
            "SELECT reserved_quote FROM real_live_spot_intents WHERE id=?", (intent.intent_id,),
        ).scalar_one() == "100.100"
    view.status = "CANCELED"
    final = reobserve_next_stopped_spot_limit_once(
        vault, view, ACCOUNT, clock=lambda: NOW + timedelta(seconds=5))
    assert final.status == "FINAL_ARCHIVED_NEEDS_SETTLEMENT"
    assert list_spot_execution_intents(vault)[0]["cancel_state"] == "FINAL_OBSERVED_AFTER_CANCEL"
    assert deletes == [intent.intent_id]
    assert cancel_next_stopped_spot_limit_once(
        vault, trade, FakeView(), ACCOUNT,
        clock=lambda: NOW + timedelta(seconds=6)).status == "NO_FRESH_OWN_OPEN_ORDER"
    vault.lock()


def test_failed_cancel_is_not_repeated_and_open_observation_must_be_fresh(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSWORD, create=True)
    policy, intent, state, rules = case()
    register_spot_policy(vault, policy, now=NOW)
    trade, _gets, _posts = fake_client(intent)
    submit(vault, trade, policy, intent, state, rules)
    _archive_open_order(vault, intent)
    stop_spot_account(vault, ACCOUNT, now=NOW + timedelta(seconds=2))
    calls = []

    def fail_delete(url, _headers):
        calls.append(url)
        raise RuntimeError("fixture-trade-secret")

    trade._http_delete = fail_delete
    with pytest.raises(MexcOrderSendError, match="observación reciente"):
        cancel_stopped_spot_limit_once(
            vault, trade, FakeView(), intent, clock=lambda: NOW + timedelta(seconds=40),
        )
    assert calls == []
    with pytest.raises(MexcOrderSendError, match="incierto") as error:
        cancel_stopped_spot_limit_once(
            vault, trade, FakeView(), intent, clock=lambda: NOW + timedelta(seconds=3),
        )
    assert SECRET not in str(error.value)
    assert list_spot_execution_intents(vault)[0]["cancel_state"] == "ATTEMPT_OUTCOME_UNKNOWN"
    with vault.session() as db:
        assert db.connection().exec_driver_sql(
            "SELECT reason, reserved_quote FROM real_live_spot_intents WHERE id=?",
            (intent.intent_id,),
        ).one() == ("CANCEL_ATTEMPTED_OUTCOME_UNKNOWN", "100.100")
    with pytest.raises(MexcOrderSendError, match="no autoriza"):
        cancel_stopped_spot_limit_once(
            vault, trade, FakeView(), intent, clock=lambda: NOW + timedelta(seconds=4),
        )
    assert len(calls) == 1
    vault.lock()
