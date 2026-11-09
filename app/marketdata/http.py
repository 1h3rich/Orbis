"""HTTP stdlib compartido por los adapters (urllib, timeout 5s, sin claves)."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

HTTP_TIMEOUT = 5.0
_USER_AGENT = "Orbis-PAPER-valuation (public market data, no keys)"


def http_get_json(url: str, timeout: float = HTTP_TIMEOUT) -> dict | list:
    """GET JSON con stdlib. Lanza en caso de fallo (el adapter lo captura)."""
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def http_post_json(url: str, payload: dict, timeout: float = HTTP_TIMEOUT) -> dict | list:
    """POST JSON público; sin credenciales ni reintentos implícitos."""
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"User-Agent": _USER_AGENT, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def qs(params: dict) -> str:
    """Querystring URL-encoded."""
    return urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
