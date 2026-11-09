"""GET /paper/portfolio: importes Decimal y procedencia de la valoración."""

from decimal import Decimal
from uuid import uuid4

from app.core.paper_portfolio import build_paper_portfolio, resolve_price_with_meta
from app.core.paper_scenarios import get_paper_scenario

TOP_LEVEL_FIELDS = [
    "quote_currency",
    "initial_balance",
    "cash_balance",
    "cash_weight",
    "positions_value",
    "equity",
    "realized_pnl",
    "unrealized_pnl",
    "total_pnl",
    "return_percentage",
    "total_fees",
    "valuation_status",
    "positions",
]

POSITION_FIELDS = [
    "asset",
    "quote_currency",
    "quantity",
    "average_cost",
    "invested",
    "current_price",
    "price_source",
    "price_as_of",
    "price_age_seconds",
    "price_stale",
    "value",
    "unrealized_pnl",
    "realized_pnl",
    "return_percentage",
    "fees",
    "weight_percentage",
]


def buy(client, **overrides):
    body = {
        "request_id": str(uuid4()),
        "asset": "BTC",
        "quote_currency": "EUR",
        "order_amount": "300",
        "price": "100000",
        "estimated_fee": "0",
    }
    body.update(overrides)
    response = client.post("/paper/buy", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def sell(client, **overrides):
    body = {
        "request_id": str(uuid4()),
        "asset": "BTC",
        "quote_currency": "EUR",
        "quantity": "0.001",
        "price": "110000",
        "fee": "1",
    }
    body.update(overrides)
    response = client.post("/paper/sell", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_portfolio_exposes_documented_fields(client, create_scenario, monkeypatch):
    monkeypatch.setenv("ORBIS_PRICE_PROVIDER", "manual")
    create_scenario()
    buy(client)
    sell(client)

    response = client.get("/paper/portfolio?quote_currency=EUR")

    assert response.status_code == 200
    payload = response.json()
    assert list(payload) == TOP_LEVEL_FIELDS

    [position] = payload["positions"]
    assert list(position) == POSITION_FIELDS
    assert position["asset"] == "BTC"
    assert position["quote_currency"] == "EUR"
    assert position["price_source"] == "last_trade"
    assert position["price_as_of"] is not None
    assert position["price_age_seconds"] is not None
    assert position["price_stale"] is True
    assert payload["valuation_status"] == "ESTIMATED"


def test_portfolio_invariants_are_exact_in_decimal(client, create_scenario, db, monkeypatch):
    # Fijar manual explícito: el defecto para demo local es kraken (con red).
    monkeypatch.setenv("ORBIS_PRICE_PROVIDER", "manual")
    create_scenario()
    buy(client)
    sell(client)

    scenario = get_paper_scenario(db, "EUR")
    portfolio = build_paper_portfolio(db, scenario)

    initial = portfolio["initial_balance"]
    cash = portfolio["cash_balance"]
    equity = portfolio["equity"]
    total_pnl = portfolio["total_pnl"]

    assert cash == Decimal("1000") - Decimal("300") + Decimal("109")
    assert equity == cash + portfolio["positions_value"]
    assert equity - initial == total_pnl
    assert total_pnl == portfolio["realized_pnl"] + portfolio["unrealized_pnl"]
    # Sin cotización publicada se valora al último precio negociado (110000)
    assert portfolio["positions_value"] == Decimal("220")
    assert portfolio["realized_pnl"] == Decimal("9")
    assert portfolio["unrealized_pnl"] == Decimal("20")
    assert portfolio["total_pnl"] == Decimal("29")
    assert portfolio["total_fees"] == Decimal("1")
    assert portfolio["return_percentage"] == Decimal("2.9")


def test_usdt_paper_scenario_is_accounted_separately_without_fx_conversion(
    client, create_scenario, db, monkeypatch,
):
    monkeypatch.setenv("ORBIS_PRICE_PROVIDER", "manual")
    create_scenario(quote_currency="EUR", initial_balance="400")
    create_scenario(quote_currency="USDT", initial_balance="1000")
    buy(client, quote_currency="USDT", order_amount="300", price="100000")
    response = client.post("/paper/prices", json={
        "asset": "BTC", "quote_currency": "USDT", "price": "110000",
    })
    assert response.status_code == 201

    usdt = build_paper_portfolio(db, get_paper_scenario(db, "USDT"))
    eur = build_paper_portfolio(db, get_paper_scenario(db, "EUR"))
    assert usdt["quote_currency"] == "USDT"
    assert usdt["cash_balance"] == Decimal("700")
    assert usdt["positions_value"] == Decimal("330")
    assert usdt["equity"] == Decimal("1030")
    assert usdt["total_pnl"] == Decimal("30")
    assert eur["cash_balance"] == eur["equity"] == Decimal("400")
    assert eur["positions"] == []
    public = client.get("/paper/portfolio?quote_currency=USDT")
    assert public.status_code == 200
    assert Decimal(str(public.json()["equity"])) == Decimal("1030")


def test_portfolio_uses_published_price_for_valuation(client, create_scenario, monkeypatch):
    # Fijar manual explícito: el defecto para demo local es kraken (con red).
    monkeypatch.setenv("ORBIS_PRICE_PROVIDER", "manual")
    create_scenario()
    buy(client)
    sell(client)

    published = client.post(
        "/paper/prices",
        json={"asset": "BTC", "quote_currency": "EUR", "price": "120000"},
    )
    assert published.status_code == 201

    payload = client.get("/paper/portfolio?quote_currency=EUR").json()
    [position] = payload["positions"]

    assert Decimal(str(position["current_price"])) == Decimal("120000")
    assert Decimal(str(position["value"])) == Decimal("240")
    assert Decimal(str(position["unrealized_pnl"])) == Decimal("40")
    assert Decimal(str(payload["equity"])) == Decimal("1049")
    assert Decimal(str(payload["total_pnl"])) == Decimal("49")
    assert payload["valuation_status"] == "ESTIMATED"
    assert position["price_source"] == "manual"
    assert position["price_as_of"] is not None
    assert position["price_stale"] is True


def test_portfolio_reports_fresh_public_reference_without_changing_money(client, create_scenario, db):
    create_scenario()
    buy(client)
    scenario = get_paper_scenario(db, "EUR")

    class FreshProvider:
        def get_price_with_meta(self, asset, quote_currency):
            assert (asset, quote_currency) == ("BTC", "EUR")
            return {
                "price": "120000", "source": "kraken",
                "as_of": "2026-10-21T12:00:00+00:00",
                "age_seconds": 2.0, "stale": False,
            }

    portfolio = build_paper_portfolio(db, scenario, price_provider=FreshProvider())
    [position] = portfolio["positions"]
    assert portfolio["valuation_status"] == "CURRENT_REFERENCE"
    assert position["price_source"] == "kraken"
    assert position["price_age_seconds"] == 2.0
    assert position["price_stale"] is False
    assert position["current_price"] == Decimal("120000")


def test_invalid_provider_price_falls_back_to_trade_as_estimate(client, create_scenario, db):
    create_scenario()
    buy(client)
    scenario = get_paper_scenario(db, "EUR")

    class InvalidProvider:
        def get_price_with_meta(self, asset, quote_currency):
            return {"price": "NaN", "source": "kraken", "stale": False}

    portfolio = build_paper_portfolio(db, scenario, price_provider=InvalidProvider())
    [position] = portfolio["positions"]
    assert portfolio["valuation_status"] == "ESTIMATED"
    assert position["price_source"] == "last_trade"
    assert position["price_stale"] is True
    assert position["current_price"] == Decimal("100000")


def test_cost_basis_fallback_is_explicitly_estimated():
    price, meta = resolve_price_with_meta(
        None, "ETH", "EUR", {}, Decimal("2300")
    )
    assert price == Decimal("2300")
    assert meta == {
        "price_source": "average_cost",
        "price_as_of": None,
        "price_age_seconds": None,
        "price_stale": True,
    }


def test_cached_quote_is_estimated_even_when_it_has_a_price():
    class CachedProvider:
        def get_price_with_meta(self, asset, quote_currency):
            return {
                "price": "32100", "source": "stale-cache",
                "as_of": "2026-10-20T00:00:00+00:00",
                "age_seconds": 900, "stale": True,
            }

    price, meta = resolve_price_with_meta(
        CachedProvider(), "BTC", "EUR", {"BTC": Decimal("30000")}, Decimal("29000")
    )
    assert price == Decimal("32100")
    assert meta["price_source"] == "stale-cache"
    assert meta["price_stale"] is True


def test_portfolio_json_round_trip_stays_consistent(client, create_scenario):
    """La respuesta HTTP viaja en float; comparamos con tolerancia de redondeo."""
    create_scenario()
    buy(client)
    sell(client)

    payload = client.get("/paper/portfolio?quote_currency=EUR").json()
    scale = Decimal("0.000000001")

    equity_minus_initial = (
        Decimal(str(payload["equity"])) - Decimal(str(payload["initial_balance"]))
    )
    total = Decimal(str(payload["total_pnl"]))
    realized = Decimal(str(payload["realized_pnl"]))
    unrealized = Decimal(str(payload["unrealized_pnl"]))

    assert abs(equity_minus_initial - total) < scale
    assert abs(total - (realized + unrealized)) < scale


def test_fully_closed_position_disappears_from_portfolio(client, create_scenario):
    create_scenario()
    bought = buy(client, estimated_fee="1")
    sell(client, quantity=str(bought["asset_received"]), price="100000", fee="0")

    payload = client.get("/paper/portfolio?quote_currency=EUR").json()

    assert payload["positions"] == []
    assert payload["valuation_status"] == "CASH_ONLY"
    assert Decimal(str(payload["positions_value"])) == Decimal("0")
    # 1000 − 300 (compra con comisión 1) + 299 acreditados en la venta total
    assert Decimal(str(payload["cash_balance"])) == Decimal("999")
    # Las comisiones de la posición cerrada siguen sumando
    assert Decimal(str(payload["total_fees"])) == Decimal("1")
    # La plusvalía realizada cierra en −1 (la comisión de compra), sin residuos
    assert Decimal(str(payload["realized_pnl"])) == Decimal("-1")


def test_portfolio_requires_an_open_scenario(client, create_scenario):
    assert client.get("/paper/portfolio").status_code == 404
    create_scenario()
    assert client.get("/paper/portfolio").status_code == 200


def test_portfolio_requires_quote_when_several_sessions_are_open(client, create_scenario):
    create_scenario()
    create_scenario(quote_currency="USD")

    ambiguous = client.get("/paper/portfolio")
    specific = client.get("/paper/portfolio?quote_currency=USD")

    assert ambiguous.status_code == 400
    assert specific.status_code == 200
    assert specific.json()["quote_currency"] == "USD"


def test_empty_session_reports_zeroed_values(client, create_scenario):
    create_scenario()

    payload = client.get("/paper/portfolio?quote_currency=EUR").json()

    assert Decimal(str(payload["cash_balance"])) == Decimal("1000")
    assert Decimal(str(payload["equity"])) == Decimal("1000")
    assert Decimal(str(payload["total_pnl"])) == Decimal("0")
    assert Decimal(str(payload["positions_value"])) == Decimal("0")
    assert payload["positions"] == []
    assert Decimal(payload["return_percentage"]) == Decimal("0")
