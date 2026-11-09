"""Barrera local-first para toda la API HTTP y WebSocket de ORBIS.

No autentica a un proceso local ni sustituye la bóveda REAL. Impide que un
bind accidental en una interfaz de red sirva la API anterior sin contraseña.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.responses import JSONResponse


CLIENT_HOSTS = frozenset(("127.0.0.1", "::1", "testclient", "testserver"))
REQUEST_HOSTS = CLIENT_HOSTS | frozenset(("localhost", "10.0.2.2"))


def _authority(raw: str) -> tuple[str, int | None] | None:
    if not raw or any(char in raw for char in "/\\@?# \t\r\n"):
        return None
    try:
        parsed = urlsplit("//" + raw)
        if parsed.username or parsed.password or not parsed.hostname:
            return None
        return parsed.hostname.lower(), parsed.port
    except ValueError:
        return None


def _allowed(scope: dict) -> bool:
    client = scope.get("client")
    if not client or client[0] not in CLIENT_HOSTS:
        return False
    headers = {}
    for name, value in scope.get("headers", []):
        lower = name.lower()
        if lower in (b"host", b"origin") and lower in headers:
            return False
        headers[lower] = value.decode("latin-1")
    host = _authority(headers.get(b"host", ""))
    if host is None or host[0] not in REQUEST_HOSTS:
        return False
    origin = headers.get(b"origin")
    if origin is None:
        return True
    try:
        parsed = urlsplit(origin)
        return (parsed.scheme in ("http", "https") and not parsed.username and not parsed.password
                and not parsed.path and not parsed.query and not parsed.fragment
                and (parsed.hostname, parsed.port) == host)
    except ValueError:
        return False


class LocalLoopbackMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or _allowed(scope):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        response = JSONResponse({"detail": "Orbis API is local-only"}, status_code=403,
                                headers={"Cache-Control": "no-store"})
        await response(scope, receive, send)
