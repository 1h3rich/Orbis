"""Órdenes PAPER a cotización pública: causalidad, evidencia e idempotencia."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4
import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

import app.main as main_module
from app.core.paper_market_orders import (
    MarketQuoteUnavailable, evidence_for, legacy_market_request_digest, market_request_digest,
    market_trade_response, quote_from_snapshot,
)
from app.core.paper_portfolio import build_paper_portfolio
from app.core.paper_scenarios import (
    close_paper_scenario, create_paper_scenario, get_paper_scenario,
)
from app.core.paper_trading import execute_configured_paper_buy, execute_paper_buy
from app.database.models import Operation, PaperMarketEvidence, PaperRequest
from app.database.database import get_db


def snapshot(*, price="100", bid="99", ask="101", status="unknown", seconds_old=0,
             symbol="BTC/EUR", provider="kraken"):
    received = datetime.now(timezone.utc) - timedelta(seconds=seconds_old)
    return {
        "available": True, "symbol": symbol, "resolved_provider": provider,
        "source": provider, "price": price, "bid": bid, "ask": ask,
        "received_at": received.isoformat(), "market_time_status": status,
        "market_time_at": received.isoformat() if status == "fresh" else None,
    }


def buy_body(request_id=None, **overrides):
    body = {
        "request_id": str(request_id or uuid4()), "asset": "BTC", "quote_currency": "EUR",
        "order_amount": "100", "estimated_fee": "0", "provider": "kraken",
    }
    body.update(overrides)
    return body


def test_quote_chooses_ask_bid_and_labels_last_as_approximation():
    data = snapshot()
    buy = quote_from_snapshot(data, asset="BTC", quote_currency="EUR", side="BUY",
                              requested_provider="kraken")
    sell = quote_from_snapshot(data, asset="BTC", quote_currency="EUR", side="SELL",
                               requested_provider="kraken")
    assert (buy.price, buy.price_basis, buy.approximate) == (Decimal("101"), "ask", True)
    assert (sell.price, sell.price_basis, sell.approximate) == (Decimal("99"), "bid", True)
    data["market_time_status"] = "fresh"
    data["market_time_at"] = data["received_at"]
    assert not quote_from_snapshot(data, asset="BTC", quote_currency="EUR", side="BUY",
                                   requested_provider="kraken").approximate
    data["ask"] = None
    last = quote_from_snapshot(data, asset="BTC", quote_currency="EUR", side="BUY",
                               requested_provider="kraken")
    assert (last.price, last.price_basis, last.approximate) == (Decimal("100"), "last", True)


def test_quote_rejects_source_that_disagrees_with_resolved_provider():
    data = snapshot(status="fresh")
    data["source"] = "manual"
    with pytest.raises(MarketQuoteUnavailable, match="procedencia"):
        quote_from_snapshot(data, asset="BTC", quote_currency="EUR", side="BUY",
                            requested_provider="kraken")
    data["source"] = "binance"
    with pytest.raises(MarketQuoteUnavailable, match="procedencia"):
        quote_from_snapshot(data, asset="BTC", quote_currency="EUR", side="BUY",
                            requested_provider="auto")


def test_market_request_digest_distinguishes_low_digits_at_ledger_limit():
    common = {
        "side": "BUY", "asset": "BTC", "quote_currency": "EUR",
        "fee": Decimal("0"), "provider": "kraken",
    }
    first = market_request_digest(
        **common, size=Decimal("999999999999999999.999999999999"),
    )
    second = market_request_digest(
        **common, size=Decimal("999999999999999999.999999999998"),
    )
    assert first != second
    assert legacy_market_request_digest(
        **common, size=Decimal("999999999999999999.999999999999"),
    ) is None


def test_market_response_keeps_exact_large_amount_and_rounding_residual():
    amount = Decimal("999999999999999999.999999999999")
    result = execute_paper_buy(
        asset="BTC", order_amount=amount, price=Decimal("2"),
        available_budget=amount, max_order_amount=amount,
        estimated_fee=Decimal("0"), max_fee_percentage=Decimal("2"),
    )
    quote = quote_from_snapshot(
        snapshot(price="2", ask="2", status="fresh"),
        asset="BTC", quote_currency="EUR", side="BUY", requested_provider="kraken",
    )
    response = market_trade_response(result, evidence_for(str(uuid4()), quote))
    assert response["amount_spent"] == "999999999999999999.999999999999"
    assert response["asset_received"] == "499999999999999999.999999999999"
    assert response["rounding_residual_quote"] == "0.000000000001"


@pytest.mark.parametrize("change", [
    {"status": "stale"}, {"status": "future"}, {"status": "invalid"},
    {"seconds_old": 61}, {"symbol": "ETH/EUR"}, {"provider": "grvt"},
    {"price": "NaN", "ask": None}, {"ask": "0"}, {"ask": "0.0000000000001"},
])
def test_quote_rejects_unusable_data(change):
    data = snapshot(**change)
    with pytest.raises(MarketQuoteUnavailable):
        quote_from_snapshot(data, asset="BTC", quote_currency="EUR", side="BUY",
                            requested_provider="kraken")


def test_market_buy_persists_server_ask_and_replays_without_network(
    client, create_scenario, db, monkeypatch,
):
    create_scenario()
    monkeypatch.setattr(main_module, "market_snapshot", lambda symbol, provider: snapshot(status="fresh"))
    body = buy_body()
    first = client.post("/paper/market/buy", json=body)
    assert first.status_code == 200, first.text
    data = first.json()
    assert data["executed"] is True
    assert data["price"] == "101"
    assert data["quote"]["price_basis"] == "ask"
    assert data["quote"]["provider"] == "kraken"
    assert data["quote"]["approximate"] is False
    evidence = db.get(PaperMarketEvidence, body["request_id"])
    assert evidence and evidence.price == "101"
    op = db.get(Operation, data["operation_id"])
    assert op and op.exchange == "kraken" and op.mode == "PAPER"

    def no_network(*_args, **_kwargs):
        raise AssertionError("retry must not fetch a new quote")

    monkeypatch.setattr(main_module, "market_snapshot", no_network)
    again = client.post("/paper/market/buy", json=body)
    assert again.status_code == 200 and again.json() == data
    assert db.query(PaperRequest).count() == 1
    assert db.query(Operation).count() == 1
    changed = client.post("/paper/market/buy", json={**body, "order_amount": "200"})
    assert changed.status_code == 409


def test_market_buy_does_not_write_if_source_disagrees_with_provider(
    client, create_scenario, db, monkeypatch,
):
    create_scenario()
    data = snapshot(status="fresh")
    data["source"] = "manual"
    monkeypatch.setattr(main_module, "market_snapshot", lambda symbol, provider: data)
    preview = client.get("/paper/market/preview?asset=BTC&quote_currency=EUR&provider=kraken")
    assert preview.status_code == 200 and preview.json()["available"] is False
    result = client.post("/paper/market/buy", json=buy_body())
    assert result.status_code == 503
    assert result.json()["code"] == "market_quote_unavailable"
    assert db.query(PaperRequest).count() == 0
    assert db.query(PaperMarketEvidence).count() == 0
    assert db.query(Operation).count() == 0


def test_market_buy_replays_old_normalized_digest_after_upgrade(
    client, create_scenario, db, monkeypatch,
):
    create_scenario()
    monkeypatch.setattr(main_module, "market_snapshot", lambda symbol, provider: snapshot(status="fresh"))
    body = buy_body(order_amount="100")
    first = client.post("/paper/market/buy", json=body)
    assert first.status_code == 200 and first.json()["executed"]
    old_payload = {
        "kind": "orbis-paper-public-market-v1", "side": "BUY",
        "symbol": "BTC/EUR", "size": "1E+2", "fee": "0", "provider": "kraken",
    }
    old_digest = hashlib.sha256(
        json.dumps(old_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    record = db.get(PaperRequest, body["request_id"])
    assert old_digest != record.payload_digest
    record.payload_digest = old_digest
    db.commit()
    monkeypatch.setattr(
        main_module, "market_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no network on replay")),
    )
    replay = client.post("/paper/market/buy", json=body)
    assert replay.status_code == 200
    assert replay.json() == first.json()


def test_market_route_canonicalizes_xbt_before_quote_and_ledger(
    client, create_scenario, db, monkeypatch,
):
    create_scenario()
    symbols = []

    def public(symbol, provider):
        symbols.append(symbol)
        return snapshot(status="fresh")

    monkeypatch.setattr(main_module, "market_snapshot", public)
    response = client.post("/paper/market/buy", json=buy_body(asset="XBT"))
    assert response.status_code == 200 and response.json()["executed"]
    assert symbols == ["BTC/EUR"]
    operation = db.get(Operation, response.json()["operation_id"])
    assert operation.asset == "BTC"


def test_market_sell_uses_bid_and_keeps_same_request_id(client, create_scenario, db, monkeypatch):
    create_scenario()
    monkeypatch.setattr(main_module, "market_snapshot", lambda symbol, provider: snapshot(status="fresh"))
    buy = client.post("/paper/market/buy", json=buy_body())
    assert buy.status_code == 200 and buy.json()["executed"]
    sell = {
        "request_id": str(uuid4()), "asset": "BTC", "quote_currency": "EUR",
        "quantity": "0.5", "fee": "0", "provider": "kraken",
    }
    result = client.post("/paper/market/sell", json=sell)
    assert result.status_code == 200, result.text
    assert result.json()["price"] == "99"
    assert result.json()["quote"]["price_basis"] == "bid"
    assert result.json()["executed"] is True
    assert db.query(PaperMarketEvidence).count() == 2


def test_tiny_buy_cannot_spend_cash_for_zero_ledger_units(
    client, create_scenario, db, monkeypatch,
):
    create_scenario()
    monkeypatch.setattr(main_module, "market_snapshot", lambda symbol, provider: snapshot(status="fresh"))
    result = client.post(
        "/paper/market/buy", json=buy_body(order_amount="0.000000000001"),
    )
    assert result.status_code == 200, result.text
    assert result.json()["executed"] is False
    assert result.json()["reason"] == "Order quantity below ledger precision"
    assert db.query(Operation).count() == 0


def test_tiny_sell_cannot_credit_cash_without_reducing_ledger_units(
    client, create_scenario, db, monkeypatch,
):
    create_scenario()
    monkeypatch.setattr(main_module, "market_snapshot", lambda symbol, provider: snapshot(status="fresh"))
    bought = client.post("/paper/market/buy", json=buy_body())
    assert bought.status_code == 200 and bought.json()["executed"]
    result = client.post("/paper/market/sell", json={
        "request_id": str(uuid4()), "asset": "BTC", "quote_currency": "EUR",
        "quantity": "0.0000000000001", "fee": "0", "provider": "kraken",
    })
    assert result.status_code == 200, result.text
    assert result.json()["executed"] is False
    assert result.json()["reason"] == "Order quantity below ledger precision"
    assert db.query(Operation).count() == 1


def test_tiny_sell_proceeds_cannot_remove_units_for_zero_cash(
    client, create_scenario, db, monkeypatch,
):
    create_scenario()
    monkeypatch.setattr(main_module, "market_snapshot", lambda symbol, provider: snapshot(status="fresh"))
    bought = client.post("/paper/market/buy", json=buy_body())
    assert bought.status_code == 200 and bought.json()["executed"]
    monkeypatch.setattr(
        main_module, "market_snapshot",
        lambda symbol, provider: snapshot(price="0.000000000001", bid="0.000000000001", status="fresh"),
    )
    result = client.post("/paper/market/sell", json={
        "request_id": str(uuid4()), "asset": "BTC", "quote_currency": "EUR",
        "quantity": "0.000000000001", "fee": "0", "provider": "kraken",
    })
    assert result.status_code == 200, result.text
    assert result.json()["executed"] is False
    assert result.json()["reason"] == "Order proceeds below ledger precision"
    assert db.query(Operation).count() == 1


def test_market_buy_rounds_units_down_before_writing_ledger(
    client, create_scenario, db, monkeypatch,
):
    create_scenario()
    monkeypatch.setattr(
        main_module, "market_snapshot",
        lambda symbol, provider: snapshot(price="3", bid="3", ask="3", status="fresh"),
    )
    response = client.post("/paper/market/buy", json=buy_body(order_amount="2"))
    assert response.status_code == 200 and response.json()["executed"]
    assert Decimal(response.json()["asset_received"]) == Decimal("0.666666666666")
    assert Decimal(response.json()["rounding_residual_quote"]) == Decimal("0.000000000002")
    operation = db.get(Operation, response.json()["operation_id"])
    assert operation.asset_received == Decimal("0.666666666666")
    assert operation.asset_received * operation.price <= operation.amount_spent - operation.trading_fee


def test_market_buy_rejects_large_relative_rounding_loss(
    client, create_scenario, db, monkeypatch,
):
    create_scenario()
    monkeypatch.setattr(
        main_module, "market_snapshot",
        lambda symbol, provider: snapshot(price="100", bid="100", ask="100", status="fresh"),
    )
    response = client.post(
        "/paper/market/buy", json=buy_body(order_amount="0.00000000019"),
    )
    assert response.status_code == 200
    assert response.json()["executed"] is False
    assert response.json()["reason"] == "Order rounding loss exceeds limit"
    assert db.query(Operation).count() == 0


def test_market_sell_rounds_cash_down_before_writing_ledger(
    client, create_scenario, db, monkeypatch,
):
    create_scenario()
    monkeypatch.setattr(main_module, "market_snapshot", lambda symbol, provider: snapshot(status="fresh"))
    bought = client.post("/paper/market/buy", json=buy_body())
    assert bought.status_code == 200 and bought.json()["executed"]
    monkeypatch.setattr(
        main_module, "market_snapshot",
        lambda symbol, provider: snapshot(price="0.8", bid="0.8", ask="0.8", status="fresh"),
    )
    response = client.post("/paper/market/sell", json={
        "request_id": str(uuid4()), "asset": "BTC", "quote_currency": "EUR",
        "quantity": "0.000000000002", "fee": "0", "provider": "kraken",
    })
    assert response.status_code == 200 and response.json()["executed"]
    assert Decimal(response.json()["amount_spent"]) == Decimal("0.000000000001")
    assert Decimal(response.json()["rounding_residual_quote"]) == Decimal("0.0000000000006")
    operation = db.get(Operation, response.json()["operation_id"])
    assert operation.amount_spent == Decimal("0.000000000001")
    assert operation.amount_spent <= operation.asset_received * operation.price - operation.trading_fee


def test_market_sell_rounding_keeps_portfolio_pnl_equal_to_equity_change(
    client, create_scenario, db, monkeypatch,
):
    create_scenario()
    monkeypatch.setattr(
        main_module, "market_snapshot",
        lambda symbol, provider: snapshot(price="1", bid="1", ask="1", status="fresh"),
    )
    bought = client.post("/paper/market/buy", json=buy_body(order_amount="1"))
    assert bought.status_code == 200 and bought.json()["executed"]
    monkeypatch.setattr(
        main_module, "market_snapshot",
        lambda symbol, provider: snapshot(price="0.8", bid="0.8", ask="0.8", status="fresh"),
    )
    sold = client.post("/paper/market/sell", json={
        "request_id": str(uuid4()), "asset": "BTC", "quote_currency": "EUR",
        "quantity": "0.000000000002", "fee": "0", "provider": "kraken",
    })
    assert sold.status_code == 200 and sold.json()["executed"]

    class FixedPrice:
        def get_price(self, asset, quote_currency):
            return Decimal("1")

    portfolio = build_paper_portfolio(
        db, get_paper_scenario(db, "EUR"), price_provider=FixedPrice(),
    )
    assert portfolio["realized_pnl"] == Decimal("-0.000000000001")
    assert portfolio["unrealized_pnl"] == Decimal("0")
    assert portfolio["equity"] - portfolio["initial_balance"] == portfolio["total_pnl"]


def test_unavailable_quote_has_no_effect_and_never_uses_client_price(
    client, create_scenario, db, monkeypatch,
):
    create_scenario()
    monkeypatch.setattr(main_module, "market_snapshot", lambda symbol, provider: snapshot(status="stale"))
    body = buy_body()
    rejected = client.post("/paper/market/buy", json=body)
    assert rejected.status_code == 503
    assert rejected.json()["code"] == "market_quote_unavailable"
    assert db.query(PaperRequest).count() == 0
    assert db.query(PaperMarketEvidence).count() == 0
    assert db.query(Operation).count() == 0
    forbidden = client.post("/paper/market/buy", json={**body, "price": "1"})
    assert forbidden.status_code == 422


def test_quote_expiring_while_waiting_for_db_rolls_back_claim(
    client, create_scenario, db, monkeypatch,
):
    create_scenario()
    monkeypatch.setattr(main_module, "market_snapshot", lambda symbol, provider: snapshot(status="fresh"))
    real_validator = main_module.quote_from_snapshot
    calls = 0

    def expires_after_first_check(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise MarketQuoteUnavailable("La cotización recibida ha caducado")
        return real_validator(*args, **kwargs)

    monkeypatch.setattr(main_module, "quote_from_snapshot", expires_after_first_check)
    response = client.post("/paper/market/buy", json=buy_body())
    assert response.status_code == 503
    assert calls == 2
    assert db.query(PaperRequest).count() == 0
    assert db.query(PaperMarketEvidence).count() == 0
    assert db.query(Operation).count() == 0


def test_legacy_request_id_cannot_be_reused_for_market_route(client, create_scenario, monkeypatch):
    create_scenario()
    identifier = str(uuid4())
    manual = client.post("/paper/buy", json={
        "request_id": identifier, "asset": "BTC", "quote_currency": "EUR",
        "order_amount": "100", "price": "100", "estimated_fee": "0",
    })
    assert manual.status_code == 200
    monkeypatch.setattr(main_module, "market_snapshot", lambda *_a, **_kw: (_ for _ in ()).throw(
        AssertionError("must not query market after id conflict")))
    conflict = client.post("/paper/market/buy", json=buy_body(identifier))
    assert conflict.status_code == 409


def test_concurrent_market_claim_persists_one_quote_and_one_operation(file_db_engine):
    factory = sessionmaker(bind=file_db_engine, autoflush=False)
    with factory() as seed:
        create_paper_scenario(
            db=seed, quote_currency="EUR", initial_balance=Decimal("1000"),
            max_order_amount=Decimal("500"), max_fee_percentage=Decimal("2"),
            daily_limit=None, monthly_limit=None,
        )
    request_id = str(uuid4())
    digest = market_request_digest(
        side="BUY", asset="BTC", quote_currency="EUR",
        size=Decimal("100"), fee=Decimal("0"), provider="kraken",
    )
    barrier = Barrier(2)

    def place(ask):
        quote = quote_from_snapshot(
            snapshot(ask=str(ask), status="fresh"),
            asset="BTC", quote_currency="EUR", side="BUY", requested_provider="kraken",
        )
        with factory() as session:
            scenario = get_paper_scenario(session, "EUR")
            barrier.wait(timeout=10)
            result = execute_configured_paper_buy(
                db=session, scenario=scenario, request_id=request_id,
                asset="BTC", order_amount="100", price=quote.price,
                estimated_fee="0", exchange="kraken", idempotency_digest=digest,
                on_claim=lambda db: db.add(evidence_for(request_id, quote)),
            )
            return result.price, result.operation_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(place, Decimal("101"))
        second = pool.submit(place, Decimal("102"))
        outcomes = [first.result(), second.result()]
    assert outcomes[0] == outcomes[1]
    with factory() as check:
        assert check.query(PaperRequest).count() == 1
        assert check.query(PaperMarketEvidence).count() == 1
        assert check.query(Operation).count() == 1
        evidence = check.get(PaperMarketEvidence, request_id)
        assert Decimal(evidence.price) == outcomes[0][0]


def test_closed_scenario_cannot_execute_after_quote_was_selected(file_db_engine):
    factory = sessionmaker(bind=file_db_engine, autoflush=False)
    with factory() as seed:
        create_paper_scenario(
            db=seed, quote_currency="EUR", initial_balance=Decimal("1000"),
            max_order_amount=Decimal("500"), max_fee_percentage=Decimal("2"),
            daily_limit=None, monthly_limit=None,
        )
    request_id = str(uuid4())
    quote = quote_from_snapshot(
        snapshot(status="fresh"), asset="BTC", quote_currency="EUR",
        side="BUY", requested_provider="kraken",
    )
    with factory() as stale, factory() as closer:
        scenario = get_paper_scenario(stale, "EUR")
        close_paper_scenario(closer, closer.get(type(scenario), scenario.id))
        result = execute_configured_paper_buy(
            db=stale, scenario=scenario, request_id=request_id,
            asset="BTC", order_amount="100", price=quote.price,
            estimated_fee="0", exchange="kraken",
            idempotency_digest=market_request_digest(
                side="BUY", asset="BTC", quote_currency="EUR",
                size=Decimal("100"), fee=Decimal("0"), provider="kraken",
            ),
            on_claim=lambda db: db.add(evidence_for(request_id, quote)),
        )
        assert result.executed is False
        assert result.reason == "Paper scenario closed"
    with factory() as check:
        assert check.query(Operation).count() == 0
        assert check.query(PaperRequest).count() == 1
        assert check.query(PaperMarketEvidence).count() == 1


def test_quote_failure_replays_concurrent_winner_instead_of_claiming_no_trade(
    file_db_engine, monkeypatch,
):
    factory = sessionmaker(bind=file_db_engine, autoflush=False)
    with factory() as seed:
        create_paper_scenario(
            db=seed, quote_currency="EUR", initial_balance=Decimal("1000"),
            max_order_amount=Decimal("500"), max_fee_percentage=Decimal("2"),
            daily_limit=None, monthly_limit=None,
        )
    body = buy_body()

    def override_db():
        with factory() as session:
            yield session

    main_module.app.dependency_overrides[get_db] = override_db

    def failed_after_winner(symbol, provider):
        quote = quote_from_snapshot(
            snapshot(status="fresh"), asset="BTC", quote_currency="EUR",
            side="BUY", requested_provider="kraken",
        )
        with factory() as winner:
            result = execute_configured_paper_buy(
                db=winner, scenario=get_paper_scenario(winner, "EUR"),
                request_id=body["request_id"], asset="BTC", order_amount="100",
                price=quote.price, estimated_fee="0", exchange="kraken",
                idempotency_digest=market_request_digest(
                    side="BUY", asset="BTC", quote_currency="EUR",
                    size=Decimal("100"), fee=Decimal("0"), provider="kraken",
                ),
                on_claim=lambda db: db.add(evidence_for(body["request_id"], quote)),
            )
            assert result.executed
        raise MarketQuoteUnavailable("Proveedor sin respuesta")

    monkeypatch.setattr(main_module, "market_snapshot", failed_after_winner)
    with TestClient(main_module.app) as client:
        response = client.post("/paper/market/buy", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["executed"] is True
    assert response.json()["quote"]["price"] == "101"
    with factory() as check:
        assert check.query(Operation).count() == 1
