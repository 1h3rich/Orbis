"""La API v1 no debe quedar expuesta por un bind accidental en LAN."""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import app
from app.security.loopback import _allowed


def test_remote_client_cannot_read_write_ui_or_open_websocket():
    with TestClient(app, client=("192.0.2.25", 43210)) as remote:
        for path in ("/", "/operations", "/portfolio/buys", "/ui/"):
            response = remote.get(path)
            assert response.status_code == 403
            assert response.json()["detail"] == "Orbis API is local-only"
        response = remote.post("/operations", json={})
        assert response.status_code == 403
        with pytest.raises(WebSocketDisconnect) as denied:
            with remote.websocket_connect("/market/stream?symbol=BTC/USD"):
                pass
        assert denied.value.code == 1008


def test_host_and_origin_must_be_local_and_same_origin(client):
    assert client.get("/").status_code == 200
    assert client.get("/", headers={"Origin": "http://testserver"}).status_code == 200
    assert client.get("/", headers={"Host": "remote.example"}).status_code == 403
    assert client.get("/", headers={"Origin": "https://outside.example"}).status_code == 403
    assert client.get("/", headers={"Origin": "null"}).status_code == 403
    assert client.get("/", headers={"Host": "10.0.2.2"}).status_code == 200
    # La bóveda REAL conserva su frontera propia; no acepta el alias del emulador.
    assert client.get("/real/vault/status", headers={"Host": "10.0.2.2"}).status_code == 403


def test_ipv6_and_malformed_authorities():
    def scope(host, origin=None):
        headers = [(b"host", host.encode())]
        if origin is not None:
            headers.append((b"origin", origin.encode()))
        return {"type": "http", "client": ("::1", 12345), "headers": headers}

    assert _allowed(scope("[::1]:8000", "http://[::1]:8000"))
    assert not _allowed(scope("outside.example:8000"))
    assert not _allowed(scope("localhost@outside.example"))
    assert not _allowed(scope("localhost:8000#outside.example"))
    assert not _allowed(scope("localhost:bad"))
    assert not _allowed(scope("localhost:8000", "http://localhost:9000"))
    duplicated = scope("localhost:8000")
    duplicated["headers"].append((b"host", b"outside.example"))
    assert not _allowed(duplicated)
