"""Comparación puntual de cantidades: nunca equivale a importar ni conciliar."""

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import hmac
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.portfolio.api import create_real_router
from app.portfolio.domain import AccountSnapshot, PortfolioError, Position
from app.portfolio.models import RealMexcAccountLink
from app.portfolio.reconciliation import compare_mexc_spot_inventory
from app.portfolio.vault import RealVault


def observed(balances):
    return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
            "observed_at": datetime.now(timezone.utc).isoformat(), "balances": balances}


def test_compare_aggregates_units_across_purchase_currencies_without_float():
    book = AccountSnapshot("account", cash={"USDT": Decimal("10"), "EUR": Decimal("-100")},
                           positions={
                               ("BTC", "USDT"): Position("BTC", "USDT", Decimal("0.123456789012345678")),
                               ("BTC", "EUR"): Position("BTC", "EUR", Decimal("0.000000000000000022")),
                           })
    result = compare_mexc_spot_inventory(book, observed([
        {"asset": "BTC", "free": "0.123456789012345700", "locked": "0", "total": "0.123456789012345700"},
        {"asset": "USDT", "free": "7", "locked": "3", "total": "10"},
    ]))
    assert result["comparison"] == "DIAGNOSTIC_ONLY_NOT_RECONCILED"
    assert result["account_venue_binding_verified"] is False
    assert result["negative_book_cash_assets"] == ["EUR"]
    rows = {row["asset"]: row for row in result["assets"]}
    assert rows["BTC"]["book_quantity"] == "0.123456789012345700"
    assert rows["BTC"]["quantity_equal"] is True
    assert rows["USDT"]["difference_mexc_minus_book"] == "0"
    assert rows["EUR"]["difference_mexc_minus_book"] == "100"
    assert result["all_quantities_equal"] is False


@pytest.mark.parametrize("bad", [
    {"asset": "BTC", "free": "1", "locked": "0.1", "total": "1"},
    {"asset": "BTC", "free": "NaN", "locked": "0", "total": "0"},
    {"asset": "BTC", "free": "-1", "locked": "0", "total": "-1"},
])
def test_compare_rejects_incoherent_observation(bad):
    with pytest.raises(PortfolioError):
        compare_mexc_spot_inventory(AccountSnapshot("account"), observed([bad]))


def test_compare_rejects_stale_or_untrusted_observation():
    balance = {"asset": "BTC", "free": "1", "locked": "0", "total": "1"}
    stale = observed([balance])
    stale["observed_at"] = "2020-01-01T00:00:00+00:00"
    with pytest.raises(PortfolioError, match="obsoleta"):
        compare_mexc_spot_inventory(AccountSnapshot("account"), stale)
    wrong_source = observed([balance])
    wrong_source["source"] = "UNTRUSTED"
    with pytest.raises(PortfolioError, match="no verificable"):
        compare_mexc_spot_inventory(AccountSnapshot("account"), wrong_source)


def test_compare_rejects_tiny_locked_amount_hidden_by_default_decimal_precision():
    with pytest.raises(PortfolioError, match="libre más bloqueado"):
        compare_mexc_spot_inventory(AccountSnapshot("account"), observed([
            {"asset": "BTC", "free": "1", "locked": "1e-40", "total": "1"},
        ]))


class FakeMexc:
    uid = "209302839"

    def __init__(self, _key, _secret):
        pass

    def account_uid(self):
        return self.uid

    def balances(self):
        return observed([
            {"asset": "BTC", "free": "0.5", "locked": "0.1", "total": "0.6"},
            {"asset": "USDT", "free": "600", "locked": "0", "total": "600"},
        ])


def test_comparison_api_requires_vault_and_view_connection_without_booking(tmp_path):
    app = FastAPI()
    app.include_router(create_real_router(tmp_path / "vault.db", mexc_factory=FakeMexc))
    client = TestClient(app)
    token = client.post("/real/vault/create", json={"passphrase": "comparison test secret"}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    account = client.post("/real/accounts", json={"name": "MEXC manual", "base_currency": "USDT"},
                          headers=headers).json()["id"]
    url = f"/real/accounts/{account}/mexc-spot-comparison"
    assert client.get(url).status_code == 401
    assert client.get(url, headers=headers).status_code == 409
    events = f"/real/accounts/{account}/events"
    for item in (
        {"kind": "CONTRIBUTION", "amount": "1000"},
        {"kind": "BUY", "asset": "BTC", "quantity": "0.4", "unit_price": "1000"},
    ):
        response = client.post(events, headers=headers, json={
            "request_id": str(uuid4()), "quote_currency": "USDT",
            "occurred_at": datetime.now(timezone.utc).isoformat(), **item})
        assert response.status_code == 200, response.text
    before = client.get(events, headers=headers).json()
    connected = client.post("/real/exchanges/mexc/connect", headers=headers,
                            json={"api_key": "fake-view-key", "secret_key": "fake-view-secret"})
    assert connected.status_code == 200, connected.text
    response = client.get(url, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    rows = {row["asset"]: row for row in response.json()["assets"]}
    assert rows["BTC"]["difference_mexc_minus_book"] == "0.2"
    assert rows["USDT"]["difference_mexc_minus_book"] == "0"
    assert response.json()["account_venue_binding_verified"] is False
    assert response.json()["local_account_link"] == "NONE"
    assert client.get(events, headers=headers).json() == before
    other = client.post("/real/accounts", json={"name": "Otra", "base_currency": "USDT"},
                        headers=headers).json()["id"]
    reference = hmac.new(b"fake-view-secret", b"fake-view-key", hashlib.sha256).hexdigest()
    separate = RealVault(tmp_path / "vault.db")
    separate.unlock("comparison test secret")
    with separate.session() as db:
        db.add(RealMexcAccountLink(account_ref=reference, account_id=other,
                                   created_at=datetime.now(timezone.utc)))
        db.commit()
    separate.lock()
    assert client.get(url, headers=headers).status_code == 409
    assigned = client.get(f"/real/accounts/{other}/mexc-spot-comparison", headers=headers)
    assert assigned.status_code == 200
    assert assigned.json()["local_account_link"] == "LOCAL_ACCOUNT_LINK_UID_UNVERIFIED"
    assert assigned.json()["local_identity_status"] == "NOT_RECORDED"
    assert assigned.json()["account_venue_binding_verified"] is False
    assert client.get(url, headers={**headers, "Origin": "https://outside.invalid"}).status_code == 403
    client.post("/real/vault/lock", headers=headers)
    assert client.get(url, headers=headers).status_code == 401
