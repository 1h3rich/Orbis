"""PriceProvider: contrato, registro por nombre y uso solo para valoración."""

from decimal import Decimal

import pytest

from app.core.config import OrbisConfig, get_orbis_config
from app.pricing import (
    ManualPriceProvider,
    StaticPriceProvider,
    available_price_providers,
    create_price_provider,
    register_price_provider,
)


def test_static_provider_returns_fixed_prices():
    provider = StaticPriceProvider({("BTC", "EUR"): Decimal("100000")})

    assert provider.get_price("BTC", "EUR") == Decimal("100000")
    assert provider.get_price("BTC", "USD") is None
    assert provider.get_price("ETH", "EUR") is None


def test_static_provider_accepts_string_prices_and_normalises_keys():
    provider = StaticPriceProvider({("btc", "eur"): "12345.67"})
    provider.set_price("eth", "eur", "2000")

    assert provider.get_price("BTC", "EUR") == Decimal("12345.67")
    assert provider.get_price("ETH", "EUR") == Decimal("2000")


def test_manual_provider_reads_the_price_quotes_table(db):
    from app.pricing.providers import upsert_price

    assert ManualPriceProvider(db).get_price("BTC", "EUR") is None

    upsert_price(db, "BTC", "EUR", "100000")
    assert ManualPriceProvider(db).get_price("BTC", "EUR") == Decimal("100000")

    # Publicar de nuevo actualiza la misma fila
    upsert_price(db, "BTC", "EUR", "110000")
    assert ManualPriceProvider(db).get_price("BTC", "EUR") == Decimal("110000")
    assert ManualPriceProvider(db).get_price("BTC", "USD") is None


def test_manual_provider_requires_a_session():
    with pytest.raises(ValueError):
        ManualPriceProvider(None)


def test_providers_are_registered_by_name():
    names = available_price_providers()

    assert "manual" in names
    assert "static" in names
    assert isinstance(create_price_provider("static"), StaticPriceProvider)


def test_unknown_provider_lists_the_available_names():
    with pytest.raises(ValueError) as error:
        create_price_provider("binance")

    message = str(error.value)
    assert "binance" in message
    assert "manual" in message
    assert "static" in message


def test_custom_provider_can_be_registered():
    class Fixed(StaticPriceProvider):
        pass

    register_price_provider("fixed-test", lambda db: Fixed({("BTC", "EUR"): "1"}))

    provider = create_price_provider("FIXED-TEST")
    assert provider.get_price("BTC", "EUR") == Decimal("1")


def test_config_selects_the_price_provider(monkeypatch):
    monkeypatch.delenv("ORBIS_PRICE_PROVIDER", raising=False)
    assert OrbisConfig().price_provider == "kraken"
    assert get_orbis_config().price_provider == "kraken"

    monkeypatch.setenv("ORBIS_PRICE_PROVIDER", "MANUAL")
    assert get_orbis_config().price_provider == "manual"

    monkeypatch.setenv("ORBIS_PRICE_PROVIDER", "STATIC")
    assert get_orbis_config().price_provider == "static"

    with pytest.raises(Exception):
        OrbisConfig(price_provider="not valid name!!")


def test_publish_price_endpoint_upserts_and_validates(client):
    created = client.post(
        "/paper/prices",
        json={"asset": "BTC", "quote_currency": "EUR", "price": "100000"},
    )
    updated = client.post(
        "/paper/prices",
        json={"asset": "BTC", "quote_currency": "EUR", "price": "105000"},
    )

    assert created.status_code == 201
    assert updated.status_code == 201
    assert Decimal(str(updated.json()["price"])) == Decimal("105000")
    assert updated.json()["source"] == "MANUAL"
    assert updated.json()["updated_at"] is not None

    listed = client.get("/paper/prices")
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert Decimal(str(listed.json()[0]["price"])) == Decimal("105000")

    for body in (
        {"asset": "BTC", "quote_currency": "EUR", "price": "0"},
        {"asset": "BTC", "quote_currency": "EUR", "price": "-1"},
        {"asset": "BTC", "quote_currency": "eur", "price": "1"},
        {"asset": "BTC", "quote_currency": "EUR", "price": "1", "source": "LIVE"},
        {"asset": "BTC", "quote_currency": "EUR"},
    ):
        assert client.post("/paper/prices", json=body).status_code == 422


def test_price_is_still_mandatory_for_buy_and_sell(client, create_scenario):
    create_scenario()
    client.post(
        "/paper/prices",
        json={"asset": "BTC", "quote_currency": "EUR", "price": "100000"},
    )

    buy = client.post("/paper/buy", json={
        "request_id": "7d0f7a4c-2b0a-4a3f-9f5f-2a5d4d4c9a11",
        "asset": "BTC", "quote_currency": "EUR", "order_amount": "10",
        "estimated_fee": "0",
    })
    sell = client.post("/paper/sell", json={
        "request_id": "7d0f7a4c-2b0a-4a3f-9f5f-2a5d4d4c9a12",
        "asset": "BTC", "quote_currency": "EUR", "quantity": "1",
        "fee": "0",
    })

    assert buy.status_code == 422
    assert sell.status_code == 422


def test_portfolio_valuation_uses_the_configured_provider(client, create_scenario, monkeypatch):
    from uuid import uuid4

    # Fijar manual explícito: el defecto para demo local es kraken (con red).
    monkeypatch.setenv("ORBIS_PRICE_PROVIDER", "manual")
    create_scenario()
    client.post("/paper/buy", json={
        "request_id": str(uuid4()), "asset": "BTC", "quote_currency": "EUR",
        "order_amount": "100", "price": "100000", "estimated_fee": "0",
    })

    client.post("/paper/prices", json={"asset": "BTC", "quote_currency": "EUR", "price": "90000"})
    with_price = client.get("/paper/portfolio?quote_currency=EUR").json()
    assert Decimal(str(with_price["positions"][0]["current_price"])) == Decimal("90000")

    # Sin cotización publicada y con un proveedor estático vacío se valora a coste
    monkeypatch.setenv("ORBIS_PRICE_PROVIDER", "static")
    at_cost = client.get("/paper/portfolio?quote_currency=EUR").json()
    position = at_cost["positions"][0]
    assert Decimal(str(position["current_price"])) == Decimal(str(position["average_cost"]))
    assert Decimal(str(position["unrealized_pnl"])) == Decimal("0")
