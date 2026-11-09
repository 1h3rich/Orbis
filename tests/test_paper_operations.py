"""GET /paper/operations: filtros, orden (timestamp, id) e historial del Ledger."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.database.models import Operation
from app.ledger.repository import create_operation

def seed(db, **overrides):
    """Inserta una operación directamente en el Ledger (para controlar fecha y modo)."""
    body = {
        "operation_type": "BUY",
        "asset": "BTC",
        "quote_currency": "EUR",
        "amount_spent": Decimal("100"),
        "asset_received": Decimal("0.001"),
        "price": Decimal("100000"),
        "mode": "PAPER",
    }
    body.update(overrides)
    operation = create_operation(db=db, **body)
    db.commit()
    return operation


def test_operations_are_ordered_by_timestamp_then_id(client, db):
    first = seed(db, amount_spent=Decimal("10"))
    second = seed(db, amount_spent=Decimal("20"))
    third = seed(db, amount_spent=Decimal("30"))
    # Misma marca de tiempo para todos los anteriores
    same_instant = seed(db, amount_spent=Decimal("40"))
    second.timestamp = first.timestamp
    third.timestamp = first.timestamp
    same_instant.timestamp = first.timestamp
    db.commit()

    response = client.get("/paper/operations")
    assert response.status_code == 200
    ids = [item["id"] for item in response.json()]
    assert ids == [first.id, second.id, third.id, same_instant.id]

    # Una operación antigua encabeza la lista aunque se insertó la última
    first.timestamp = first.timestamp - timedelta(days=1)
    db.commit()
    ordered = [item["id"] for item in client.get("/paper/operations").json()]
    assert ordered[0] == first.id


def test_filters_by_asset_quote_and_side(client, db):
    seed(db, asset="BTC", quote_currency="EUR")
    seed(db, asset="ETH", quote_currency="EUR")
    seed(db, asset="BTC", quote_currency="USD")
    seed(db, operation_type="SELL", asset="BTC", quote_currency="EUR")

    by_asset = client.get("/paper/operations?asset=ETH").json()
    by_quote = client.get("/paper/operations?quote=USD").json()
    by_quote_alias = client.get("/paper/operations?quote_currency=USD").json()
    by_side = client.get("/paper/operations?side=SELL").json()
    by_both = client.get("/paper/operations?asset=BTC&side=BUY").json()

    assert [item["asset"] for item in by_asset] == ["ETH"]
    assert [item["quote_currency"] for item in by_quote] == ["USD"]
    assert [item["quote_currency"] for item in by_quote_alias] == ["USD"]
    assert [item["operation_type"] for item in by_side] == ["SELL"]
    assert len(by_both) == 2
    assert {item["operation_type"] for item in by_both} == {"BUY"}


def test_filters_by_day_and_excludes_other_days(client, db):
    # Ambos registros usan el mismo reloj fijo: la suite puede cruzar medianoche.
    day = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
    old = seed(db, amount_spent=Decimal("50"))
    old.timestamp = day - timedelta(days=1)
    recent = seed(db, amount_spent=Decimal("70"))
    recent.timestamp = day
    db.commit()

    today = day.strftime("%Y-%m-%d")
    yesterday = (day - timedelta(days=1)).strftime("%Y-%m-%d")

    today_items = client.get(f"/paper/operations?date={today}").json()
    yesterday_items = client.get(f"/paper/operations?date={yesterday}").json()

    assert len(today_items) == 1
    assert Decimal(str(today_items[0]["amount_spent"])) == Decimal("70")
    assert [item["id"] for item in yesterday_items] == [old.id]


def test_rejects_malformed_date(client):
    response = client.get("/paper/operations?date=05-10-2026")
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid date; expected YYYY-MM-DD"


def test_chart_interval_filters_market_without_silent_200_operation_cutoff(client, db):
    base = datetime(2026, 10, 10, tzinfo=timezone.utc)
    for index in range(205):
        item = seed(db, asset="BTC", quote_currency="EUR")
        item.timestamp = base + timedelta(minutes=index)
    outside = seed(db, asset="BTC", quote_currency="EUR")
    outside.timestamp = base + timedelta(hours=4)
    other_pair = seed(db, asset="ETH", quote_currency="EUR")
    other_pair.timestamp = base + timedelta(minutes=3)
    db.commit()

    response = client.get("/paper/operations", params={
        "asset": "BTC", "quote": "EUR",
        "start_at": base.isoformat(),
        "end_before": (base + timedelta(hours=4)).isoformat(),
        "order": "asc",
    })
    assert response.status_code == 200
    items = response.json()
    assert len(items) == 205
    assert all(item["asset"] == "BTC" and item["quote_currency"] == "EUR" for item in items)
    assert items[0]["timestamp"].startswith("2026-10-10T00:00:00")
    assert items[-1]["timestamp"].startswith("2026-10-10T03:24:00")

    offset = client.get("/paper/operations", params={
        "asset": "BTC", "quote": "EUR",
        "start_at": "2026-10-10T04:00:00+02:00",
        "end_before": "2026-10-10T04:02:00+02:00",
    })
    assert offset.status_code == 200
    assert [item["timestamp"] for item in offset.json()] == [
        (base + timedelta(hours=2, minutes=minute)).replace(tzinfo=None).isoformat()
        for minute in range(2)
    ]


def test_chart_interval_requires_timezone_and_order(client):
    assert client.get("/paper/operations?start_at=2026-10-10T00:00:00").status_code == 400
    assert client.get("/paper/operations?end_before=2026-10-10T01:00:00").status_code == 400
    response = client.get("/paper/operations", params={
        "start_at": "2026-10-11T00:00:00Z",
        "end_before": "2026-10-10T00:00:00Z",
    })
    assert response.status_code == 400


def test_filters_by_strategy_id(client, db):
    from app.database.models import Strategy

    strategy = Strategy(name="DCA", strategy_type="DCA", enabled=True, config={})
    db.add(strategy)
    db.commit()
    db.refresh(strategy)

    seed(db, strategy_id=strategy.id)
    seed(db)

    response = client.get(f"/paper/operations?strategy_id={strategy.id}")

    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["strategy_id"] == strategy.id


def test_strategy_with_paper_operations_cannot_be_deleted(client, db, db_engine):
    """La operación conserva su estrategia con las FK activas por defecto."""
    from app.database.models import Strategy

    with db_engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1

    strategy = Strategy(name="Historial", strategy_type="DCA", enabled=True, config={})
    db.add(strategy)
    db.commit()
    db.refresh(strategy)
    operation = seed(db, strategy_id=strategy.id)

    response = client.delete(f"/strategies/{strategy.id}")

    assert response.status_code == 409
    assert "desactívala" in response.json()["detail"]
    assert client.get(f"/strategies/{strategy.id}").status_code == 200
    history = client.get(f"/paper/operations?strategy_id={strategy.id}").json()
    assert [row["id"] for row in history] == [operation.id]
    with db_engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []


def test_sqlite_rejects_new_orphan_operation(db, db_engine):
    """El bloqueo está en SQLite, no sólo en la validación de una ruta HTTP."""
    with db_engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1

    with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
        seed(db, strategy_id=999999)
    db.rollback()
    assert db.query(Operation).count() == 0


def test_excludes_live_operations(client, db):
    seed(db, mode="LIVE", asset="LIVEBTC")
    seed(db, mode="PAPER", asset="BTC")

    paper = client.get("/paper/operations").json()
    legacy = client.get("/operations").json()

    assert [item["asset"] for item in paper] == ["BTC"]
    assert [item["asset"] for item in legacy] == ["BTC"]


def test_unknown_filters_return_empty_list(client, create_scenario):
    create_scenario()
    response = client.get("/paper/operations?asset=DOGE&side=BUY")

    assert response.status_code == 200
    assert response.json() == []


def test_invalid_side_is_rejected(client):
    response = client.get("/paper/operations?side=HOLD")
    assert response.status_code == 422


def test_buys_and_sells_from_the_api_are_listed(client, create_scenario):
    from uuid import uuid4

    create_scenario()
    client.post("/paper/buy", json={
        "request_id": str(uuid4()), "asset": "BTC", "quote_currency": "EUR",
        "order_amount": "50", "price": "100000", "estimated_fee": "0",
    })
    client.post("/paper/sell", json={
        "request_id": str(uuid4()), "asset": "BTC", "quote_currency": "EUR",
        "quantity": "0.0002", "price": "100000", "fee": "0",
    })

    items = client.get("/paper/operations").json()

    assert [item["operation_type"] for item in items] == ["BUY", "SELL"]
    assert all(item["mode"] == "PAPER" for item in items)
    assert all(item["status"] == "EXECUTED" for item in items)
    assert Decimal(str(items[1]["amount_spent"])) == Decimal("20")
