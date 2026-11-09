"""Activación explícita de vigilancia de parada, sin transporte MEXC real."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import hmac
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.live_execution.journal import (
    begin_spot_submission, register_spot_policy, reserve_spot_intent,
    stop_spot_account,
)
from app.live_execution.mexc_stop_supervisor import MexcStopSupervisorState
from app.live_execution.mexc_uid_guard import bind_uid_for_spot_submission
from app.live_execution.policy import LiveSpotIntent, LiveSpotPolicy, LiveSpotState
from app.portfolio.api import RealAccess, create_real_router


VIEW_KEY = "fixture-view-key"
VIEW_SECRET = "fixture-view-secret"
TRADE_KEY = "fixture-trade-key"
TRADE_SECRET = "fixture-trade-secret"
UID = "123456789"
ACCOUNT = hmac.new(VIEW_SECRET.encode(), VIEW_KEY.encode(), hashlib.sha256).hexdigest()
PASSWORD = "isolated supervisor api password"


class View:
    def __init__(self, key, secret):
        assert (key, secret) == (VIEW_KEY, VIEW_SECRET)

    def balances(self):
        return {"source": "fixture", "balances": []}

    def account_reference(self):
        return ACCOUNT

    def account_uid(self):
        return UID


class Probe:
    def __init__(self, key, secret):
        assert (key, secret) == (TRADE_KEY, TRADE_SECRET)

    def account_uid(self):
        return UID


class Live:
    calls = []

    def __init__(self, key, secret):
        assert (key, secret) == (TRADE_KEY, TRADE_SECRET)
        self.calls.append("created")

    def pair_view_account(self, uid):
        assert uid == UID
        self.calls.append("paired-get-only")


class Worker:
    instances = []

    def __init__(self, vault, trade, view, account_ref):
        assert vault.unlocked and isinstance(trade, Live) and isinstance(view, View)
        assert account_ref == ACCOUNT
        self.running = False
        self.cycles = 0
        self.instances.append(self)

    def start(self):
        self.running = True

    def request_stop(self):
        self.running = False

    def join(self, _timeout):
        return not self.running

    def state(self):
        return MexcStopSupervisorState(self.running, "RUNNING" if self.running else "STOPPED",
                                       self.cycles, None, 1)


def pending_intent(vault, *, account_ref=ACCOUNT, uid=UID):
    now = datetime.now(timezone.utc)
    policy = LiveSpotPolicy(
        policy_id=str(uuid4()), strategy_version_id="v1", account_ref=account_ref,
        symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT",
        max_order_quote=Decimal("150"), max_daily_turnover_quote=Decimal("500"),
        max_exposure_quote=Decimal("1000"), max_daily_loss_quote=Decimal("50"),
        max_fee_bps=Decimal("20"), max_slippage_bps=Decimal("50"),
        max_data_age_seconds=60, expires_at=now + timedelta(hours=1), enabled=True,
    )
    intent = LiveSpotIntent(
        intent_id=str(uuid4()), policy_id=policy.policy_id,
        strategy_version_id="v1", account_ref=account_ref, symbol="BTCUSDT",
        side="BUY", quantity_base=Decimal("0.001"),
        limit_price_quote=Decimal("100000"), estimated_fee_quote=Decimal("0.1"),
        signal_at=now,
    )
    state = LiveSpotState(
        account_ref=account_ref, symbol="BTCUSDT", reconciled=True, stopped=False,
        account_verified=True, instrument_verified=True, observed_at=now,
        quote_at=now, best_bid=Decimal("99999"), best_ask=Decimal("100000"),
        free_base=Decimal(0), free_quote=Decimal("200"), pending_base=Decimal(0),
        pending_quote=Decimal(0), daily_turnover_quote=Decimal(0),
        pending_turnover_quote=Decimal(0), exposure_quote=Decimal(0),
        daily_realized_pnl_quote=Decimal(0),
    )
    register_spot_policy(vault, policy, now=now)
    reserve_spot_intent(vault, intent, state, now=now)
    begin_spot_submission(vault, intent, state, now=now)
    bind_uid_for_spot_submission(vault, account_ref, uid, now=now)
    stop_spot_account(vault, account_ref, now=now)
    return intent.intent_id


def access_at(tmp_path):
    Worker.instances = []
    Live.calls = []
    access = RealAccess(tmp_path / "real.db", mexc_factory=View,
                        mexc_trade_factory=Probe, mexc_live_factory=Live,
                        mexc_stop_supervisor_factory=Worker)
    token = access.unlock(PASSWORD, create=True)
    return access, token


def connect(access, token):
    access.connect_mexc(token, VIEW_KEY, VIEW_SECRET)
    access.connect_mexc_trade(token, TRADE_KEY, TRADE_SECRET)


def test_supervisor_requires_stopped_intent_and_both_keys(tmp_path):
    access, token = access_at(tmp_path)
    deadline = access._deadline
    access.mexc_stop_supervisor_status(token)
    assert access._deadline == deadline
    intent_id = pending_intent(access.vault)
    try:
        access.start_mexc_stop_supervisor(token, intent_id)
        assert False, "Debe exigir View/Trade"
    except HTTPException as exc:
        assert exc.status_code == 409
    connect(access, token)
    status = access.start_mexc_stop_supervisor(token, intent_id)
    assert status["running"] is True and status["scope"] == "LOCAL_PROCESS_ONLY"
    assert status["new_orders_enabled"] is False
    assert status["may_cancel_own_open_order"] is True
    assert Live.calls == ["created", "paired-get-only"]
    assert len(Worker.instances) == 1
    assert all(secret not in str(status) for secret in (VIEW_SECRET, TRADE_SECRET, UID))
    try:
        access.start_mexc_stop_supervisor(token, intent_id)
        assert False, "No debe arrancar dos trabajadores"
    except HTTPException as exc:
        assert exc.status_code == 409
    assert access.stop_mexc_stop_supervisor(token)["running"] is False
    access.lock()


def test_unmonitored_stopped_intent_survives_restart_without_auto_start(tmp_path):
    access, token = access_at(tmp_path)
    assert access.mexc_stop_supervisor_status(token)["recovery_required"] is False
    pending_intent(access.vault)
    status = access.mexc_stop_supervisor_status(token)
    assert status["running"] is False
    assert status["unmonitored_stopped_accounts"] == 1
    assert status["unmonitored_stopped_intents"] == 1
    assert status["recovery_required"] is True
    access.lock()

    restarted = RealAccess(tmp_path / "real.db", mexc_factory=View,
                           mexc_trade_factory=Probe, mexc_live_factory=Live,
                           mexc_stop_supervisor_factory=Worker)
    new_token = restarted.unlock(PASSWORD)
    status = restarted.mexc_stop_supervisor_status(new_token)
    assert status["status"] == "NOT_STARTED"
    assert status["recovery_required"] is True
    assert status["may_cancel_own_open_order"] is False
    assert restarted._mexc is None and restarted._mexc_trade_credentials is None
    assert Worker.instances == []
    restarted.lock()


def test_active_worker_only_covers_its_own_stopped_account(tmp_path):
    access, token = access_at(tmp_path)
    own_id = pending_intent(access.vault)
    pending_intent(access.vault, account_ref="b" * 64, uid="987654321")
    connect(access, token)
    status = access.start_mexc_stop_supervisor(token, own_id)
    assert status["running"] is True
    assert status["stopped_accounts_with_unresolved"] == 2
    assert status["unmonitored_stopped_accounts"] == 1
    assert status["unmonitored_stopped_intents"] == 1
    assert status["recovery_required"] is True
    access.lock()


def test_stop_requested_is_not_counted_as_future_monitoring(tmp_path):
    access, token = access_at(tmp_path)
    own_id = pending_intent(access.vault)
    connect(access, token)
    active = access.start_mexc_stop_supervisor(token, own_id)
    assert active["monitoring_active"] is True
    assert active["recovery_required"] is False

    worker = Worker.instances[-1]
    worker.request_stop()
    # Un hilo puede seguir dentro de su último ciclo; ya no repetirá la
    # vigilancia, aunque ese ciclo todavía pudiera cancelar una orden.
    class FinishingWorker:
        def state(self):
            return MexcStopSupervisorState(True, "RUNNING", 0, None, 1, True)

    access._mexc_stop_supervisor = FinishingWorker()
    stopping = access.mexc_stop_supervisor_status(token)
    assert stopping["running"] is True
    assert stopping["monitoring_active"] is False
    assert stopping["recovery_required"] is True
    assert stopping["unmonitored_stopped_intents"] == 1
    access._mexc_stop_supervisor = worker
    access.lock()


def test_stalled_worker_is_not_counted_as_monitoring(tmp_path):
    access, token = access_at(tmp_path)
    own_id = pending_intent(access.vault)
    connect(access, token)
    access.start_mexc_stop_supervisor(token, own_id)
    worker = Worker.instances[-1]

    class StalledWorker:
        def state(self):
            return MexcStopSupervisorState(True, "RUNNING", 0, None, 1,
                                           False, True)

    access._mexc_stop_supervisor = StalledWorker()
    status = access.mexc_stop_supervisor_status(token)
    assert status["running"] is True
    assert status["stalled"] is True
    assert status["monitoring_active"] is False
    assert status["recovery_required"] is True
    access._mexc_stop_supervisor = worker
    access.lock()


def test_terminal_worker_status_is_not_counted_as_monitoring_while_thread_is_alive(tmp_path):
    access, token = access_at(tmp_path)
    own_id = pending_intent(access.vault)
    connect(access, token)
    access.start_mexc_stop_supervisor(token, own_id)
    worker = Worker.instances[-1]

    for terminal_status in ("ERROR_NEEDS_REVIEW", "STALLED_NEEDS_REVIEW",
                            "VAULT_LOCKED", "VAULT_SESSION_REPLACED", "STOPPED"):
        class FinishingWorker:
            def state(self):
                return MexcStopSupervisorState(True, terminal_status, 0, None, 1)

        access._mexc_stop_supervisor = FinishingWorker()
        status = access.mexc_stop_supervisor_status(token)
        assert status["running"] is True
        assert status["monitoring_active"] is False
        assert status["recovery_required"] is True
        assert status["unmonitored_stopped_intents"] == 1
        # Un ciclo en curso aún podría haber emitido un DELETE; el riesgo no
        # desaparece hasta que el hilo acabe y se reobserve MEXC.
        assert status["may_cancel_own_open_order"] is True
    access._mexc_stop_supervisor = worker
    access.lock()


def test_disconnect_and_lock_stop_worker_and_clear_trade_session(tmp_path):
    access, token = access_at(tmp_path)
    intent_id = pending_intent(access.vault)
    connect(access, token)
    access.start_mexc_stop_supervisor(token, intent_id)
    worker = Worker.instances[-1]
    access.disconnect_mexc_trade(token)
    assert worker.running is False
    assert access.mexc_trade_status(token)["verified"] is False
    access.connect_mexc_trade(token, TRADE_KEY, TRADE_SECRET)
    access.start_mexc_stop_supervisor(token, intent_id)
    worker = Worker.instances[-1]
    access.lock()
    assert worker.running is False and not access.vault.unlocked


def test_inflight_cycle_blocks_credential_replacement_without_losing_session(tmp_path):
    access, token = access_at(tmp_path)
    connect(access, token)

    class BusyWorker:
        requested = False

        def request_stop(self):
            self.requested = True

        def join(self, _timeout):
            return False

        def state(self):
            return MexcStopSupervisorState(True, "RUNNING", 0, None, 1,
                                           self.requested)

    busy = BusyWorker()
    access._mexc_stop_supervisor = busy
    try:
        access.disconnect_mexc_trade(token)
        assert False, "No debe soltar credenciales mientras sigue el ciclo"
    except HTTPException as exc:
        assert exc.status_code == 409
    assert busy.requested is True
    assert access.mexc_trade_status(token)["verified"] is True
    access._mexc_stop_supervisor = None
    access.lock()


def test_api_requires_confirmation_and_local_token_before_activation(tmp_path):
    app = FastAPI()
    app.include_router(create_real_router(tmp_path / "real.db", mexc_factory=View,
                                          mexc_trade_factory=Probe,
                                          mexc_live_factory=Live,
                                          mexc_stop_supervisor_factory=Worker))
    client = TestClient(app)
    path = f"/real/execution/mexc/intents/{uuid4()}/supervise-stop"
    assert client.post(path, json={"confirmation": "VIGILAR_PARADA"}).status_code == 401
    token = client.post("/real/vault/create", json={"passphrase": PASSWORD}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    assert client.post(path, json={"confirmation": "SI"}, headers=headers).status_code == 422
    assert client.post(path, json={"confirmation": "VIGILAR_PARADA"},
                       headers={**headers, "Origin": "https://evil.example"}).status_code == 403
    status = client.get("/real/execution/mexc/stop-supervisor/status", headers=headers)
    assert status.status_code == 200 and status.headers["cache-control"] == "no-store"
    assert status.json()["running"] is False
    assert client.post("/real/execution/mexc/stop-supervisor/stop",
                       json={"confirmation": "DETENER_VIGILANCIA"}, headers=headers).status_code == 200
