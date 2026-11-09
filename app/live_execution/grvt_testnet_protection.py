"""Contratos nativos TP/SL TESTNET, distintos del puente IOC existente.

GRVT no incluye metadata.trigger en EIP-712. Su integridad depende del
contrato local y de contrastar la respuesta del exchange, no de la firma.
Este módulo compila/verifica; no autoriza ni envía órdenes por sí mismo.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
import re
import time

from app.live_execution.grvt_testnet_order import (
    GrvtTestnetLimits, GrvtTestnetOrderObservation, _ADDRESS, _validate_limit_contract, _wire_equal,
)
from app.integrations.grvt_readonly import _order as _parse_order
from app.live_execution.grvt_testnet_signer import (
    GrvtTestnetInstrument, GrvtTestnetLimitDraft, GrvtTestnetSignError,
    _positive_decimal, _exact_uint, _UINT64_MAX, _sign_limit, _verify_limit,
)
from app.live_execution.grvt_testnet_quote import GrvtTestnetQuote, guard_testnet_limit_price
from app.live_execution.grvt_testnet_risk import GrvtTestnetRiskState, evaluate_testnet_risk
from app.derivatives.paper_math import MarginRules


PROTECTION_LIFETIME_NS = 86_400_000_000_000  # Política ORBIS: máximo 24 h.


@dataclass(frozen=True, slots=True)
class GrvtTestnetProtectionObservation:
    order: GrvtTestnetOrderObservation
    trigger: GrvtTestnetTrigger
    expiration_ns: int
    active_observed: bool
    protection_guaranteed: bool = False


@dataclass(frozen=True, slots=True)
class GrvtTestnetTrigger:
    kind: str
    price: Decimal

    def __post_init__(self) -> None:
        if self.kind not in ("STOP_LOSS", "TAKE_PROFIT"):
            raise GrvtTestnetSignError("Tipo TP/SL testnet inválido")
        _exact_uint(_positive_decimal(self.price), 9, _UINT64_MAX)

    def metadata(self) -> dict:
        return {"trigger_type": self.kind, "tpsl": {
            "trigger_by": "LAST", "trigger_price": format(self.price, "f"),
            "close_position": False, "is_split_position": False,
        }}


def compile_testnet_protection(state: GrvtTestnetRiskState, rules: MarginRules,
                               instrument: GrvtTestnetInstrument, limits: GrvtTestnetLimits,
                               quote: GrvtTestnetQuote, *, trigger: GrvtTestnetTrigger,
                               max_slippage_bps: int, builder: str, client_order_id: str,
                               nonce: int, expiration_ns: int, now_ns: int) -> GrvtTestnetLimitDraft:
    """Planifica la reducción completa observada, no un tamaño de capital PAPER.

    LAST decide el disparador; bid/ask sólo comprueban frescura y spread. El
    redondeo al tick nunca empeora el presupuesto de deslizamiento del límite.
    Esto no reserva ni autoriza un POST: requiere diario y revalidación al enviar.
    """
    try:
        if (not isinstance(state, GrvtTestnetRiskState)
                or not isinstance(instrument, GrvtTestnetInstrument)
                or not isinstance(quote, GrvtTestnetQuote)
                or not isinstance(trigger, GrvtTestnetTrigger)
                or quote.instrument != limits.instrument or instrument.name != limits.instrument
                or type(max_slippage_bps) is not int or not 0 <= max_slippage_bps <= 100
                or not isinstance(builder, str) or not _ADDRESS.fullmatch(builder)
                or not isinstance(client_order_id, str)
                or not re.fullmatch(r"[0-9]{19,20}", client_order_id)
                or not 2**63 <= int(client_order_id) <= 2**64 - 1
                or type(nonce) is not int or not 0 <= nonce < 2**32
                or type(expiration_ns) is not int or type(now_ns) is not int
                or not now_ns + 60_000_000_000 <= expiration_ns <= now_ns + PROTECTION_LIFETIME_NS
                or len(state.positions) != 1 or state.positions[0][0] != limits.instrument):
            raise ValueError("incompatible protection scope")
        last = _positive_decimal(quote.last_price)
        held = state.positions[0][1]
        if type(held) is not Decimal or not held.is_finite() or not held:
            raise ValueError("missing position")
        buying = held < 0
        should_be_higher = buying == (trigger.kind == "STOP_LOSS")
        if (trigger.price <= last if should_be_higher else trigger.price >= last):
            raise ValueError("trigger already met")
        guard_testnet_limit_price("BUY" if buying else "SELL",
            quote.ask if buying else quote.bid, quote,
            max_spread_bps=100, max_slippage_bps=0, now_ns=now_ns)
        with localcontext() as ctx:
            ctx.prec = 100
            if trigger.price % instrument.tick_size:
                raise ValueError("trigger tick")
            multiplier = Decimal(1) + (Decimal(max_slippage_bps) / 10_000) * (1 if buying else -1)
            ticks = (trigger.price * multiplier / instrument.tick_size).to_integral_value(
                rounding=ROUND_FLOOR if buying else ROUND_CEILING)
            price = ticks * instrument.tick_size
            size = held.copy_abs()
            if size * max(last, trigger.price, price) > limits.max_order_notional:
                raise ValueError("position outside notional cap")
            if size < instrument.min_size or size * price < instrument.min_notional:
                raise ValueError("instrument minimum")
            _exact_uint(size, instrument.base_decimals, _UINT64_MAX)
            _exact_uint(price, 9, _UINT64_MAX)
            draft = GrvtTestnetLimitDraft(limits.sub_account_id, limits.instrument,
                "BUY" if buying else "SELL", size, price, True, builder,
                client_order_id, nonce, expiration_ns)
            decision = evaluate_testnet_risk(draft, limits, state, rules, now_ns=now_ns)
            if not decision.allowed:
                raise ValueError("risk blocked")
        return draft
    except Exception:
        raise GrvtTestnetSignError("Plan TP/SL testnet no verificable") from None


def validate_testnet_protection(order: dict, limits: GrvtTestnetLimits, *, now_ns: int,
                                expected_trigger: GrvtTestnetTrigger | None = None) -> str:
    """Sólo GTT limitada, reduce-only, tamaño explícito y disparador LAST."""
    client_id = _validate_limit_contract(order, limits, now_ns,
        time_in_force="GOOD_TILL_TIME", metadata_fields={"client_order_id", "trigger"},
        lifetime_ns=PROTECTION_LIFETIME_NS)
    try:
        raw = order["metadata"]["trigger"]
        tpsl = raw["tpsl"]
        if (order["reduce_only"] is not True or set(raw) != {"trigger_type", "tpsl"}
                or set(tpsl) != {"trigger_by", "trigger_price", "close_position", "is_split_position"}
                or tpsl["trigger_by"] != "LAST" or tpsl["close_position"] is not False
                or tpsl["is_split_position"] is not False
                or not isinstance(tpsl["trigger_price"], str)):
            raise ValueError("invalid trigger")
        trigger = GrvtTestnetTrigger(raw["trigger_type"], Decimal(tpsl["trigger_price"]))
        if raw != trigger.metadata() or expected_trigger is not None and trigger != expected_trigger:
            raise ValueError("trigger mismatch")
        price = Decimal(order["legs"][0]["limit_price"])
        with localcontext() as ctx:
            ctx.prec = 100
            # Nunca un límite peor que 1 % respecto al disparador, ni pasivo
            # en el instante de disparo. Un gap aún puede impedir el fill.
            deviation = (price - trigger.price) * 10_000 / trigger.price
        if order["legs"][0]["is_buying_asset"]:
            if not 0 <= deviation <= 100:
                raise ValueError("buy protection bound")
        elif not -100 <= deviation <= 0:
            raise ValueError("sell protection bound")
    except Exception:
        raise GrvtTestnetSignError("Contrato TP/SL testnet no verificable") from None
    return client_id


def sign_testnet_protection(draft: GrvtTestnetLimitDraft, limits: GrvtTestnetLimits,
                            instrument: GrvtTestnetInstrument, trigger: GrvtTestnetTrigger, *,
                            dedicated_private_key: bytes, expected_signer: str,
                            now_ns: int | None = None) -> dict:
    """Firma GTT testnet; el disparador se valida pero NO está firmado por GRVT."""
    current = time.time_ns() if now_ns is None else now_ns
    if (not isinstance(draft, GrvtTestnetLimitDraft)
            or not isinstance(instrument, GrvtTestnetInstrument)
            or not isinstance(trigger, GrvtTestnetTrigger) or draft.reduce_only is not True):
        raise GrvtTestnetSignError("Protección testnet debe ser reduce-only")
    with localcontext() as ctx:
        ctx.prec = 100
        if trigger.price % instrument.tick_size:
            raise GrvtTestnetSignError("Disparador fuera del tick GRVT testnet")
    order = _sign_limit(draft, limits, instrument,
        dedicated_private_key=dedicated_private_key, expected_signer=expected_signer,
        now_ns=current, time_in_force="GOOD_TILL_TIME", time_in_force_code=1,
        lifetime_ns=PROTECTION_LIFETIME_NS, metadata={"trigger": trigger.metadata()})
    validate_testnet_protection(order, limits, now_ns=current, expected_trigger=trigger)
    return order


def verify_testnet_protection(order: dict, limits: GrvtTestnetLimits,
                              instrument: GrvtTestnetInstrument, *,
                              expected_trigger: GrvtTestnetTrigger,
                              now_ns: int | None = None) -> str:
    """Verifica la firma y el disparador esperado por dos controles distintos."""
    current = time.time_ns() if now_ns is None else now_ns
    try:
        validate_testnet_protection(order, limits, now_ns=current,
                                   expected_trigger=expected_trigger)
        with localcontext() as ctx:
            ctx.prec = 100
            if expected_trigger.price % instrument.tick_size:
                raise ValueError("trigger tick")
        return _verify_limit(order, limits, instrument, now_ns=current,
            time_in_force="GOOD_TILL_TIME", time_in_force_code=1,
            lifetime_ns=PROTECTION_LIFETIME_NS,
            metadata_fields={"client_order_id", "trigger"})
    except Exception:
        raise GrvtTestnetSignError("Firma o disparador TP/SL testnet no verificable") from None


def observe_testnet_protection(row: object, order: dict, limits: GrvtTestnetLimits, *,
                                expected_trigger: GrvtTestnetTrigger, created_at_ns: int,
                                now_ns: int) -> GrvtTestnetProtectionObservation:
    """Contrasta el contrato completo; nunca confunde reloj, acuse y fills.

    La orden esperada se valida al instante de creación para poder recuperarla
    tras caducar. No modifica el estado, infiere cancelación ni permite reenvío.
    """
    if (type(created_at_ns) is not int or type(now_ns) is not int
            or not 0 < created_at_ns <= now_ns + 1_000_000_000):
        raise GrvtTestnetSignError("Reloj de observación TP/SL testnet inválido")
    client_id = validate_testnet_protection(order, limits, now_ns=created_at_ns,
                                           expected_trigger=expected_trigger)
    try:
        if (not isinstance(row, dict) or any(not _wire_equal(row.get(key), order[key]) for key in (
                "sub_account_id", "is_market", "time_in_force", "post_only", "reduce_only",
                "legs", "signature", "builder", "builder_fee"))
                or not _wire_equal(row.get("metadata", {}).get("trigger"), expected_trigger.metadata())):
            raise ValueError("protection contract mismatch")
        parsed = _parse_order(row, limits.sub_account_id, open_only=False, now_ns=now_ns)
        if (parsed["client_order_id"] != client_id or len(parsed["legs"]) != 1
                or parsed.get("create_time_ns") is None
                or int(parsed["create_time_ns"]) < created_at_ns - 1_000_000_000):
            raise ValueError("protection identity mismatch")
        leg = parsed["legs"][0]
        size, filled = Decimal(leg["size"]), Decimal(leg["traded_size"])
        if (parsed["status"] == "FILLED" and filled != size
                or parsed["status"] == "REJECTED" and filled != 0):
            raise ValueError("inconsistent fill state")
        observed = GrvtTestnetOrderObservation(limits.sub_account_id, client_id,
            parsed["order_id"], parsed["status"], leg["instrument"], leg["side"],
            leg["size"], leg["traded_size"], leg["limit_price"], parsed["update_time_ns"],
            create_time_ns=parsed["create_time_ns"])
        expiration = int(order["signature"]["expiration"])
        active = parsed["status"] == "OPEN" and filled < size and now_ns < expiration
        return GrvtTestnetProtectionObservation(observed, expected_trigger, expiration, active)
    except Exception:
        raise GrvtTestnetSignError("Observación TP/SL testnet no verificable") from None
