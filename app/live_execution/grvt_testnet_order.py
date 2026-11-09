"""Transporte interno de una orden GRVT *testnet* ya firmada.

No firma, no conserva secretos ni acepta endpoints configurables. El puente
local confirmado y su UI verifican una firma EIP-712 de un firmante testnet;
GRVT comprueba criptográficamente la firma. La respuesta no acredita fills.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
import json
import re
import time
from threading import Lock
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from app.live_execution.grvt_testnet_protection import (
        GrvtTestnetTrigger, GrvtTestnetProtectionObservation,
    )

import httpx

from app.integrations.grvt_readonly import _order as _parse_grvt_order


AUTH_URL = "https://edge.testnet.grvt.io/auth/api_key/login"
ORDER_URL = "https://trades.testnet.grvt.io/full/v1/create_order"
BULK_ORDERS_URL = "https://trades.testnet.grvt.io/full/v2/bulk_orders"
LOOKUP_URL = "https://trades.testnet.grvt.io/full/v1/order"
FILL_HISTORY_URL = "https://trades.testnet.grvt.io/full/v1/fill_history"
FUNDING_HISTORY_URL = "https://trades.testnet.grvt.io/full/v1/funding_payment_history"
TRANSFER_HISTORY_URL = "https://trades.testnet.grvt.io/full/v1/transfer_history"
WITHDRAWAL_HISTORY_URL = "https://trades.testnet.grvt.io/full/v1/withdrawal_history"
DEPOSIT_HISTORY_URL = "https://trades.testnet.grvt.io/full/v1/deposit_history"
INTEREST_HISTORY_URL = "https://trades.testnet.grvt.io/full/v1/interest_payment_history"
CANCEL_URL = "https://trades.testnet.grvt.io/full/v1/cancel_order"
_READ_URLS = {
    "account_summary": "https://trades.testnet.grvt.io/full/v1/account_summary",
    "open_orders": "https://trades.testnet.grvt.io/full/v1/open_orders",
}
_ADDRESS = re.compile(r"0x[a-fA-F0-9]{40}\Z")
_HEX_32 = re.compile(r"0x[a-fA-F0-9]{64}\Z")
_SUB_ID = re.compile(r"[0-9]{1,30}\Z")
_INSTRUMENT = re.compile(r"[A-Z0-9]+_[A-Z0-9]+_Perp\Z")
_ORDER_FIELDS = frozenset({"sub_account_id", "is_market", "time_in_force",
                            "post_only", "reduce_only", "legs", "signature",
                            "metadata", "builder", "builder_fee"})
_SIGNATURE_FIELDS = frozenset({"signer", "r", "s", "v", "expiration", "nonce", "chain_id"})


class GrvtTestnetOrderError(RuntimeError):
    """Error saneado; no incluye secretos, firma ni respuestas externas."""


@dataclass(frozen=True, slots=True)
class GrvtTestnetLimits:
    sub_account_id: str
    instrument: str
    max_order_notional: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.sub_account_id, str) or not _SUB_ID.fullmatch(self.sub_account_id):
            raise GrvtTestnetOrderError("Subcuenta testnet inválida")
        if not isinstance(self.instrument, str) or not _INSTRUMENT.fullmatch(self.instrument):
            raise GrvtTestnetOrderError("Instrumento testnet inválido")
        if (type(self.max_order_notional) is not Decimal
                or not self.max_order_notional.is_finite()
                or self.max_order_notional <= 0
                or len(self.max_order_notional.as_tuple().digits) > 30):
            raise GrvtTestnetOrderError("Límite testnet inválido")


@dataclass(frozen=True, slots=True)
class GrvtTestnetOrderAck:
    sub_account_id: str
    client_order_id: str
    order_id: str
    accepted_by_testnet: bool
    execution_unverified: bool = True


@dataclass(frozen=True, slots=True)
class GrvtTestnetOrderObservation:
    sub_account_id: str
    client_order_id: str
    order_id: str
    status: str
    instrument: str
    side: str
    size: str
    traded_size: str
    limit_price: str | None
    update_time_ns: str
    execution_unverified: bool = True
    create_time_ns: str | None = None


def _decimal_text(value: object) -> Decimal:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,30}(?:\.[0-9]{1,18})?", value):
        raise GrvtTestnetOrderError("Cantidad o precio de orden testnet inválido")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise GrvtTestnetOrderError("Cantidad o precio de orden testnet inválido") from None
    if not number.is_finite() or number <= 0:
        raise GrvtTestnetOrderError("Cantidad o precio de orden testnet inválido")
    return number


def _validate_order(order: object, limits: GrvtTestnetLimits, now_ns: int) -> str:
    """El circuito IOC no admite metadatos TP/SL ni firmas de larga duración."""
    return _validate_limit_contract(order, limits, now_ns,
        time_in_force="IMMEDIATE_OR_CANCEL", metadata_fields={"client_order_id"},
        lifetime_ns=300_000_000_000)


def _validate_limit_contract(order: object, limits: GrvtTestnetLimits, now_ns: int, *,
                             time_in_force: str, metadata_fields: set[str],
                             lifetime_ns: int) -> str:
    """Campos comunes de límites; el llamador valida el contrato de metadatos."""
    if type(now_ns) is not int or now_ns <= 0:
        raise GrvtTestnetOrderError("Reloj testnet inválido")
    if not isinstance(order, dict) or set(order) != _ORDER_FIELDS:
        raise GrvtTestnetOrderError("Contrato de orden testnet inválido")
    if (order["sub_account_id"] != limits.sub_account_id
            or order["is_market"] is not False
            or order["time_in_force"] != time_in_force
            or order["post_only"] is not False
            or type(order["reduce_only"]) is not bool):
        raise GrvtTestnetOrderError("Orden fuera del alcance testnet permitido")
    legs = order["legs"]
    if (not isinstance(legs, list) or len(legs) != 1
            or not isinstance(legs[0], dict)
            or set(legs[0]) != {"instrument", "size", "limit_price", "is_buying_asset"}):
        raise GrvtTestnetOrderError("La orden testnet requiere un único tramo límite")
    leg = legs[0]
    if leg["instrument"] != limits.instrument or type(leg["is_buying_asset"]) is not bool:
        raise GrvtTestnetOrderError("Instrumento o lado testnet inválido")
    with localcontext() as context:
        context.prec = 80
        if _decimal_text(leg["size"]) * _decimal_text(leg["limit_price"]) > limits.max_order_notional:
            raise GrvtTestnetOrderError("La orden supera el límite testnet")
    signature = order["signature"]
    if not isinstance(signature, dict) or set(signature) != _SIGNATURE_FIELDS:
        raise GrvtTestnetOrderError("Firma testnet incompleta")
    if (not isinstance(signature["signer"], str) or not _ADDRESS.fullmatch(signature["signer"])
            or any(not isinstance(signature[key], str) or not _HEX_32.fullmatch(signature[key])
                   for key in ("r", "s"))
            or type(signature["v"]) is not int or signature["v"] not in (27, 28)
            or type(signature["nonce"]) is not int or not 0 <= signature["nonce"] <= 2**32 - 1
            or signature["chain_id"] != "326"):
        raise GrvtTestnetOrderError("Firma o red testnet inválida")
    expiration = signature["expiration"]
    if (not isinstance(expiration, str) or not expiration.isdecimal()
            or not now_ns < int(expiration) <= now_ns + lifetime_ns):
        raise GrvtTestnetOrderError("Firma testnet caducada o demasiado larga")
    metadata = order["metadata"]
    if not isinstance(metadata, dict) or set(metadata) != metadata_fields:
        raise GrvtTestnetOrderError("Metadatos testnet inválidos")
    client_id = metadata["client_order_id"]
    if (not isinstance(client_id, str) or not client_id.isdecimal()
            or not 2**63 <= int(client_id) <= 2**64 - 1):
        raise GrvtTestnetOrderError("ID de orden testnet fuera del rango propio")
    if (not isinstance(order["builder"], str) or not _ADDRESS.fullmatch(order["builder"])
            or order["builder_fee"] != "0"):
        raise GrvtTestnetOrderError("Builder o comisión testnet no permitidos")
    return client_id


def _post(url: str, body: dict, headers: dict[str, str]) -> httpx.Response:
    return httpx.post(url, json=body, headers=headers, timeout=5.0,
                      follow_redirects=False, trust_env=False)


def _wire_equal(actual: object, expected: object) -> bool:
    """Igualdad JSON tipada: False no equivale a 0 ni un entero a un float."""
    try:
        return (json.dumps(actual, sort_keys=True, allow_nan=False)
                == json.dumps(expected, sort_keys=True, allow_nan=False))
    except (TypeError, ValueError):
        return False


def _order_ack(result: object, order: dict, limits: GrvtTestnetLimits) -> GrvtTestnetOrderAck:
    if not isinstance(result, dict):
        raise ValueError("unconfirmed")
    metadata = result.get("metadata")
    order_id = result.get("order_id")
    client_id = order["metadata"]["client_order_id"]
    if (result.get("sub_account_id") != limits.sub_account_id
            or not isinstance(metadata, dict) or metadata.get("client_order_id") != client_id
            or not _wire_equal(metadata.get("trigger"), order["metadata"].get("trigger"))
            or any(not _wire_equal(result.get(key), order[key]) for key in (
                "is_market", "time_in_force", "post_only", "reduce_only",
                "legs", "signature", "builder", "builder_fee"))
            or not isinstance(order_id, str) or not re.fullmatch(r"[A-Za-z0-9x]{1,80}", order_id)):
        raise ValueError("wrong order")
    return GrvtTestnetOrderAck(limits.sub_account_id, client_id, order_id, True)


class GrvtTestnetOrderClient:
    """Una sola transmisión por instancia; resultado dudoso exige consulta por ID."""

    def __init__(self, api_key: str, *,
                 http_post: Callable[[str, dict, dict[str, str]], httpx.Response] = _post,
                 clock_ns: Callable[[], int] = time.time_ns):
        if (not isinstance(api_key, str) or not 8 <= len(api_key) <= 256
                or not api_key.isascii() or any(char.isspace() for char in api_key)):
            raise GrvtTestnetOrderError("Clave GRVT testnet inválida")
        self._post = http_post
        self._clock_ns = clock_ns
        self._used = False
        self._creation_lock = Lock()
        self._cancel_used = False
        try:
            response = http_post(AUTH_URL, {"api_key": api_key},
                                 {"Cookie": "rm=true;", "Content-Type": "application/json"})
            body = response.json() if response.status_code == 200 else None
            cookie = response.cookies.get("gravity")
            account = response.headers.get("x-grvt-account-id")
        except Exception:
            raise GrvtTestnetOrderError("No se pudo autenticar GRVT testnet") from None
        if (not isinstance(body, dict) or body.get("status") != "success"
                or not isinstance(cookie, str) or not 8 <= len(cookie) <= 4096
                or not cookie.isascii() or any(char.isspace() or char in ";\r\n" for char in cookie)
                or not isinstance(account, str) or not _ADDRESS.fullmatch(account)):
            raise GrvtTestnetOrderError("GRVT testnet no confirmó la sesión")
        sub = body.get("sub_account_id")
        if sub is not None and (not isinstance(sub, str) or not _SUB_ID.fullmatch(sub)):
            raise GrvtTestnetOrderError("GRVT testnet devolvió una subcuenta inválida")
        self._cookie = cookie
        self._account = account
        self._bound_sub = sub

    def __repr__(self) -> str:
        return "GrvtTestnetOrderClient(<redacted>)"

    @property
    def creation_available(self) -> bool:
        """Disponibilidad local; no concede permisos ni permite resetear un POST."""
        with self._creation_lock: return not self._used

    def _headers(self) -> dict[str, str]:
        return {"Cookie": f"gravity={self._cookie}",
                "X-Grvt-Account-Id": self._account,
                "Content-Type": "application/json"}

    def matches_main_account_id(self, candidate: object) -> bool:
        """Comprueba el destino de un depósito sin revelar la dirección autenticada."""
        return isinstance(candidate, str) and candidate == self._account

    def read_testnet(self, path: str, sub_account_id: str) -> dict:
        """Sólo resumen y órdenes abiertas de la subcuenta vinculada."""
        if path not in _READ_URLS or sub_account_id != self._bound_sub:
            raise GrvtTestnetOrderError("Consulta testnet fuera de alcance")
        try:
            response = self._post(_READ_URLS[path],
                                  {"sub_account_id": sub_account_id},
                                  self._headers())
            body = response.json() if response.status_code == 200 else None
            if not isinstance(body, dict) or "error" in body or "code" in body:
                raise ValueError("invalid account data")
            return body
        except Exception:
            raise GrvtTestnetOrderError("Datos privados testnet no verificables") from None

    def read_fill_history(self, sub_account_id: str, start_time_ns: str,
                          cursor: str = "", *, end_time_ns: str | None = None) -> dict:
        """Página privada testnet; el consumidor debe agotar todos los cursores."""
        if (sub_account_id != self._bound_sub or not isinstance(start_time_ns, str)
                or not start_time_ns.isdecimal() or int(start_time_ns) <= 0
                or end_time_ns is not None and (not isinstance(end_time_ns, str)
                    or not end_time_ns.isdecimal() or int(end_time_ns) < int(start_time_ns))
                or not isinstance(cursor, str) or len(cursor) > 1024
                or not cursor.isascii() or any(ord(char) < 32 for char in cursor)):
            raise GrvtTestnetOrderError("Consulta de fills testnet fuera de alcance")
        try:
            response = self._post(FILL_HISTORY_URL,
                                  {"sub_account_id": sub_account_id,
                                   "start_time": start_time_ns,
                                   **({"end_time":end_time_ns} if end_time_ns is not None else {}),
                                   "limit": 500, "cursor": cursor},
                                  self._headers())
            body = response.json() if response.status_code == 200 else None
            if not isinstance(body, dict) or "error" in body or "code" in body:
                raise ValueError("invalid fills")
            return body
        except Exception:
            raise GrvtTestnetOrderError("Historial de fills testnet no verificable") from None

    def read_funding_history(self, sub_account_id: str, start_time_ns: str,
                             end_time_ns: str, cursor: str = "") -> dict:
        """Página privada testnet de pagos; el consumidor agota cursores."""
        if (sub_account_id != self._bound_sub
                or not isinstance(start_time_ns, str) or not start_time_ns.isdecimal()
                or not isinstance(end_time_ns, str) or not end_time_ns.isdecimal()
                or not 0 < int(start_time_ns) <= int(end_time_ns)
                or not isinstance(cursor, str) or len(cursor) > 1024
                or not cursor.isascii()
                or any(ord(char) < 32 for char in cursor)):
            raise GrvtTestnetOrderError("Consulta de financiación testnet fuera de alcance")
        try:
            response = self._post(FUNDING_HISTORY_URL,
                                  {"sub_account_id": sub_account_id,
                                   "start_time": start_time_ns,
                                   "end_time": end_time_ns,
                                   "limit": 500, "cursor": cursor},
                                  self._headers())
            body = response.json() if response.status_code == 200 else None
            if not isinstance(body, dict) or "error" in body or "code" in body:
                raise ValueError("invalid funding")
            return body
        except Exception:
            raise GrvtTestnetOrderError("Financiación testnet no verificable") from None

    def read_interest_history(self, sub_account_id: str, start_time_ns: str,
                              end_time_ns: str, cursor: str = "") -> dict:
        """Cargos de interés MAM de la subcuenta testnet vinculada."""
        if (sub_account_id != self._bound_sub
                or not isinstance(start_time_ns, str) or not start_time_ns.isdecimal()
                or not isinstance(end_time_ns, str) or not end_time_ns.isdecimal()
                or not 0 < int(start_time_ns) <= int(end_time_ns)
                or not isinstance(cursor, str) or len(cursor) > 1024
                or not cursor.isascii()
                or any(ord(char) < 32 for char in cursor)):
            raise GrvtTestnetOrderError("Consulta de interés testnet fuera de alcance")
        try:
            response = self._post(INTEREST_HISTORY_URL,
                                  {"sub_account_id": sub_account_id,
                                   "start_time": start_time_ns,
                                   "end_time": end_time_ns, "limit": 500,
                                   "cursor": cursor}, self._headers())
            body = response.json() if response.status_code == 200 else None
            if not isinstance(body, dict) or "error" in body or "code" in body:
                raise ValueError("invalid interest")
            return body
        except Exception:
            raise GrvtTestnetOrderError("Interés testnet no verificable") from None

    def read_transfer_history(self, sub_account_id: str, start_time_ns: str,
                              end_time_ns: str, cursor: str = "") -> dict:
        """Transferencias del main account; el auditor filtra la subcuenta."""
        if (sub_account_id != self._bound_sub
                or not isinstance(start_time_ns, str) or not start_time_ns.isdecimal()
                or not isinstance(end_time_ns, str) or not end_time_ns.isdecimal()
                or not 0 < int(start_time_ns) <= int(end_time_ns)
                or not isinstance(cursor, str) or len(cursor) > 1024
                or not cursor.isascii()
                or any(ord(char) < 32 for char in cursor)):
            raise GrvtTestnetOrderError("Consulta de transferencias testnet fuera de alcance")
        try:
            response = self._post(TRANSFER_HISTORY_URL,
                                  {"currency": [], "start_time": start_time_ns,
                                   "end_time": end_time_ns, "limit": 500,
                                   "cursor": cursor}, self._headers())
            body = response.json() if response.status_code == 200 else None
            if not isinstance(body, dict) or "error" in body or "code" in body:
                raise ValueError("invalid transfers")
            return body
        except Exception:
            raise GrvtTestnetOrderError("Transferencias testnet no verificables") from None

    def read_withdrawal_history(self, sub_account_id: str, start_time_ns: str,
                                end_time_ns: str, cursor: str = "") -> dict:
        """Retiros del main account; el auditor filtra la subcuenta vinculada."""
        if (sub_account_id != self._bound_sub
                or not isinstance(start_time_ns, str) or not start_time_ns.isdecimal()
                or not isinstance(end_time_ns, str) or not end_time_ns.isdecimal()
                or not 0 < int(start_time_ns) <= int(end_time_ns)
                or not isinstance(cursor, str) or len(cursor) > 1024
                or not cursor.isascii()
                or any(ord(char) < 32 for char in cursor)):
            raise GrvtTestnetOrderError("Consulta de retiros testnet fuera de alcance")
        try:
            response = self._post(WITHDRAWAL_HISTORY_URL,
                                  {"currency": [], "start_time": start_time_ns,
                                   "end_time": end_time_ns, "limit": 500,
                                   "cursor": cursor}, self._headers())
            body = response.json() if response.status_code == 200 else None
            if not isinstance(body, dict) or "error" in body or "code" in body:
                raise ValueError("invalid withdrawals")
            return body
        except Exception:
            raise GrvtTestnetOrderError("Retiros testnet no verificables") from None

    def read_deposit_history(self, sub_account_id: str, start_time_ns: str,
                             end_time_ns: str, cursor: str = "") -> dict:
        """Depósitos de la cuenta principal; nunca son saldo de subcuenta."""
        if (sub_account_id != self._bound_sub
                or not isinstance(start_time_ns, str) or not start_time_ns.isdecimal()
                or not isinstance(end_time_ns, str) or not end_time_ns.isdecimal()
                or not 0 < int(start_time_ns) <= int(end_time_ns)
                or not isinstance(cursor, str) or len(cursor) > 1024
                or not cursor.isascii()
                or any(ord(char) < 32 for char in cursor)):
            raise GrvtTestnetOrderError("Consulta de depósitos testnet fuera de alcance")
        try:
            response = self._post(DEPOSIT_HISTORY_URL,
                                  {"currency": [], "start_time": start_time_ns,
                                   "end_time": end_time_ns, "limit": 500,
                                   "cursor": cursor}, self._headers())
            body = response.json() if response.status_code == 200 else None
            if not isinstance(body, dict) or "error" in body or "code" in body:
                raise ValueError("invalid deposits")
            return body
        except Exception:
            raise GrvtTestnetOrderError("Depósitos testnet no verificables") from None

    def _take_creation(self) -> None:
        # La cuota en memoria complementa el claim duradero del servicio.
        # También se comparte atómicamente entre modalidades y entre hilos.
        with self._creation_lock:
            if self._used:
                raise GrvtTestnetOrderError('Transmisión testnet ya consumida; consulta los IDs')
            self._used = True

    def submit_signed_limit(self, order: dict, limits: GrvtTestnetLimits) -> GrvtTestnetOrderAck:
        return self._submit_limit(order, limits, _validate_order)

    def submit_signed_protection(self, order: dict, limits: GrvtTestnetLimits, *,
                                  expected_trigger: GrvtTestnetTrigger) -> GrvtTestnetOrderAck:
        """Transporte TP/SL interno: requiere diario/permiso por parte del servicio.

        Comparte la concesión de un solo POST con IOC. Un acuse no demuestra
        protección activa ni ejecución: exige observar orden, trigger y fills.
        """
        from app.live_execution.grvt_testnet_protection import (
            GrvtTestnetTrigger, validate_testnet_protection,
        )
        if not isinstance(expected_trigger, GrvtTestnetTrigger):
            raise GrvtTestnetOrderError("Disparador TP/SL esperado inválido")
        def validate(candidate: dict, policy: GrvtTestnetLimits, now: int) -> str:
            return validate_testnet_protection(candidate, policy, now_ns=now,
                                               expected_trigger=expected_trigger)
        return self._submit_limit(order, limits, validate)

    def _submit_limit(self, order: dict, limits: GrvtTestnetLimits,
                       validator: Callable[[dict, GrvtTestnetLimits, int], str]) -> GrvtTestnetOrderAck:
        if self._used:
            raise GrvtTestnetOrderError("Transmisión testnet ya consumida; consulta la orden")
        now_ns = self._clock_ns()
        if type(now_ns) is not int or now_ns <= 0:
            raise GrvtTestnetOrderError("Reloj testnet inválido")
        client_id = validator(order, limits, now_ns)
        if self._bound_sub != limits.sub_account_id:
            raise GrvtTestnetOrderError("Clave testnet no vinculada a la subcuenta")
        self._take_creation()  # Nunca reintentar un POST de resultado incierto.
        try:
            response = self._post(
                ORDER_URL, {"order": order}, self._headers(),
            )
            body = response.json() if response.status_code == 200 else None
            result = body.get("result") if isinstance(body, dict) and "error" not in body and "code" not in body else None
            return _order_ack(result, order, limits)
        except Exception:
            raise GrvtTestnetOrderError(
                "Resultado de orden testnet desconocido; consulta el ID de cliente antes de actuar"
            ) from None

    def submit_signed_oco(self, orders: list[dict], limits: GrvtTestnetLimits,
                           ) -> tuple[GrvtTestnetOrderAck, GrvtTestnetOrderAck]:
        """Un único bulk nativo; jamás degrada a dos POST individuales."""
        from app.live_execution.grvt_testnet_oco import validate_oco_orders, oco_ack_from_result
        if self._used:
            raise GrvtTestnetOrderError("Transmisión testnet ya consumida; consulta ambos IDs")
        sent_ns = self._clock_ns()
        validate_oco_orders(orders, limits, now_ns=sent_ns)
        if self._bound_sub != limits.sub_account_id:
            raise GrvtTestnetOrderError("Clave testnet no vinculada a la subcuenta")
        self._take_creation()
        try:
            response = self._post(BULK_ORDERS_URL, {"sub_account_id": limits.sub_account_id,
                "orders": orders, "order_i_ds": [], "client_order_i_ds": []}, self._headers())
            body = response.json() if response.status_code == 200 else None
            result = body.get("result") if isinstance(body,dict) and "error" not in body and "code" not in body else None
            if (not isinstance(result,dict) or not isinstance(result.get("orders"),list)
                    or len(result["orders"]) != 2 or result.get("cancel_acks") != []):
                raise ValueError("incomplete OCO ack")
            acks = tuple(oco_ack_from_result(row,order,limits,now_ns=sent_ns)
                         for row,order in zip(result["orders"],orders))
            if acks[0].order_id == acks[1].order_id:
                raise ValueError("duplicate OCO ids")
            return acks
        except Exception:
            raise GrvtTestnetOrderError("Resultado OCO testnet desconocido; consulta ambos IDs y no reenvíes") from None

    def submit_signed_bracket(self, orders: list[dict], entry_limits: GrvtTestnetLimits,
                              exit_limits: GrvtTestnetLimits,
                              ) -> tuple[GrvtTestnetOrderAck, GrvtTestnetOrderAck, GrvtTestnetOrderAck]:
        """Transporte OTOCO de un uso; requiere diario/preflight del llamador.

        No crea una concesión duradera ni es una API de activación autónoma.
        Timeout o acuse parcial consumen la cuota y exigen consultar los 3 IDs.
        """
        from app.live_execution.grvt_testnet_bracket import validate_testnet_bracket,bracket_ack_from_result
        if self._used:
            raise GrvtTestnetOrderError('Transmisión testnet ya consumida; consulta los tres IDs')
        orders = json.loads(json.dumps(orders,allow_nan=False))
        sent_ns = self._clock_ns()
        ids = validate_testnet_bracket(orders,entry_limits,exit_limits,now_ns=sent_ns)
        if self._bound_sub != entry_limits.sub_account_id:
            raise GrvtTestnetOrderError('Clave testnet no vinculada a la subcuenta')
        self._take_creation()
        try:
            response = self._post(BULK_ORDERS_URL,{'sub_account_id':entry_limits.sub_account_id,
                'orders':orders,'order_i_ds':[],'client_order_i_ds':[]},self._headers())
            body = response.json() if response.status_code == 200 else None
            result = body.get('result') if isinstance(body,dict) and 'error' not in body and 'code' not in body else None
            if (not isinstance(result,dict) or not isinstance(result.get('orders'),list)
                    or len(result['orders']) != 3 or result.get('cancel_acks') != []):
                raise ValueError('incomplete linked acknowledgement')
            rows = {row['metadata']['client_order_id']:row for row in result['orders']}
            if set(rows) != set(ids): raise ValueError('wrong linked identities')
            acks = tuple(bracket_ack_from_result(rows[cid],order,entry_limits,exit_limits,now_ns=sent_ns)
                         for cid,order in zip(ids,orders))
            if len({ack.order_id for ack in acks}) != 3: raise ValueError('duplicate linked exchange identities')
            return acks
        except Exception:
            raise GrvtTestnetOrderError('Resultado OTOCO testnet desconocido; consulta los tres IDs y no reenvíes') from None

    def cancel_oco_by_client_id(self, sub_account_id: str, client_order_id: str) -> None:
        """Un bulk vacío cancela el grupo por un miembro; no prueba su estado."""
        self._cancel_group_by_client_id(sub_account_id,client_order_id,label='OCO')

    def cancel_bracket_by_client_id(self, sub_account_id: str, client_order_id: str) -> None:
        """Sólo transporte OTOCO; el servicio debe reclamar su diario antes."""
        self._cancel_group_by_client_id(sub_account_id,client_order_id,label='OTOCO')

    def _take_cancellation(self) -> None:
        with self._creation_lock:
            if self._cancel_used:
                raise GrvtTestnetOrderError('Cancelación testnet ya consumida; consulta los IDs')
            self._cancel_used=True

    def _cancel_group_by_client_id(self,sub_account_id: str,client_order_id: str,*,label: str) -> None:
        if (sub_account_id != self._bound_sub or not isinstance(client_order_id,str)
                or not re.fullmatch(r"[0-9]{1,20}",client_order_id)
                or not 2**63 <= int(client_order_id) <= 2**64-1):
            raise GrvtTestnetOrderError(f'Identidad de cancelación {label} inválida')
        self._take_cancellation()
        try:
            response = self._post(BULK_ORDERS_URL,{"sub_account_id":sub_account_id,"orders":[],
                "order_i_ds":[],"client_order_i_ds":[client_order_id],"time_to_live_ms":"5000"},self._headers())
            body = response.json() if response.status_code == 200 else None
            result = body.get("result") if isinstance(body,dict) and "error" not in body and "code" not in body else None
            if (not isinstance(result,dict) or result.get("orders") != []
                    or not isinstance(result.get("cancel_acks"),list) or len(result["cancel_acks"]) != 1):
                raise ValueError("invalid bulk cancel ack")
            ack = result["cancel_acks"][0]
            if not isinstance(ack,dict) or not (ack.get("ack") is True or ack.get("ack") == "true"):
                raise ValueError("unconfirmed bulk cancel")
        except Exception:
            raise GrvtTestnetOrderError(f'Resultado de cancelación {label} desconocido; consulta los IDs y no reenvíes') from None

    def cancel_by_client_id(self, sub_account_id: str,
                            client_order_id: str) -> GrvtTestnetOrderObservation:
        """Un POST testnet por instancia; la respuesta no cierra la conciliación."""
        if (sub_account_id != self._bound_sub
                or not isinstance(client_order_id, str) or not client_order_id.isdecimal()
                or not 2**63 <= int(client_order_id) <= 2**64 - 1):
            raise GrvtTestnetOrderError("Identidad de cancelación testnet inválida")
        self._take_cancellation()
        try:
            response = self._post(
                CANCEL_URL,
                {"sub_account_id": sub_account_id,
                 "client_order_id": client_order_id,
                 "time_to_live_ms": "5000"}, self._headers(),
            )
            body = response.json() if response.status_code == 200 else None
            ack = (body.get("result") if isinstance(body, dict)
                   and "error" not in body and "code" not in body else None)
            # GRVT responde AckResponse, no Order. El acuse sólo confirma
            # recepción; observar el ID después sigue siendo obligatorio.
            if not isinstance(ack, dict) or not (ack.get("ack") is True or ack.get("ack") == "true"):
                raise ValueError("unconfirmed cancellation ack")
            observed = self.lookup_order(sub_account_id, client_order_id)
        except Exception:
            raise GrvtTestnetOrderError(
                "Resultado de cancelación testnet desconocido; consulta por ID antes de actuar"
            ) from None
        return observed

    def lookup_order(self, sub_account_id: str,
                     client_order_id: str) -> GrvtTestnetOrderObservation:
        """Consulta puntual por ID; ausencia o error jamás implica orden no enviada."""
        row = self._lookup_order_payload(sub_account_id, client_order_id)
        try:
            parsed = _parse_grvt_order(row, sub_account_id, open_only=False,
                                       now_ns=self._clock_ns())
            if parsed["client_order_id"] != client_order_id or len(parsed["legs"]) != 1:
                raise ValueError("other order")
            leg = parsed["legs"][0]
            if not _INSTRUMENT.fullmatch(leg["instrument"]):
                raise ValueError("other instrument")
        except Exception:
            raise GrvtTestnetOrderError(
                "Orden testnet no verificable; conserva el ID y no reenvíes"
            ) from None
        return GrvtTestnetOrderObservation(
            sub_account_id, client_order_id, parsed["order_id"], parsed["status"],
            leg["instrument"], leg["side"], leg["size"], leg["traded_size"],
            leg["limit_price"], parsed["update_time_ns"],
            create_time_ns=parsed.get("create_time_ns"),
        )

    def lookup_protection(self, order: dict, limits: GrvtTestnetLimits, *,
                            expected_trigger: GrvtTestnetTrigger,
                            created_at_ns: int) -> GrvtTestnetProtectionObservation:
        """Lectura TP/SL sin POST de orden, disponible también tras caducar."""
        from app.live_execution.grvt_testnet_protection import (
            GrvtTestnetTrigger, observe_testnet_protection, validate_testnet_protection,
        )
        try:
            if not isinstance(expected_trigger, GrvtTestnetTrigger):
                raise ValueError("missing expected trigger")
            client_id = validate_testnet_protection(order, limits, now_ns=created_at_ns,
                                                   expected_trigger=expected_trigger)
            row = self._lookup_order_payload(limits.sub_account_id, client_id)
            return observe_testnet_protection(row, order, limits,
                expected_trigger=expected_trigger, created_at_ns=created_at_ns, now_ns=self._clock_ns())
        except Exception:
            raise GrvtTestnetOrderError(
                "Protección testnet no verificable; conserva el ID y no reenvíes"
            ) from None

    def _lookup_order_payload(self, sub_account_id: str, client_order_id: str) -> dict:
        if (self._bound_sub != sub_account_id
                or not isinstance(client_order_id, str) or not client_order_id.isdecimal()
                or not 2**63 <= int(client_order_id) <= 2**64 - 1):
            raise GrvtTestnetOrderError("Identidad de orden testnet inválida")
        return self._lookup_testnet_order({"sub_account_id": sub_account_id, "client_order_id": client_order_id})

    def lookup_order_payload_by_id(self, sub_account_id: str, order_id: str) -> dict:
        if (self._bound_sub != sub_account_id or not isinstance(order_id, str)
                or not re.fullmatch(r"[A-Za-z0-9x]{1,80}", order_id) or order_id == "0"):
            raise GrvtTestnetOrderError("Identidad de orden testnet inválida")
        row = self._lookup_testnet_order({"sub_account_id": sub_account_id, "order_id": order_id})
        if row.get("sub_account_id") != sub_account_id or row.get("order_id") != order_id:
            raise GrvtTestnetOrderError("Orden testnet no verificable")
        return row

    def _lookup_testnet_order(self, payload: dict[str, str]) -> dict:
        try:
            response = self._post(LOOKUP_URL, payload, self._headers())
            body = response.json() if response.status_code == 200 else None
            row = body.get("result") if isinstance(body, dict) and "error" not in body and "code" not in body else None
            if not isinstance(row, dict):
                raise ValueError("missing order")
            return row
        except Exception:
            raise GrvtTestnetOrderError(
                "Orden testnet no verificable; conserva el ID y no reenvíes"
            ) from None
