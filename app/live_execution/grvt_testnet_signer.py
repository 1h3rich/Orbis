"""Firma EIP-712 de una orden límite GRVT *testnet* con clave dedicada.

Sin API/UI ni almacenamiento de la clave. Sólo se aceptan metadatos del
endpoint público testnet y conversiones decimales exactas, sin truncamiento.
El firmante NO autoriza por sí mismo operar: falta conciliación y permiso de
Trade de la cuenta testnet, que GRVT verificará al recibir la orden.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
import re
import secrets
import time
from typing import Callable

from eth_account import Account
import httpx
from app.live_execution.grvt_order_codec import (ORDER_TYPES as _ORDER_TYPES,
    ORDER_WITHOUT_BUILDER_TYPES as _ORDER_WITHOUT_BUILDER_TYPES,order_signable)

from app.live_execution.grvt_testnet_order import (
    GrvtTestnetLimits, _ADDRESS, _INSTRUMENT, _validate_limit_contract,
)


INSTRUMENT_URL = "https://market-data.testnet.grvt.io/full/v1/instrument"
_CLIENT_MIN = 2**63
_UINT64_MAX = 2**64 - 1
_UINT256_MAX = 2**256 - 1


def _order_signable(message: dict):
    """El SDK selecciona Order sin builder, no OrderWithBuilderFee a fee cero."""
    return order_signable(message,chain_id=326)


class GrvtTestnetSignError(RuntimeError):
    """Error seguro sin clave, firma, metadatos brutos ni payload EIP-712."""


@dataclass(frozen=True, slots=True)
class GrvtTestnetInstrument:
    name: str
    asset_id: int
    base_decimals: int
    tick_size: Decimal
    min_size: Decimal
    min_notional: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _INSTRUMENT.fullmatch(self.name):
            raise GrvtTestnetSignError("Instrumento testnet inválido")
        if type(self.asset_id) is not int or not 0 < self.asset_id <= _UINT256_MAX:
            raise GrvtTestnetSignError("ID de instrumento testnet inválido")
        if type(self.base_decimals) is not int or not -6 <= self.base_decimals <= 18:
            raise GrvtTestnetSignError("Precisión de instrumento testnet inválida")
        for field in ("tick_size", "min_size", "min_notional"):
            value = getattr(self, field)
            if (type(value) is not Decimal or not value.is_finite()
                    or value <= 0 or len(value.as_tuple().digits) > 30):
                raise GrvtTestnetSignError("Reglas de instrumento testnet inválidas")


@dataclass(frozen=True, slots=True)
class GrvtTestnetLimitDraft:
    sub_account_id: str
    instrument: str
    side: str
    size: Decimal
    limit_price: Decimal
    reduce_only: bool
    builder: str
    client_order_id: str
    nonce: int
    expiration_ns: int


def _positive_decimal(value: object) -> Decimal:
    if type(value) is not Decimal or not value.is_finite() or value <= 0:
        raise GrvtTestnetSignError("Cantidad o precio testnet inválido")
    if len(value.as_tuple().digits) > 30 or abs(value.as_tuple().exponent) > 18:
        raise GrvtTestnetSignError("Cantidad o precio testnet fuera de rango")
    return value


def _exact_uint(value: Decimal, scale: int, max_value: int) -> int:
    with localcontext() as context:
        context.prec = 100
        scaled = value * (Decimal(10) ** scale)
    if scaled != scaled.to_integral_value() or not 0 < scaled <= max_value:
        raise GrvtTestnetSignError("Precisión de orden incompatible con GRVT")
    return int(scaled)


def _http_post(url: str, body: dict) -> httpx.Response:
    return httpx.post(url, json=body, timeout=5.0,
                      follow_redirects=False, trust_env=False)


def fetch_testnet_instrument(instrument: str, *,
                             http_post: Callable[[str, dict], httpx.Response] = _http_post,
                             ) -> GrvtTestnetInstrument:
    """Lee identidad y mínimos del instrumento sólo desde GRVT testnet."""
    if not isinstance(instrument, str) or not _INSTRUMENT.fullmatch(instrument):
        raise GrvtTestnetSignError("Instrumento testnet inválido")
    try:
        response = http_post(INSTRUMENT_URL, {"instrument": instrument})
        body = response.json() if response.status_code == 200 else None
        row = body.get("result") if isinstance(body, dict) and "error" not in body and "code" not in body else None
        if (not isinstance(row, dict) or row.get("instrument") != instrument
                or row.get("kind") != "PERPETUAL"
                or not isinstance(row.get("venues"), list)
                or "ORDERBOOK" not in row["venues"]):
            raise ValueError("instrument mismatch")
        raw_hash = row["instrument_hash"]
        if (not isinstance(raw_hash, str) or not re.fullmatch(
                r"(?:0x[0-9a-fA-F]{1,64}|[0-9]{1,78})", raw_hash)):
            raise ValueError("invalid hash")
        asset_id = int(raw_hash, 16 if raw_hash.startswith("0x") else 10)
        decimals = row["base_decimals"]
        if type(decimals) is not int:
            raise ValueError("invalid decimals")
        def decimal_field(name: str) -> Decimal:
            raw = row[name]
            if not isinstance(raw, str) or not re.fullmatch(r"[0-9]{1,30}(?:\.[0-9]{1,18})?", raw):
                raise ValueError("invalid decimal")
            return Decimal(raw)
        return GrvtTestnetInstrument(instrument, asset_id, decimals,
                                     decimal_field("tick_size"),
                                     decimal_field("min_size"),
                                     decimal_field("min_notional"))
    except Exception:
        raise GrvtTestnetSignError("Metadatos testnet no verificables") from None


def sign_testnet_limit(draft: GrvtTestnetLimitDraft,
                       limits: GrvtTestnetLimits,
                       instrument: GrvtTestnetInstrument,
                       *, dedicated_private_key: bytes,
                       expected_signer: str,
                       now_ns: int | None = None) -> dict:
    """Devuelve la orden firmada; jamás expone ni persiste la clave privada."""
    return _sign_limit(draft, limits, instrument, dedicated_private_key=dedicated_private_key,
                       expected_signer=expected_signer, now_ns=now_ns)


def _sign_limit(draft: GrvtTestnetLimitDraft, limits: GrvtTestnetLimits,
                instrument: GrvtTestnetInstrument, *, dedicated_private_key: bytes,
                expected_signer: str, now_ns: int | None,
                time_in_force: str = "IMMEDIATE_OR_CANCEL", time_in_force_code: int = 3,
                lifetime_ns: int = 300_000_000_000, metadata: dict | None = None) -> dict:
    current_ns = time.time_ns() if now_ns is None else now_ns
    if type(current_ns) is not int or current_ns <= 0:
        raise GrvtTestnetSignError("Reloj testnet inválido")
    if (not isinstance(draft, GrvtTestnetLimitDraft)
            or draft.sub_account_id != limits.sub_account_id
            or draft.instrument != limits.instrument
            or instrument.name != limits.instrument
            or draft.side not in ("BUY", "SELL")
            or type(draft.reduce_only) is not bool
            or not isinstance(draft.builder, str) or not _ADDRESS.fullmatch(draft.builder)
            or not isinstance(expected_signer, str) or not _ADDRESS.fullmatch(expected_signer)
            or not isinstance(draft.client_order_id, str) or not draft.client_order_id.isdecimal()
            or not _CLIENT_MIN <= int(draft.client_order_id) <= _UINT64_MAX
            or type(draft.nonce) is not int or not 0 <= draft.nonce <= 2**32 - 1
            or type(draft.expiration_ns) is not int
            or not current_ns < draft.expiration_ns <= current_ns + lifetime_ns):
        raise GrvtTestnetSignError("Borrador de orden testnet inválido")
    size = _positive_decimal(draft.size)
    price = _positive_decimal(draft.limit_price)
    with localcontext() as context:
        context.prec = 100
        notional = size * price
        if size < instrument.min_size or notional < instrument.min_notional:
            raise GrvtTestnetSignError("Orden inferior al mínimo GRVT testnet")
        if notional > limits.max_order_notional:
            raise GrvtTestnetSignError("Orden superior al límite testnet")
        if price % instrument.tick_size:
            raise GrvtTestnetSignError("Precio fuera del tick GRVT testnet")
    contract_size = _exact_uint(size, instrument.base_decimals, _UINT64_MAX)
    limit_price = _exact_uint(price, 9, _UINT64_MAX)
    if type(dedicated_private_key) is not bytes or len(dedicated_private_key) != 32:
        raise GrvtTestnetSignError("Clave de firma dedicada inválida")
    try:
        account = Account.from_key(dedicated_private_key)
        if account.address.lower() != expected_signer.lower():
            raise ValueError("wrong signer")
        message = {
            "subAccountID": int(draft.sub_account_id),
            "isMarket": False, "timeInForce": time_in_force_code, "postOnly": False,
            "reduceOnly": draft.reduce_only,
            "legs": [{"assetID": instrument.asset_id,
                      "contractSize": contract_size, "limitPrice": limit_price,
                      "isBuyingContract": draft.side == "BUY"}],
            "builder": draft.builder, "builderFee": 0,
            "nonce": draft.nonce, "expiration": draft.expiration_ns,
        }
        signable = _order_signable(message)
        signed = account.sign_message(signable)
        signature = {"signer": account.address,
                     "r": "0x" + signed.r.to_bytes(32, "big").hex(),
                     "s": "0x" + signed.s.to_bytes(32, "big").hex(),
                     "v": signed.v, "expiration": str(draft.expiration_ns),
                     "nonce": draft.nonce, "chain_id": "326"}
    except Exception:
        raise GrvtTestnetSignError("No se pudo firmar la orden testnet") from None
    order = {
        "sub_account_id": draft.sub_account_id,
        "is_market": False, "time_in_force": time_in_force,
        "post_only": False, "reduce_only": draft.reduce_only,
        "legs": [{"instrument": draft.instrument, "size": format(size, "f"),
                  "limit_price": format(price, "f"),
                  "is_buying_asset": draft.side == "BUY"}],
        "signature": signature,
        "metadata": {"client_order_id": draft.client_order_id, **(metadata or {})},
        "builder": draft.builder, "builder_fee": "0",
    }
    _validate_limit_contract(order, limits, current_ns, time_in_force=time_in_force,
        metadata_fields={"client_order_id", *(metadata or {})}, lifetime_ns=lifetime_ns)
    return order


def verify_testnet_limit_signature(order: dict, limits: GrvtTestnetLimits,
                                   instrument: GrvtTestnetInstrument,
                                   *, now_ns: int | None = None) -> str:
    """Comprueba que la firma EIP-712 testnet cubre la orden exacta.

    La verificación local no demuestra que GRVT haya autorizado al firmante
    ni que una orden haya sido aceptada o ejecutada.
    """
    return _verify_limit(order, limits, instrument, now_ns=now_ns)


def _verify_limit(order: dict, limits: GrvtTestnetLimits, instrument: GrvtTestnetInstrument,
                   *, now_ns: int | None, time_in_force: str = "IMMEDIATE_OR_CANCEL",
                   time_in_force_code: int = 3, lifetime_ns: int = 300_000_000_000,
                   metadata_fields: set[str] | None = None) -> str:
    current_ns = time.time_ns() if now_ns is None else now_ns
    try:
        _validate_limit_contract(order, limits, current_ns, time_in_force=time_in_force,
            metadata_fields=metadata_fields or {"client_order_id"}, lifetime_ns=lifetime_ns)
        if not isinstance(instrument, GrvtTestnetInstrument) or instrument.name != limits.instrument:
            raise ValueError("instrument mismatch")
        leg = order["legs"][0]
        size = _positive_decimal(Decimal(leg["size"]))
        price = _positive_decimal(Decimal(leg["limit_price"]))
        with localcontext() as context:
            context.prec = 100
            if (size < instrument.min_size or size * price < instrument.min_notional
                    or price % instrument.tick_size):
                raise ValueError("instrument rules")
        signature = order["signature"]
        message = {
            "subAccountID": int(order["sub_account_id"]),
            "isMarket": False, "timeInForce": time_in_force_code, "postOnly": False,
            "reduceOnly": order["reduce_only"],
            "legs": [{"assetID": instrument.asset_id,
                      "contractSize": _exact_uint(size, instrument.base_decimals, _UINT64_MAX),
                      "limitPrice": _exact_uint(price, 9, _UINT64_MAX),
                      "isBuyingContract": leg["is_buying_asset"]}],
            "builder": order["builder"], "builderFee": 0,
            "nonce": signature["nonce"], "expiration": int(signature["expiration"]),
        }
        signable = _order_signable(message)
        recovered = Account.recover_message(
            signable,
            vrs=(signature["v"], int(signature["r"], 16), int(signature["s"], 16)),
        )
        if recovered.lower() != signature["signer"].lower():
            raise ValueError("signer mismatch")
        return recovered
    except Exception:
        raise GrvtTestnetSignError("Firma u orden GRVT testnet no verificable") from None


def verify_testnet_limit_from_public_instrument(order: dict, limits: GrvtTestnetLimits,
                                                *, now_ns: int | None = None) -> str:
    """Lee reglas testnet frescas y verifica la firma antes de cualquier POST de orden."""
    instrument = fetch_testnet_instrument(limits.instrument)
    return verify_testnet_limit_signature(order, limits, instrument, now_ns=now_ns)


def new_testnet_ids() -> tuple[str, int]:
    """IDs independientes; no son autoridad para enviar una orden."""
    return str(_CLIENT_MIN + secrets.randbits(63)), secrets.randbits(32)
