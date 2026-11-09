"""GRVT es datos públicos puntuales; el PAPER spot no lo suplanta."""

from pathlib import Path

import pytest
from starlette.websockets import WebSocketDisconnect


@pytest.mark.parametrize("provider", ["grvt", "auto"])
def test_grvt_stream_routes_send_only_one_snapshot(client, monkeypatch, provider):
    from app.marketdata import service

    calls = []

    def fake_snapshot(symbol, provider=None):
        calls.append((symbol, provider))
        return {"symbol": symbol, "available": True, "price": "100", "resolved_provider": "grvt"}

    monkeypatch.setattr(service, "snapshot", fake_snapshot)
    with client.websocket_connect(f"/market/stream?symbol=BTC%2FUSDT%3APERP&provider={provider}") as ws:
        assert ws.receive_json()["price"] == "100"
        try:
            ws.receive_json()
        except WebSocketDisconnect:
            pass
        else:
            raise AssertionError("El WS GRVT no debe sondear REST repetidamente")
    assert len(calls) == 1

    calls.clear()
    response = client.get(f"/market/stream-sse?symbol=BTC%2FUSDT%3APERP&provider={provider}&rounds=3")
    assert response.status_code == 200
    assert response.text.count("data: ") == 1
    assert len(calls) == 1


def test_grvt_terminal_keeps_derivative_trading_disabled():
    root = Path(__file__).resolve().parents[1]
    source = (root / "app/web/static/estudio.js").read_text(encoding="utf-8")
    assert 'provider === "grvt" || market.endsWith(":PERP")' in source
    assert "operativa PAPER LONG/SHORT pendiente" in source
    assert 'if (provider === "grvt" || market.endsWith(":PERP")) return;' in source
