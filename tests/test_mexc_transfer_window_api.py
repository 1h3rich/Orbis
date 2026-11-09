"""Ruta local de diagnóstico MEXC, con View ficticia y bóveda temporal."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import hmac
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.live_execution.journal import register_spot_policy
from app.live_execution.mexc_uid_guard import bind_uid_for_spot_submission
from app.live_execution.policy import LiveSpotPolicy
from app.portfolio import api as real_api


KEY = "isolated-transfer-view-key"
SECRET = "isolated-transfer-view-secret"
ACCOUNT = hmac.new(SECRET.encode(), KEY.encode(), hashlib.sha256).hexdigest()


class View:
    movement = False

    def __init__(self, key, secret):
        self.account_ref = hmac.new(secret.encode(), key.encode(), hashlib.sha256).hexdigest()

    def account_reference(self):
        return self.account_ref

    def account_uid(self):
        return "22334455"

    def balances(self):
        return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "observed_at": datetime.now(timezone.utc).isoformat(), "balances": []}

    def transfers_recent(self, *, days):
        assert days == 1
        now = datetime.now(timezone.utc)
        deposits = [{"direction": "deposit", "asset": "BTC", "network": "BTC",
                     "amount": "0.01", "fee": "0", "status": 5,
                     "time_ms": int((now - timedelta(minutes=1)).timestamp() * 1000)}] if self.movement else []
        return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "coverage": "RECENT_WINDOW_BELOW_PAGE_LIMIT",
                "start_ms": int((now - timedelta(days=1)).timestamp() * 1000),
                "end_ms": int(now.timestamp() * 1000),
                "observed_at": now.isoformat(), "deposits": deposits, "withdrawals": []}


def policy(account_ref=ACCOUNT):
    now = datetime.now(timezone.utc)
    return LiveSpotPolicy(
        policy_id=str(uuid4()), strategy_version_id="v1", account_ref=account_ref,
        symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT",
        max_order_quote=Decimal("150"), max_daily_turnover_quote=Decimal("500"),
        max_exposure_quote=Decimal("1000"), max_daily_loss_quote=Decimal("50"),
        max_fee_bps=Decimal("20"), max_slippage_bps=Decimal("50"),
        max_data_age_seconds=30, expires_at=now + timedelta(hours=1), enabled=True,
    )


def test_protected_transfer_diagnostic_never_enables_real_orders(tmp_path, monkeypatch):
    path = tmp_path / "real.db"
    access = real_api.RealAccess(path, mexc_factory=View)
    monkeypatch.setattr(real_api, "RealAccess", lambda *_args, **_kwargs: access)
    app = FastAPI()
    app.include_router(real_api.create_real_router(path))
    client = TestClient(app)
    current = policy()
    endpoint = f"/real/execution/mexc/policies/{current.policy_id}/transfer-window-audit"
    assert client.post(endpoint, json={"days": 1}).status_code == 401
    token = client.post("/real/vault/create", json={"passphrase": "isolated strong password"}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    assert client.post(endpoint, json={"days": 1}, headers=headers).status_code == 409
    assert client.post("/real/exchanges/mexc/connect", json={"api_key": KEY, "secret_key": SECRET},
                       headers=headers).status_code == 200
    assert client.post(endpoint, json={"days": 1}, headers=headers).status_code == 404
    register_spot_policy(access.vault, current, now=datetime.now(timezone.utc))
    bind_uid_for_spot_submission(access.vault, ACCOUNT, "22334455",
                                 now=datetime.now(timezone.utc))
    response = client.post(endpoint, json={"days": 1}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["clear_for_window"] is True
    assert response.json()["can_submit_real_order"] is False
    assert response.json()["scope"] == "ACCOUNT_TRANSFER_WINDOW_DIAGNOSTIC"
    assert all(secret not in response.text for secret in (ACCOUNT, KEY, SECRET))

    access._mexc.movement = True
    observed = client.post(endpoint, json={"days": 1}, headers=headers)
    assert observed.status_code == 200
    assert observed.json()["clear_for_window"] is False
    assert observed.json()["reason"] == "TRANSFER_ACTIVITY_REQUIRES_RECONCILIATION"
    assert observed.json()["deposit_count"] == 1
    assert observed.json()["can_submit_real_order"] is False

    other = policy("b" * 64)
    register_spot_policy(access.vault, other, now=datetime.now(timezone.utc))
    assert client.post(
        f"/real/execution/mexc/policies/{other.policy_id}/transfer-window-audit",
        json={"days": 1}, headers=headers,
    ).status_code == 409
    assert client.post(endpoint, json={"days": 2}, headers=headers).status_code == 422
    client.post("/real/vault/lock", headers=headers)
    assert client.post(endpoint, json={"days": 1}, headers=headers).status_code == 401
