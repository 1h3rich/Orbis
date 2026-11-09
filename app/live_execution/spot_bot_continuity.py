"""Políticas diarias subordinadas a una concesión humana inmutable."""

from dataclasses import replace
from datetime import datetime,timedelta
from uuid import UUID,uuid5

from sqlalchemy import select

from app.live_execution.journal import _stored_policy,_register_spot_policy_in_transaction
from app.live_execution.mexc_continuity_review import record_continuity_review,verify_continuity_review
from app.live_execution.mexc_execution_state import fetch_spot_execution_inputs
from app.live_execution.policy import _utc
from app.portfolio.mexc_parity_archive import read_observation
from app.portfolio.models import RealLiveSpotPolicy,RealMexcParityObservation,RealLiveSpotIntent,RealLiveStop
from app.portfolio.repository import account_book_revision
from app.live_execution.spot_bot_permission import (
    continuity_scope,validate_continuity_grant,validate_continuity_policy,
)

REFRESH_PROTOCOL = "BOT_CONTINUITY_REFRESH_V1"
REFRESH_NAMESPACE = UUID("0797f01e-3d66-474e-9c1a-96d93a1083be")


def validate_continuity_authorization(db,config:dict,state:dict,history:list,index:int) -> None:
    item = history[index]
    grant = item["execution_permission"]
    root_index = next(i for i,entry in enumerate(history) if entry["policy_id"] == grant["root_policy_id"])
    root_item = history[root_index]
    if root_index > index or root_item["execution_permission"] != grant:
        raise ValueError("La concesión perdió su aprobación humana")
    root = _stored_policy(db,grant["root_policy_id"])
    draft = _stored_policy(db,grant["draft_policy_id"])
    validate_continuity_grant(grant,root,draft,grant["scope"])
    scope = grant["scope"]
    request = root_item.get("request") if root_index else config.get("activation_request")
    if (not isinstance(request,dict) or request.get("execution_mode") != "CONTINUOUS"
            or request.get("continuous_until") != grant["expires_at"]
            or request.get("draft_id") != draft.policy_id
            or root_item["source"] != scope["source"] or root_item["entry_budget_quote"] != scope["entry_budget_quote"]
            or scope["bot_id"] != config["id"] or scope["account_id"] != config["account_id"]
            or scope["account_ref"] != config["account_ref"]
            or root_item["started_at"] != grant["approved_at"]):
        raise ValueError("El alcance no coincide con su aprobación")
    policy = _stored_policy(db,item["policy_id"])
    validate_continuity_policy(grant,policy)
    if item["source"] != scope["source"] or item["entry_budget_quote"] != scope["entry_budget_quote"]:
        raise ValueError("La renovación cambió el presupuesto o el origen")
    if index == len(history)-1 and continuity_scope(config) != scope:
        raise ValueError("El alcance activo de continuidad cambió")
    if item["policy_id"] == root.policy_id:
        if "continuity" in item: raise ValueError("Aprobación humana con renovación artificial")
        return
    refresh = item["continuity"]
    previous = history[index-1]
    parent = _stored_policy(db,previous["policy_id"])
    review = db.get(RealMexcParityObservation,refresh["review_id"])
    if review is None: raise ValueError("Falta la revisión causal")
    report = read_observation(review)
    at = datetime.fromisoformat(refresh["refreshed_at"])
    boundary = datetime.fromisoformat(report["causal"]["boundary_at"])
    stored = db.get(RealLiveSpotPolicy,policy.policy_id)
    if (set(refresh) != {"protocol","previous_policy_id","review_id","review_digest","market_pointer",
                         "refreshed_at","previous_config_digest","grant_id"}
            or refresh["protocol"] != REFRESH_PROTOCOL or refresh["grant_id"] != grant["grant_id"]
            or previous.get("execution_permission") != grant or refresh["previous_policy_id"] != parent.policy_id
            or review.report_digest != refresh["review_digest"] or report["causal"]["policy_id"] != parent.policy_id
            or review.account_id != config["account_id"] or review.account_ref != config["account_ref"]
            or policy.reconciliation_anchor_id != review.id or not review.checked_at <= at < parent.expires_at
            or (at-review.checked_at).total_seconds() > policy.max_data_age_seconds
            or stored is None or stored.created_at != at or item["started_at"] != at.isoformat()
            or policy.expires_at != min(datetime.fromisoformat(grant["expires_at"]),boundary+timedelta(days=1))
            or policy.expires_at <= parent.expires_at
            or refresh["market_pointer"] != state["market_heads"][parent.policy_id]):
        raise ValueError("La renovación no corresponde a su revisión causal")
    if index == len(history)-1 and state.get("continuity_head") != {
        "policy_id":policy.policy_id,"review_id":review.id,"review_digest":review.report_digest,"grant_id":grant["grant_id"]}:
        raise ValueError("El cursor perdió su renovación continua")


def renewal_due(policy,grant:dict,*,now:datetime) -> bool:
    return (grant.get("protocol") == "CONTINUOUS_V1"
        and policy.expires_at < datetime.fromisoformat(grant["expires_at"])
        and policy.expires_at-timedelta(hours=12) <= now < policy.expires_at)


def renew_bot_policy(vault,view,bot_id:str,claim:str,source,grant:dict,*,clock,
                     quote_get=None,rules_fetch=None,assert_active,assert_active_in_transaction):
    """Cambia apertura/política dentro del MISMO claim y concesión; no envía."""
    from app.live_execution.spot_bot_store import (bot_record,bot_authorizations,bot_ownership_baseline,
        owned_spot_bot_ledger,encoded,assert_spot_bot_cycle,SpotBotStoreError)
    from app.live_execution.spot_bot_market import market_seed,save_market_seed,read_market_stream,_observation
    from app.portfolio.mexc_withdrawal_journal import assert_no_pending_withdrawals
    from app.lab.engine import INTERVALS

    assert_active()
    with vault.session() as db:
        row,config,state = bot_record(db,bot_id)
        parent = _stored_policy(db,row.policy_id)
        snapshot = (row.config_digest,row.state_digest,row.updated_at,row.cycle_id)
        if config.get("execution_permission") != grant or grant.get("protocol") != "CONTINUOUS_V1":
            raise SpotBotStoreError("El trabajador no tiene la concesión de continuidad")
        if bot_ownership_baseline(db,row.account_id,bot_id) != config["ownership_baseline"]:
            raise SpotBotStoreError("La continuidad no adopta movimientos manuales")
    ledger,ledger_revision = owned_spot_bot_ledger(vault,bot_id)
    now = _utc(clock(),"now")
    if (not now < parent.expires_at or source.candle_t != int(now.timestamp())//INTERVALS[source.timeframe]*INTERVALS[source.timeframe]-INTERVALS[source.timeframe]
            or source.market_observation != state["market_heads"][parent.policy_id]
            or config["source"].get("kind") != "DCA" and ledger.position.units > 0
               and (now-datetime.fromisoformat(state["last_checked_at"])).total_seconds() > 30):
        raise SpotBotStoreError("La continuidad perdió observación de mercado o de precio")
    review_id = str(uuid5(REFRESH_NAMESPACE,bot_id+":"+parent.policy_id))
    report = record_continuity_review(vault,view,parent.policy_id,review_id=review_id,
        assert_active=assert_active,assert_active_in_transaction=assert_active_in_transaction,
        clock=clock,quote_get=quote_get,rules_fetch=rules_fetch)
    at = _utc(clock(),"now")
    boundary = datetime.fromisoformat(report["causal"]["boundary_at"])
    child = replace(parent,policy_id=str(uuid5(REFRESH_NAMESPACE,parent.policy_id+":"+review_id)),
        reconciliation_anchor_id=review_id,expires_at=min(datetime.fromisoformat(grant["expires_at"]),boundary+timedelta(days=1)))
    validate_continuity_policy(grant,child)
    inputs = fetch_spot_execution_inputs(vault,view,child,clock=clock,quote_get=quote_get,rules_fetch=rules_fetch)
    at = _utc(clock(),"now")
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            assert_active_in_transaction(db)
            row,current,current_state = bot_record(db,bot_id)
            review = db.get(RealMexcParityObservation,review_id)
            stop = db.get(RealLiveStop,row.account_ref)
            current_ledger,current_revision = owned_spot_bot_ledger(vault,bot_id,db=db)
            if ((row.config_digest,row.state_digest,row.updated_at,row.cycle_id) != snapshot
                    or row.cycle_id != claim or stop is not None and stop.stopped
                    or current_ledger != ledger or current_revision != ledger_revision
                    or bot_ownership_baseline(db,row.account_id,bot_id) != current["ownership_baseline"]
                    or account_book_revision(db,row.account_id) != report["book_revision"]
                    or review is None or read_observation(review) != report
                    or not review.checked_at <= at < min(parent.expires_at,child.expires_at)
                    or config["source"].get("kind") != "DCA" and ledger.position.units > 0
                       and (at-datetime.fromisoformat(state["last_checked_at"])).total_seconds() > 30
                    or (at-review.checked_at).total_seconds() > child.max_data_age_seconds
                    or any(stamp > at or (at-stamp).total_seconds() > child.max_data_age_seconds
                           for stamp in (inputs.state.observed_at,inputs.state.quote_at,inputs.rules.observed_at))
                    or ledger.position.units > inputs.state.free_base
                    or db.scalar(select(RealLiveSpotIntent.id).where(RealLiveSpotIntent.account_ref==row.account_ref,
                        RealLiveSpotIntent.status.in_(("RESERVED","SUBMISSION_UNKNOWN"))).limit(1))):
                raise SpotBotStoreError("La parada, la cuenta o el bot cambiaron durante la renovación")
            verify_continuity_review(db,vault,review)
            assert_no_pending_withdrawals(db,row.account_ref,at)
            history = bot_authorizations(current)
            history.append({"policy_id":child.policy_id,"source":current["source"],
                **({'research_binding':current['research_binding']} if 'research_binding' in current else {}),
                "entry_budget_quote":current["entry_budget_quote"],"started_at":at.isoformat(),"execution_permission":grant,
                "continuity":{"protocol":REFRESH_PROTOCOL,"previous_policy_id":parent.policy_id,"review_id":review_id,
                    "review_digest":review.report_digest,"market_pointer":source.market_observation,
                    "refreshed_at":at.isoformat(),"previous_config_digest":row.config_digest,"grant_id":grant["grant_id"]}})
            _register_spot_policy_in_transaction(db,child,now=at)
            current.update(policy_id=child.policy_id,authorizations=history)
            save_market_seed(db,market_seed(source,bot_id,child.policy_id,now=at),current,current_state)
            current_state["continuity_head"] = {"policy_id":child.policy_id,"review_id":review_id,
                "review_digest":review.report_digest,"grant_id":grant["grant_id"]}
            row.policy_id,row.updated_at = child.policy_id,at
            row.config_json,row.config_digest = encoded(current)
            row.state_json,row.state_digest = encoded(current_state)
            bot_record(db,bot_id)
            assert_active_in_transaction(db)
            db.commit()
        except Exception:
            db.rollback(); raise
    with vault.session() as db:
        seed,bars,pointer = read_market_stream(db,child.policy_id)
        _,config,state = bot_record(db,bot_id)
    return child,config,state,_observation(seed,bars,pointer)
