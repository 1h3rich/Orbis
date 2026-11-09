"""Borradores REAL MEXC: límites persistentes, sin activación ni envío."""

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.lab.store import LabStore
from app.portfolio.api import create_real_router
from app.portfolio.vault import RealVault


KEY = "draft-view-access-key"
SECRET = "draft-view-secret-key"
UID = "210234567"
REF = hmac.new(SECRET.encode(), KEY.encode(), hashlib.sha256).hexdigest()
PASSPHRASE = "temporary draft policy password"


class View:
    def __init__(self, api_key, secret_key):
        assert (api_key, secret_key) == (KEY, SECRET)

    def balances(self):
        return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "observed_at": datetime.now(timezone.utc).isoformat(), "balances": []}

    def account_reference(self):
        return REF

    def account_uid(self):
        return UID


def definition(direction="LONG"):
    return {"direction": direction, "initial_capital": "1000", "allocation": "1",
            "fee_rate": "0", "slippage_rate": "0", "stop_loss": "0.1",
            "take_profit": "0.2", "entry": {"kind": "CLOSE_ABOVE_EMA", "period": 2}}


def draft(version_id, *, request_id=None):
    return {"request_id": request_id or str(uuid4()), "strategy_version_id": version_id,
            "symbol": "BTCUSDT", "base_asset": "BTC", "quote_asset": "USDT",
            "max_order_quote": "100", "max_daily_turnover_quote": "200",
            "max_exposure_quote": "300", "max_daily_loss_quote": "25",
            "max_fee_bps": "20", "max_slippage_bps": "30",
            "max_data_age_seconds": 60,
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()}


def test_policy_draft_requires_lab_view_uid_and_real_account_link(tmp_path):
    real_path, lab_path = tmp_path / "real.db", tmp_path / "lab.db"
    lab = LabStore(lab_path)
    long_id = lab.create_strategy("LONG", definition())["version_id"]
    short_id = lab.create_strategy("SHORT", definition("SHORT"))["version_id"]
    app = FastAPI()
    app.include_router(create_real_router(real_path, lab_path=lab_path, mexc_factory=View))
    client = TestClient(app)
    token = client.post("/real/vault/create", json={"passphrase": PASSPHRASE}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    account_id = client.post("/real/accounts", json={"name": "MEXC"}, headers=headers).json()["id"]
    path = f"/real/accounts/{account_id}/mexc-policy-drafts"
    payload = draft(long_id)
    assert client.post(path, json=payload, headers=headers).status_code == 409
    assert client.get(path, headers=headers).json() == []
    assert client.post("/real/exchanges/mexc/connect", json={
        "api_key": KEY, "secret_key": SECRET}, headers=headers).status_code == 200
    assert client.post(path, json=payload, headers=headers).status_code == 409
    link_path = f"/real/accounts/{account_id}/mexc-link"
    assert client.get(link_path, headers=headers).json()["linked"] is False
    linked = client.post(link_path, json={"confirmation": "VINCULAR"}, headers=headers)
    assert linked.status_code == 200 and linked.json()["linked"] is True
    assert linked.json()["orders_enabled"] is False
    assert client.get(link_path, headers=headers).json()["linked"] is True
    assert client.post(path, json={**payload, "strategy_version_id": short_id}, headers=headers).status_code == 422
    assert client.post(path, json={**payload, "strategy_version_id": str(uuid4())}, headers=headers).status_code == 409
    assert client.post(path, json={**payload, "symbol": "ETHUSDT"}, headers=headers).status_code == 422
    assert client.post(path, json={**payload, "max_order_quote": "NaN"}, headers=headers).status_code == 422
    assert client.post(path, json={**payload, "expires_at": "2026-10-18T00:00:00"}, headers=headers).status_code == 422
    assert client.post(path, json={**payload, "expires_at": "2020-01-01T00:00:00Z"}, headers=headers).status_code == 422
    created = client.post(path, json=payload, headers=headers)
    assert created.status_code == 200, created.text
    assert created.headers["cache-control"] == "no-store"
    assert created.json()["id"] == payload["request_id"]
    assert created.json()["enabled"] is False
    assert created.json()["execution_enabled"] is False
    assert created.json()["status"] == "DRAFT_NOT_ACTIVATED"
    assert "account_ref" not in created.json()
    assert client.post(path, json=payload, headers=headers).status_code == 200
    changed = {**payload, "max_order_quote": "101"}
    assert client.post(path, json=changed, headers=headers).status_code == 409
    listed = client.get(path, headers=headers)
    assert listed.status_code == 200 and listed.headers["cache-control"] == "no-store"
    assert len(listed.json()) == 1 and listed.json()[0]["max_order_quote"] == "100"
    assert client.get(path).status_code == 401
    assert client.post("/real/vault/lock", headers=headers).status_code == 200
    next_token = client.post("/real/vault/unlock", json={"passphrase": PASSPHRASE}).json()["token"]
    restored = client.get(path, headers={"X-Orbis-Vault-Token": next_token}).json()
    assert restored == listed.json()
    observer = RealVault(real_path)
    observer.unlock(PASSPHRASE)
    with observer.session() as db:
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_live_spot_intents").scalar_one() == 0
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_events").scalar_one() == 0
    observer.lock()


def test_mexc_view_link_existing_book_is_explicit_unique_and_non_accounting(tmp_path):
    vault_path = tmp_path / "real.db"

    class AnyView(View):
        def __init__(self, api_key, secret_key):
            self.ref = hmac.new(secret_key.encode(), api_key.encode(), hashlib.sha256).hexdigest()

        def account_reference(self):
            return self.ref

    app = FastAPI()
    app.include_router(create_real_router(vault_path, mexc_factory=AnyView))
    client = TestClient(app)
    token = client.post("/real/vault/create", json={"passphrase": PASSPHRASE}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    first = client.post("/real/accounts", json={"name": "Ya registrada"}, headers=headers).json()["id"]
    second = client.post("/real/accounts", json={"name": "Otra"}, headers=headers).json()["id"]
    event_path = f"/real/accounts/{first}/events"
    event = {"request_id": str(uuid4()), "kind": "CONTRIBUTION", "quote_currency": "USDT",
             "amount": "500", "occurred_at": datetime.now(timezone.utc).isoformat()}
    assert client.post(event_path, json=event, headers=headers).status_code == 200
    before = client.get(event_path, headers=headers).json()
    first_link = f"/real/accounts/{first}/mexc-link"
    second_link = f"/real/accounts/{second}/mexc-link"
    assert client.get(first_link).status_code == 401
    assert client.post(first_link, json={"confirmation": "VINCULAR"}, headers=headers).status_code == 409
    assert client.post("/real/exchanges/mexc/connect", json={
        "api_key": KEY, "secret_key": SECRET}, headers=headers).status_code == 200
    assert client.post(first_link, json={"confirmation": "OTRO"}, headers=headers).status_code == 422
    assert client.post(first_link, json={"confirmation": "VINCULAR"},
                       headers={**headers, "Origin": "https://evil.test"}).status_code == 403
    linked = client.post(first_link, json={"confirmation": "VINCULAR"}, headers=headers)
    assert linked.status_code == 200, linked.text
    assert linked.json() == {"connected": True, "linked": True, "already_linked": False,
                             "reconciled": False, "orders_enabled": False}
    assert client.post(first_link, json={"confirmation": "VINCULAR"},
                       headers=headers).json()["already_linked"] is True
    status = client.get(first_link, headers=headers)
    assert status.status_code == 200 and status.headers["cache-control"] == "no-store"
    assert status.json()["identity_status"] == "SAME_KEY"
    assert client.get(second_link, headers=headers).json()["assigned_elsewhere"] is True
    assert client.post(second_link, json={"confirmation": "VINCULAR"}, headers=headers).status_code == 409
    assert client.get(event_path, headers=headers).json() == before
    assert UID not in str(linked.json()) and REF not in str(linked.json())
    assert client.post("/real/exchanges/mexc/connect", json={
        "api_key": "rotated-view", "secret_key": "rotated-secret"}, headers=headers).status_code == 200
    rotated = client.post(second_link, json={"confirmation": "VINCULAR"}, headers=headers)
    assert rotated.status_code == 409
    assert client.get(first_link, headers=headers).json()["account_has_other_link"] is True
    assert client.post(first_link, json={"confirmation": "VINCULAR"}, headers=headers).status_code == 409
    observer = RealVault(vault_path)
    observer.unlock(PASSPHRASE)
    with observer.session() as db:
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_mexc_account_links").scalar_one() == 1
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_mexc_uid_links").scalar_one() == 1
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_live_spot_intents").scalar_one() == 0
    observer.lock()


def test_mexc_link_rejects_identity_change_during_fresh_balance_read(tmp_path):
    class ChangingView(View):
        def __init__(self, api_key, secret_key):
            super().__init__(api_key, secret_key)
            self.uid_reads = 0

        def account_uid(self):
            self.uid_reads += 1
            return UID if self.uid_reads == 1 else "999000111"

    path = tmp_path / "real.db"
    app = FastAPI()
    app.include_router(create_real_router(path, mexc_factory=ChangingView))
    client = TestClient(app)
    token = client.post("/real/vault/create", json={"passphrase": PASSPHRASE}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    account_id = client.post("/real/accounts", json={"name": "Una"}, headers=headers).json()["id"]
    assert client.post("/real/exchanges/mexc/connect", json={
        "api_key": KEY, "secret_key": SECRET}, headers=headers).status_code == 200
    response = client.post(f"/real/accounts/{account_id}/mexc-link",
                           json={"confirmation": "VINCULAR"}, headers=headers)
    assert response.status_code == 409
    observer = RealVault(path)
    observer.unlock(PASSPHRASE)
    with observer.session() as db:
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_mexc_account_links").scalar_one() == 0
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_mexc_uid_links").scalar_one() == 0
    observer.lock()
