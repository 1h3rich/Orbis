"""Un proveedor público lento no debe congelar la API local de Orbis."""

from threading import Event, Thread

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.web.mount import register_ui


def test_slow_websocket_snapshot_does_not_block_other_requests(monkeypatch):
    started = Event()
    release = Event()
    health_done = Event()
    errors = []
    statuses = []
    snapshots = []

    def slow_snapshot(symbol, provider=None):
        started.set()
        if not release.wait(5):
            raise RuntimeError("Timed out waiting for the test to release the provider")
        return {"symbol": symbol, "available": True, "price": "100"}

    monkeypatch.setattr("app.marketdata.service.snapshot", slow_snapshot)
    app = FastAPI()
    register_ui(app)

    @app.get("/__test_health")
    async def health():
        return {"ok": True}

    with TestClient(app) as client:
        def receive_snapshot():
            try:
                with client.websocket_connect("/market/stream?symbol=BTC/EUR&provider=kraken") as websocket:
                    snapshots.append(websocket.receive_json())
            except Exception as exc:
                errors.append(exc)

        def read_health():
            try:
                statuses.append(client.get("/__test_health").status_code)
            except Exception as exc:
                errors.append(exc)
            finally:
                health_done.set()

        ws_thread = Thread(target=receive_snapshot, daemon=True)
        health_thread = Thread(target=read_health, daemon=True)
        ws_thread.start()
        try:
            assert started.wait(2), "El WebSocket no inició la consulta pública"
            health_thread.start()
            responsive = health_done.wait(1)
        finally:
            release.set()
            ws_thread.join(timeout=3)
            if health_thread.ident is not None:
                health_thread.join(timeout=3)

    assert responsive, "Una consulta externa bloqueó el event loop y la API local"
    assert not ws_thread.is_alive() and not health_thread.is_alive()
    assert statuses == [200]
    assert snapshots == [{"symbol": "BTC/EUR", "available": True, "price": "100"}]
    assert not errors, errors
