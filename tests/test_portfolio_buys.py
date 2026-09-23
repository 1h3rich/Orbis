from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.database import Base, get_db
from app.ledger.repository import create_operation
from app.main import app
from app.math.calculations import calculate_buy_summaries, calculate_portfolio_summary


class Buy:
    def __init__(self, mode, asset, quote_currency, amount, units, fee):
        self.operation_type = "BUY"
        self.status = "EXECUTED"
        self.mode = mode
        self.asset = asset
        self.quote_currency = quote_currency
        self.amount_spent = Decimal(amount)
        self.asset_received = Decimal(units)
        self.trading_fee = Decimal(fee)
        self.withdrawal_fee = Decimal("0")
        self.network_fee = Decimal("0")


def test_buy_summaries_separate_modes_and_currency_pairs():
    operations = [
        Buy("PAPER", "BTC", "EUR", "100", "0.001", "1"),
        Buy("PAPER", "BTC", "EUR", "200", "0.002", "2"),
        Buy("PAPER", "BTC", "USD", "50", "0.0005", "0.5"),
        Buy("LIVE", "BTC", "EUR", "700", "0.007", "7"),
    ]

    summaries = calculate_buy_summaries(operations, mode="PAPER")

    assert summaries == [
        {
            "asset": "BTC",
            "quote_currency": "EUR",
            "total_invested": Decimal("300"),
            "total_asset_received": Decimal("0.003"),
            "total_fees": Decimal("3"),
            "average_buy_price": Decimal("100000"),
        },
        {
            "asset": "BTC",
            "quote_currency": "USD",
            "total_invested": Decimal("50"),
            "total_asset_received": Decimal("0.0005"),
            "total_fees": Decimal("0.5"),
            "average_buy_price": Decimal("100000"),
        },
    ]


def test_legacy_portfolio_summary_rejects_mixed_pairs():
    operations = [
        Buy("PAPER", "BTC", "EUR", "100", "0.001", "1"),
        Buy("PAPER", "ETH", "EUR", "50", "0.01", "0.5"),
    ]

    with pytest.raises(ValueError, match="multiple asset/currency pairs"):
        calculate_portfolio_summary(operations)


def test_portfolio_api_requires_one_pair_and_separates_modes():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)

    with Session() as db:
        for mode, asset, currency, amount in (
            ("PAPER", "BTC", "EUR", "100"),
            ("PAPER", "ETH", "EUR", "50"),
            ("LIVE", "BTC", "EUR", "700"),
        ):
            create_operation(
                db=db,
                operation_type="BUY",
                asset=asset,
                quote_currency=currency,
                amount_spent=Decimal(amount),
                asset_received=Decimal("0.001"),
                price=Decimal("100000"),
                mode=mode,
            )

    def override_get_db():
        with Session() as db:
            yield db

    previous_override = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            summaries = client.get("/portfolio/buys?mode=PAPER")
            ambiguous = client.get("/portfolio/summary?mode=PAPER")
            live = client.get("/portfolio/summary?mode=LIVE")
            btc = client.get("/portfolio/summary?mode=PAPER&asset=BTC")

        assert summaries.status_code == 200
        assert [item["asset"] for item in summaries.json()] == ["BTC", "ETH"]
        assert ambiguous.status_code == 400
        assert live.status_code == 200
        assert Decimal(str(live.json()["total_invested"])) == Decimal("700")
        assert Decimal(str(btc.json()["total_invested"])) == Decimal("100")
    finally:
        if previous_override is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous_override
        engine.dispose()
