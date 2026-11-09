"""Cotización PAPER controlada para recorridos de gráficos sin red externa."""


def mock_preview(route):
    route.fulfill(status=200, json={
        "available": True, "symbol": "BTC/EUR", "provider": "kraken",
        "price": "123", "buy_price": "123", "sell_price": "123",
        "source": "kraken", "age_seconds": 1, "stale": False,
    })
