"""Puerta conservadora para un único perpetuo GRVT testnet.

No reproduce el motor de margen de GRVT ni autoriza mainnet. Abre sólo con
subcuenta simple cross, sin otras posiciones ni órdenes observadas y con
colchón de equity 2x el nocional. Cierres exigen reduce-only y posición exacta.
La ausencia de órdenes abiertas no prueba ausencia de órdenes en tránsito;
el diario local también debe bloquear intentos pendientes.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
import re
import time
from typing import Callable

import httpx

from app.derivatives.paper_math import MarginRules, PaperPerpError
from app.live_execution.grvt_margin_response import margin_rules_response
from app.live_execution.grvt_testnet_order import GrvtTestnetLimits, GrvtTestnetOrderClient
from app.live_execution.grvt_testnet_signer import GrvtTestnetLimitDraft


MARGIN_RULES_URL = "https://market-data.testnet.grvt.io/full/v1/margin_rules"
_NUMBER = re.compile(r"-?[0-9]{1,30}(?:\.[0-9]{1,18})?\Z")


class GrvtTestnetRiskError(RuntimeError):
    """Estado privado o reglas externas no verificables; sin datos brutos."""


@dataclass(frozen=True, slots=True)
class GrvtTestnetRiskState:
    sub_account_id: str
    settle_currency: str
    event_time_ns: int
    observed_at_ns: int
    margin_type: str
    sub_account_mode: str
    total_equity: Decimal
    settle_balance: Decimal
    initial_margin: Decimal
    maintenance_margin: Decimal
    unrealized_pnl: Decimal
    positions: tuple[tuple[str, Decimal], ...]
    open_order_count: int


@dataclass(frozen=True, slots=True)
class GrvtTestnetRiskDecision:
    allowed: bool
    reason: str
    notional: Decimal
    mode: str


def _decimal(raw: object, *, nonnegative: bool = False) -> Decimal:
    if not isinstance(raw, str) or not _NUMBER.fullmatch(raw):
        raise ValueError("invalid decimal")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise ValueError("invalid decimal") from None
    if not value.is_finite() or nonnegative and value < 0:
        raise ValueError("invalid decimal")
    return value


def _public_post(url: str, body: dict) -> httpx.Response:
    return httpx.post(url, json=body, timeout=5.0,
                      follow_redirects=False, trust_env=False)


def fetch_testnet_risk(client: GrvtTestnetOrderClient, instrument: str,
                       sub_account_id: str, *,
                       public_post: Callable[[str, dict], httpx.Response] = _public_post,
                       clock_ns: Callable[[], int] = time.time_ns,
                       ) -> tuple[GrvtTestnetRiskState, MarginRules]:
    """Obtiene estado de cuenta y límites desde GRVT testnet, sin cachear."""
    now_ns = clock_ns()
    if type(now_ns) is not int or now_ns <= 0:
        raise GrvtTestnetRiskError("Reloj testnet inválido")
    try:
        account = client.read_testnet("account_summary", sub_account_id).get("result")
        orders = client.read_testnet("open_orders", sub_account_id).get("result")
        response = public_post(MARGIN_RULES_URL, {"instrument": instrument})
        raw_rules = margin_rules_response(response)
        rules = MarginRules.from_payload(
            {**raw_rules, "fetched_at_ns": now_ns} if isinstance(raw_rules, dict) else None,
            instrument, now_ns,
        )
        if (not isinstance(account, dict) or account.get("sub_account_id") != sub_account_id
                or not isinstance(orders, list) or len(orders) > 500
                or not isinstance(account.get("positions"), list)
                or len(account["positions"]) > 100
                or not isinstance(account.get("spot_balances"), list)
                or len(account["spot_balances"]) > 100):
            raise ValueError("incomplete account")
        for row in orders:
            if (not isinstance(row, dict) or row.get("sub_account_id") != sub_account_id
                    or not isinstance(row.get("state"), dict)
                    or row["state"].get("status") not in ("PENDING", "OPEN")):
                raise ValueError("incomplete orders")
        positions = []
        balances = {}
        for row in account["spot_balances"]:
            if (not isinstance(row, dict)
                    or not isinstance(row.get("currency"), str)
                    or row["currency"] in balances):
                raise ValueError("incomplete balances")
            balances[row["currency"]] = _decimal(row.get("balance"))
        seen = set()
        for row in account["positions"]:
            if (not isinstance(row, dict) or row.get("sub_account_id") != sub_account_id
                    or not isinstance(row.get("instrument"), str)
                    or row["instrument"] in seen):
                raise ValueError("incomplete positions")
            seen.add(row["instrument"])
            size = _decimal(row.get("size"))
            if size:
                positions.append((row["instrument"], size))
        event_raw = account.get("event_time")
        if not isinstance(event_raw, str) or not event_raw.isdecimal():
            raise ValueError("missing event time")
        event_ns = int(event_raw)
        if event_ns <= 0 or event_ns > now_ns + 2_000_000_000:
            raise ValueError("future event time")
        currency = account.get("settle_currency")
        if not isinstance(currency, str) or currency not in balances:
            raise ValueError("missing settlement balance")
        if (account.get("sub_account_mode") == "SINGLE_ASSET_MODE"
                and any(asset != currency and amount != 0
                        for asset, amount in balances.items())):
            raise ValueError("unexpected collateral in single asset mode")
        state = GrvtTestnetRiskState(
            sub_account_id, currency, event_ns, now_ns,
            account.get("margin_type"), account.get("sub_account_mode"),
            _decimal(account.get("total_equity")),
            balances[currency],
            _decimal(account.get("initial_margin"), nonnegative=True),
            _decimal(account.get("maintenance_margin"), nonnegative=True),
            _decimal(account.get("unrealized_pnl")),
            tuple(positions), len(orders),
        )
        return state, rules
    except Exception:
        raise GrvtTestnetRiskError("Riesgo GRVT testnet no verificable") from None


def evaluate_testnet_risk(draft: GrvtTestnetLimitDraft,
                          limits: GrvtTestnetLimits,
                          state: GrvtTestnetRiskState,
                          rules: MarginRules, *, now_ns: int) -> GrvtTestnetRiskDecision:
    """Bloqueo previo, no predicción de liquidación ni permiso real de orden."""
    if (type(now_ns) is not int or now_ns <= 0
            or not isinstance(draft, GrvtTestnetLimitDraft)
            or not isinstance(state, GrvtTestnetRiskState)
            or not isinstance(rules, MarginRules)):
        raise GrvtTestnetRiskError("Preflight testnet inválido")
    with localcontext() as context:
        context.prec = 100
        notional = draft.size * draft.limit_price
        equity_matches = state.total_equity == state.settle_balance + state.unrealized_pnl
    mode = "REDUCE" if draft.reduce_only else "OPEN"
    def deny(reason: str) -> GrvtTestnetRiskDecision:
        return GrvtTestnetRiskDecision(False, reason, notional, mode)
    if (draft.sub_account_id != limits.sub_account_id or state.sub_account_id != limits.sub_account_id
            or draft.instrument != limits.instrument or rules.instrument != limits.instrument):
        return deny("SCOPE_MISMATCH")
    if (state.event_time_ns > now_ns + 2_000_000_000
            or state.observed_at_ns > now_ns + 2_000_000_000
            or now_ns - state.event_time_ns > 5_000_000_000
            or now_ns - state.observed_at_ns > 5_000_000_000
            or rules.fetched_at_ns > now_ns + 2_000_000_000
            or now_ns - rules.fetched_at_ns > 30_000_000_000):
        return deny("STALE_RISK_DATA")
    quote = draft.instrument.split("_")[1]
    if state.settle_currency != quote:
        return deny("SETTLEMENT_CURRENCY_MISMATCH")
    if (state.margin_type != "SIMPLE_CROSS_MARGIN"
            or state.sub_account_mode != "SINGLE_ASSET_MODE"):
        return deny("UNSUPPORTED_MARGIN_MODE")
    if state.open_order_count:
        return deny("OPEN_ORDERS_PRESENT")
    if (notional <= 0 or notional > limits.max_order_notional
            or draft.size <= 0 or draft.size > rules.max_size):
        return deny("ORDER_OR_POSITION_LIMIT")
    try:
        tier = rules.tier_for(notional)
    except PaperPerpError:
        return deny("MARGIN_TIER_UNKNOWN")
    if draft.reduce_only:
        if len(state.positions) != 1 or state.positions[0][0] != draft.instrument:
            return deny("POSITION_NOT_UNIQUE")
        held = state.positions[0][1]
        if ((held > 0 and draft.side != "SELL")
                or (held < 0 and draft.side != "BUY")
                or draft.size > abs(held)):
            return deny("NOT_REDUCING_POSITION")
        if not equity_matches:
            return deny("EQUITY_PNL_MISMATCH")
        return GrvtTestnetRiskDecision(True, "REDUCE_ONLY_TESTNET", notional, mode)
    if state.positions or state.initial_margin != 0 or state.maintenance_margin != 0:
        return deny("EXISTING_EXPOSURE")
    if state.unrealized_pnl != 0:
        return deny("UNEXPLAINED_PNL")
    if (state.total_equity <= 0 or state.total_equity < notional * 2
            or tier.max_leverage < 1):
        return deny("EQUITY_BUFFER")
    if state.settle_balance < notional * 2:
        return deny("SETTLEMENT_BUFFER")
    if not equity_matches:
        return deny("EQUITY_PNL_MISMATCH")
    return GrvtTestnetRiskDecision(True, "CONSERVATIVE_TESTNET_PREFLIGHT", notional, mode)
