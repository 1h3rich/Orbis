"""Ciclo interno LAB→PAPER→MEXC Spot REAL, con decisión durable antes del POST.

No se inicia al importar el módulo, abrir la bóveda o crear una fila. La API
y el supervisor deberán conceder una autorización efímera explícita por bot.
El cierre/reinicio no implica una reanudación automática de dinero real.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, localcontext
from typing import Callable
from contextlib import nullcontext
from uuid import UUID, uuid5

from sqlalchemy import select

from app.automation.spot_strategy_paper import SpotStrategyPaper
from app.lab.engine import INTERVALS
from app.live_execution.journal import _stored_policy,recover_spot_intent
from app.live_execution.mexc_execution import activate_spot_execution, submit_anchored_spot_limit
from app.live_execution.mexc_execution_state import fetch_spot_execution_inputs
from app.live_execution.mexc_order_lifecycle import synchronize_spot_order
from app.live_execution.policy import LiveSpotIntent, _money, _utc
from app.live_execution.spot_bot_plan import SpotBotCursor, plan_spot_bot
from app.live_execution.spot_bot_preparation import PreparedSpotBotOrder
from app.live_execution.dca_bot_source import verified_native_bot_observation as verified_spot_bot_observation
from app.live_execution.spot_bot_store import (
    LEGACY_CANCEL_REVIEWS, LEGACY_CANCEL_STATE, SpotBotStoreError, archive_spot_bot_decision, assert_spot_bot_cycle, bot_record,
    claim_spot_bot_cycle, decoded, finish_spot_bot_cycle, owned_spot_bot_ledger, register_spot_bot,
)
from app.portfolio.models import RealLiveSpotIntent, RealSpotBotDecision, RealLiveOrderObservation, MexcObservedFill
from app.portfolio.vault import RealVault
from app.research.mexc_binding import check_mexc_research


ORDER_NAMESPACE = UUID("b5844c83-d86f-4118-894a-1311326564e2")


def activate_mexc_spot_bot(vault, view, trade, paper: SpotStrategyPaper, *,
                            draft_id: str, anchor_id: str, policy_id: str, bot_id: str,
                            paper_session_id: str, entry_budget_quote: Decimal,
                            clock=None, quote_get=None, rules_fetch=None,
                            activation_request: dict | None = None, dca_limits: dict | None = None,
                            execution_mode:str="SESSION",continuous_until:datetime|None=None,
                            research_validation_id:str|None=None, research_review_digest:str|None=None) -> str:
    """Verifica el ensayo, activa límites independientes y no envía órdenes."""
    now = clock or (lambda: datetime.now(timezone.utc))
    _money(entry_budget_quote, "entry_budget_quote", positive=True)
    with paper.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        source = verified_spot_bot_observation(paper, paper_session_id, now=now(), verify_backtest=True)
        research = check_mexc_research(paper,source,research_validation_id) if research_validation_id is not None else None
        if research is not None:
            research.assert_review(research_review_digest)
        with vault.session() as real_db:
            draft = _stored_policy(real_db, draft_id)
            if (draft.manual_only or source.version_id != draft.strategy_version_id
                    or source.symbol.replace("/", "") != draft.symbol
                    or entry_budget_quote > draft.max_order_quote):
                raise SpotBotStoreError("El origen del bot y sus límites no coinciden")

        def persist(policy, at):
            if research is not None: research.assert_current()
            register_spot_bot(vault, policy, source, bot_id=bot_id,
                              entry_budget_quote=entry_budget_quote, now=at, register_policy=True,
                              activation_request=activation_request,dca_limits=dca_limits,own_market=True,execution_mode=execution_mode,
                              continuous_until=continuous_until,
                              draft_policy=draft if execution_mode == "CONTINUOUS" else None,
                              research_binding=research.binding.exported() if research is not None else None)

        activate_spot_execution(vault, view, trade, draft_id, anchor_id, policy_id,
                                clock=now, quote_get=quote_get, rules_fetch=rules_fetch, persist_policy=persist,
                                expires_before=continuous_until if execution_mode == "CONTINUOUS" else None)
        return str(UUID(bot_id))


def run_mexc_spot_bot_once(
    vault: RealVault, view, trade, paper: SpotStrategyPaper, bot_id: str, *,
    clock: Callable[[], datetime] | None = None, quote_get=None, rules_fetch=None,
    assert_active: Callable[[], None],
    expected_policy_id: str | None = None,
    market_candles: Callable | None = None,
    execution_permission: dict | None = None,
    on_policy_renewed:Callable|None=None,
) -> dict:
    """Un ciclo reclamado: seguir pedido pendiente o decidir con capital REAL.

    Las reservas/POST inciertos se consultan, nunca se reenvían. Si quedó una
    decisión sin reserva o una reserva sin envío, se exige revisión humana.
    Un fallo de red/origen pausa el bot conservando asientos y pedidos. Un
    segundo proceso no puede robar un ciclo cuyo resultado sea desconocido.
    """
    now = clock or (lambda: datetime.now(timezone.utc))
    assert_active()
    generation = vault.session_generation
    if generation is None:
        raise SpotBotStoreError("La bóveda REAL está bloqueada")
    with vault.guard_session_generation(generation):
        claim, config, state = claim_spot_bot_cycle(vault, bot_id, now=now(), expected_policy_id=expected_policy_id)
        pause = False

        def active():
            assert_active()
            assert_spot_bot_cycle(vault, bot_id, claim)

        def active_in_transaction(db):
            assert_active()
            assert_spot_bot_cycle(vault, bot_id, claim, db=db)

        try:
            if config.get("execution_permission") != execution_permission:
                raise SpotBotStoreError("El trabajador no tiene la concesión operativa aprobada")
            with vault.session() as db:
                policy = _stored_policy(db, config["policy_id"])
                last = db.get(RealSpotBotDecision, state["last_decision_id"]) if state["last_decision_id"] else None
                if last is not None:
                    last_payload = decoded(last.payload_json, last.payload_digest)
                    intent = db.get(RealLiveSpotIntent, last.intent_id)
                    pending_id = last.intent_id
                    if intent is not None:
                        pending_status = intent.status
                    else:
                        pending_status = LEGACY_CANCEL_STATE if pending_id in config.get(LEGACY_CANCEL_REVIEWS, {}) else "UNRESERVED"
                else:
                    pending_id = pending_status = None
            if pending_status in ("UNRESERVED", "RESERVED"):
                pause = True
                state["reason"] = "ORDER_REVIEW_REQUIRED"
                return {"bot_id":bot_id, "action":"PAUSE", "reason":state["reason"], "intent_id":pending_id}
            if pending_status == "SUBMISSION_UNKNOWN":
                active()
                result = synchronize_spot_order(vault, view, pending_id, config["account_id"], clock=now,
                                                 assert_active=active)
                state["reason"] = "ORDER_SYNCHRONIZED" if result["reservation_closed"] else "ORDER_PENDING"
                if result["reservation_closed"]:
                    owned_spot_bot_ledger(vault,bot_id)
                    pending_intent = recover_spot_intent(vault,pending_id)
                    budget = (Decimal(last_payload["dca"]["budget_limit"]) if config["source"].get("kind") == "DCA"
                              else Decimal(config["entry_budget_quote"]))
                    audit = _execution_costs(vault,pending_intent,policy.max_fee_bps,budget)
                    state["last_execution_costs"] = audit
                    if audit["violation"]:
                        pause = True
                        state["reason"] = audit["violation"]
                return {"bot_id":bot_id,"action":"FOLLOW_UP","reason":state["reason"],"order":result,"paused":pause,
                        "journal_status":"RESERVATION_CLOSED" if result["reservation_closed"] else "SUBMISSION_UNKNOWN"}
            executions = []
            ledger, revision = owned_spot_bot_ledger(vault, bot_id,executions=executions)
            with (nullcontext(None) if execution_permission is not None else paper.connect()) as paper_db:
                if paper_db is not None:
                    paper_db.execute("BEGIN IMMEDIATE")
                    source = verified_spot_bot_observation(paper, config["source"]["session_id"], now=now())
                    if source.binding() != config["source"]:
                        raise SpotBotStoreError("El origen LAB/PAPER del bot cambió")
                elif config["policy_id"] not in config.get("market_stream_seeds",{}):
                    raise SpotBotStoreError("La concesión perdió su mercado REAL aprobado")
                active()
                if config["policy_id"] in config.get("market_stream_seeds",{}):
                    from app.live_execution.spot_bot_market import observe_bot_market
                    if market_candles is None:
                        if execution_permission is not None: raise SpotBotStoreError("Falta el proveedor operativo de mercado")
                        adapter = paper.paper.adapter_factory() if hasattr(paper,"paper") else paper.adapter_factory()
                        market_candles = adapter.get_candles
                    source = observe_bot_market(vault,bot_id,claim,candles=market_candles,clock=now,assert_active=active)
                if execution_permission is not None and execution_permission.get("protocol") == "CONTINUOUS_V1":
                    from app.live_execution.spot_bot_continuity import renewal_due,renew_bot_policy
                    checked = _utc(now(),"now")
                    due = int(checked.timestamp())//INTERVALS[source.timeframe]*INTERVALS[source.timeframe]-INTERVALS[source.timeframe]
                    # Una publicación retrasada o un precio sin observar no
                    # renuevan autoridad. El planificador conserva sus pausas.
                    if (renewal_due(policy,execution_permission,now=checked) and source.candle_t == due
                            and (config["source"].get("kind") == "DCA" or ledger.position.units == 0
                                 or (checked-datetime.fromisoformat(state["last_checked_at"])).total_seconds() <= 30)):
                        policy,config,state,source = renew_bot_policy(vault,view,bot_id,claim,source,execution_permission,
                            clock=now,quote_get=quote_get,rules_fetch=rules_fetch,
                            assert_active=active,assert_active_in_transaction=active_in_transaction)
                        if on_policy_renewed is not None: on_policy_renewed(policy)
                inputs = fetch_spot_execution_inputs(vault, view, policy, clock=now,
                                                       quote_get=quote_get, rules_fetch=rules_fetch)
                planned_at = _utc(now(), "now")
                cursor = SpotBotCursor(state["last_candle_t"], datetime.fromisoformat(state["last_checked_at"]),
                                       Decimal(state["trailing_high"]) if state["trailing_high"] else None)
                if config["source"].get("kind") == "DCA":
                    from app.live_execution.dca_bot_plan import plan_dca_bot
                    from app.live_execution.spot_bot_store import bot_decision_payload
                    with vault.session() as db:
                        decisions = [bot_decision_payload(db,row) for row in db.scalars(select(RealSpotBotDecision).where(
                            RealSpotBotDecision.bot_id == bot_id).order_by(RealSpotBotDecision.created_at,RealSpotBotDecision.id)).all()]
                    plan = plan_dca_bot(policy,inputs.state,inputs.rules,source,ledger.position,state,config,decisions,executions,now=planned_at)
                else:
                    plan = plan_spot_bot(policy, inputs.state, inputs.rules, source, ledger.position, cursor,
                                          entry_budget_quote=Decimal(config["entry_budget_quote"]), now=planned_at)
                if plan.dca is not None: state["dca_reference"] = plan.dca["reference"]
                state.update(last_candle_t=source.candle_t, last_checked_at=inputs.state.quote_at.isoformat(),
                             trailing_high=str(plan.trailing_high) if plan.trailing_high is not None else None,
                             reason=plan.reason)
                if plan.action in ("WAIT", "PAUSE"):
                    pause = plan.action == "PAUSE"
                    return {"bot_id":bot_id, "action":plan.action, "reason":plan.reason}
                key = "|".join((policy.account_ref, source.version_id, source.timeframe, plan.action,
                                 str(source.candle_t) if plan.action == "BUY" else
                                 (execution_permission["grant_id"] if execution_permission and execution_permission.get("protocol") == "CONTINUOUS_V1" else policy.policy_id) + ":" + revision))
                if plan.dca is not None:
                    key = "|".join((policy.account_ref,source.version_id,bot_id,"DCA",plan.dca["key"]))
                intent_id = str(uuid5(ORDER_NAMESPACE, key))
                signal_at = (datetime.fromtimestamp(source.candle_t + INTERVALS[source.timeframe], timezone.utc)
                             if plan.action == "BUY" else planned_at)
                intent = LiveSpotIntent(intent_id, policy.policy_id, policy.strategy_version_id,
                    policy.account_ref, policy.symbol, plan.action, plan.quantity_base, plan.limit_price_quote,
                    plan.estimated_fee_quote, signal_at)
                preparation = archive_spot_bot_decision(vault, bot_id, claim, intent, plan, source,
                    ledger_revision=revision, quote_at=inputs.state.quote_at, best_bid=inputs.state.best_bid,
                    best_ask=inputs.state.best_ask, owned_units=ledger.position.units,
                    owned_cost_quote=ledger.position.cost_quote, now=planned_at,reserve_state=inputs.state)
                if not isinstance(preparation,PreparedSpotBotOrder):
                    raise SpotBotStoreError("La decisión no dejó una preparación atómica")
                decision_id = preparation.decision_id
                state["last_decision_id"] = decision_id
                if preparation.recorded.status != "RESERVED":
                    pause = True
                    state["reason"] = "ORDER_PREPARATION_REJECTED"
                    return {"bot_id":bot_id,"action":"PAUSE","reason":state["reason"],
                            "intent_id":intent.intent_id,"journal_status":preparation.recorded.status}
                active()
                result = submit_anchored_spot_limit(vault, view, trade, policy.policy_id, intent.intent_id,
                    side=intent.side, quantity_base=intent.quantity_base, limit_price_quote=intent.limit_price_quote,
                    clock=now, quote_get=quote_get, rules_fetch=rules_fetch, assert_active=active,
                    bot_intent=intent, bot_decision_id=decision_id,prepared_bot_order=preparation,
                    assert_active_in_transaction=active_in_transaction)
                if result.get("stage") == "REVIEW_REQUIRED":
                    pause = True
                    state["reason"] = "ORDER_REVIEW_REQUIRED"
                elif result.get("journal_status") == "RESERVATION_CLOSED":
                    # Los hechos ejecutados se conservan aunque el exchange
                    # cobre fuera de lo aprobado; parar no revierte un fill.
                    owned_spot_bot_ledger(vault, bot_id)
                    budget = Decimal(plan.dca["budget_limit"]) if plan.dca else Decimal(config["entry_budget_quote"])
                    audit = _execution_costs(vault, intent, policy.max_fee_bps, budget)
                    state["last_execution_costs"] = audit
                    if audit["violation"]:
                        pause = True
                        state["reason"] = audit["violation"]
                return {"bot_id":bot_id, "action":plan.action, "reason":state["reason"], "order":result, "paused":pause}
        except Exception:
            pause = True
            state["reason"] = "CYCLE_REVIEW_REQUIRED"
            raise
        finally:
            finish_spot_bot_cycle(vault, bot_id, claim, state, now=now(), pause=pause)


def _execution_costs(vault: RealVault, intent: LiveSpotIntent, max_fee_bps: Decimal, budget: Decimal) -> dict:
    with vault.session() as db:
        observed = db.scalar(select(RealLiveOrderObservation).where(
            RealLiveOrderObservation.intent_id == intent.intent_id).order_by(
                RealLiveOrderObservation.observed_at.desc()).limit(1))
        if observed is None:
            raise SpotBotStoreError("La ejecución del bot no tiene observación final")
        fills = db.scalars(select(MexcObservedFill).where(MexcObservedFill.account_ref == intent.account_ref,
            MexcObservedFill.symbol == intent.symbol, MexcObservedFill.order_id == observed.external_order_id)).all()
        with localcontext() as context:
            context.prec = 300
            gross = sum((fill.quote_quantity for fill in fills), Decimal(0))
            # El asset base se obtiene de la política registrada, no de una
            # suposición sobre el largo del ticker del instrumento.
            policy = _stored_policy(db, intent.policy_id)
            quote_fee = sum((fill.commission for fill in fills if fill.commission_asset == policy.quote_asset), Decimal(0))
            equivalent_fee = quote_fee + sum((fill.commission * fill.price for fill in fills
                                               if fill.commission_asset == policy.base_asset), Decimal(0))
            spent = gross + quote_fee if intent.side == "BUY" else Decimal(0)
            violation = ("EXECUTED_FEE_EXCEEDED" if equivalent_fee > gross * max_fee_bps / Decimal(10000)
                         else "EXECUTED_BUDGET_EXCEEDED" if spent > budget else None)
            return {"intent_id":intent.intent_id,"cash_spent_quote":str(spent),
                    "fee_equivalent_quote":str(equivalent_fee),"violation":violation}
