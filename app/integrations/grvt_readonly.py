"""Consulta privada puntual de GRVT, confinada a endpoints de lectura.

La clave API sólo se usa para obtener una cookie de sesión en memoria. No se
guardan direcciones completas, cookies, respuestas brutas ni credenciales en
SQLite. El servidor GRVT no confirma aquí los permisos de la clave: el usuario
debe proporcionar una clave View, y este cliente nunca tiene ruta de orden.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import re
import time
from typing import Callable

import httpx


AUTH_URL = "https://edge.grvt.io/auth/api_key/login"
TRADES_ROOT = "https://trades.grvt.io/full/v1"
_READ_PATHS = frozenset(("aggregated_account_summary", "get_sub_accounts", "account_summary", "open_orders", "order", "fill_history", "get_all_initial_leverage"))
_CURRENCY = re.compile(r"^[A-Z0-9]{2,24}$")
_INSTRUMENT = re.compile(r"^[A-Za-z0-9_:\-]{3,80}$")
_ADDRESS = re.compile(r"^0x[a-fA-F0-9]{40}$")
_SUB_ID = re.compile(r"^[0-9]{1,30}$")


class GrvtReadOnlyError(RuntimeError):
    """Mensaje seguro: nunca incluye clave, cookie, dirección o respuesta."""


def _post(url: str, payload: dict, headers: dict[str, str]) -> httpx.Response:
    try:
        return httpx.post(url, json=payload, headers=headers, timeout=5.0,
                          follow_redirects=False, trust_env=False)
    except httpx.HTTPError:
        raise GrvtReadOnlyError("No se pudo conectar con GRVT") from None


def _payload(response: httpx.Response) -> dict:
    if response.status_code == 429:
        raise GrvtReadOnlyError("GRVT limita las consultas; espera antes de reintentar")
    if response.status_code in (401, 403):
        raise GrvtReadOnlyError("GRVT rechazó el acceso a la cuenta")
    if response.status_code != 200:
        raise GrvtReadOnlyError("GRVT no devolvió datos de cuenta")
    try:
        data = response.json()
    except ValueError:
        raise GrvtReadOnlyError("Respuesta de GRVT no válida") from None
    if not isinstance(data, dict) or "code" in data or "error" in data:
        raise GrvtReadOnlyError("GRVT no devolvió datos de cuenta válidos")
    return data


def _decimal(value: object, label: str, *, nonnegative: bool = False,
             positive: bool = False) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 80:
        raise GrvtReadOnlyError(f"{label} de GRVT no es decimal textual")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise GrvtReadOnlyError(f"{label} de GRVT no es decimal textual") from None
    if (not number.is_finite() or len(number.as_tuple().digits) > 40
            or not -40 <= number.as_tuple().exponent <= 40
            or nonnegative and number < 0 or positive and number <= 0):
        raise GrvtReadOnlyError(f"{label} de GRVT fuera de rango")
    return format(number, "f")


def _balances(rows: object) -> list[dict]:
    if not isinstance(rows, list) or len(rows) > 500:
        raise GrvtReadOnlyError("Saldos GRVT incompletos")
    balances, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise GrvtReadOnlyError("Saldo GRVT malformado")
        currency = row.get("currency")
        if not isinstance(currency, str) or not _CURRENCY.fullmatch(currency) or currency in seen:
            raise GrvtReadOnlyError("Activo GRVT inválido o duplicado")
        seen.add(currency)
        amount = _decimal(row.get("balance"), "Saldo", nonnegative=True)
        if Decimal(amount):
            balances.append({"asset": currency, "balance": amount})
    return sorted(balances, key=lambda item: item["asset"])


def _order(row: object, sub_id: str, *, open_only: bool, now_ns: int) -> dict:
    if not isinstance(row, dict) or row.get("sub_account_id") != sub_id:
        raise GrvtReadOnlyError("Orden GRVT de otra subcuenta")
    order_id = row.get("order_id")
    metadata, state, legs = row.get("metadata"), row.get("state"), row.get("legs")
    if (not isinstance(order_id, str) or not 1 <= len(order_id) <= 80
            or not re.fullmatch(r"[A-Za-z0-9x]+", order_id)
            or not isinstance(metadata, dict) or not isinstance(state, dict)
            or not isinstance(legs, list) or not 1 <= len(legs) <= 20):
        raise GrvtReadOnlyError("Orden GRVT malformada")
    client_id = metadata.get("client_order_id")
    if (not isinstance(client_id, str) or not client_id.isdecimal()
            or not 1 <= len(client_id) <= 20):
        raise GrvtReadOnlyError("ID de orden GRVT inválido")
    status = state.get("status")
    allowed = ("PENDING", "OPEN") if open_only else (
        "PENDING", "OPEN", "FILLED", "REJECTED", "CANCELLED")
    if status not in allowed:
        raise GrvtReadOnlyError("Estado de orden GRVT incoherente")
    traded = state.get("traded_size")
    if not isinstance(traded, list) or len(traded) != len(legs):
        raise GrvtReadOnlyError("Ejecución de orden GRVT incompleta")
    update_time = state.get("update_time")
    if (not isinstance(update_time, str) or not update_time.isdecimal()
            or not 0 < int(update_time) <= now_ns + 2_000_000_000):
        raise GrvtReadOnlyError("Orden GRVT sin hora verificable")
    parsed_legs = []
    for leg, filled in zip(legs, traded):
        if not isinstance(leg, dict):
            raise GrvtReadOnlyError("Tramo de orden GRVT malformado")
        instrument = leg.get("instrument")
        if not isinstance(instrument, str) or not _INSTRUMENT.fullmatch(instrument):
            raise GrvtReadOnlyError("Instrumento de orden GRVT inválido")
        buying = leg.get("is_buying_asset")
        if not isinstance(buying, bool):
            raise GrvtReadOnlyError("Dirección de orden GRVT inválida")
        size = _decimal(leg.get("size"), "Tamaño de orden", positive=True)
        filled_size = _decimal(filled, "Tamaño ejecutado", nonnegative=True)
        if Decimal(filled_size) > Decimal(size):
            raise GrvtReadOnlyError("Ejecución de orden GRVT excede el tamaño")
        price = leg.get("limit_price")
        parsed_legs.append({"instrument": instrument, "side": "BUY" if buying else "SELL",
                            "size": size, "traded_size": filled_size,
                            "limit_price": None if price is None else _decimal(
                                price, "Precio límite", nonnegative=True)})
    reduce_only = row.get("reduce_only", False)
    if not isinstance(reduce_only, bool):
        raise GrvtReadOnlyError("Reduce-only GRVT inválido")
    parsed = {"sub_account_id": sub_id, "order_id": order_id,
            "client_order_id": client_id, "status": status,
            "reduce_only": reduce_only, "legs": parsed_legs,
            "update_time_ns": update_time}
    created = metadata.get("create_time")
    if created is not None:
        if (not isinstance(created, str) or not created.isdecimal()
                or not 0 < int(created) <= int(update_time)):
            raise GrvtReadOnlyError("Orden GRVT sin creación verificable")
        parsed["create_time_ns"] = created
    return parsed


class GrvtReadOnlyClient:
    """Sesión mainnet acotada a consultas de lectura, sin clave persistida."""

    def __init__(self, api_key: str, *,
                 http_post: Callable[[str, dict, dict[str, str]], httpx.Response] = _post,
                 clock_ns: Callable[[], int] = time.time_ns):
        if (not isinstance(api_key, str) or not 8 <= len(api_key) <= 256
                or not api_key.isascii() or any(char.isspace() for char in api_key)):
            raise GrvtReadOnlyError("Formato de clave GRVT no válido")
        self._http_post = http_post
        try:
            response = http_post(AUTH_URL, {"api_key": api_key},
                                 {"Cookie": "rm=true;", "Content-Type": "application/json"})
            body = _payload(response)
            cookie = response.cookies.get("gravity")
            account = response.headers.get("x-grvt-account-id")
        except GrvtReadOnlyError:
            raise
        except Exception:
            raise GrvtReadOnlyError("No se pudo autenticar GRVT") from None
        if (body.get("status") != "success" or not isinstance(cookie, str)
                or not 8 <= len(cookie) <= 4096 or not cookie.isascii()
                or any(char.isspace() or char in ";\r\n" for char in cookie)
                or not isinstance(account, str) or not _ADDRESS.fullmatch(account)):
            raise GrvtReadOnlyError("GRVT no confirmó una sesión de cuenta válida")
        sub = body.get("sub_account_id")
        if sub is not None and (not isinstance(sub, str) or not _SUB_ID.fullmatch(sub)):
            raise GrvtReadOnlyError("GRVT devolvió una subcuenta inválida")
        self._cookie = cookie
        self._account = account
        self._bound_sub = sub
        self._clock_ns = clock_ns

    def __repr__(self) -> str:
        return "GrvtReadOnlyClient(<redacted>)"

    def _query(self, path: str, payload: dict | None = None) -> dict:
        if path not in _READ_PATHS:
            raise GrvtReadOnlyError("Consulta GRVT no permitida")
        try:
            response = self._http_post(f"{TRADES_ROOT}/{path}", payload or {},
                                       {"Cookie": f"gravity={self._cookie}",
                                        "X-Grvt-Account-Id": self._account,
                                        "Content-Type": "application/json"})
            return _payload(response)
        except GrvtReadOnlyError:
            raise
        except Exception:
            raise GrvtReadOnlyError("No se pudo consultar GRVT") from None

    def _sub_ids(self) -> list[str]:
        if self._bound_sub is not None:
            return [self._bound_sub]
        raw = self._query("get_sub_accounts").get("sub_account_ids")
        if (not isinstance(raw, list) or len(raw) > 20 or any(
                not isinstance(item, str) or not _SUB_ID.fullmatch(item) for item in raw)
                or len(set(raw)) != len(raw)):
            raise GrvtReadOnlyError("Lista de subcuentas GRVT incompleta")
        return raw

    def open_orders(self) -> dict:
        """Foto observacional; omite firmas y no garantiza órdenes en tránsito."""
        orders: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for sub_id in self._sub_ids():
            rows = self._query("open_orders", {"sub_account_id": sub_id}).get("result")
            if not isinstance(rows, list) or len(rows) > 500:
                raise GrvtReadOnlyError("Órdenes GRVT incompletas")
            for row in rows:
                parsed = _order(row, sub_id, open_only=True, now_ns=self._clock_ns())
                client_id = parsed["client_order_id"]
                if (sub_id, client_id) in seen:
                    raise GrvtReadOnlyError("ID de orden GRVT inválido o duplicado")
                seen.add((sub_id, client_id))
                orders.append(parsed)
        return {"source": "GRVT_MAINNET_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "in_flight_orders_excluded_possible": True,
                "complete_for_execution": False, "orders": orders}

    def position_configuration(self, sub_account_id: str, instrument: str):
        """Read the explicit instrument setting; never infer a missing default."""
        from app.integrations.grvt_position_config import configuration_response, scope
        try:
            scope(sub_account_id, instrument)
            if sub_account_id not in self._sub_ids():
                raise ValueError('inaccessible subaccount')
            started = self._clock_ns()
            raw = self._query('get_all_initial_leverage', {'sub_account_id': sub_account_id})
            return configuration_response(raw, sub_account_id, instrument,
                requested_at_ns=started, received_at_ns=self._clock_ns())
        except Exception:
            raise GrvtReadOnlyError('Configuración de margen y apalancamiento GRVT no verificable') from None

    def order(self, sub_account_id: str, client_order_id: str) -> dict:
        """Lee una orden concreta, incluso terminal, sin exponer la firma."""
        if (not isinstance(sub_account_id, str) or not _SUB_ID.fullmatch(sub_account_id)
                or not isinstance(client_order_id, str) or not client_order_id.isdecimal()
                or not 1 <= len(client_order_id) <= 20):
            raise GrvtReadOnlyError("Identificador de orden GRVT inválido")
        if sub_account_id not in self._sub_ids():
            raise GrvtReadOnlyError("Subcuenta GRVT no accesible")
        row = self._query("order", {"sub_account_id": sub_account_id,
                                    "client_order_id": client_order_id}).get("result")
        parsed = _order(row, sub_account_id, open_only=False, now_ns=self._clock_ns())
        if parsed["client_order_id"] != client_order_id:
            raise GrvtReadOnlyError("GRVT devolvió otra orden")
        return {"source": "GRVT_MAINNET_PRIVATE_READONLY",
                "accounting": "OBSERVED_NOT_IMPORTED",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "complete_for_execution": False, "order": parsed}

    def order_fills(self, sub_account_id: str, client_order_id: str) -> dict:
        """Contrasta fills de una orden; nunca concilia saldo ni habilita trading."""
        observed = self.order(sub_account_id, client_order_id)
        order = observed["order"]
        created = order.get("create_time_ns")
        if created is None:
            raise GrvtReadOnlyError("GRVT no dio la hora de creación de la orden")
        cursor = ""
        cursors = set()
        fills = []
        seen = set()
        for _ in range(20):
            payload = {"sub_account_id": sub_account_id, "start_time": str(int(created) - 1),
                       "limit": 500, "cursor": cursor}
            page = self._query("fill_history", payload)
            rows, following = page.get("result"), page.get("next")
            if (not isinstance(rows, list) or len(rows) > 500
                    or not isinstance(following, str) or len(following) > 1024
                    or not following.isascii() or any(ord(ch) < 32 for ch in following)):
                raise GrvtReadOnlyError("Historial de fills GRVT incompleto")
            for row in rows:
                if not isinstance(row, dict) or row.get("sub_account_id") != sub_account_id:
                    raise GrvtReadOnlyError("Fill GRVT de otra subcuenta")
                if row.get("order_id") != order["order_id"]:
                    continue
                if row.get("client_order_id") != client_order_id:
                    raise GrvtReadOnlyError("Fill GRVT de otra orden")
                trade_id = row.get("trade_id")
                instrument = row.get("instrument")
                currency = row.get("fee_currency")
                event_time = row.get("event_time")
                side, taker = row.get("is_buyer"), row.get("is_taker")
                if (not isinstance(trade_id, str) or not 1 <= len(trade_id) <= 100
                        or not re.fullmatch(r"[A-Za-z0-9_:\-]+", trade_id)
                        or trade_id in seen or not isinstance(instrument, str)
                        or not _INSTRUMENT.fullmatch(instrument)
                        or not isinstance(currency, str) or not _CURRENCY.fullmatch(currency)
                        or not isinstance(event_time, str) or not event_time.isdecimal()
                        or not int(created) <= int(event_time) <= self._clock_ns() + 2_000_000_000
                        or not isinstance(side, bool) or not isinstance(taker, bool)):
                    raise GrvtReadOnlyError("Fill GRVT malformado o duplicado")
                seen.add(trade_id)
                fills.append({"trade_id": trade_id, "event_time_ns": event_time,
                              "instrument": instrument, "side": "BUY" if side else "SELL",
                              "role": "TAKER" if taker else "MAKER",
                              "size": _decimal(row.get("size"), "Tamaño de fill", positive=True),
                              "price": _decimal(row.get("price"), "Precio de fill", positive=True),
                              "fee": _decimal(row.get("fee"), "Comisión de fill"),
                              "builder_fee": _decimal(row.get("builder_fee"), "Builder fee", nonnegative=True),
                              "fee_currency": currency,
                              "realized_pnl": _decimal(row.get("realized_pnl"), "P/L realizado")})
            if not following:
                break
            if following == cursor or following in cursors:
                raise GrvtReadOnlyError("Cursor de fills GRVT repetido")
            cursors.add(following)
            cursor = following
        else:
            raise GrvtReadOnlyError("Historial de fills GRVT excede el límite verificable")
        totals = {(leg["instrument"], leg["side"]): Decimal("0") for leg in order["legs"]}
        if len(totals) != len(order["legs"]):
            raise GrvtReadOnlyError("Tramos de orden GRVT duplicados")
        for fill in fills:
            key = (fill["instrument"], fill["side"])
            if key not in totals:
                raise GrvtReadOnlyError("Fill GRVT incompatible con la orden")
            totals[key] += Decimal(fill["size"])
        matches = all(totals[(leg["instrument"], leg["side"])] == Decimal(leg["traded_size"])
                      for leg in order["legs"])
        return {"source": "GRVT_MAINNET_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "account_ref": hashlib.sha256(self._account.lower().encode("ascii")).hexdigest(),
                "history_pages_drained": True, "fill_sizes_match_order_state": matches,
                "complete_for_execution": False, "order": order, "fills": fills}

    def snapshot(self) -> dict:
        aggregate = self._query("aggregated_account_summary").get("result")
        if (not isinstance(aggregate, dict) or not isinstance(aggregate.get("main_account_id"), str)
                or aggregate["main_account_id"].lower() != self._account.lower()):
            raise GrvtReadOnlyError("Resumen GRVT no corresponde a la cuenta")
        total = _decimal(aggregate.get("total_equity"), "Equity")
        sub_equity = _decimal(aggregate.get("total_sub_account_equity"), "Equity de trading")
        balances = _balances(aggregate.get("spot_balances"))
        sub_ids = self._sub_ids()
        trading = []
        for sub_id in sub_ids:
            row = self._query("account_summary", {"sub_account_id": sub_id}).get("result")
            if not isinstance(row, dict) or row.get("sub_account_id") != sub_id:
                raise GrvtReadOnlyError("Resumen GRVT de otra subcuenta")
            settle_currency = row.get("settle_currency")
            if not isinstance(settle_currency, str) or not _CURRENCY.fullmatch(settle_currency):
                raise GrvtReadOnlyError("Divisa de liquidación GRVT inválida")
            event_time = row.get("event_time")
            if not isinstance(event_time, str) or not event_time.isdecimal():
                raise GrvtReadOnlyError("Resumen GRVT sin hora verificable")
            event_ns, now_ns = int(event_time), self._clock_ns()
            if event_ns <= 0 or event_ns > now_ns + 2_000_000_000:
                raise GrvtReadOnlyError("Resumen GRVT con hora futura o inválida")
            positions = row.get("positions")
            if not isinstance(positions, list) or len(positions) > 500:
                raise GrvtReadOnlyError("Posiciones GRVT incompletas")
            parsed = []
            for position in positions:
                if not isinstance(position, dict) or position.get("sub_account_id") != sub_id:
                    raise GrvtReadOnlyError("Posición GRVT de otra subcuenta")
                instrument = position.get("instrument")
                if not isinstance(instrument, str) or not _INSTRUMENT.fullmatch(instrument):
                    raise GrvtReadOnlyError("Instrumento GRVT inválido")
                size = _decimal(position.get("size"), "Tamaño")
                if not Decimal(size):
                    continue
                parsed.append({"instrument": instrument, "side": "LONG" if Decimal(size) > 0 else "SHORT",
                               "size": size, "entry_price": _decimal(position.get("entry_price"), "Entrada", positive=True),
                               "mark_price": _decimal(position.get("mark_price"), "Mark", positive=True),
                               "unrealized_pnl": _decimal(position.get("unrealized_pnl"), "P/L")})
            trading.append({"sub_account_id": sub_id,
                            "settle_currency": settle_currency,
                            "total_equity": _decimal(row.get("total_equity"), "Equity de subcuenta"),
                            "maintenance_margin": _decimal(row.get("maintenance_margin"), "Mantenimiento", nonnegative=True),
                            "unrealized_pnl": _decimal(row.get("unrealized_pnl"), "P/L"),
                            "balances": _balances(row.get("spot_balances")),
                            "positions": parsed,
                            "event_time_ns": event_time,
                            "fresh": now_ns - event_ns <= 60_000_000_000})
        return {"source": "GRVT_MAINNET_PRIVATE_READONLY",
                "accounting": "OBSERVED_NOT_IMPORTED",
                "permission_verified": False,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "total_equity_usd": total,
                "trading_equity_usd": sub_equity,
                "spot_balances_aggregated": balances,
                "trading_accounts": trading}
