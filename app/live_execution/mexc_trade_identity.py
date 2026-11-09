"""Inspección GET-only de una clave MEXC Trade, sin envío de órdenes.

La API local protegida expone sólo su diagnóstico de permisos e identidad,
no un transporte de órdenes. La UI permite esta verificación optativa sin
activar envíos ni conservar la clave tras bloquear.
"""

from __future__ import annotations

import re
import secrets
from typing import Callable

from app.integrations.mexc_readonly import MexcReadOnlyClient, MexcReadOnlyError


_TRADE_PERMISSIONS = frozenset(("SPOT_ACCOUNT_READ", "SPOT_DEAL_READ", "SPOT_DEAL_WRITE"))
_REQUIRED_PERMISSIONS = frozenset(("SPOT_ACCOUNT_READ", "SPOT_DEAL_WRITE"))
_UID = re.compile(r"[1-9][0-9]{0,31}\Z")


class MexcTradeIdentityError(RuntimeError):
    """Error saneado: no contiene claves, UID ni respuestas brutas."""


class MexcTradeIdentityProbe:
    """GET-only: comprueba una clave Trade sin dotarla de envío de órdenes."""

    def __init__(self, api_key: str, secret_key: str, *,
                 http_get: Callable[[str, dict[str, str]], dict | list] | None = None,
                 clock_ms: Callable[[], int] | None = None):
        options: dict = {}
        if http_get is not None:
            options["http_get"] = http_get
        if clock_ms is not None:
            options["clock_ms"] = clock_ms
        try:
            self._reader = MexcReadOnlyClient(api_key, secret_key, **options)
        except MexcReadOnlyError:
            raise MexcTradeIdentityError("Formato de credencial MEXC no válido") from None
        self._api_key = api_key

    def __repr__(self) -> str:
        return "MexcTradeIdentityProbe(<redacted>)"

    def account_uid(self) -> str:
        """Devuelve UID sólo al orquestador interno para comparación inmediata."""
        try:
            key_info = self._reader._signed_get("/api/v3/apiKeyInfo",
                                                 {"accessKey": self._api_key})
            if not isinstance(key_info, dict):
                raise MexcTradeIdentityError("MEXC no informó permisos verificables")
            if key_info.get("status") != "VALID":
                raise MexcTradeIdentityError("La clave MEXC Trade no está activa")
            raw = key_info.get("permissions")
            if isinstance(raw, str):
                permissions = frozenset(part.strip() for part in raw.split(",") if part.strip())
            elif isinstance(raw, list) and all(isinstance(part, str) for part in raw):
                permissions = frozenset(raw)
            else:
                raise MexcTradeIdentityError("MEXC no informó permisos verificables")
            if not _REQUIRED_PERMISSIONS <= permissions or not permissions <= _TRADE_PERMISSIONS:
                raise MexcTradeIdentityError("La clave MEXC Trade requiere sólo permisos Spot de lectura y trading")
            echoed = key_info.get("apikey", key_info.get("accessKey"))
            if echoed is not None and (not isinstance(echoed, str)
                                       or not secrets.compare_digest(echoed, self._api_key)):
                raise MexcTradeIdentityError("MEXC devolvió una clave distinta")
            validity = key_info.get("remainingValidity")
            if validity is not None and (type(validity) is not int
                                         or validity != -999 and validity <= 0):
                raise MexcTradeIdentityError("La clave MEXC Trade no tiene validez verificable")
            payload = self._reader._signed_get("/api/v3/uid")
            if not isinstance(payload, dict):
                raise MexcTradeIdentityError("MEXC no devolvió una identidad de cuenta verificable")
            uid = payload.get("uid")
            if not isinstance(uid, str) or _UID.fullmatch(uid) is None:
                raise MexcTradeIdentityError("MEXC no devolvió una identidad de cuenta verificable")
            return uid
        except MexcReadOnlyError:
            raise MexcTradeIdentityError("No se pudo verificar la clave MEXC Trade") from None


def verify_same_mexc_account(view_uid: str, trade_probe: MexcTradeIdentityProbe) -> bool:
    """Fail-closed; nunca devuelve ni registra ninguno de los UID."""
    if not isinstance(view_uid, str) or _UID.fullmatch(view_uid) is None:
        raise MexcTradeIdentityError("La identidad MEXC View no está verificada")
    trade_uid = trade_probe.account_uid()
    return secrets.compare_digest(view_uid, trade_uid)
