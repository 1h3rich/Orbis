"""Archivo cifrado de bots/decisiones y reconstrucción de unidades propias."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone, timedelta
from decimal import Decimal,InvalidOperation,localcontext
import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy import and_, select

from app.automation.dca import SIGNAL_ENGINE_VERSIONS, VOLUME_ENGINE_VERSION, dca_signal_window_key
from app.live_execution.journal import (
    LiveJournalError, _stored_policy, _register_spot_policy_in_transaction, intent_request_digest, recover_spot_intent_in_session,
    reserve_spot_intent_in_transaction,
)
from app.live_execution.mexc_fill_event import booked_event_matches
from app.live_execution.policy import LiveSpotIntent, LiveSpotPolicy, LiveSpotState, _derived_money, _money, _utc
from app.live_execution.spot_bot_preparation import PreparedSpotBotOrder
from app.live_execution.spot_bot_plan import SpotBotPlan
from app.live_execution.spot_bot_position import OwnedSpotFill, fold_owned_spot_fills
from app.live_execution.spot_bot_source import SpotBotObservation
from app.portfolio.mexc_observations import observed_fill_digest_matches
from app.portfolio.models import (
    MexcObservedFill, RealEvent, RealLiveOrderObservation, RealLiveSpotIntent, RealLiveStop,
    RealMexcAccountLink, RealMexcFillBooking, RealLiveSpotSettlement, RealSpotBot, RealSpotBotDecision,RealSpotBotMarketStream,
)
from app.portfolio.vault import RealVault
from app.portfolio.repository import verify_account_event_digests


class SpotBotStoreError(LiveJournalError):
    """El bot o su pedido no están íntegros o ya hay otro ciclo en curso."""


ATOMIC_PREPARATION_PROTOCOL = "DECISION_RESERVATION_ATOMIC_V1"
LEGACY_CANCEL_STATE = "LEGACY_CANCELED_WITHOUT_FILLS"
LEGACY_CANCEL_REVIEWS = "legacy_no_fill_reviews"
LEGACY_FILL_RECOVERIES = "legacy_fill_recoveries"


def bot_decision_payload(db,row: RealSpotBotDecision) -> dict:
    """Lee formatos heredados; el protocolo nuevo exige su reserva exacta."""
    payload = decoded(row.payload_json,row.payload_digest)
    if payload.get("bot_id") != row.bot_id or payload.get("intent_id") != row.intent_id:
        raise SpotBotStoreError("La decisión cambió de bot o pedido")
    if "preparation_protocol" in payload:
        if payload["preparation_protocol"] != ATOMIC_PREPARATION_PROTOCOL:
            raise SpotBotStoreError("Protocolo de preparación del bot desconocido")
        try:
            UUID(payload["preparation_cycle_id"])
            if not all(isinstance(payload.get(key),str) for key in ("quantity_base","limit_price_quote")):
                raise ValueError
            quantity,price = Decimal(payload["quantity_base"]),Decimal(payload["limit_price_quote"])
            _money(quantity,"quantity_base",positive=True)
            _money(price,"limit_price_quote",positive=True)
        except (KeyError,ValueError,TypeError,AttributeError,InvalidOperation):
            raise SpotBotStoreError("Los términos de la preparación del bot son inválidos") from None
        intent = db.get(RealLiveSpotIntent,row.intent_id)
        if intent is None or intent.request_digest != payload.get("request_digest"):
            raise SpotBotStoreError("La preparación atómica perdió su reserva o ésta cambió")
        try:
            restored = recover_spot_intent_in_session(db,intent.id)
        except (LiveJournalError,ValueError,TypeError):
            raise SpotBotStoreError("El pedido de la preparación atómica está alterado") from None
        bot,config,_ = bot_record(db,row.bot_id)
        sources = {item["policy_id"]:item["source"] for item in bot_authorizations(config)}
        if config["source"].get("kind") == "DCA":
            detail = payload.get("dca")
            if (restored.side != "BUY" or not isinstance(detail,dict) or detail.get("kind") not in ("BASE","DIP")
                    or not isinstance(detail.get("key"),str) or not detail["key"].startswith(detail["kind"]+":")):
                raise SpotBotStoreError("La decisión DCA no corresponde a una compra identificada")
        source = sources.get(restored.policy_id)
        if restored.policy_id in config.get("market_stream_seeds",{}):
            from app.live_execution.spot_bot_market import verify_market_decision
            verify_market_decision(db,payload,restored.policy_id)
            if source and source.get("kind") == "DCA" and source["engine_version"] in SIGNAL_ENGINE_VERSIONS:
                with localcontext() as context:
                    context.prec = 300
                    reserved = quantity * price + restored.estimated_fee_quote
                if reserved > Decimal(payload["dca"]["budget_limit"]):
                    raise SpotBotStoreError("La reserva DCA supera el importe ajustado archivado")
        elif payload.get("market_observation") is not None:
            raise SpotBotStoreError("Observación REAL fuera de un seguimiento aprobado")
        elif source and source.get("kind") == "DCA" and source["engine_version"] == VOLUME_ENGINE_VERSION:
            raise SpotBotStoreError("La decisión DCA de volumen perdió su observación REAL")
        if (source != payload.get("source") or restored.account_ref != bot.account_ref
                or source is None or restored.strategy_version_id != source["version_id"]
                or restored.symbol != source["symbol"].replace("/","")
                or intent_request_digest(restored) != payload["request_digest"] or restored.side != payload.get("side")
                or restored.quantity_base != quantity or restored.limit_price_quote != price):
            raise SpotBotStoreError("La preparación y los importes del pedido no coinciden")
    return payload


def encoded(value: dict) -> tuple[str, str]:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return raw, hashlib.sha256(raw.encode()).hexdigest()


def decoded(raw: str, digest: str) -> dict:
    if hashlib.sha256(raw.encode()).hexdigest() != digest:
        raise SpotBotStoreError("La huella del bot o su decisión cambió")
    try:
        result = json.loads(raw)
        if not isinstance(result, dict) or encoded(result)[0] != raw:
            raise ValueError
        return result
    except (ValueError, TypeError):
        raise SpotBotStoreError("El archivo del bot es ilegible") from None


def bot_authorizations(config: dict) -> list[dict]:
    """Linaje plano de permisos; los bots v25 anteriores tienen un único origen."""
    history = config.get("authorizations")
    if history is None:
        return [{"policy_id":config["policy_id"], "source":config["source"],
                 "entry_budget_quote":config["entry_budget_quote"], "started_at":config["created_at"],
                 **({'research_binding':config['research_binding']} if 'research_binding' in config else {}),
                 **({"execution_permission":config["execution_permission"]} if "execution_permission" in config else {})}]
    if not isinstance(history, list) or not history:
        raise SpotBotStoreError("El historial de autorizaciones del bot es ilegible")
    policies, requests = set(), set()
    for item in history:
        if (not isinstance(item, dict) or not isinstance(item.get("policy_id"), str)
                or item["policy_id"] in policies or not isinstance(item.get("source"), dict)
                or not isinstance(item.get("entry_budget_quote"), str)):
            raise SpotBotStoreError("El historial de autorizaciones del bot cambió")
        policies.add(item["policy_id"])
        if "request_id" in item:
            if item["request_id"] in requests:
                raise SpotBotStoreError("Una revisión del bot está duplicada")
            requests.add(item["request_id"])
        for name in ("version_id", "symbol", "timeframe", "engine_version", "definition_hash", "kind"):
            if item["source"].get(name) != config["source"].get(name):
                raise SpotBotStoreError("La estrategia del bot cambió entre autorizaciones")
    if any(history[-1].get(name) != config.get(name) for name in ("policy_id", "source", "entry_budget_quote","execution_permission","research_binding")):
        raise SpotBotStoreError("La autorización actual no coincide con su historial")
    return history


def bot_external_book_events(db, account_id: str, bot_id: str) -> list[RealEvent]:
    """Verified originals with the bot's own booked fills removed."""
    verify_account_event_digests(db, account_id)
    own_ids = set(db.scalars(select(RealMexcFillBooking.event_id)
        .join(MexcObservedFill, MexcObservedFill.id == RealMexcFillBooking.fill_id)
        .join(RealLiveOrderObservation, and_(
            RealLiveOrderObservation.account_ref == MexcObservedFill.account_ref,
            RealLiveOrderObservation.symbol == MexcObservedFill.symbol,
            RealLiveOrderObservation.external_order_id == MexcObservedFill.order_id))
        .join(RealSpotBotDecision, RealSpotBotDecision.intent_id == RealLiveOrderObservation.intent_id)
        .where(RealSpotBotDecision.bot_id == bot_id)).all())
    return [row for row in db.scalars(select(RealEvent).where(RealEvent.account_id == account_id)
            .order_by(RealEvent.id)).all() if row.id not in own_ids]


def ownership_fingerprint(events: list[RealEvent]) -> str:
    other = [(row.id,row.payload_digest) for row in sorted(events,key=lambda row:row.id)]
    return hashlib.sha256(json.dumps(other,separators=(",", ":")).encode()).hexdigest()


def bot_ownership_baseline(db, account_id: str, bot_id: str) -> str:
    return ownership_fingerprint(bot_external_book_events(db,account_id,bot_id))


def _validate_manual_allocation(allocation: dict, symbol: str) -> None:
    base, quote = symbol.split('/')
    fills = allocation['manual_fill_count']
    movements = allocation['movement_count']
    if (allocation['base_asset'] != base or allocation['quote_currency'] != quote
            or type(fills) is not int or type(movements) is not int or not 1 <= fills <= movements):
        raise ValueError('allocation scope')

    def units(field: str) -> Decimal:
        raw = allocation[field]
        if not isinstance(raw, str):
            raise ValueError('allocation units')
        return _derived_money(Decimal(raw), field)

    bought = units('manual_purchase_units')
    if allocation['state'] == 'MANUAL_PURCHASES':
        if bought <= 0:
            raise ValueError('purchase quantity')
        return
    with localcontext() as context:
        context.prec = 300
        sold = units('manual_sale_units')
        assigned = Decimal(0)
        if allocation['state'] == 'MANUAL_BOT_DEBIT':
            shares = allocation['assigned_sales']
            if not isinstance(shares, list) or not 1 <= len(shares) <= fills:
                raise ValueError('assigned sale count')
            if any(set(share) != {"event_id", "event_hash", "fill_id", "fill_hash",
                                  "observation_id", "observation_hash", "units"} for share in shares):
                raise ValueError('assigned sale fields')
            quantities = [_derived_money(Decimal(share['units']), 'units') for share in shares]
            if any(quantity <= 0 for quantity in quantities):
                raise ValueError('assigned sale quantity')
            assigned = sum(quantities, Decimal(0))
            if (assigned != units('assigned_units') or assigned > sold
                    or units('owned_units') - assigned != units('owned_units_after')
                    or units('owned_cost_quote_after') > units('owned_cost_quote')):
                raise ValueError('assigned sale basis')
            units('assigned_net_quote')
        if sold <= 0 or units('outside_units_before') + bought - sold + assigned != units('outside_units_after'):
            raise ValueError('sale attribution')


def bot_record(db, identifier: str):
    row = db.get(RealSpotBot, identifier)
    if row is None:
        raise SpotBotStoreError("Bot REAL no encontrado")
    config, state = decoded(row.config_json, row.config_digest), decoded(row.state_json, row.state_digest)
    if (config.get("id") != row.id or config.get("policy_id") != row.policy_id
            or config.get("account_id") != row.account_id or config.get("account_ref") != row.account_ref
            or row.status not in ("ACTIVE", "PAUSED", "STOPPED")):
        raise SpotBotStoreError("El ámbito del bot cambió")
    authorizations = bot_authorizations(config)
    for index,authorization in enumerate(authorizations):
        request = authorization.get('request') if index else config.get('activation_request')
        requested_research = request.get('research_validation_id') if isinstance(request,dict) else None
        reviewed_research = request.get('research_review_digest') if isinstance(request,dict) else None
        allocation_digest = request.get('allocation_review_digest') if index and isinstance(request,dict) else None
        if allocation_digest is not None:
            review = authorization.get('review')
            allocation = review.get('allocation') if isinstance(review,dict) else None
            if (not isinstance(allocation,dict) or allocation.get('state') not in ('CASH_ONLY', 'MANUAL_PURCHASES', 'MANUAL_SALES', 'MANUAL_BOT_DEBIT')
                    or allocation.get('review_digest')!=allocation_digest
                    or allocation.get('owned_units')!=review.get('units')
                    or allocation.get('owned_cost_quote')!=review.get('cost_quote')):
                raise SpotBotStoreError('La autorización perdió su revisión de asignación')
            if allocation['state'] in ('MANUAL_PURCHASES', 'MANUAL_SALES', 'MANUAL_BOT_DEBIT'):
                try:
                    _validate_manual_allocation(allocation, authorization['source']['symbol'])
                except (KeyError, ValueError, TypeError, InvalidOperation):
                    raise SpotBotStoreError('La operación manual perdió su revisión de asignación') from None
        research = authorization.get('research_binding')
        if (requested_research is not None or reviewed_research is not None) and research is None:
            raise SpotBotStoreError('La autorización perdió su validación fuera de muestra')
        if research is not None:
            from app.research.mexc_binding import read_mexc_research_binding
            try:
                anchor = ({'policy':'RESET_AT_REAL_ACTIVATION','candle_t':config['dca_start_candle_t'],
                           'price':config['dca_initial_reference']} if authorization['source'].get('kind') == 'DCA' else None)
                checked = read_mexc_research_binding(research,authorization['source'],dca_anchor=anchor)
                if requested_research is not None and str(checked.research.artifact_id) != requested_research:
                    raise ValueError('reference')
                if reviewed_research is not None and checked.sha256 != reviewed_research:
                    raise ValueError('review')
            except (KeyError,TypeError,ValueError):
                raise SpotBotStoreError('La validación archivada del bot cambió') from None
        if 'continuity' in authorization and index and research != authorizations[index-1].get('research_binding'):
            raise SpotBotStoreError('La renovación cambió la investigación autorizada')
        if "execution_permission" in authorization:
            from app.live_execution.spot_bot_permission import validate_permission
            try:
                if authorization["execution_permission"].get("protocol") == "CONTINUOUS_V1":
                    from app.live_execution.spot_bot_continuity import validate_continuity_authorization
                    validate_continuity_authorization(db,config,state,authorizations,index)
                else:
                    validate_permission(authorization["execution_permission"],_stored_policy(db,authorization["policy_id"]),authorization["source"])
                if authorization["policy_id"] not in config.get("market_stream_seeds",{}): raise ValueError("market")
            except (ValueError,KeyError,TypeError,AttributeError,StopIteration):
                raise SpotBotStoreError("La concesión operativa del bot cambió") from None
    if config.get("market_stream_seeds"):
        try:
            seeds,heads = config["market_stream_seeds"],state["market_heads"]
            if not isinstance(seeds,dict) or not isinstance(heads,dict) or set(seeds) != set(heads):
                raise ValueError("heads")
            for policy_id,digest in seeds.items():
                stream = db.get(RealSpotBotMarketStream,policy_id)
                if (stream is None or stream.bot_id != row.id or stream.seed_digest != digest
                        or heads[policy_id]["policy_id"] != policy_id):
                    raise ValueError("stream")
        except (KeyError,ValueError,TypeError):
            raise SpotBotStoreError("El bot perdió su archivo de mercado REAL aprobado") from None
    if LEGACY_CANCEL_REVIEWS in config:
        from app.live_execution.spot_bot_legacy_review import validate_legacy_cancel_reviews
        validate_legacy_cancel_reviews(db, row, config)
    if LEGACY_FILL_RECOVERIES in config:
        from app.live_execution.spot_bot_legacy_fills import validate_legacy_fill_recoveries
        validate_legacy_fill_recoveries(db, row.id, config)
    return row, config, state


def register_spot_bot(vault: RealVault, policy: LiveSpotPolicy, observation: SpotBotObservation, *,
                      bot_id: str, entry_budget_quote: Decimal, now: datetime, register_policy: bool = False,
                      activation_request: dict | None = None, dca_limits: dict | None = None,
                      own_market: bool = False,execution_mode: str = "SESSION",
                      continuous_until:datetime|None=None,draft_policy:LiveSpotPolicy|None=None,
                      research_binding:dict|None=None) -> str:
    """Vincula una política activa sin pedidos previos a una estrategia probada.

    Es una primitiva interna. El coordinador verifica el origen/backtest y el
    saldo antes de llamarla. La fila ACTIVE por sí sola no inicia un trabajador;
    las credenciales y el permiso efímero de ejecución no se persisten.
    """
    now = _utc(now, "now")
    _money(entry_budget_quote, "entry_budget_quote", positive=True)
    bot_id = str(UUID(bot_id))
    if (policy.manual_only or not policy.enabled or not policy.reconciliation_anchor_id
            or entry_budget_quote > policy.max_order_quote
            or observation.version_id != policy.strategy_version_id
            or observation.symbol.replace("/", "") != policy.symbol or now >= policy.expires_at):
        raise SpotBotStoreError("El origen, la política o el presupuesto no coinciden")
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            if register_policy:
                _register_spot_policy_in_transaction(db, policy, now=now)
            link = db.get(RealMexcAccountLink, policy.account_ref)
            stop = db.get(RealLiveStop, policy.account_ref)
            if link is None or stop is not None and stop.stopped or _stored_policy(db, policy.policy_id) != policy:
                raise SpotBotStoreError("La cuenta del bot no está habilitada")
            bots = db.scalars(select(RealSpotBot).where(RealSpotBot.account_ref == policy.account_ref)).all()
            if (any(bot.status == "ACTIVE" or policy.policy_id in {
                    item["policy_id"] for item in bot_authorizations(bot_record(db, bot.id)[1])} for bot in bots)
                    or db.scalar(select(RealLiveSpotIntent.id).where(RealLiveSpotIntent.policy_id == policy.policy_id).limit(1))):
                raise SpotBotStoreError("La cuenta ya tiene un bot activo o la política ya se utilizó")
            config = {"id":bot_id, "account_id":link.account_id, "account_ref":policy.account_ref,
                      "policy_id":policy.policy_id, "entry_budget_quote":str(entry_budget_quote),
                      "source":observation.binding(), "created_at":now.isoformat()}
            config["ownership_baseline"] = bot_ownership_baseline(db, link.account_id, bot_id)
            if activation_request is not None:
                config["activation_request"] = dict(activation_request)
            state = {"last_candle_t":observation.candle_t, "last_checked_at":now.isoformat(),
                     "trailing_high":None, "last_decision_id":None, "reason":"WAITING_NEXT_CANDLE"}
            if observation.binding().get("kind") == "DCA":
                from app.live_execution.dca_bot_plan import dca_real_limits
                if dca_limits is None: raise SpotBotStoreError("DCA requiere asignación y límite mensual REAL explícitos")
                if dca_signal_window_key(observation.definition) and (not own_market or research_binding is None):
                    raise SpotBotStoreError("DCA condicionado requiere mercado REAL propio y reserva verificada")
                config["dca_limits"] = dca_real_limits(dca_limits["budget_total_quote"],dca_limits["monthly_limit_quote"])
                config.update(dca_initial_reference=str(observation.candles[-1].c),dca_start_candle_t=observation.candle_t)
                state.update(dca_reference=config["dca_initial_reference"],dca_skip_through_t=observation.candle_t)
            elif dca_limits is not None:
                raise SpotBotStoreError("Límites DCA en una estrategia no DCA")
            if research_binding is not None:
                from app.research.mexc_binding import read_mexc_research_binding
                anchor = ({'policy':'RESET_AT_REAL_ACTIVATION','candle_t':config['dca_start_candle_t'],
                           'price':config['dca_initial_reference']} if observation.binding().get('kind') == 'DCA' else None)
                config['research_binding'] = read_mexc_research_binding(research_binding,observation.binding(),dca_anchor=anchor).exported()
            if execution_mode == "CONTINUOUS":
                from app.live_execution.spot_bot_permission import make_continuity_grant,continuity_scope
                if not own_market or continuous_until is None or draft_policy is None:
                    raise SpotBotStoreError("La continuidad requiere mercado propio, límites y fecha explícitos")
                config["execution_permission"] = make_continuity_grant(policy,draft_policy,continuity_scope(config),
                    bot_id,now=now,until=continuous_until)
            elif continuous_until is not None or draft_policy is not None:
                raise SpotBotStoreError("La fecha de continuidad exige su permiso separado")
            elif execution_mode == "OPERATIONAL":
                from app.live_execution.spot_bot_permission import make_permission
                if not own_market: raise SpotBotStoreError("La concesión requiere mercado REAL propio")
                config["execution_permission"] = make_permission(policy,observation.binding(),bot_id,now=now)
            elif execution_mode != "SESSION": raise SpotBotStoreError("Modo de permiso desconocido")
            if own_market:
                from app.live_execution.spot_bot_market import market_seed,save_market_seed,seed_pointer
                # El FK depende de la fila de bot que se añade más abajo.
                seed = market_seed(observation,bot_id,policy.policy_id,now=now)
                config["market_stream_seeds"] = {policy.policy_id:encoded(seed)[1]}
                state["market_heads"] = {policy.policy_id:seed_pointer(seed,encoded(seed)[1])}
            config_json, config_digest = encoded(config)
            state_json, state_digest = encoded(state)
            db.add(RealSpotBot(id=bot_id, account_id=link.account_id, account_ref=policy.account_ref,
                               policy_id=policy.policy_id, config_json=config_json, config_digest=config_digest,
                               state_json=state_json, state_digest=state_digest, status="ACTIVE", cycle_id=None,
                               created_at=now, updated_at=now))
            if own_market:
                db.flush()
                save_market_seed(db,seed,config,state)
            db.flush()
            bot_record(db,bot_id)
            db.commit()
        except Exception:
            db.rollback()
            raise
    return bot_id


def claim_spot_bot_cycle(vault: RealVault, bot_id: str, *, now: datetime,
                         expected_policy_id: str | None = None) -> tuple[str, dict, dict]:
    now = _utc(now, "now")
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            row, config, state = bot_record(db, bot_id)
            stop = db.get(RealLiveStop, row.account_ref)
            if row.status != "ACTIVE" or row.cycle_id is not None or stop is not None and stop.stopped:
                raise SpotBotStoreError("Bot detenido, pausado o con un ciclo pendiente de revisión")
            if expected_policy_id is not None and row.policy_id != expected_policy_id:
                raise SpotBotStoreError("Este trabajador pertenece a una autorización anterior")
            if now < row.updated_at:
                raise SpotBotStoreError("El reloj del bot retrocedió")
            claim = str(uuid4())
            row.cycle_id, row.updated_at = claim, now
            db.commit()
            return claim, config, state
        except Exception:
            db.rollback()
            raise


def pause_spot_bot(vault: RealVault, bot_id: str, *, now: datetime, reason: str = "USER_STOP",
                   only_if_active: bool = False, expected_policy_id: str | None = None) -> None:
    """Invalida ciclos en curso sin borrar su marca ni liberar pedidos inciertos."""
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            row, _, state = bot_record(db, bot_id)
            if (only_if_active and row.status != "ACTIVE"
                    or expected_policy_id is not None and row.policy_id != expected_policy_id):
                db.commit()
                return
            row.status = "PAUSED"
            state["reason"] = reason
            state["control_revision"] = str(uuid4())
            row.state_json, row.state_digest = encoded(state)
            row.updated_at = max(row.updated_at, _utc(now, "now"))
            db.commit()
        except Exception:
            db.rollback()
            raise


def assert_spot_bot_cycle(vault: RealVault, bot_id: str, claim: str, *, db=None) -> None:
    with (vault.session() if db is None else nullcontext(db)) as db:
        row, _, _ = bot_record(db, bot_id)
        stop = db.get(RealLiveStop, row.account_ref)
        if row.cycle_id != claim or row.status != "ACTIVE" or stop is not None and stop.stopped:
            raise SpotBotStoreError("El bot o la cuenta se detuvieron durante el ciclo")


def finish_spot_bot_cycle(vault: RealVault, bot_id: str, claim: str, state: dict, *,
                          now: datetime, pause: bool = False) -> None:
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            row, _, current = bot_record(db, bot_id)
            if row.cycle_id != claim:
                raise SpotBotStoreError("El ciclo del bot cambió")
            if row.status != "ACTIVE":
                state = {**state, "reason":current["reason"], "control_revision":current.get("control_revision")}
            if "market_heads" in current:
                # La vela se confirma antes de evaluar/enviar. Un fallo
                # posterior no revierte la cabeza durable a la copia del claim.
                state = {**state,"market_heads":current["market_heads"]}
            if "continuity_head" in current:
                state = {**state,"continuity_head":current["continuity_head"]}
            row.state_json, row.state_digest = encoded(state)
            row.cycle_id = None
            row.updated_at = max(row.updated_at, _utc(now, "now"))
            if pause and row.status == "ACTIVE":
                row.status = "PAUSED"
            db.commit()
        except Exception:
            db.rollback()
            raise


def archive_spot_bot_decision(vault: RealVault, bot_id: str, claim: str,
                              intent: LiveSpotIntent, plan: SpotBotPlan, source: SpotBotObservation,
                              *, ledger_revision: str, quote_at: datetime, best_bid: Decimal,
                              best_ask: Decimal, owned_units: Decimal, owned_cost_quote: Decimal,
                              now: datetime,reserve_state: LiveSpotState) -> PreparedSpotBotOrder:
    """Decisión, cursor y reserva juntos; un commit no concede replay de envío."""
    generation = vault.session_generation
    if generation is None:
        raise SpotBotStoreError("Desbloquea REAL antes de preparar una decisión")
    with vault.guard_session_generation(generation),vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            row, config, state = bot_record(db, bot_id)
            stop = db.get(RealLiveStop, row.account_ref)
            if (row.status != "ACTIVE" or row.cycle_id != claim or stop is not None and stop.stopped
                    or intent.policy_id != row.policy_id or source.binding() != config["source"]):
                raise SpotBotStoreError("El bot o su procedencia cambiaron antes de archivar")
            payload = {"bot_id":bot_id, "intent_id":intent.intent_id, "request_digest":intent_request_digest(intent),
                       "preparation_protocol":ATOMIC_PREPARATION_PROTOCOL,"preparation_cycle_id":claim,
                       "side":intent.side, "quantity_base":str(intent.quantity_base),
                       "limit_price_quote":str(intent.limit_price_quote), "signal_at":intent.signal_at.isoformat(),
                       "candle_t":source.candle_t, "reason":plan.reason, "source":source.binding(),
                       "observation_digest":source.observation_digest, "ledger_revision":ledger_revision,
                       "real_quote":{"at":quote_at.isoformat(),"bid":str(best_bid),"ask":str(best_ask)},
                       "owned_position":{"units":str(owned_units),"cost_quote":str(owned_cost_quote)}}
            if plan.dca is not None:
                payload["dca"] = plan.dca
                state["dca_reference"] = plan.dca["reference"]
            if source.market_observation is not None:
                payload["market_observation"] = source.market_observation
            raw, digest = encoded(payload)
            last = db.get(RealSpotBotDecision,state["last_decision_id"]) if state["last_decision_id"] else None
            if last is not None and bot_decision_payload(db,last).get("preparation_cycle_id") == claim:
                raise SpotBotStoreError("El ciclo ya preparó una decisión; no concede otra reserva")
            existing = db.get(RealSpotBotDecision, intent.intent_id)
            if existing is not None or db.get(RealLiveSpotIntent,intent.intent_id) is not None:
                raise SpotBotStoreError("Una preparación existente nunca concede otra continuación de envío")
            db.add(RealSpotBotDecision(id=intent.intent_id, bot_id=bot_id, intent_id=intent.intent_id,
                                       payload_json=raw, payload_digest=digest, created_at=now))
            state.update(last_candle_t=source.candle_t, last_checked_at=quote_at.isoformat(),
                         trailing_high=str(plan.trailing_high) if plan.trailing_high is not None else None,
                         last_decision_id=intent.intent_id, reason=plan.reason)
            row.state_json, row.state_digest = encoded(state)
            recorded = reserve_spot_intent_in_transaction(db,intent,reserve_state,now=now)
            db.flush()
            bot_decision_payload(db,db.get(RealSpotBotDecision,intent.intent_id))
            preparation = PreparedSpotBotOrder(vault=vault,bot_id=bot_id,cycle_id=claim,generation=generation,
                decision_id=intent.intent_id,decision_digest=digest,request_digest=intent_request_digest(intent),recorded=recorded)
            db.commit()
            return preparation
        except Exception:
            db.rollback()
            raise


def validate_bot_order_scope(vault: RealVault, intent: LiveSpotIntent, *, decision_id: str | None = None,
                              db=None) -> None:
    """Impide usar una política del bot como puerta de órdenes manuales."""
    with (vault.session() if db is None else nullcontext(db)) as db:
        bots = db.scalars(select(RealSpotBot).where(RealSpotBot.account_ref == intent.account_ref)).all()
        if decision_id is None:
            if any(bot.status == "ACTIVE" or intent.policy_id in {
                    item["policy_id"] for item in bot_authorizations(bot_record(db, bot.id)[1])} for bot in bots):
                raise SpotBotStoreError("Detén y revisa el bot antes de usar operativa manual")
            return
        decision = db.get(RealSpotBotDecision, decision_id)
        if decision is None:
            raise SpotBotStoreError("El pedido del bot no tiene decisión archivada")
        bot, _, _ = bot_record(db, decision.bot_id)
        payload = bot_decision_payload(db,decision)
        if (bot.status != "ACTIVE" or bot.cycle_id is None or bot.policy_id != intent.policy_id
                or decision.intent_id != intent.intent_id or payload.get("request_digest") != intent_request_digest(intent)):
            raise SpotBotStoreError("El pedido no coincide con el ciclo y la decisión del bot")


def _replay_sale_attributions(db, row, history, fills_by_policy, identities):
    from app.live_execution.spot_bot_allocation import read_assigned_spot_sale

    ledger = fold_owned_spot_fills([])
    previous_at = None
    seen = {identity[0] for identity in identities}
    for item in history:
        allocation = item.get("review", {}).get("allocation", {})
        if allocation.get("state") == "MANUAL_BOT_DEBIT":
            if (ledger.position.units != Decimal(allocation["owned_units"]) or
                    ledger.position.cost_quote != Decimal(allocation["owned_cost_quote"])):
                raise SpotBotStoreError("La base de coste de la venta atribuida cambió")
            sales = []
            started = datetime.fromisoformat(item["started_at"])
            for share in allocation["assigned_sales"]:
                sale = read_assigned_spot_sale(db, row, item["source"]["symbol"], share)
                if (sale.fill_id in seen or sale.occurred_at > started or
                        previous_at is not None and sale.occurred_at <= previous_at):
                    raise SpotBotStoreError("Las ventas manuales atribuidas se duplican o se solapan")
                seen.add(sale.fill_id)
                previous_at = sale.occurred_at
                sales.append(sale)
                identities.append(("MANUAL_BOT_DEBIT", share["fill_id"], share["fill_hash"],
                                   share["event_id"], share["event_hash"], share["observation_id"],
                                   share["observation_hash"], share["units"]))
            after = fold_owned_spot_fills(sales, initial=ledger)
            with localcontext() as context:
                context.prec = 300
                if (after.position.units != Decimal(allocation["owned_units_after"]) or
                        after.position.cost_quote != Decimal(allocation["owned_cost_quote_after"]) or
                        after.net_cash_flow_quote - ledger.net_cash_flow_quote != Decimal(allocation["assigned_net_quote"])):
                    raise SpotBotStoreError("La venta atribuida no reproduce la posición aprobada")
            ledger = after
        fills = fills_by_policy.get(item["policy_id"], [])
        if fills:
            if previous_at is not None and fills[0].occurred_at < previous_at:
                raise SpotBotStoreError("Los fills posteriores no respetan el orden de la atribución")
            ledger = fold_owned_spot_fills(fills, initial=ledger)
            previous_at = fills[-1].occurred_at
    return ledger


def owned_spot_bot_ledger(vault: RealVault, bot_id: str, *, db=None, executions: list[dict] | None = None,
                          execution_policies: dict[str, str] | None = None):
    """Reconstruye desde fills asentados y exactos; excluye el inventario anterior."""
    fills, identities, fills_by_policy = [], [], {}
    with (vault.session() if db is None else nullcontext(db)) as db:
        row, config, _ = bot_record(db, bot_id)
        history = bot_authorizations(config)
        sources = {item["policy_id"]:item["source"] for item in history}
        link = db.get(RealMexcAccountLink, row.account_ref)
        if link is None or link.account_id != row.account_id:
            raise SpotBotStoreError("La cuenta del bot cambió")
        decisions = db.scalars(select(RealSpotBotDecision).where(RealSpotBotDecision.bot_id == bot_id).order_by(
            RealSpotBotDecision.created_at, RealSpotBotDecision.id)).all()
        for decision in decisions:
            payload = bot_decision_payload(db,decision)
            intent = db.get(RealLiveSpotIntent, decision.intent_id)
            legacy_cancel = config.get(LEGACY_CANCEL_REVIEWS, {}).get(decision.intent_id)
            if intent is None and legacy_cancel is not None:
                identities.append((LEGACY_CANCEL_STATE, decision.intent_id, legacy_cancel["review_digest"]))
                continue
            if intent is None or intent.status in ("RESERVED", "SUBMISSION_UNKNOWN"):
                raise SpotBotStoreError("La última orden del bot requiere seguimiento antes de reconstruir")
            restored = recover_spot_intent_in_session(db, intent.id)
            if (intent.policy_id not in sources or payload.get("source") != sources[intent.policy_id]
                    or restored.account_ref != row.account_ref
                    or restored.strategy_version_id != sources[intent.policy_id]["version_id"]
                    or restored.symbol != sources[intent.policy_id]["symbol"].replace("/", "")
                    or payload.get("bot_id") != bot_id or payload.get("intent_id") != intent.id
                    or payload.get("request_digest") != intent.request_digest
                    or intent.request_digest != intent_request_digest(restored)):
                raise SpotBotStoreError("La intención del bot cambió")
            if intent.status in ("STOPPED", "REJECTED"):
                continue
            if intent.status != "RESERVATION_CLOSED":
                raise SpotBotStoreError("Estado de orden del bot desconocido")
            observed = db.scalar(select(RealLiveOrderObservation).where(
                RealLiveOrderObservation.intent_id == intent.id).order_by(
                    RealLiveOrderObservation.observed_at.desc(), RealLiveOrderObservation.id.desc()).limit(1))
            settlement = db.get(RealLiveSpotSettlement, intent.id)
            if (observed is None or settlement is None or settlement.account_ref != row.account_ref
                    or settlement.external_order_id != observed.external_order_id):
                raise SpotBotStoreError("El cierre de la orden del bot no es verificable")
            rows = db.scalars(select(MexcObservedFill).where(MexcObservedFill.account_ref == row.account_ref,
                MexcObservedFill.symbol == intent.symbol, MexcObservedFill.order_id == observed.external_order_id).order_by(
                    MexcObservedFill.time_ms, MexcObservedFill.id)).all()
            from app.live_execution.reconciliation_view import _project_fills
            if _project_fills(intent, observed, rows)["status"] not in (
                    "FINAL_WITHOUT_EXECUTION", "NEEDS_ACCOUNT_LINK_AND_BOOKING"):
                raise SpotBotStoreError("El archivo de fills propios está incompleto o alterado")
            for fill in rows:
                booking = db.scalar(select(RealMexcFillBooking).where(RealMexcFillBooking.fill_id == fill.id))
                event = db.get(RealEvent, booking.event_id) if booking is not None else None
                if (not observed_fill_digest_matches(fill) or fill.side != intent.side
                        or not booked_event_matches(fill, intent, booking, event, link)):
                    raise SpotBotStoreError("El fill propio no coincide con su asiento REAL")
                fills.append(OwnedSpotFill(fill.id, intent.id, fill.side, fill.quantity, fill.quote_quantity,
                    fill.commission, "BASE" if fill.commission_asset == intent.base_asset else "QUOTE",
                    datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=fill.time_ms),
                    payload["candle_t"] if fill.side == "BUY" else None))
                identities.append((fill.id, fill.payload_digest, event.id, event.payload_digest))
                fills_by_policy.setdefault(intent.policy_id, []).append(fills[-1])
                if execution_policies is not None:
                    execution_policies[fill.id] = intent.policy_id
                if executions is not None:
                    executions.append({"fill_id":fill.id,"intent_id":intent.id,"event_id":event.id,
                        "side":fill.side,"time_ms":fill.time_ms,"occurred_at":fills[-1].occurred_at.isoformat(),
                        "price":str(fill.price),"quantity_base":str(fill.quantity),"gross_quote":str(fill.quote_quantity),
                        "fee":str(fill.commission),"fee_asset":fill.commission_asset,
                        "reason":payload["reason"],"signal_at":payload["signal_at"],"decision_at":decision.created_at.isoformat()})
        if any(item.get("review", {}).get("allocation", {}).get("state") == "MANUAL_BOT_DEBIT" for item in history):
            ledger = _replay_sale_attributions(db, row, history, fills_by_policy, identities)
        else:
            ledger = fold_owned_spot_fills(fills)
    return ledger, hashlib.sha256(json.dumps(identities, separators=(",", ":")).encode()).hexdigest()
