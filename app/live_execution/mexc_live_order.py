"""Transporte MEXC Spot LIMIT real, aislado y sin acceso desde la API/UI.

Un POST fallido puede haberse ejecutado: el llamante debe haber marcado de
forma persistente el intento antes de invocar este transporte y no reintentar.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import re
import time
from typing import Callable
from urllib.parse import urlencode
from uuid import UUID

import httpx

from app.live_execution.mexc_trade_identity import (
    MexcTradeIdentityError, MexcTradeIdentityProbe, verify_same_mexc_account,
)
from app.live_execution.mexc_test_order import (
    MexcOrderTestError, MexcOrderTestResult, MexcSpotOrderTestClient,
)
from app.live_execution.journal import RecordedIntent
from app.live_execution.mexc_instrument import MexcPreflight
from app.live_execution.policy import LiveSpotIntent


_ORDER_URL = "https://api.mexc.com/api/v3/order"
_ORDER_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


class MexcOrderSendError(RuntimeError):
    """Resultado de envío no fiable; nunca contiene credenciales o respuesta bruta."""


@dataclass(frozen=True, slots=True)
class MexcOrderAcknowledgement:
    intent_id: str
    client_order_id: str
    external_order_id: str


@dataclass(frozen=True, slots=True)
class MexcCancelAcknowledgement:
    intent_id: str
    client_order_id: str
    external_order_id: str
    external_status: str


def _http_post(url: str, body: str, headers: dict[str, str]) -> object:
    try:
        response = httpx.post(url, content=body, headers=headers, timeout=5.0,
                              follow_redirects=False, trust_env=False)
    except httpx.HTTPError:
        raise MexcOrderSendError("Resultado del POST MEXC incierto; consultar la orden, nunca reintentar") from None
    if response.status_code != 200:
        raise MexcOrderSendError("Resultado del POST MEXC incierto; consultar la orden, nunca reintentar")
    try:
        return response.json()
    except ValueError:
        raise MexcOrderSendError("Respuesta de orden MEXC no verificable; nunca reintentar") from None


def _http_delete(url: str, headers: dict[str, str]) -> object:
    try:
        response = httpx.delete(url, headers=headers, timeout=5.0,
                                follow_redirects=False, trust_env=False)
    except httpx.HTTPError:
        raise MexcOrderSendError("Resultado de cancelación MEXC incierto; consultar la orden") from None
    if response.status_code != 200:
        raise MexcOrderSendError("Resultado de cancelación MEXC incierto; consultar la orden")
    try:
        return response.json()
    except ValueError:
        raise MexcOrderSendError("Respuesta de cancelación MEXC no verificable; consultar la orden") from None


def _decimal(raw: object) -> Decimal | None:
    if not isinstance(raw, str) or len(raw) > 100:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    return value if value.is_finite() and value > 0 else None


class MexcSpotLiveClient:
    """Clave Trade en memoria; sin retiros, transferencias ni endpoint genérico."""

    def __init__(self, api_key: str, secret_key: str, *,
                 http_get: Callable | None = None,
                 http_post: Callable[[str, str, dict[str, str]], object] = _http_post,
                 http_delete: Callable[[str, dict[str, str]], object] = _http_delete,
                 clock_ms: Callable[[], int] | None = None):
        options = {}
        if http_get is not None:
            options["http_get"] = http_get
        if clock_ms is not None:
            options["clock_ms"] = clock_ms
        try:
            self._probe = MexcTradeIdentityProbe(api_key, secret_key, **options)
        except MexcTradeIdentityError:
            raise MexcOrderSendError("Formato de credencial MEXC Trade no válido") from None
        self._api_key = api_key
        self._secret_key = secret_key
        self._http_post = http_post
        self._http_delete = http_delete
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._paired_until = 0.0
        self._tester = MexcSpotOrderTestClient(
            api_key, secret_key, http_post=http_post, clock_ms=self._clock_ms,
        )

    def __repr__(self) -> str:
        return "MexcSpotLiveClient(<redacted>)"

    def pair_view_account(self, view_uid: str) -> None:
        """Comprueba permisos mínimos y UID idéntico sin persistirlo."""
        try:
            same = verify_same_mexc_account(view_uid, self._probe)
        except MexcTradeIdentityError:
            raise MexcOrderSendError("No se pudo verificar identidad y permisos de la clave Trade") from None
        if not same:
            raise MexcOrderSendError("La clave Trade no pertenece a la cuenta View")
        self._paired_until = time.monotonic() + 30

    def test_reserved_limit(self, intent: LiveSpotIntent, recorded: RecordedIntent,
                            preflight: MexcPreflight) -> MexcOrderTestResult:
        if time.monotonic() >= self._paired_until:
            raise MexcOrderSendError("La identidad Trade/View ya no está verificada")
        try:
            return self._tester.test_reserved_limit(intent, recorded, preflight)
        except MexcOrderTestError:
            raise MexcOrderSendError("MEXC no validó la orden de prueba") from None

    def place_limit_order(self, intent: LiveSpotIntent) -> MexcOrderAcknowledgement:
        """Un único POST; cualquier respuesta dudosa exige observación externa."""
        if not isinstance(intent, LiveSpotIntent):
            raise MexcOrderSendError("La intención de orden no es válida")
        if time.monotonic() >= self._paired_until:
            raise MexcOrderSendError("La identidad Trade/View ya no está verificada")
        timestamp = self._clock_ms()
        if type(timestamp) is not int or timestamp <= 0:
            raise MexcOrderSendError("Reloj local no válido para MEXC")
        client_order_id = UUID(intent.intent_id).hex
        body = urlencode([
            ("symbol", intent.symbol), ("side", intent.side), ("type", "LIMIT"),
            ("quantity", format(intent.quantity_base, "f")),
            ("price", format(intent.limit_price_quote, "f")),
            ("newClientOrderId", client_order_id),
            ("recvWindow", "5000"), ("timestamp", str(timestamp)),
        ])
        signature = hmac.new(self._secret_key.encode("ascii"), body.encode("ascii"),
                             hashlib.sha256).hexdigest()
        try:
            response = self._http_post(
                _ORDER_URL, body + "&signature=" + signature,
                {"X-MEXC-APIKEY": self._api_key,
                 "Content-Type": "application/json"},
            )
        except MexcOrderSendError:
            raise
        except Exception:
            raise MexcOrderSendError("Resultado del POST MEXC incierto; consultar la orden, nunca reintentar") from None
        if not isinstance(response, dict):
            raise MexcOrderSendError("Respuesta de orden MEXC no verificable; nunca reintentar")
        order_id = response.get("orderId")
        if type(order_id) is int:
            order_id = str(order_id)
        if (not isinstance(order_id, str) or not _ORDER_ID.fullmatch(order_id)
                or response.get("symbol") != intent.symbol
                or response.get("side") != intent.side
                or response.get("type") != "LIMIT"
                or response.get("clientOrderId", client_order_id) != client_order_id
                or _decimal(response.get("price")) != intent.limit_price_quote
                or _decimal(response.get("origQty")) != intent.quantity_base):
            raise MexcOrderSendError("Respuesta de orden MEXC no verificable; nunca reintentar")
        return MexcOrderAcknowledgement(intent.intent_id, client_order_id, order_id)

    def cancel_limit_order(self, intent: LiveSpotIntent) -> MexcCancelAcknowledgement:
        """Cancela sólo el ID cliente de esta intención; el resultado exige reobservación.

        El coordinador exige parada, observación propia abierta y registro
        previo del intento antes de llamar a este transporte.
        """
        if not isinstance(intent, LiveSpotIntent):
            raise MexcOrderSendError("La intención de orden no es válida")
        if time.monotonic() >= self._paired_until:
            raise MexcOrderSendError("La identidad Trade/View ya no está verificada")
        timestamp = self._clock_ms()
        if type(timestamp) is not int or timestamp <= 0:
            raise MexcOrderSendError("Reloj local no válido para MEXC")
        client_order_id = UUID(intent.intent_id).hex
        query = urlencode([
            ("symbol", intent.symbol), ("origClientOrderId", client_order_id),
            ("recvWindow", "5000"), ("timestamp", str(timestamp)),
        ])
        signature = hmac.new(self._secret_key.encode("ascii"), query.encode("ascii"),
                             hashlib.sha256).hexdigest()
        try:
            response = self._http_delete(
                _ORDER_URL + "?" + query + "&signature=" + signature,
                {"X-MEXC-APIKEY": self._api_key},
            )
        except MexcOrderSendError:
            raise
        except Exception:
            raise MexcOrderSendError("Resultado de cancelación MEXC incierto; consultar la orden") from None
        if not isinstance(response, dict):
            raise MexcOrderSendError("Respuesta de cancelación MEXC no verificable; consultar la orden")
        order_id = response.get("orderId")
        if type(order_id) is int:
            order_id = str(order_id)
        status = response.get("status")
        if (not isinstance(order_id, str) or not _ORDER_ID.fullmatch(order_id)
                or response.get("symbol") != intent.symbol
                or response.get("origClientOrderId") != client_order_id
                or response.get("side") != intent.side
                or response.get("type") != "LIMIT"
                or _decimal(response.get("price")) != intent.limit_price_quote
                or _decimal(response.get("origQty")) != intent.quantity_base
                or status not in ("CANCELED", "PARTIALLY_CANCELED")):
            raise MexcOrderSendError("Respuesta de cancelación MEXC no verificable; consultar la orden")
        return MexcCancelAcknowledgement(intent.intent_id, client_order_id, order_id, status)
