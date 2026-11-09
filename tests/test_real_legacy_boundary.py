from fastapi.testclient import TestClient
from decimal import Decimal

from app.main import app
from app.ledger.repository import create_operation


def test_legacy_operations_cannot_write_unencrypted_live_record():
    response = TestClient(app).post(
        "/operations",
        json={
            "operation_type": "BUY", "asset": "BTC", "quote_currency": "EUR",
            "amount_spent": "10", "asset_received": "0.1", "price": "100",
            "mode": "LIVE",
        },
    )
    assert response.status_code == 409
    assert "unencrypted" in response.json()["detail"]


def test_unencrypted_api_never_returns_legacy_live_values(client, db):
    live = create_operation(
        db=db, operation_type="BUY", asset="SECRETCOIN", quote_currency="EUR",
        amount_spent=Decimal("987654.321"), asset_received=Decimal("3"),
        price=Decimal("329218.107"), mode="LIVE",
    )
    paper = create_operation(
        db=db, operation_type="BUY", asset="BTC", quote_currency="EUR",
        amount_spent=Decimal("20"), asset_received=Decimal("0.2"),
        price=Decimal("100"), mode="PAPER",
    )
    db.commit()
    rows = client.get("/operations")
    assert rows.status_code == 200
    assert [row["id"] for row in rows.json()] == [paper.id]
    for path in ("/portfolio/summary?mode=LIVE", "/portfolio/buys?mode=LIVE"):
        response = client.get(path)
        assert response.status_code == 410
        assert "SECRETCOIN" not in response.text
        assert "987654" not in response.text
    assert client.get("/portfolio/buys?mode=PAPER").json()[0]["asset"] == "BTC"
    assert db.get(type(live), live.id) is not None  # Se oculta, no se borra ni migra.
