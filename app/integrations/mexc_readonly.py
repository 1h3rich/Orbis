"""Lectura puntual de MEXC Spot con firma HMAC; nunca envía órdenes.

La credencial pertenece a la sesión REAL desbloqueada y no se persiste. El
adaptador sólo conoce rutas GET permitidas: permisos, saldos, fills Spot y
transferencias recientes. Ningún dato observado se convierte aquí en un evento
contable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import hmac
import re
import time
from typing import Callable
from urllib.parse import urlencode

import httpx


MEXC_BASE = "https://api.mexc.com"
_TRANSFER_PATHS = {
    "deposit": "/api/v3/capital/deposit/hisrec",
    "withdrawal": "/api/v3/capital/withdraw/history",
}
_MX_DEDUCT_STATUS_PATH = "/api/v3/mxDeduct/enable"
_PATHS = frozenset(("/api/v3/apiKeyInfo", "/api/v3/account", "/api/v3/uid",
                    "/api/v3/order", "/api/v3/openOrders",
                    "/api/v3/myTrades", "/api/v3/capital/config/getall",
                    "/api/v3/capital/deposit/address",
                    "/api/v3/capital/withdraw/address",
                    _MX_DEDUCT_STATUS_PATH,
                    *_TRANSFER_PATHS.values()))
_ASSET = re.compile(r"^[A-Z0-9]{2,24}$")
_SYMBOL = re.compile(r"^[A-Z0-9]{3,30}$")
_CLIENT_ORDER_ID = re.compile(r"^[0-9a-f]{32}$")
_EXTERNAL_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_EVM_TX_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_DAY_MS = 86_400_000
_WINDOW_PAGE_LIMIT = 1000
_WINDOW_MAX_REQUESTS = 8
_TRANSFER_MAX_REQUESTS = 128
_TRANSFER_MAX_ROWS = 10000


class MexcReadOnlyError(RuntimeError):
    """Error seguro: no contiene URL, clave, firma ni respuesta bruta."""


def _amount(raw: object, *, positive: bool = False) -> Decimal:
    if not isinstance(raw, str) or len(raw) > 100:
        raise MexcReadOnlyError("MEXC devolvió un importe no decimal")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise MexcReadOnlyError("MEXC devolvió un importe no decimal") from None
    if (not value.is_finite() or len(value.as_tuple().digits) > 40
            or abs(value.as_tuple().exponent) > 100
            or value < 0 or positive and value == 0):
        raise MexcReadOnlyError("MEXC devolvió un importe inválido")
    return Decimal(0) if value == 0 else value


def _http_get(url: str, headers: dict[str, str]) -> dict | list:
    try:
        response = httpx.get(url, headers=headers, timeout=5.0,
                             follow_redirects=False, trust_env=False)
    except httpx.HTTPError:
        raise MexcReadOnlyError("No se pudo conectar con MEXC") from None
    if response.status_code == 429:
        raise MexcReadOnlyError("MEXC limita las consultas; espera antes de reintentar")
    if response.status_code in (401, 403):
        raise MexcReadOnlyError("MEXC rechazó la credencial o sus permisos")
    if response.status_code != 200:
        raise MexcReadOnlyError("MEXC no devolvió datos de cuenta")
    try:
        payload = response.json()
    except ValueError:
        raise MexcReadOnlyError("Respuesta de MEXC no válida") from None
    if not isinstance(payload, (dict, list)):
        raise MexcReadOnlyError("Respuesta de MEXC no válida")
    return payload


class MexcReadOnlyClient:
    """Cliente mainnet limitado por construcción a consultas GET de lectura."""

    def __init__(self, api_key: str, secret_key: str, *,
                 http_get: Callable[[str, dict[str, str]], dict | list] = _http_get,
                 clock_ms: Callable[[], int] | None = None):
        if (not isinstance(api_key, str) or not isinstance(secret_key, str)
                or not 8 <= len(api_key) <= 128 or not 8 <= len(secret_key) <= 256
                or not api_key.isascii() or not secret_key.isascii()
                or any(char.isspace() for char in api_key + secret_key)):
            raise MexcReadOnlyError("Formato de credencial MEXC no válido")
        self._api_key = api_key
        self._secret_key = secret_key
        self._http_get = http_get
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)

    def __repr__(self) -> str:
        return "MexcReadOnlyClient(<redacted>)"

    def account_reference(self) -> str:
        """Referencia local estable de esta clave View, sin persistir secretos."""
        return hmac.new(self._secret_key.encode("utf-8"),
                        self._api_key.encode("utf-8"), hashlib.sha256).hexdigest()

    def _signed_get(self, path: str, params: dict[str, str] | None = None) -> dict | list:
        if path not in _PATHS:
            raise MexcReadOnlyError("Consulta MEXC no permitida")
        timestamp = self._clock_ms()
        if type(timestamp) is not int or timestamp <= 0:
            raise MexcReadOnlyError("Reloj local no válido para MEXC")
        items = list((params or {}).items()) + [("recvWindow", "5000"), ("timestamp", str(timestamp))]
        query = urlencode(items)
        signature = hmac.new(self._secret_key.encode("ascii"), query.encode("ascii"),
                             hashlib.sha256).hexdigest()
        url = f"{MEXC_BASE}{path}?{query}&signature={signature}"
        try:
            payload = self._http_get(url, {"X-MEXC-APIKEY": self._api_key})
        except MexcReadOnlyError:
            raise
        except Exception:
            raise MexcReadOnlyError("No se pudo consultar MEXC") from None
        if path == _MX_DEDUCT_STATUS_PATH:
            if not isinstance(payload, dict) or type(payload.get("code")) is not int or payload["code"] != 0:
                raise MexcReadOnlyError("MEXC no confirmó el ajuste de comisiones en MX")
            return payload
        expected = list if path in ("/api/v3/myTrades", "/api/v3/openOrders",
                                     "/api/v3/capital/config/getall",
                                     "/api/v3/capital/deposit/address") or path in _TRANSFER_PATHS.values() else dict
        if (not isinstance(payload, expected)
                or isinstance(payload, dict) and "code" in payload and "msg" in payload):
            raise MexcReadOnlyError("MEXC no devolvió datos de cuenta válidos")
        return payload

    def verify_permissions(self) -> tuple[str, ...]:
        payload = self._signed_get("/api/v3/apiKeyInfo", {"accessKey": self._api_key})
        if payload.get("status") != "VALID":
            raise MexcReadOnlyError("La clave MEXC no está activa")
        raw = payload.get("permissions")
        if isinstance(raw, str):
            permissions = tuple(part.strip() for part in raw.split(",") if part.strip())
        elif isinstance(raw, list) and all(isinstance(part, str) for part in raw):
            permissions = tuple(raw)
        else:
            raise MexcReadOnlyError("MEXC no informó los permisos de la clave")
        if "SPOT_ACCOUNT_READ" not in permissions:
            raise MexcReadOnlyError("La clave requiere permiso SPOT_ACCOUNT_READ")
        if any(not re.fullmatch(r"[A-Z_]+_READ", part) for part in permissions):
            raise MexcReadOnlyError("La clave tiene permisos de escritura; crea una sólo lectura")
        echoed = payload.get("apikey", payload.get("accessKey"))
        if echoed is not None and (not isinstance(echoed, str) or
                                   not hmac.compare_digest(echoed, self._api_key)):
            raise MexcReadOnlyError("MEXC devolvió una clave distinta")
        validity = payload.get("remainingValidity")
        if validity is not None and (type(validity) is not int or
                                     (validity != -999 and validity <= 0)):
            raise MexcReadOnlyError("La clave MEXC está caducada o sin validez verificable")
        return permissions

    def ethereum_token_contracts(self) -> dict[str, str]:
        """Contratos EVM únicos publicados para depósitos ETH habilitados.

        Es metadato del exchange en el momento de la consulta, no una prueba
        histórica de identidad del token ni permiso de retirada.
        """
        return self.ethereum_token_contracts_by_direction()["deposit"]

    def bsc_withdrawal_route(self, address: str) -> dict:
        """Ruta USDT/BSC y destino guardado en MEXC; consulta, no autorización."""
        if (not isinstance(address,str) or not _EVM_ADDRESS.fullmatch(address)
                or int(address[2:],16) == 0):
            raise MexcReadOnlyError("Destino de retirada EVM inválido")
        if "SPOT_WITHDRAW_READ" not in self.verify_permissions():
            raise MexcReadOnlyError("La clave View requiere SPOT_WITHDRAW_READ")
        catalog = self._signed_get("/api/v3/capital/config/getall")
        coins = [coin for coin in catalog if isinstance(coin,dict) and coin.get("coin") == "USDT"]
        if len(coins) != 1 or not isinstance(coins[0].get("networkList"),list):
            raise MexcReadOnlyError("Catálogo USDT ambiguo")
        routes = [row for row in coins[0]["networkList"] if isinstance(row,dict) and row.get("netWork") == "BSC"]
        if len(routes) != 1 or routes[0].get("withdrawEnable") is not True:
            raise MexcReadOnlyError("MEXC no confirma retiradas USDT/BSC activas")
        route = routes[0]
        contract = route.get("contract")
        if (not isinstance(contract,str) or not _EVM_ADDRESS.fullmatch(contract) or int(contract[2:],16) == 0
                or route.get("coin","USDT") != "USDT"):
            raise MexcReadOnlyError("Contrato USDT/BSC no verificable")
        fee, minimum, maximum = (_amount(route.get(key),positive=key != "withdrawFee")
                                  for key in ("withdrawFee","withdrawMin","withdrawMax"))
        if minimum > maximum:
            raise MexcReadOnlyError("Límites de retirada inconsistentes")
        raw_step = route.get("withdrawIntegerMultiple")
        step = _amount(raw_step,positive=True) if raw_step is not None else None
        matches = 0
        expected_pages = expected_total = None
        total = 0
        for page in range(1,21):
            data = self._signed_get("/api/v3/capital/withdraw/address",{"coin":"USDT","page":str(page),"limit":"100"})
            rows,pages,count = data.get("data"),data.get("totalPageNum"),data.get("totalRecords")
            if (not isinstance(rows,list) or len(rows) > 100 or type(pages) is not int or not 0 <= pages <= 20
                    or type(count) is not int or not 0 <= count <= 2000 or data.get("page") != page
                    or pages == 0 and count != 0 or pages > 0 and count == 0
                    or expected_pages is not None and (pages,count) != (expected_pages,expected_total)):
                raise MexcReadOnlyError("Lista de destinos MEXC incompleta o cambiante")
            expected_pages,expected_total = pages,count
            total += len(rows)
            for row in rows:
                if not isinstance(row,dict):
                    raise MexcReadOnlyError("Destino MEXC malformado")
                if (row.get("coin") == "USDT" and row.get("network") in ("BSC","BEP20","BEP20(BSC)")
                        and isinstance(row.get("address"),str) and row["address"].lower() == address.lower()):
                    if row.get("memo") not in (None,""):
                        raise MexcReadOnlyError("Destino BSC con memo no admitido")
                    matches += 1
            if page >= pages:
                break
        if total != expected_total or matches != 1:
            raise MexcReadOnlyError("Guarda primero esta wallet USDT/BSC en MEXC; debe aparecer una sola vez")
        return {"source":"MEXC_SPOT_PRIVATE_READONLY","chain_id":"0x38","asset":"USDT","network":"BSC",
                "token_contract":contract.lower(),"fee":format(fee,"f"),"minimum":format(minimum,"f"),
                "maximum":format(maximum,"f"),"increment":format(step,"f") if step is not None else None,
                "recipient_listed":True,"observed_at":datetime.now(timezone.utc).isoformat()}

    def ethereum_erc20_deposit_destination(self, asset: str) -> dict:
        """Consulta un único destino ERC-20 temporal; no lo persiste ni envía.

        Sólo admite tokens con contrato ETH inequívoco y depósito habilitado
        en la ficha actual de MEXC. El usuario aún debe contrastar red, token
        y destino en el exchange antes de mover fondos.
        """
        return self.evm_token_deposit_destination(asset, chain_id="0x1")

    def evm_token_deposit_destination(self, asset: str, *, chain_id: str) -> dict:
        networks = {"0x1":("ETH", ("ERC20", "ETH"), "ETHEREUM_MAINNET", "ETH"),
                    "0x38":("BSC", ("BEP20(BSC)", "BSC", "BEP20"), "BNB_SMART_CHAIN_MAINNET", "BNB")}
        if chain_id not in networks:
            raise MexcReadOnlyError("Red EVM de depósito no admitida")
        _, aliases, network_name, native = networks[chain_id]
        if not isinstance(asset, str) or not _ASSET.fullmatch(asset) or asset == native:
            raise MexcReadOnlyError("Activo ERC-20 MEXC no válido")
        contract = self.evm_token_contracts_by_direction(chain_id=chain_id)["deposit"].get(asset)
        if contract is None:
            raise MexcReadOnlyError("MEXC no confirma un contrato ERC-20 único con depósitos activos")
        rows = self._signed_get("/api/v3/capital/deposit/address", {"coin": asset})
        if len(rows) > 100:
            raise MexcReadOnlyError("MEXC devolvió demasiados destinos de depósito")
        candidates = [row for row in rows if isinstance(row, dict)
                      and row.get("network") in aliases]
        if len(candidates) != 1:
            raise MexcReadOnlyError("MEXC no devolvió un destino único para esta red")
        row = candidates[0]
        address = row.get("address")
        if (row.get("coin") != asset or row.get("memo") not in (None, "")
                or not isinstance(address, str) or not _EVM_ADDRESS.fullmatch(address)
                or int(address[2:], 16) == 0):
            raise MexcReadOnlyError("Destino MEXC no verificable para esta red")
        return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "network": network_name, "asset": asset,
                "token_contract": contract, "deposit_address": address,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "stored": False, "transfer_sent": False, "ready_for_execution": False}

    def ethereum_token_contracts_by_direction(self) -> dict[str, dict[str, str]]:
        """Metadato ETH único para depósitos/retiros habilitados en una lectura.

        La ficha es actual, no prueba qué contrato se usó en una operación
        pasada ni demuestra que su código implemente un token seguro.
        """
        return self.evm_token_contracts_by_direction(chain_id="0x1")

    def evm_token_contracts_by_direction(self, *, chain_id: str) -> dict[str, dict[str, str]]:
        network_code = {"0x1":"ETH", "0x38":"BSC"}.get(chain_id)
        if network_code is None:
            raise MexcReadOnlyError("Red EVM de depósito no admitida")
        if "SPOT_WITHDRAW_READ" not in self.verify_permissions():
            raise MexcReadOnlyError("La clave requiere permiso SPOT_WITHDRAW_READ")
        payload = self._signed_get("/api/v3/capital/config/getall")
        if len(payload) > 5000:
            raise MexcReadOnlyError("Catálogo MEXC demasiado grande")
        found: dict[str, dict[str, set[str]]] = {"deposit": {}, "withdrawal": {}}
        ambiguous: dict[str, set[str]] = {"deposit": set(), "withdrawal": set()}
        for coin in payload:
            if not isinstance(coin, dict):
                raise MexcReadOnlyError("Catálogo MEXC inválido")
            asset, networks = coin.get("coin"), coin.get("networkList")
            if not isinstance(asset, str) or not _ASSET.fullmatch(asset):
                continue
            if not isinstance(networks, list) or len(networks) > 100:
                raise MexcReadOnlyError("Redes del catálogo MEXC inválidas")
            for network in networks:
                if not isinstance(network, dict):
                    raise MexcReadOnlyError("Red del catálogo MEXC inválida")
                if network.get("netWork") != network_code:
                    continue
                contract = network.get("contract")
                for direction, enabled_field in (("deposit", "depositEnable"),
                                                 ("withdrawal", "withdrawEnable")):
                    if (network.get(enabled_field) is not True
                            or network.get("coin", asset) != asset
                            or not isinstance(contract, str)
                            or not re.fullmatch(r"0x[0-9a-fA-F]{40}", contract)
                            or int(contract[2:], 16) == 0):
                        ambiguous[direction].add(asset)
                        continue
                    found[direction].setdefault(asset, set()).add(contract.lower())
        return {direction: {asset: next(iter(contracts))
                            for asset, contracts in found[direction].items()
                            if len(contracts) == 1 and asset not in ambiguous[direction]}
                for direction in ("deposit", "withdrawal")}

    def mx_deduct_enabled(self) -> bool:
        """Lee si la cuenta usa MX para comisiones Spot; no modifica el ajuste."""
        if "SPOT_DEAL_READ" not in self.verify_permissions():
            raise MexcReadOnlyError("La clave MEXC requiere permiso SPOT_DEAL_READ")
        payload = self._signed_get(_MX_DEDUCT_STATUS_PATH)
        data = payload.get("data")
        enabled = data.get("mxDeductEnable") if isinstance(data, dict) else None
        if type(enabled) is not bool:
            raise MexcReadOnlyError("MEXC no confirmó el ajuste de comisiones en MX")
        return enabled

    def balances(self) -> dict:
        self.verify_permissions()
        payload = self._signed_get("/api/v3/account")
        if payload.get("accountType") not in (None, "SPOT"):
            raise MexcReadOnlyError("MEXC no devolvió una cuenta Spot")
        rows = payload.get("balances")
        if not isinstance(rows, list):
            raise MexcReadOnlyError("MEXC no devolvió saldos Spot válidos")
        parsed = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                raise MexcReadOnlyError("MEXC devolvió un saldo malformado")
            asset = row.get("asset")
            if not isinstance(asset, str) or not _ASSET.fullmatch(asset) or asset in seen:
                raise MexcReadOnlyError("MEXC devolvió un activo inválido o duplicado")
            seen.add(asset)
            free = _amount(row.get("free"))
            locked = _amount(row.get("locked"))
            if free or locked:
                with localcontext() as context:
                    context.prec = 300
                    total = free + locked
                parsed.append({"asset": asset, "free": format(free, "f"),
                               "locked": format(locked, "f"),
                               "total": format(total, "f")})
        return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "balances": sorted(parsed, key=lambda row: row["asset"])}

    def account_uid(self) -> str:
        """UID privado de la cuenta, sólo para comparación en memoria.

        Nunca se devuelve por una ruta pública ni se usa como coste, saldo o
        referencia de archivo. Una clave Trade futura deberá consultar su
        propio UID y compararlo antes de compartir la misma política REAL.
        """
        self.verify_permissions()
        payload = self._signed_get("/api/v3/uid")
        uid = payload.get("uid")
        if not isinstance(uid, str) or not re.fullmatch(r"[1-9][0-9]{0,31}", uid):
            raise MexcReadOnlyError("MEXC no devolvió una identidad de cuenta verificable")
        return uid

    def order_by_client_id(self, symbol: str, client_order_id: str) -> dict:
        """Consulta una orden conocida; una ausencia/error NO prueba que no se envió.

        El endpoint sólo cubre siete días. Nunca se consulta como paso para
        reemitir automáticamente una intención de resultado incierto.
        """
        if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
            raise MexcReadOnlyError("Par Spot MEXC no válido")
        if not isinstance(client_order_id, str) or not _CLIENT_ORDER_ID.fullmatch(client_order_id):
            raise MexcReadOnlyError("ID de orden cliente MEXC no válido")
        if "SPOT_DEAL_READ" not in self.verify_permissions():
            raise MexcReadOnlyError("La clave MEXC requiere permiso SPOT_DEAL_READ")
        payload = self._signed_get("/api/v3/order", {
            "symbol": symbol, "origClientOrderId": client_order_id,
        })
        if (payload.get("symbol") != symbol or payload.get("clientOrderId") != client_order_id
                or payload.get("origClientOrderId", client_order_id) != client_order_id):
            raise MexcReadOnlyError("MEXC devolvió una orden con identidad distinta")
        if payload.get("type") != "LIMIT" or payload.get("side") not in ("BUY", "SELL"):
            raise MexcReadOnlyError("MEXC devolvió un tipo de orden inesperado")
        status = payload.get("status")
        if status not in ("NEW", "FILLED", "PARTIALLY_FILLED", "CANCELED",
                          "PARTIALLY_CANCELED"):
            raise MexcReadOnlyError("MEXC devolvió un estado de orden desconocido")
        quantity_raw = payload.get("Qty", payload.get("origQty"))
        if "Qty" in payload and "origQty" in payload and payload["Qty"] != payload["origQty"]:
            raise MexcReadOnlyError("MEXC devolvió cantidades de orden contradictorias")
        quantity = _amount(quantity_raw, positive=True)
        executed = _amount(payload.get("executedQty"))
        cumulative_quote = _amount(payload.get("cumulativeQuoteQty"))
        price = _amount(payload.get("price"), positive=True)
        order_id = payload.get("orderId")
        if type(order_id) is int:
            order_id = str(order_id)
        if not isinstance(order_id, str) or not _EXTERNAL_ID.fullmatch(order_id):
            raise MexcReadOnlyError("MEXC devolvió un ID de orden inválido")
        if executed > quantity or status == "NEW" and executed != 0 or (
                status == "FILLED" and executed != quantity) or (
                status in ("PARTIALLY_FILLED", "PARTIALLY_CANCELED")
                and not 0 < executed < quantity):
            raise MexcReadOnlyError("MEXC devolvió una ejecución de orden incoherente")
        return {"source": "MEXC_SPOT_PRIVATE_READONLY", "symbol": symbol,
                "client_order_id": client_order_id, "order_id": order_id,
                "side": payload["side"],
                "type": "LIMIT", "status": status, "price": format(price, "f"),
                "quantity": format(quantity, "f"), "executed_quantity": format(executed, "f"),
                "cumulative_quote": format(cumulative_quote, "f"),
                "accounting": "OBSERVED_NOT_IMPORTED"}

    def open_orders(self, symbol: str) -> dict:
        """Órdenes abiertas de un par exacto; no atribuye ninguna a ORBIS.

        MEXC no pagina esta consulta. Una respuesta excesiva o incoherente
        bloquea cualquier uso como prueba de riesgo, nunca se trunca.
        """
        if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
            raise MexcReadOnlyError("Par Spot MEXC no válido")
        return self._open_orders(symbol)

    def open_orders_unfiltered(self) -> dict:
        """Sonda sin símbolo para descubrir bloqueos, NO para probar cobertura.

        La documentación de MEXC menciona esta variante pero también marca
        `symbol` obligatorio y no garantiza paginación ni universo completo.
        Aunque la respuesta sea corta, nunca se etiqueta como cuenta íntegra.
        """
        return self._open_orders(None)

    def _open_orders(self, symbol: str | None) -> dict:
        if "SPOT_DEAL_READ" not in self.verify_permissions():
            raise MexcReadOnlyError("La clave MEXC requiere permiso SPOT_DEAL_READ")
        payload = self._signed_get("/api/v3/openOrders", {"symbol": symbol} if symbol else None)
        if not isinstance(payload, list) or len(payload) >= 1000:
            raise MexcReadOnlyError("Cobertura de órdenes abiertas MEXC no verificable")
        parsed = []
        order_ids = set()
        client_ids = set()
        for row in payload:
            row_symbol = row.get("symbol") if isinstance(row, dict) else None
            if (not isinstance(row, dict)
                    or not isinstance(row_symbol, str) or not _SYMBOL.fullmatch(row_symbol)
                    or symbol is not None and row_symbol != symbol
                    or row.get("side") not in ("BUY", "SELL")
                    or row.get("status") not in ("NEW", "PARTIALLY_FILLED")
                    or not isinstance(row.get("type"), str)
                    or not re.fullmatch(r"[A-Z_]{2,32}", row["type"])):
                raise MexcReadOnlyError("MEXC devolvió una orden abierta inválida")
            order_id = row.get("orderId")
            if type(order_id) is int:
                order_id = str(order_id)
            client_id = row.get("clientOrderId")
            if (not isinstance(order_id, str) or not _EXTERNAL_ID.fullmatch(order_id)
                    or not isinstance(client_id, str) or not _EXTERNAL_ID.fullmatch(client_id)
                    or order_id in order_ids or client_id in client_ids):
                raise MexcReadOnlyError("MEXC devolvió IDs de orden duplicados o inválidos")
            order_ids.add(order_id)
            client_ids.add(client_id)
            quantity = _amount(row.get("origQty"), positive=True)
            executed = _amount(row.get("executedQty"))
            if (executed >= quantity or row["status"] == "NEW" and executed != 0
                    or row["status"] == "PARTIALLY_FILLED" and executed == 0):
                raise MexcReadOnlyError("MEXC devolvió una orden abierta incoherente")
            price = _amount(row.get("price"), positive=row["type"] == "LIMIT")
            with localcontext() as context:
                context.prec = 300
                remaining = quantity - executed
            # Un resultado cuya precisión supera el modelo financiero local
            # no se redondea para aparentar una reserva válida.
            remaining = _amount(format(remaining, "f"), positive=True)
            parsed.append({
                "symbol": row_symbol, "order_id": order_id, "client_order_id": client_id,
                "side": row["side"], "type": row["type"], "status": row["status"],
                "price": format(price, "f"), "quantity": format(quantity, "f"),
                "executed_quantity": format(executed, "f"),
                "remaining_quantity": format(remaining, "f"),
            })
        return {"source": "MEXC_SPOT_PRIVATE_READONLY",
                "accounting": "OBSERVED_NOT_IMPORTED", "symbol": symbol,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "coverage": ("ALL_OPEN_ORDERS_FOR_ONE_SYMBOL" if symbol is not None
                             else "UNFILTERED_COVERAGE_UNPROVEN"),
                "orders": parsed}

    def trades_for_order(self, symbol: str, order_id: str) -> dict:
        """Obtiene fills de un único orderId; página llena = cobertura incierta."""
        if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
            raise MexcReadOnlyError("Par Spot MEXC no válido")
        if not isinstance(order_id, str) or not _EXTERNAL_ID.fullmatch(order_id):
            raise MexcReadOnlyError("ID de orden MEXC no válido")
        self.verify_permissions()
        payload = self._signed_get("/api/v3/myTrades", {
            "symbol": symbol, "orderId": order_id, "limit": "1000",
        })
        rows = self._parse_trades(symbol, payload, 1000)
        if len(rows) == 1000 or any(row["order_id"] != order_id for row in rows):
            raise MexcReadOnlyError("Fills MEXC de la orden incompletos o ajenos")
        return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "coverage": "ORDER_ID_BELOW_PAGE_LIMIT", "symbol": symbol,
                "order_id": order_id, "observed_at": datetime.now(timezone.utc).isoformat(),
                "trades": rows}

    def trades(self, symbol: str, *, limit: int = 100) -> dict:
        """Una página reciente de fills, no un historial completo ni coste fiscal."""
        if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
            raise MexcReadOnlyError("Par Spot MEXC no válido")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise MexcReadOnlyError("Límite de operaciones MEXC no válido")
        self.verify_permissions()
        payload = self._signed_get("/api/v3/myTrades", {"symbol": symbol, "limit": str(limit)})
        parsed = self._parse_trades(symbol, payload, limit)
        return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "coverage": "RECENT_MONTH_SINGLE_PAGE_INCOMPLETE", "symbol": symbol,
                "limit": limit, "observed_at": datetime.now(timezone.utc).isoformat(),
                "trades": parsed}

    @staticmethod
    def _parse_trades(symbol: str, payload: list, limit: int,
                      start_ms: int | None = None, end_ms: int | None = None) -> list[dict]:
        if not isinstance(payload, list) or len(payload) > limit:
            raise MexcReadOnlyError("MEXC devolvió operaciones inválidas")
        parsed = []
        seen = set()
        for row in payload:
            if not isinstance(row, dict) or row.get("symbol") != symbol or type(row.get("isBuyer")) is not bool:
                raise MexcReadOnlyError("MEXC devolvió una operación Spot inválida")
            trade_id = row.get("id")
            order_id = row.get("orderId")
            if type(trade_id) is int:
                trade_id = str(trade_id)
            if type(order_id) is int:
                order_id = str(order_id)
            if (not isinstance(trade_id, str) or not _EXTERNAL_ID.fullmatch(trade_id)
                    or trade_id in seen or not isinstance(order_id, str)
                    or not _EXTERNAL_ID.fullmatch(order_id)):
                raise MexcReadOnlyError("MEXC devolvió IDs de operación inválidos")
            seen.add(trade_id)
            timestamp = row.get("time")
            if type(timestamp) is not int or not 0 < timestamp <= 8_640_000_000_000_000:
                raise MexcReadOnlyError("MEXC devolvió una fecha de operación inválida")
            if (start_ms is not None and timestamp < start_ms
                    or end_ms is not None and timestamp > end_ms):
                raise MexcReadOnlyError("MEXC devolvió una operación fuera del rango solicitado")
            fee_asset = row.get("commissionAsset")
            if not isinstance(fee_asset, str) or not _ASSET.fullmatch(fee_asset):
                raise MexcReadOnlyError("MEXC devolvió un activo de comisión inválido")
            amounts = {}
            for source, target, positive in (("price", "price", True), ("qty", "quantity", True),
                                              ("quoteQty", "quote_quantity", True),
                                              ("commission", "commission", False)):
                amount = _amount(row.get(source), positive=positive)
                amounts[target] = format(amount, "f")
            parsed.append({"id": trade_id, "order_id": order_id,
                           "side": "BUY" if row["isBuyer"] else "SELL", "time_ms": timestamp,
                           "commission_asset": fee_asset, **amounts})
        return parsed

    def trades_window(self, symbol: str, *, days: int = 7) -> dict:
        """Recorre un rango reciente por tiempo; nunca afirma conocer meses anteriores.

        Una página llena no demuestra cobertura. Se divide el intervalo hasta
        obtener páginas bajo el límite, o falla íntegramente si no es posible.
        """
        if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
            raise MexcReadOnlyError("Par Spot MEXC no válido")
        if type(days) is not int or days not in (1, 7, 30):
            raise MexcReadOnlyError("Periodo MEXC no admitido")
        end_ms = self._clock_ms()
        if type(end_ms) is not int or end_ms <= 30 * _DAY_MS:
            raise MexcReadOnlyError("Reloj local no válido para MEXC")
        start_ms = end_ms - days * _DAY_MS
        self.verify_permissions()
        requests = 0
        collected: dict[str, dict] = {}

        def scan(first: int, last: int) -> None:
            nonlocal requests
            if requests >= _WINDOW_MAX_REQUESTS:
                raise MexcReadOnlyError("Rango MEXC demasiado denso; consulta un periodo menor")
            requests += 1
            raw = self._signed_get("/api/v3/myTrades", {
                "symbol": symbol, "startTime": str(first), "endTime": str(last),
                "limit": str(_WINDOW_PAGE_LIMIT),
            })
            page = self._parse_trades(symbol, raw, _WINDOW_PAGE_LIMIT, first, last)
            if len(page) == _WINDOW_PAGE_LIMIT:
                if first == last:
                    raise MexcReadOnlyError("Más de 1.000 fills MEXC en un milisegundo; rango incompleto")
                middle = first + (last - first) // 2
                scan(first, middle)
                scan(middle + 1, last)
                return
            for row in page:
                if row["id"] in collected:
                    raise MexcReadOnlyError("MEXC devolvió un fill repetido entre rangos")
                collected[row["id"]] = row

        scan(start_ms, end_ms)
        return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "coverage": "RECENT_MONTH_WINDOW_BELOW_PAGE_LIMIT", "symbol": symbol,
                "limit": _WINDOW_PAGE_LIMIT, "start_ms": start_ms, "end_ms": end_ms,
                "pages": requests, "observed_at": datetime.now(timezone.utc).isoformat(),
                "trades": sorted(collected.values(), key=lambda row: (row["time_ms"], row["id"]))}

    def transfers_recent(self, *, days: int = 7, include_source_ids: bool = False,
                         include_withdrawal_evm: bool = False) -> dict:
        """Observa depósitos/retiros hasta 90 días; sin direcciones ni memos.

        Cada petición abarca como máximo siete días. Una página llena no
        demuestra cobertura; se subdivide o se rechaza el lote entero.
        """
        if include_withdrawal_evm and not include_source_ids:
            raise MexcReadOnlyError("La conciliación de retirada requiere identidades transitorias internas")
        if type(days) is not int or days not in (1, 7, 30, 90):
            raise MexcReadOnlyError("Periodo de transferencias MEXC no admitido")
        end_ms = self._clock_ms()
        if type(end_ms) is not int or end_ms <= 90 * _DAY_MS:
            raise MexcReadOnlyError("Reloj local no válido para MEXC")
        # Un margen evita que la primera ventana de 90 días caduque mientras
        # se realiza la petición. Se expone el rango efectivo, sin prometer
        # una cobertura de minutos que el proveedor ya no deja consultar.
        start_ms = end_ms - days * _DAY_MS + (60_000 if days == 90 else 0)
        permissions = self.verify_permissions()
        if "SPOT_WITHDRAW_READ" not in permissions:
            raise MexcReadOnlyError("La clave requiere permiso SPOT_WITHDRAW_READ para consultar ingresos y retiros")
        result: dict[str, list[dict]] = {}
        requests = 0
        for direction, path in _TRANSFER_PATHS.items():
            rows = []

            def scan(first: int, last: int) -> None:
                nonlocal requests
                if requests >= _TRANSFER_MAX_REQUESTS:
                    raise MexcReadOnlyError("Historial MEXC demasiado denso; consulta un periodo menor")
                requests += 1
                payload = self._signed_get(path, {
                    "startTime": str(first), "endTime": str(last), "limit": "1000",
                })
                if len(payload) >= 1000:
                    if first == last:
                        raise MexcReadOnlyError("Más de 1.000 transferencias MEXC en un milisegundo; rango incompleto")
                    middle = first + (last - first) // 2
                    before = len(rows)
                    scan(first, middle)
                    scan(middle + 1, last)
                    if len(rows) - before < len(payload):
                        raise MexcReadOnlyError("MEXC devolvió páginas de transferencias contradictorias")
                    return
                for item in payload:
                    if not isinstance(item, dict):
                        raise MexcReadOnlyError("MEXC devolvió una transferencia inválida")
                    asset = item.get("coin")
                    if not isinstance(asset, str) or not _ASSET.fullmatch(asset):
                        raise MexcReadOnlyError("MEXC devolvió un activo de transferencia inválido")
                    stamp = item.get("insertTime" if direction == "deposit" else "applyTime")
                    if type(stamp) is not int or not first <= stamp <= last:
                        raise MexcReadOnlyError("MEXC devolvió una transferencia fuera del rango")
                    status = item.get("status")
                    if type(status) is not int or not 0 <= status <= 20:
                        raise MexcReadOnlyError("MEXC devolvió un estado de transferencia inválido")
                    network = item.get("network")
                    if network is not None and (not isinstance(network, str) or len(network) > 40
                                                or not re.fullmatch(r"[A-Za-z0-9()._-]+", network)):
                        raise MexcReadOnlyError("MEXC devolvió una red inválida")
                    amount = _amount(item.get("amount"), positive=True)
                    fee = _amount(item.get("transactionFee", "0")) if direction == "withdrawal" else Decimal(0)
                    row = {"direction": direction, "asset": asset, "network": network,
                           "amount": format(amount, "f"), "fee": format(fee, "f"),
                           "status": status, "time_ms": stamp}
                    if direction == "withdrawal":
                        route = item.get("transferType")
                        if route is not None and (type(route) is not int or route not in (0, 1)):
                            raise MexcReadOnlyError("MEXC devolvió un tipo de retiro inválido")
                        row["transfer_type"] = route
                    if include_source_ids:
                        source_id = item.get("txId" if direction == "deposit" else "id")
                        if source_id is not None and (not isinstance(source_id, str)
                                                       or not 1 <= len(source_id) <= 256
                                                       or not source_id.isascii()
                                                       or not source_id.isprintable()):
                            raise MexcReadOnlyError("MEXC devolvió un identificador de traslado inválido")
                        row["source_identity"] = source_id
                        if direction == "deposit" and network in ("ERC20", "ETH", "BSC", "BEP20", "BEP20(BSC)"):
                            destination = item.get("address")
                            if (isinstance(destination, str)
                                    and _EVM_ADDRESS.fullmatch(destination)
                                    and not item.get("memo") and not item.get("addressTag")):
                                row["destination_identity"] = destination
                        if direction == "withdrawal" and include_withdrawal_evm and item.get("withdrawOrderId") is not None:
                            request_identity = item["withdrawOrderId"]
                            if not isinstance(request_identity,str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}",request_identity):
                                raise MexcReadOnlyError("Referencia de solicitud de retirada inválida")
                            row["request_identity"] = request_identity
                        if direction == "withdrawal" and (network in ("ERC20", "ETH") or
                                include_withdrawal_evm and network in (None,"BSC","BEP20","BEP20(BSC)")):
                            chain_hash = item.get("transHash")
                            fallback_hash = item.get("txId")
                            if not isinstance(chain_hash, str) or not _EVM_TX_HASH.fullmatch(chain_hash):
                                chain_hash = fallback_hash
                            # La documentación no establece precedencia si ambos son
                            # hashes distintos: no inventar una identidad de cadena.
                            if (isinstance(chain_hash, str) and _EVM_TX_HASH.fullmatch(chain_hash)
                                    and (not isinstance(fallback_hash, str)
                                         or not _EVM_TX_HASH.fullmatch(fallback_hash)
                                         or fallback_hash.lower() == chain_hash.lower())):
                                row["chain_identity"] = chain_hash.lower()
                            destination = item.get("address")
                            if (isinstance(destination, str) and _EVM_ADDRESS.fullmatch(destination)
                                    and not item.get("memo")):
                                row["destination_identity"] = destination
                    rows.append(row)
                    if len(rows) > _TRANSFER_MAX_ROWS:
                        raise MexcReadOnlyError("Historial MEXC demasiado grande; consulta un periodo menor")

            cursor = start_ms
            while cursor <= end_ms:
                window_end = min(end_ms, cursor + 7 * _DAY_MS)
                scan(cursor, window_end)
                cursor = window_end + 1
            result[direction + "s"] = sorted(rows, key=lambda row: row["time_ms"], reverse=True)
        return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "coverage": "RECENT_WINDOW_BELOW_PAGE_LIMIT", "start_ms": start_ms,
                "end_ms": end_ms, "requests": requests,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "deposits": result["deposits"], "withdrawals": result["withdrawals"]}
