"""Inventario inicial MEXC: coste declarado, lote atómico y sin órdenes."""

from datetime import datetime, timezone
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.portfolio.api import create_real_router
from app.portfolio.domain import PortfolioError
from app.portfolio.mexc_opening import prepare_mexc_opening
from app.portfolio.vault import RealVault


class FakeView:
    quantity = "0.6"
    uid = "209302839"

    def __init__(self, _key: str, _secret: str):
        pass

    def account_uid(self) -> str:
        return self.uid

    def balances(self) -> dict:
        return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "observed_at": datetime.now(timezone.utc).isoformat(), "balances": [
                    {"asset": "BTC", "free": self.quantity, "locked": "0", "total": self.quantity},
                    {"asset": "USDT", "free": "600", "locked": "40", "total": "640"},
                ]}


def setup_client(tmp_path):
    app = FastAPI()
    app.include_router(create_real_router(tmp_path / "vault.db", mexc_factory=FakeView))
    client = TestClient(app)
    token = client.post("/real/vault/create", json={"passphrase": "opening inventory secret"}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    account = client.post("/real/accounts", headers=headers,
                          json={"name": "MEXC", "base_currency": "USDT"}).json()["id"]
    connect = client.post("/real/exchanges/mexc/connect", headers=headers,
                          json={"api_key": "view-key", "secret_key": "view-secret"})
    assert connect.status_code == 200, connect.text
    return client, headers, account


def test_opening_inventory_preview_commit_retry_and_non_reconciliation(tmp_path):
    FakeView.quantity, FakeView.uid = "0.6", "209302839"
    client, headers, account = setup_client(tmp_path)
    endpoint = f"/real/accounts/{account}/mexc-opening"
    body = {"quote_currency": "USDT", "costs": {"BTC": "50123.456789012345678901"},
            "costs_confirmed": True}
    assert client.post(endpoint + "/preview", json=body).status_code == 401
    assert client.post(endpoint + "/preview", headers=headers,
                       json={"quote_currency": "USDT", "costs": {"BTC": "0"}}).status_code == 422
    missing = client.post(endpoint + "/preview", headers=headers,
                          json={"quote_currency": "USDT", "costs": {}, "costs_confirmed": True})
    assert missing.status_code == 422
    preview = client.post(endpoint + "/preview", headers=headers, json=body)
    assert preview.status_code == 200, preview.text
    info = preview.json()
    assert info["status"] == "PREVIEW" and info["orders_enabled"] is False
    assert info["reconciled"] is False and info["counts"] == {"total": 2, "new": 2, "already_present": 0}
    assert [row["asset"] for row in info["lines"]] == ["BTC", "USDT"]
    assert info["lines"][0]["declared_total_cost"] == "30074.0740734074074073406"
    assert client.get(f"/real/accounts/{account}/events", headers=headers).json() == []
    inspect = RealVault(tmp_path / "vault.db")
    inspect.unlock("opening inventory secret")
    with inspect.session() as db:
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_mexc_account_links").scalar_one() == 0
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_mexc_uid_links").scalar_one() == 0
    inspect.lock()

    commit_body = {**body, "observed_at": info["occurred_at"],
                   "preview_hash": info["preview_hash"], "confirmation": "IMPORTAR_SALDO_INICIAL"}
    tampered = client.post(endpoint + "/commit", headers=headers,
                           json={**commit_body, "preview_hash": "0" * 64})
    assert tampered.status_code == 409
    committed = client.post(endpoint + "/commit", headers=headers, json=commit_body)
    assert committed.status_code == 200, committed.text
    assert committed.json()["counts"]["new"] == 2
    events = client.get(f"/real/accounts/{account}/events", headers=headers).json()
    assert {event["kind"] for event in events} == {"CONTRIBUTION", "ADJUSTMENT_IN"}
    assert all("MEXC" in event["note"] for event in events)
    assert all(event["kind"] != "BUY" for event in events)
    again = client.post(endpoint + "/commit", headers=headers, json=commit_body)
    assert again.status_code == 200, again.text
    assert again.json()["counts"] == {"total": 2, "new": 0, "already_present": 2}
    assert client.get(f"/real/accounts/{account}/events", headers=headers).json() == events
    comparison = client.get(f"/real/accounts/{account}/mexc-spot-comparison", headers=headers).json()
    assert comparison["all_quantities_equal"] is True
    assert comparison["account_venue_binding_verified"] is False
    assert comparison["local_account_link"] == "LOCAL_ACCOUNT_LINK"
    assert comparison["local_identity_status"] == "SAME_KEY"
    assert client.get("/real/exchanges/mexc/trade/status", headers=headers).json()["execution_enabled"] is False

    other = client.post("/real/accounts", headers=headers,
                        json={"name": "Otra cuenta", "base_currency": "USDT"}).json()["id"]
    other_endpoint = f"/real/accounts/{other}/mexc-opening/preview"
    assert client.post(other_endpoint, headers=headers, json=body).status_code == 409
    reconnect = client.post("/real/exchanges/mexc/connect", headers=headers,
                            json={"api_key": "rotated-view-key", "secret_key": "rotated-view-secret"})
    assert reconnect.status_code == 200
    assert client.post(other_endpoint, headers=headers, json=body).status_code == 409
    assert client.get(f"/real/accounts/{account}/mexc-spot-comparison", headers=headers).status_code == 409
    assert client.get(f"/real/accounts/{other}/events", headers=headers).json() == []


def test_opening_commit_receipt_replays_exact_request_without_live_balance(tmp_path, monkeypatch):
    FakeView.quantity, FakeView.uid = "0.6", "209302839"
    client, headers, account = setup_client(tmp_path)
    endpoint = f"/real/accounts/{account}/mexc-opening"
    body = {"quote_currency": "USDT", "costs": {"BTC": "50123.456789"}, "costs_confirmed": True}
    preview = client.post(endpoint + "/preview", headers=headers, json=body).json()
    command = {**body, "observed_at": preview["occurred_at"],
               "preview_hash": preview["preview_hash"], "confirmation": "IMPORTAR_SALDO_INICIAL"}
    accepted = client.post(endpoint + "/commit", headers=headers, json=command)
    assert accepted.status_code == 200, accepted.text
    events_url = f"/real/accounts/{account}/events"
    events = client.get(events_url, headers=headers).json()
    assert len(events) == 2
    workflow = client.get(f"/real/accounts/{account}/mexc-workflow", headers=headers).json()
    assert workflow["view"] == "LINKED" and workflow["book_empty"] is False

    def offline(*_):
        raise ConnectionError("Venue offline after successful commit")

    monkeypatch.setattr(FakeView, "balances", offline)
    replay = client.post(endpoint + "/commit", headers=headers, json=command)
    assert replay.status_code == 200, replay.text
    assert replay.json() == accepted.json() | {"counts": {"total": 2, "new": 0, "already_present": 2}}
    tampered = client.post(endpoint + "/commit", headers=headers, json={**command, "costs": {"BTC": "1"}})
    assert tampered.status_code == 409
    other = client.post("/real/accounts", headers=headers,
                        json={"name": "Otra", "base_currency": "USDT"}).json()["id"]
    assert client.post(f"/real/accounts/{other}/mexc-opening/commit",
                       headers=headers, json=command).status_code == 503
    assert client.get(events_url, headers=headers).json() == events
    assert client.post("/real/exchanges/mexc/disconnect", headers=headers).status_code == 200
    assert client.post(endpoint + "/commit", headers=headers, json=command).status_code == 409


def test_comparison_refuses_changed_uid_or_mid_read_identity(tmp_path, monkeypatch):
    FakeView.quantity, FakeView.uid = "0.6", "209302839"
    client, headers, account = setup_client(tmp_path)
    body = {"quote_currency": "USDT", "costs": {"BTC": "100"}, "costs_confirmed": True}
    opening = f"/real/accounts/{account}/mexc-opening"
    preview = client.post(opening + "/preview", headers=headers, json=body).json()
    commit = {**body, "observed_at": preview["occurred_at"],
              "preview_hash": preview["preview_hash"], "confirmation": "IMPORTAR_SALDO_INICIAL"}
    assert client.post(opening + "/commit", headers=headers, json=commit).status_code == 200
    comparison = f"/real/accounts/{account}/mexc-spot-comparison"
    FakeView.uid = "209302840"
    assert client.get(comparison, headers=headers).status_code == 409
    values = iter(("209302839", "209302840"))
    monkeypatch.setattr(FakeView, "account_uid", lambda self: next(values))
    assert client.get(comparison, headers=headers).status_code == 409
    assert len(client.get(f"/real/accounts/{account}/events", headers=headers).json()) == 2


def test_opening_inventory_rejects_changed_balance_identity_and_nonempty_book(tmp_path):
    FakeView.quantity, FakeView.uid = "0.6", "209302839"
    client, headers, account = setup_client(tmp_path)
    endpoint = f"/real/accounts/{account}/mexc-opening"
    body = {"quote_currency": "USDT", "costs": {"BTC": "100"}, "costs_confirmed": True}
    preview = client.post(endpoint + "/preview", headers=headers, json=body).json()
    commit = {**body, "observed_at": preview["occurred_at"],
              "preview_hash": preview["preview_hash"], "confirmation": "IMPORTAR_SALDO_INICIAL"}
    FakeView.quantity = "0.7"
    assert client.post(endpoint + "/commit", headers=headers, json=commit).status_code == 409
    FakeView.quantity = "0.6"
    FakeView.uid = "209302840"
    assert client.post(endpoint + "/commit", headers=headers, json=commit).status_code == 409
    FakeView.uid = "209302839"
    assert client.post(endpoint + "/commit", headers=headers,
                       json={**commit, "costs": {"BTC": "101"}}).status_code == 409
    assert client.get(f"/real/accounts/{account}/events", headers=headers).json() == []
    inspect = RealVault(tmp_path / "vault.db")
    inspect.unlock("opening inventory secret")
    with inspect.session() as db:
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_mexc_account_links").scalar_one() == 0
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_mexc_uid_links").scalar_one() == 0
    inspect.lock()

    event = client.post(f"/real/accounts/{account}/events", headers=headers, json={
        "request_id": "c179ff99-7cbb-4a59-a0aa-3172a7793cc4",
        "kind": "CONTRIBUTION", "quote_currency": "USDT", "amount": "10",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    })
    assert event.status_code == 200
    assert client.post(endpoint + "/preview", headers=headers, json=body).status_code == 409
    assert client.post(endpoint + "/commit", headers=headers, json=commit).status_code == 409


def test_opening_cost_must_be_explicit_and_valid():
    observed = FakeView("", "").balances()
    kwargs = {"quote_currency": "USDT", "now": datetime.now(timezone.utc)}
    for costs in ({}, {"BTC": "NaN"}, {"BTC": "-1"}, {"BTC": "1", "ETH": "2"}):
        try:
            prepare_mexc_opening("account", observed, costs=costs, **kwargs)
        except PortfolioError:
            pass
        else:
            raise AssertionError("Coste incompleto o inválido aceptado")
    result = prepare_mexc_opening("account", observed, costs={"BTC": "0"}, **kwargs)
    assert Decimal(result["lines"][0]["declared_total_cost"]) == 0
    assert result["accounting"].endswith("NOT_RECONCILED")
    try:
        prepare_mexc_opening("account", observed, quote_currency="BTC", costs={"USDT": "1"},
                             now=datetime.now(timezone.utc))
    except PortfolioError:
        pass
    else:
        raise AssertionError("BTC no puede tratarse como efectivo de apertura")
