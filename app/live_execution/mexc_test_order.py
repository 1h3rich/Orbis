"""Prueba firmada MEXC Spot que no llega al motor de casación.

Sólo conoce POST /api/v3/order/test. No contiene la ruta de orden real ni
acepta una URL arbitraria. La credencial permanece en memoria del objeto.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
import hashlib
import hmac
import time
from typing import Callable
from urllib.parse import urlencode
from uuid import UUID

import httpx

from app.live_execution.journal import RecordedIntent
from app.live_execution.mexc_instrument import MexcPreflight, MEXC_BASE
from app.live_execution.policy import LiveSpotIntent, LiveSpotPolicy, sell_base_reservation


_TEST_PATH = "/api/v3/order/test"


class MexcOrderTestError(RuntimeError):
    """Error saneado; nunca incluye clave, firma ni respuesta bruta."""


@dataclass(frozen=True, slots=True)
class MexcOrderTestResult:
    intent_id: str
    client_order_id: str
    accepted_by_test_endpoint: bool
    executed: bool = False


def _http_post(url: str, body: str, headers: dict[str, str]) -> object:
    try:
        response = httpx.post(url, content=body, headers=headers, timeout=5.0,
                              follow_redirects=False, trust_env=False)
    except httpx.HTTPError:
        raise MexcOrderTestError("No se pudo probar la orden en MEXC") from None
    if response.status_code == 429:
        raise MexcOrderTestError("MEXC limita las pruebas de órdenes; espera antes de reintentar")
    if response.status_code in (401, 403):
        raise MexcOrderTestError("MEXC rechazó la credencial o sus permisos")
    if response.status_code != 200:
        raise MexcOrderTestError("MEXC rechazó la prueba de orden")
    try:
        return response.json()
    except ValueError:
        raise MexcOrderTestError("MEXC devolvió una prueba de orden inválida") from None


class MexcSpotOrderTestClient:
    def __init__(self, api_key: str, secret_key: str, *,
                 http_post: Callable[[str, str, dict[str, str]], object] = _http_post,
                 clock_ms: Callable[[], int] | None = None):
        if (not isinstance(api_key, str) or not isinstance(secret_key, str)
                or not 8 <= len(api_key) <= 128 or not 8 <= len(secret_key) <= 256
                or not api_key.isascii() or not secret_key.isascii()
                or any(char.isspace() for char in api_key + secret_key)):
            raise MexcOrderTestError("Formato de credencial MEXC no válido")
        self._api_key = api_key
        self._secret_key = secret_key
        self._http_post = http_post
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)

    def __repr__(self) -> str:
        return "MexcSpotOrderTestClient(<redacted>)"

    def test_reserved_limit(self, intent: LiveSpotIntent, recorded: RecordedIntent,
                            preflight: MexcPreflight) -> MexcOrderTestResult:
        """Valida contra order/test, nunca contra /order ni el matching engine."""
        if (recorded.intent_id != intent.intent_id or recorded.status != "RESERVED"
                or not preflight.allowed or preflight.reason != "LOCAL_PREFLIGHT_ONLY"):
            raise MexcOrderTestError("La intención no tiene reserva y preflight válidos")
        if (intent.side == "BUY" and recorded.reserved_quote <= 0
                or intent.side == "SELL" and recorded.reserved_base != sell_base_reservation(intent)):
            raise MexcOrderTestError("La reserva no coincide con la intención")
        return self._send_test(intent)

    def test_draft_buy(self, policy: LiveSpotPolicy, intent: LiveSpotIntent,
                       preflight: MexcPreflight) -> MexcOrderTestResult:
        """Ensayo de un borrador BUY, sin reserva ni permiso de ejecución real."""
        if (policy.enabled or intent.side != "BUY" or intent.policy_id != policy.policy_id
                or intent.account_ref != policy.account_ref
                or intent.strategy_version_id != policy.strategy_version_id
                or intent.symbol != policy.symbol or not preflight.allowed
                or preflight.reason != "LOCAL_PREFLIGHT_ONLY"):
            raise MexcOrderTestError("El borrador no tiene preflight válido")
        with localcontext() as context:
            context.prec = 300
            notional = intent.quantity_base * intent.limit_price_quote
            if (notional > policy.max_order_quote or notional > policy.max_daily_turnover_quote
                    or notional > policy.max_exposure_quote
                    or intent.estimated_fee_quote * Decimal(10000) > notional * policy.max_fee_bps):
                raise MexcOrderTestError("La prueba supera los límites del borrador")
        return self._send_test(intent)

    def _send_test(self, intent: LiveSpotIntent) -> MexcOrderTestResult:
        timestamp = self._clock_ms()
        if type(timestamp) is not int or timestamp <= 0:
            raise MexcOrderTestError("Reloj local no válido para MEXC")
        client_order_id = UUID(intent.intent_id).hex
        items = [
            ("symbol", intent.symbol), ("side", intent.side), ("type", "LIMIT"),
            ("quantity", format(intent.quantity_base, "f")),
            ("price", format(intent.limit_price_quote, "f")),
            ("newClientOrderId", client_order_id),
            ("recvWindow", "5000"), ("timestamp", str(timestamp)),
        ]
        body = urlencode(items)
        signature = hmac.new(self._secret_key.encode("ascii"), body.encode("ascii"),
                             hashlib.sha256).hexdigest()
        signed_body = body + "&signature=" + signature
        try:
            payload = self._http_post(
                MEXC_BASE + _TEST_PATH, signed_body,
                {"X-MEXC-APIKEY": self._api_key, "Content-Type": "application/json"},
            )
        except MexcOrderTestError:
            raise
        except Exception:
            raise MexcOrderTestError("No se pudo probar la orden en MEXC") from None
        if payload != {}:
            raise MexcOrderTestError("MEXC no confirmó la prueba de orden")
        return MexcOrderTestResult(intent.intent_id, client_order_id, True)
