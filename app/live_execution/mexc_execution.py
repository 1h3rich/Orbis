"""Coordinación de apertura revisada, orden LIMIT única y seguimiento MEXC.

El llamante configura View y Trade localmente. Ningún saldo, coste, comisión
o permiso de autorización procede de una bandera enviada por la interfaz.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, DecimalException, ROUND_CEILING, ROUND_FLOOR, localcontext
from typing import Callable, Literal
from uuid import UUID, uuid4

from sqlalchemy import select

from app.integrations.mexc_readonly import MexcReadOnlyClient
from app.live_execution.journal import (
    LiveJournalError, _stored_policy, policy_predates_reopening, recover_spot_intent, register_spot_policy,
)
from app.live_execution.mexc_execution_state import fetch_spot_execution_inputs
from app.live_execution.mexc_instrument import preflight_limit_order
from app.live_execution.spot_bot_preparation import PreparedSpotBotOrder
from app.live_execution.mexc_live_order import MexcOrderSendError, MexcSpotLiveClient
from app.live_execution.mexc_live_submit import MexcLiveInputs, submit_spot_limit_once
from app.live_execution.mexc_order_lifecycle import MexcLifecycleError, synchronize_spot_order
from app.live_execution.mexc_order_observer import MexcOrderObservationError
from app.live_execution.mexc_uid_guard import assert_known_mexc_uid
from app.live_execution.policy import LiveSpotIntent, LiveSpotPolicy, evaluate_spot_intent, sell_base_reservation, _money, _utc
from app.live_execution.reconciliation_view import PENDING_SPOT_STATUSES
from app.live_execution.spot_bot_plan import _base_step
from app.portfolio.mexc_parity_archive import read_observation
from app.portfolio.models import RealLiveSpotIntent, RealLiveStop, RealMexcParityObservation
from app.portfolio.vault import RealVault


class MexcExecutionError(RuntimeError):
    """La apertura o el pedido no corresponden a una operativa REAL válida."""


def _validate_limit_terms(side: str, quantity: Decimal, price: Decimal) -> None:
    if (side not in ("BUY", "SELL") or type(quantity) is not Decimal or type(price) is not Decimal
            or any(not value.is_finite() or value <= 0 or len(value.as_tuple().digits) > 40
                   or abs(value.as_tuple().exponent) > 100 for value in (quantity, price))):
        raise MexcExecutionError("Cantidad o precio de orden inválidos")


def _manual_limit_intent(
    policy: LiveSpotPolicy, request_id: str, side: Literal["BUY", "SELL"],
    quantity: Decimal, price: Decimal, fee_rate: Decimal, now: datetime, *,
    max_spend_quote: Decimal | None = None,
) -> LiveSpotIntent:
    with localcontext() as context:
        context.prec = 300
        fee = quantity * price * fee_rate
    try:
        return LiveSpotIntent(
            intent_id=request_id, policy_id=policy.policy_id,
            strategy_version_id=policy.strategy_version_id, account_ref=policy.account_ref,
            symbol=policy.symbol, side=side, quantity_base=quantity,
            limit_price_quote=price, estimated_fee_quote=fee, signal_at=_utc(now, "now"),
            max_spend_quote=max_spend_quote)
    except ValueError:
        raise MexcExecutionError("Cantidad, precio o comisión fuera del rango admitido") from None


def _quoted_limit_price(side: Literal["BUY", "SELL"], inputs: MexcLiveInputs) -> Decimal:
    buying = side == "BUY"
    reference = inputs.state.best_ask if buying else inputs.state.best_bid
    price = reference.quantize(Decimal(1).scaleb(-inputs.rules.quote_precision),
        rounding=ROUND_CEILING if buying else ROUND_FLOOR).normalize()
    if price <= 0:
        raise MexcExecutionError("El precio observado no permite un límite válido")
    return price


def _limit_terms_from_quote_budget(side: Literal["BUY", "SELL"], budget: Decimal,
                                   inputs: MexcLiveInputs) -> tuple[Decimal, Decimal]:
    """Quote budget includes the upper fee for buys and caps gross proceeds for sells."""
    buying = side == "BUY"
    try:
        with localcontext() as context:
            context.prec = 300
            price = _quoted_limit_price(side, inputs)
            step = _base_step(inputs.rules)
            unit_cost = price * step * (1 + inputs.rules.fee_rate_upper if buying else 1)
            quantity = ((budget // unit_cost) * step).normalize()
    except DecimalException:
        raise MexcExecutionError("El importe no permite calcular una cantidad válida") from None
    if quantity <= 0:
        raise MexcExecutionError("El importe no cubre el incremento mínimo de cantidad")
    return quantity, price


def _maximum_sell_terms(policy: LiveSpotPolicy, inputs: MexcLiveInputs,
                        now: datetime) -> tuple[Decimal, Decimal]:
    """Largest whole lot at the quoted limit after keeping the upper base fee."""
    state, rules = inputs.state, inputs.rules
    with localcontext() as context:
        context.prec = 300
        price = _quoted_limit_price("SELL", inputs)
        step = _base_step(rules)
        available = state.free_base - state.pending_base
        lot_with_fee = step * (1 + rules.fee_rate_upper)
        lot_notional = step * price
        lots = int(min(available // lot_with_fee, min(policy.max_order_quote, rules.max_quote) // lot_notional))
        if lots < 1:
            raise MexcExecutionError("El saldo o el límite por orden no cubre un incremento de venta con comisión")
        quantity = (lots * step).normalize()
        if policy.allow_risk_reducing_exits:
            return quantity, price

        # These two limits can bind older policies. Both denials become more
        # restrictive with size once the daily loss gate itself has passed.
        intent = _manual_limit_intent(policy, str(uuid4()), "SELL", quantity, price, rules.fee_rate_upper, now)
        risk = evaluate_spot_intent(policy, intent, state, now=now)
        if risk.reason not in {"DAILY_TURNOVER_LIMIT", "PROJECTED_DAILY_LOSS_LIMIT"}:
            return quantity, price
        low, high = 0, lots
        while low + 1 < high:
            middle = (low + high) // 2
            candidate = replace(intent, quantity_base=(middle * step).normalize(),
                                estimated_fee_quote=middle * lot_notional * rules.fee_rate_upper)
            if evaluate_spot_intent(policy, candidate, state, now=now).allowed:
                low = middle
            else:
                high = middle
        # Retain the original denial if no positive lot passes the risk gate.
        return ((low * step).normalize() if low else quantity), price


def review_anchored_spot_limit(
    vault: RealVault, view: MexcReadOnlyClient, trade: MexcSpotLiveClient,
    policy: LiveSpotPolicy, *, side: Literal["BUY", "SELL"],
    quantity_base: Decimal | None = None, limit_price_quote: Decimal | None = None,
    budget_quote: Decimal | None = None,
    max_available_sell: bool = False,
    quote_get: Callable | None = None, rules_fetch: Callable | None = None,
    assert_active: Callable[[], None],
) -> dict:
    """Evaluate a manual LIMIT using fresh evidence without reserving or posting an order."""
    from app.live_execution.spot_bot_store import validate_bot_order_scope

    if max_available_sell:
        if side != "SELL" or any(value is not None for value in (quantity_base, limit_price_quote, budget_quote)):
            raise MexcExecutionError("El máximo disponible sólo admite el lado de venta")
    elif budget_quote is not None:
        try:
            _money(budget_quote, "budget_quote", positive=True)
        except ValueError:
            raise MexcExecutionError("Importe de orden inválido") from None
        if side not in ("BUY", "SELL") or quantity_base is not None or limit_price_quote is not None:
            raise MexcExecutionError("El cálculo por importe requiere únicamente el lado y el importe")
    else:
        _validate_limit_terms(side, quantity_base, limit_price_quote)
    inputs = fetch_spot_execution_inputs(vault, view, policy, quote_get=quote_get, rules_fetch=rules_fetch)
    assert_active()
    try:
        uid = view.account_uid()
        if view.account_reference() != policy.account_ref or not assert_known_mexc_uid(vault, policy.account_ref, uid):
            raise ValueError("View identity changed")
        trade.pair_view_account(uid)
        if view.account_reference() != policy.account_ref or view.account_uid() != uid:
            raise ValueError("View identity changed")
    except Exception:
        raise MexcExecutionError("No se pudo verificar la clave MEXC Trade para esta cuenta") from None
    now = datetime.now(timezone.utc)
    if max_available_sell:
        quantity_base, limit_price_quote = _maximum_sell_terms(policy, inputs, now)
    elif budget_quote is not None:
        quantity_base, limit_price_quote = _limit_terms_from_quote_budget(side, budget_quote, inputs)
    if max_available_sell or budget_quote is not None:
        _validate_limit_terms(side, quantity_base, limit_price_quote)
    intent = _manual_limit_intent(policy, str(uuid4()), side, quantity_base, limit_price_quote,
                                  inputs.rules.fee_rate_upper, now,
                                  max_spend_quote=budget_quote if side == "BUY" else None)
    with vault.session() as db:
        db.connection().exec_driver_sql("BEGIN")
        if _stored_policy(db, policy.policy_id) != policy:
            raise MexcExecutionError("La operativa cambió durante la revisión")
        stop = db.get(RealLiveStop, policy.account_ref)
        if stop is not None and stop.stopped:
            raise MexcExecutionError("La cuenta MEXC está detenida")
        if policy_predates_reopening(db, policy.policy_id, stop):
            raise MexcExecutionError("Esta política es anterior a la reapertura; habilita una operativa nueva")
        pending = db.scalar(select(RealLiveSpotIntent.id).where(
            RealLiveSpotIntent.account_ref == policy.account_ref,
            RealLiveSpotIntent.status.in_(PENDING_SPOT_STATUSES)).limit(1))
        if pending is not None:
            raise MexcExecutionError("Sincroniza la orden pendiente antes de revisar otra")
        validate_bot_order_scope(vault, intent, db=db)
    now = datetime.now(timezone.utc)
    preflight = preflight_limit_order(policy, intent, inputs.rules, last_price=inputs.last_price,
                                     last_price_at=inputs.last_price_at, now=now)
    risk = evaluate_spot_intent(policy, intent, inputs.state, now=now)
    assert_active()
    result = {
        "compatible": preflight.allowed and risk.allowed,
        "reason": risk.reason if preflight.allowed and not risk.allowed else preflight.reason,
        "side": side, "symbol": policy.symbol, "quote_asset": policy.quote_asset,
        "notional_quote": format(risk.notional_quote, "f"),
        "estimated_max_fee_quote": format(intent.estimated_fee_quote, "f"),
        "min_order_quote": format(inputs.rules.min_quote, "f"),
        "base_step": format(inputs.rules.base_step, "f"),
        "checked_at": now.isoformat(),
    }
    if max_available_sell or budget_quote is not None:
        with localcontext() as context:
            context.prec = 300
            notional = intent.quantity_base * intent.limit_price_quote
            total = notional + (intent.estimated_fee_quote if side == "BUY" else -intent.estimated_fee_quote)
            available_base = inputs.state.free_base
            if max_available_sell:
                available_base -= inputs.state.pending_base
                result.update(size_mode="MAX_AVAILABLE",
                    estimated_max_fee_base=format(sell_base_reservation(intent) - intent.quantity_base, "f"),
                    base_outside_order=format(available_base - intent.quantity_base, "f"))
        result.update(quantity_base=format(intent.quantity_base, "f"),
            limit_price_quote=format(intent.limit_price_quote, "f"),
            estimated_total_quote=format(total, "f"), available_quote=format(inputs.state.free_quote, "f"),
            available_base=format(available_base, "f"), reference_side="ASK" if side == "BUY" else "BID",
            quote_at=inputs.state.quote_at.isoformat())
        if budget_quote is not None:
            result["budget_quote"] = format(budget_quote, "f")
    return result


def activate_spot_execution(
    vault: RealVault, view: MexcReadOnlyClient, trade: MexcSpotLiveClient,
    draft_id: str, anchor_id: str, active_policy_id: str, *,
    clock: Callable[[], datetime] | None = None,
    quote_get: Callable | None = None,
    rules_fetch: Callable | None = None,
    persist_policy: Callable[[LiveSpotPolicy, datetime], None] | None = None,
    expires_before: datetime | None = None,
) -> LiveSpotPolicy:
    """Crea una política activa nueva tras revalidar una apertura reciente.

    El borrador se conserva inmutable. La sesión dura como máximo un día desde
    la apertura, permite salidas Spot de reducción y no activa ningún bot.
    Cada orden posterior requiere un pedido explícito y otro barrido completo.
    """
    now = clock or (lambda: datetime.now(timezone.utc))
    try:
        active_policy_id, anchor_id = str(UUID(active_policy_id)), str(UUID(anchor_id))
    except (ValueError, TypeError, AttributeError):
        raise MexcExecutionError("Identificador de activación o apertura inválido") from None
    with vault.session() as db:
        db.connection().exec_driver_sql("BEGIN")
        draft = _stored_policy(db, draft_id)
        anchor = db.get(RealMexcParityObservation, anchor_id)
        if draft.enabled or anchor is None or anchor.account_ref != draft.account_ref:
            raise MexcExecutionError("El borrador y la apertura no corresponden a esta operativa")
        read_observation(anchor)
        from app.live_execution.mexc_continuity_review import opening_boundary
        boundary = opening_boundary(db,anchor)
        checked_at = _utc(now(), "now")
        if not 0 <= (checked_at - anchor.checked_at).total_seconds() <= draft.max_data_age_seconds:
            raise MexcExecutionError("Guarda una nueva comprobación de saldo antes de activar")
        active = replace(draft, policy_id=active_policy_id, enabled=True,
                         reconciliation_anchor_id=anchor_id, allow_risk_reducing_exits=True,
                         expires_at=min(draft.expires_at, boundary + timedelta(days=1)))
        if expires_before is not None:
            active = replace(active,expires_at=min(active.expires_at,_utc(expires_before,"expires_before")))
    inputs = fetch_spot_execution_inputs(
        vault, view, active, clock=now, quote_get=quote_get, rules_fetch=rules_fetch)
    try:
        uid = view.account_uid()
        if (view.account_reference() != active.account_ref
                or not assert_known_mexc_uid(vault, active.account_ref, uid)):
            raise ValueError("View identity changed")
        trade.pair_view_account(uid)
        if view.account_uid() != uid:
            raise ValueError("View identity changed")
    except Exception:
        raise MexcExecutionError("No se pudo verificar la clave MEXC Trade para esta cuenta") from None
    if not inputs.state.reconciled:
        raise MexcExecutionError("La cuenta MEXC no está preparada")
    if _utc(now(), "now") >= active.expires_at:
        raise MexcExecutionError("La política caducó durante la activación")
    registered_at = _utc(now(), "now")
    if persist_policy is None:
        register_spot_policy(vault, active, now=registered_at)
    else:
        persist_policy(active, registered_at)
    return active


def submit_anchored_spot_limit(
    vault: RealVault, view: MexcReadOnlyClient, trade: MexcSpotLiveClient,
    policy_id: str, request_id: str, *, side: Literal["BUY", "SELL"],
    quantity_base: Decimal, limit_price_quote: Decimal,
    max_spend_quote: Decimal | None = None,
    clock: Callable[[], datetime] | None = None,
    quote_get: Callable | None = None,
    rules_fetch: Callable | None = None,
    assert_active: Callable[[], None] | None = None,
    bot_intent: LiveSpotIntent | None = None,
    bot_decision_id: str | None = None,
    prepared_bot_order: PreparedSpotBotOrder | None = None,
    assert_active_in_transaction: Callable | None = None,
) -> dict:
    """Un pedido explícito, un ID y como máximo un POST negociable.

    Un ID ya registrado se devuelve tal cual, incluso si quedó reservado
    antes del POST. Reanudar seguimiento nunca vuelve a enviar la orden.
    """
    now = clock or (lambda: datetime.now(timezone.utc))
    active = assert_active or (lambda: None)
    active()
    _validate_limit_terms(side, quantity_base, limit_price_quote)
    if max_spend_quote is not None:
        try:
            _money(max_spend_quote, "max_spend_quote", positive=True)
        except ValueError:
            raise MexcExecutionError("Presupuesto máximo inválido") from None
        if side != "BUY" or bot_intent is not None:
            raise MexcExecutionError("El presupuesto máximo sólo corresponde a compras manuales")
    try:
        request_id = str(UUID(request_id))
    except (ValueError, TypeError, AttributeError):
        raise MexcExecutionError("Identificador de orden inválido") from None
    with vault.session() as db:
        policy = _stored_policy(db, policy_id)
        if not policy.enabled or policy.reconciliation_anchor_id is None:
            raise MexcExecutionError("La política no tiene una apertura REAL activa")
        anchor = db.get(RealMexcParityObservation, policy.reconciliation_anchor_id)
        if anchor is None:
            raise MexcExecutionError("La apertura REAL no está disponible")
        account_id = anchor.account_id
        prior = db.get(RealLiveSpotIntent, request_id)
        prior_status = prior.status if prior else None
    if prior_status is not None:
        prior_intent = recover_spot_intent(vault, request_id)
        if (prior_intent.policy_id != policy_id or prior_intent.side != side
                or prior_intent.quantity_base != quantity_base
                or prior_intent.limit_price_quote != limit_price_quote
                or prior_intent.max_spend_quote != max_spend_quote):
            raise MexcExecutionError("El ID de orden ya se usó con otros parámetros")
        if prepared_bot_order is None:
            return {"intent_id": request_id, "journal_status": prior_status,
                    "replayed_without_post": True, "new_order_post_attempted": False}
        if bot_intent is None or bot_decision_id is None or not isinstance(prepared_bot_order,PreparedSpotBotOrder):
            raise MexcExecutionError("La continuación requiere una preparación propia del bot")
        prepared_bot_order.take(vault,bot_intent,bot_decision_id)
    elif prepared_bot_order is not None or bot_intent is not None or bot_decision_id is not None:
        raise MexcExecutionError("La decisión del bot no tiene una reserva preparada; no se reconstruye para enviarla")
    generation = vault.session_generation
    if generation is None:
        raise MexcExecutionError("La bóveda REAL está bloqueada")
    with vault.guard_session_generation(generation):
        inputs = fetch_spot_execution_inputs(
            vault, view, policy,current_intent_id=request_id if prepared_bot_order is not None else None,
            clock=now, quote_get=quote_get, rules_fetch=rules_fetch)
        intent = bot_intent or _manual_limit_intent(
            policy, request_id, side, quantity_base, limit_price_quote,
            inputs.rules.fee_rate_upper, now(), max_spend_quote=max_spend_quote)
        if (intent.intent_id != request_id or intent.policy_id != policy.policy_id
                or intent.strategy_version_id != policy.strategy_version_id
                or intent.account_ref != policy.account_ref or intent.symbol != policy.symbol
                or intent.side != side or intent.quantity_base != quantity_base
                or intent.limit_price_quote != limit_price_quote):
            raise MexcExecutionError("El pedido del bot no corresponde a esta operativa")
        from app.live_execution.spot_bot_store import validate_bot_order_scope
        validate_bot_order_scope(vault, intent, decision_id=bot_decision_id)

        def refresh():
            active()
            validate_bot_order_scope(vault, intent, decision_id=bot_decision_id)
            return fetch_spot_execution_inputs(
                vault, view, policy, current_intent_id=request_id,
                clock=now, quote_get=quote_get, rules_fetch=rules_fetch)

        active()
        try:
            def authorized():
                active()
                validate_bot_order_scope(vault, intent, decision_id=bot_decision_id)

            def authorized_in_transaction(db):
                # No abrir/cerrar sesiones SQLCipher dentro del cerrojo final:
                # una conexión reutilizada revertiría BEGIN IMMEDIATE.
                if assert_active_in_transaction is not None:
                    assert_active_in_transaction(db)
                else:
                    active()
                validate_bot_order_scope(vault, intent, decision_id=bot_decision_id, db=db)

            ack = submit_spot_limit_once(
                vault, trade, view, policy, intent, inputs.state, inputs.rules,
                last_price=inputs.last_price, last_price_at=inputs.last_price_at,
                refresh_inputs=refresh, clock=now, assert_authorized=authorized,
                assert_authorized_in_transaction=authorized_in_transaction)
        except (MexcOrderSendError, LiveJournalError) as exc:
            with vault.session() as db:
                row = db.get(RealLiveSpotIntent, request_id)
                status = row.status if row is not None else "NOT_SUBMITTED"
                budget_blocked = row is not None and row.reason == "BUDGET_LIMIT"
            return {"intent_id": request_id, "journal_status": status,
                    "stage": "REVIEW_REQUIRED", "detail": (
                        "La orden supera el presupuesto con la comisión actual. Calcula de nuevo."
                        if budget_blocked else str(exc)),
                    "replayed_without_post": False,
                    "new_order_post_attempted": status == "SUBMISSION_UNKNOWN"}
        active()
        try:
            follow_up = synchronize_spot_order(
                vault, view, request_id, account_id, clock=now, assert_active=active)
        except (MexcLifecycleError, MexcOrderObservationError):
            follow_up = {"stage": "FOLLOW_UP_REQUIRED", "reservation_closed": False}
        with vault.session() as db:
            status = db.get(RealLiveSpotIntent, request_id).status
        return {"intent_id": request_id, "journal_status": status,
                "external_order_id": ack.external_order_id,
                "replayed_without_post": False, "new_order_post_attempted": True,
                "synchronization": follow_up}
