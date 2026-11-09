"""Revisión explícita de un bot pausado, conservando fills y permisos previos.

El hueco de observación se registra, nunca se reproduce como ejecución en
directo. Rearmar no envía órdenes ni adopta inventario de PAPER o de MEXC.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, localcontext
from uuid import UUID

from sqlalchemy import select

from app.automation.dca import dca_signal_window_key
from app.lab.engine import INTERVALS
from app.live_execution.journal import _stored_policy, _register_spot_policy_in_transaction
from app.live_execution.mexc_execution import activate_spot_execution
from app.live_execution.mexc_execution_recovery import resume_spot_execution
from app.live_execution.mexc_execution_state import fetch_spot_execution_inputs
from app.live_execution.policy import _money, _utc
from app.live_execution.dca_bot_source import verified_native_bot_observation as verified_spot_bot_observation
from app.live_execution.spot_bot_allocation import review_spot_bot_allocation
from app.live_execution.spot_bot_store import (
    SpotBotStoreError, bot_authorizations, bot_record,
    encoded, owned_spot_bot_ledger,
)
from app.portfolio.models import RealLiveSpotIntent, RealLiveStop, RealSpotBot, RealMexcParityObservation
from app.portfolio.repository import account_book_revision
from app.research.mexc_binding import archived_dca_anchor, check_mexc_research


def rearm_request(draft_id, anchor_id, paper_session_id, entry_budget_quote,execution_mode="SESSION",continuous_until=None,
                  research_validation_id=None,allocation_review_digest=None, research_review_digest=None) -> dict:
    if execution_mode not in ("SESSION","OPERATIONAL","CONTINUOUS"): raise SpotBotStoreError("Modo de permiso desconocido")
    if execution_mode == "CONTINUOUS" and continuous_until is None: raise SpotBotStoreError("Indica fecha de continuidad")
    if execution_mode != "CONTINUOUS" and continuous_until is not None: raise SpotBotStoreError("La fecha requiere permiso continuo")
    return {"draft_id":str(UUID(draft_id)), "anchor_id":str(UUID(anchor_id)),
            "paper_session_id":str(UUID(paper_session_id)), "entry_budget_quote":str(entry_budget_quote),
            **({"execution_mode":execution_mode} if execution_mode != "SESSION" else {}),
            **({'research_validation_id':str(UUID(research_validation_id))} if research_validation_id is not None else {}),
            **({'research_review_digest':research_review_digest} if research_review_digest is not None else {}),
            **({"allocation_review_digest":allocation_review_digest} if allocation_review_digest is not None else {}),
            **({"continuous_until":_utc(continuous_until,"continuous_until").isoformat()} if continuous_until is not None else {})}


def prior_rearm(config: dict, request_id: str, request: dict) -> dict | None:
    for item in bot_authorizations(config):
        if item.get("request_id") == request_id:
            if item.get("request") != request:
                raise SpotBotStoreError("El ID de rearme ya se utilizó con otros datos")
            return item
    return None


def rearm_mexc_spot_bot(vault, view, trade, paper, bot_id: str, *, request_id: str,
                        policy_id: str, draft_id: str, anchor_id: str, paper_session_id: str,
                        entry_budget_quote: Decimal, assert_authorized,
                        clock=None, quote_get=None, rules_fetch=None,execution_mode="SESSION",continuous_until=None,
                        research_validation_id=None,allocation_review_digest=None,research_review_digest=None) -> dict:
    """Nueva política y cursor en un único commit; STOP sólo tras revisar todo.

    El llamante retira el permiso del trabajador anterior antes de entrar.
    Un ciclo antiguo queda invalidado por política/claim; no se roba una
    reserva ni se interpreta un POST incierto como una orden inexistente.
    """
    now = clock or (lambda: datetime.now(timezone.utc))
    _money(entry_budget_quote, "entry_budget_quote", positive=True)
    request_id, policy_id = str(UUID(request_id)), str(UUID(policy_id))
    request = rearm_request(draft_id, anchor_id, paper_session_id, entry_budget_quote,execution_mode,continuous_until,
                            research_validation_id,allocation_review_digest,research_review_digest)
    assert_authorized()
    generation = vault.session_generation
    if generation is None:
        raise SpotBotStoreError("Desbloquea REAL antes de revisar el bot")
    with vault.guard_session_generation(generation), paper.connect() as paper_db:
        paper_db.execute("BEGIN IMMEDIATE")
        with vault.session() as db:
            row, config, state = bot_record(db, bot_id)
            previous = prior_rearm(config, request_id, request)
            if previous is not None:
                return {"replayed":True, "policy_id":previous["policy_id"]}
            if row.status != "PAUSED":
                raise SpotBotStoreError("Detén el bot antes de rearmarlo")
            snapshot = (row.config_digest, row.state_digest, row.cycle_id, row.updated_at)
            draft = _stored_policy(db, draft_id)
            if (draft.manual_only or draft.enabled or draft.account_ref != row.account_ref
                    or draft.strategy_version_id != config["source"]["version_id"]
                    or draft.symbol != config["source"]["symbol"].replace("/", "")
                    or entry_budget_quote > draft.max_order_quote):
                raise SpotBotStoreError("Los límites nuevos no corresponden al mismo bot")
            stop = db.get(RealLiveStop, row.account_ref)
            stopped = bool(stop is not None and stop.stopped)
            stop_snapshot = (stop.stopped, stop.updated_at) if stop is not None else None
            anchor = db.get(RealMexcParityObservation, anchor_id)
            if (anchor is None or anchor.account_ref != row.account_ref or anchor.account_id != row.account_id
                    or anchor.checked_at < row.updated_at):
                raise SpotBotStoreError("Guarda una apertura nueva después de detener el bot")
            account_id = row.account_id
            book_revision = account_book_revision(db, account_id)
            if book_revision != anchor.book_revision:
                raise SpotBotStoreError("El libro cambió después de revisar el saldo")
        # Exige que TODAS las decisiones tengan un resultado final verificable.
        # Un claim de un proceso caído sólo se invalida si no deja envío incierto.
        ledger, revision = owned_spot_bot_ledger(vault, bot_id)
        source = verified_spot_bot_observation(paper, paper_session_id, now=now(), verify_backtest=True)
        anchor = archived_dca_anchor(config)
        research = check_mexc_research(paper,source,research_validation_id,dca_anchor=anchor) if research_validation_id is not None else None
        if research is not None:
            research.assert_review(research_review_digest)
        gated = source.binding().get("kind") == "DCA" and dca_signal_window_key(source.definition)
        if gated and (research is None or config["policy_id"] not in config.get("market_stream_seeds", {})):
            raise SpotBotStoreError("DCA condicionado requiere mercado REAL propio y reserva verificada")
        if (any(source.binding()[key] != config["source"][key] for key in (
                "version_id", "symbol", "timeframe", "definition_hash", "engine_version"))
                or source.candle_t < state["last_candle_t"]
                or source.candle_t != int(now().timestamp()) // INTERVALS[source.timeframe]
                   * INTERVALS[source.timeframe] - INTERVALS[source.timeframe]):
            raise SpotBotStoreError("Selecciona una observación PAPER actual de la misma estrategia y temporalidad")
        with vault.session() as db:
            row, _, _ = bot_record(db,bot_id)
            baseline, allocation = review_spot_bot_allocation(vault,db,row,config,ledger,revision,book_revision)
        if allocation["state"] == "ASSET_REVIEW_REQUIRED":
            raise SpotBotStoreError("Hay movimientos de activos ajenos al bot; revisa su asignación antes de rearmar")
        if allocation.get("review_digest") != allocation_review_digest:
            raise SpotBotStoreError("Confirma la revisión de asignación vigente antes de rearmar")
        assigned_sale = allocation["state"] == "MANUAL_BOT_DEBIT"
        effective_units = Decimal(allocation["owned_units_after"]) if assigned_sale else ledger.position.units
        effective_cost = Decimal(allocation["owned_cost_quote_after"]) if assigned_sale else ledger.position.cost_quote

        def apply(db, policy, inputs, at):
            assert_authorized()
            if research is not None: research.assert_current()
            row, current_config, current_state = bot_record(db, bot_id)
            current_stop = db.get(RealLiveStop, row.account_ref)
            actual_stop = ((current_stop.stopped, current_stop.updated_at) if current_stop is not None else None)
            if (row.status != "PAUSED" or
                    (row.config_digest, row.state_digest, row.cycle_id, row.updated_at) != snapshot
                    or actual_stop != stop_snapshot or account_book_revision(db, account_id) != book_revision
                    or db.scalar(select(RealSpotBot.id).where(RealSpotBot.account_ref == row.account_ref,
                        RealSpotBot.id != bot_id, RealSpotBot.status == "ACTIVE").limit(1))
                    or db.scalar(select(RealLiveSpotIntent.id).where(RealLiveSpotIntent.account_ref == row.account_ref,
                        RealLiveSpotIntent.status.in_(("RESERVED", "SUBMISSION_UNKNOWN"))).limit(1))):
                raise SpotBotStoreError("El bot, su parada, el saldo o una orden cambiaron durante la revisión")
            verified, latest_revision = owned_spot_bot_ledger(vault, bot_id, db=db)
            if latest_revision != revision or verified != ledger or effective_units > inputs.state.free_base:
                raise SpotBotStoreError("Las unidades propias del bot no están disponibles o cambiaron")
            if review_spot_bot_allocation(vault,db,row,current_config,verified,latest_revision,book_revision) != (baseline,allocation):
                raise SpotBotStoreError("La asignación de la cuenta cambió durante la revisión")
            if (at >= policy.expires_at or at < row.updated_at
                    or source.candle_t != int(at.timestamp()) // INTERVALS[source.timeframe]
                       * INTERVALS[source.timeframe] - INTERVALS[source.timeframe]
                    or any(stamp > at or (at - stamp).total_seconds() > policy.max_data_age_seconds
                       for stamp in (inputs.state.observed_at, inputs.state.quote_at, inputs.rules.observed_at))):
                raise SpotBotStoreError("La revisión del bot caducó antes de confirmarse")
            history = bot_authorizations(current_config)
            if any(item["policy_id"] == policy.policy_id for item in history):
                raise SpotBotStoreError("El rearme requiere una política nueva")
            if gated:
                from app.live_execution.spot_bot_market import read_market_stream
                _, previous_bars, _ = read_market_stream(db, current_config["policy_id"])
                if tuple(previous_bars) != source.candles[:len(previous_bars)]:
                    if "volume_gate" in source.definition:
                        raise SpotBotStoreError("El histórico PAPER del rearme debe conservar el volumen y los cierres del bot REAL")
                    raise SpotBotStoreError("El histórico PAPER del rearme debe conservar los cierres de la media del bot REAL")
            with localcontext() as context:
                context.prec = 300
                average = effective_cost / effective_units if effective_units else None
                highs = [value for value in (average, Decimal(current_state["trailing_high"])
                         if current_state["trailing_high"] else None, inputs.state.best_bid) if value is not None]
                high = max(highs) if effective_units else None
            permission = None
            if execution_mode == "CONTINUOUS":
                from app.live_execution.spot_bot_permission import make_continuity_grant,continuity_scope
                if not current_config.get("market_stream_seeds"): raise SpotBotStoreError("Recrea el bot legado con mercado propio")
                scoped = {**current_config,"source":source.binding(),"entry_budget_quote":str(entry_budget_quote),"ownership_baseline":baseline}
                permission = make_continuity_grant(policy,draft,continuity_scope(scoped),request_id,now=at,until=continuous_until)
            elif execution_mode == "OPERATIONAL":
                from app.live_execution.spot_bot_permission import make_permission
                if not current_config.get("market_stream_seeds"): raise SpotBotStoreError("Recrea el bot legado con mercado propio")
                permission = make_permission(policy,source.binding(),request_id,now=at)
            history.append({"policy_id":policy.policy_id, "source":source.binding(),
                **({'research_binding':research.binding.exported()} if research is not None else {}),
                **({"execution_permission":permission} if permission is not None else {}),
                "entry_budget_quote":str(entry_budget_quote), "started_at":at.isoformat(),
                "request_id":request_id, "request":request, "previous_config_digest":row.config_digest,
                "review":{"gap_from":current_state["last_checked_at"], "checked_at":at.isoformat(),
                    "observation_gap_acknowledged":True, "ledger_revision":revision,
                    "units":str(ledger.position.units), "cost_quote":str(ledger.position.cost_quote),
                    "book_revision":book_revision, "quote_at":inputs.state.quote_at.isoformat(),
                    "bid":str(inputs.state.best_bid), "ask":str(inputs.state.best_ask),
                    **({"allocation":allocation} if allocation_review_digest is not None else {})}})
            current_config.update(policy_id=policy.policy_id, source=source.binding(),
                entry_budget_quote=str(entry_budget_quote), authorizations=history, ownership_baseline=baseline)
            current_config.pop('research_binding',None)
            if research is not None: current_config['research_binding'] = research.binding.exported()
            current_config.pop("execution_permission",None)
            if permission is not None: current_config["execution_permission"] = permission
            current_state.update(last_candle_t=source.candle_t, last_checked_at=inputs.state.quote_at.isoformat(),
                trailing_high=str(high) if high is not None else None, reason="REARM_REVIEWED",
                control_revision=request_id)
            current_state.pop("continuity_head",None)
            if config["source"].get("kind") == "DCA":
                current_state["dca_skip_through_t"] = source.candle_t
            if config.get("market_stream_seeds"):
                from app.live_execution.spot_bot_market import market_seed,save_market_seed
                save_market_seed(db,market_seed(source,bot_id,policy.policy_id,now=at),current_config,current_state)
            # Conserva last_decision_id y todos los fills; no compra la vela revisada.
            row.config_json, row.config_digest = encoded(current_config)
            row.state_json, row.state_digest = encoded(current_state)
            row.policy_id, row.cycle_id, row.status, row.updated_at = policy.policy_id, None, "ACTIVE", at
            db.flush()
            bot_record(db, bot_id)
            if assigned_sale:
                adjusted, _ = owned_spot_bot_ledger(vault, bot_id, db=db)
                if adjusted.position.units != effective_units or adjusted.position.cost_quote != effective_cost:
                    raise SpotBotStoreError("La atribución no reproduce la posición revisada")

        if stopped:
            resume_spot_execution(vault, view, trade, draft_id, anchor_id, policy_id, clock=now,
                quote_get=quote_get, rules_fetch=rules_fetch, persist_in_transaction=apply,
                expires_before=continuous_until if execution_mode == "CONTINUOUS" else None)
        else:
            def persist(policy, at):
                inputs = fetch_spot_execution_inputs(vault, view, policy, clock=now,
                    quote_get=quote_get, rules_fetch=rules_fetch)
                completed_at = _utc(now(), "now")
                with vault.session() as db:
                    try:
                        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
                        _register_spot_policy_in_transaction(db, policy, now=completed_at)
                        apply(db, policy, inputs, completed_at)
                        db.commit()
                    except Exception:
                        db.rollback()
                        raise
            activate_spot_execution(vault, view, trade, draft_id, anchor_id, policy_id, clock=now,
                quote_get=quote_get, rules_fetch=rules_fetch, persist_policy=persist,
                expires_before=continuous_until if execution_mode == "CONTINUOUS" else None)
        assert_authorized()
        with vault.session() as db:
            committed, _, _ = bot_record(db, bot_id)
            if committed.policy_id != policy_id or committed.status != "ACTIVE":
                raise SpotBotStoreError("El rearme no quedó confirmado o el bot volvió a detenerse")
        return {"replayed":False, "policy_id":policy_id}
