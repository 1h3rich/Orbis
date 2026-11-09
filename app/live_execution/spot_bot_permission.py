"""Concesión operativa efímera: independiente del token HTTP, no de la política."""

from datetime import datetime, timedelta
from decimal import Decimal
import json
import re
from uuid import UUID

from app.lab.store import content_hash
from app.live_execution.journal import _canonical
from app.live_execution.policy import LiveSpotPolicy, _money, _utc


OPERATIONAL = "OPERATIONAL_V1"
CONTINUOUS = "CONTINUOUS_V1"


def continuity_scope(config: dict) -> dict:
    return {"bot_id":config["id"],"account_id":config["account_id"],"account_ref":config["account_ref"],
            **{name:config.get(name) for name in ("source","entry_budget_quote","dca_limits",
                "dca_initial_reference","dca_start_candle_t","ownership_baseline")}}


def _risk_terms(policy: LiveSpotPolicy) -> dict:
    raw = json.loads(_canonical(policy))
    return {key:value for key,value in raw.items() if key not in (
        "policy_id","expires_at","enabled","reconciliation_anchor_id","allow_risk_reducing_exits","resumed_after_stop_at")}


def continuity_terms(policy: LiveSpotPolicy) -> dict:
    return {"risk":_risk_terms(policy),"allow_risk_reducing_exits":policy.allow_risk_reducing_exits,
            "resumed_after_stop_at":policy.resumed_after_stop_at.isoformat() if policy.resumed_after_stop_at else None}


def make_continuity_grant(policy: LiveSpotPolicy, draft: LiveSpotPolicy, scope: dict,
                          grant_id: str, *, now: datetime, until: datetime) -> dict:
    at,end = _utc(now,"now"),_utc(until,"until")
    if (not policy.enabled or draft.enabled or not policy.reconciliation_anchor_id
            or not at < policy.expires_at <= end <= min(draft.expires_at,at+timedelta(days=30))
            or _risk_terms(policy) != _risk_terms(draft) or not policy.allow_risk_reducing_exits):
        raise ValueError("La concesión continua supera la duración o los límites revisados")
    try:
        if set(scope) != {"bot_id","account_id","account_ref","source","entry_budget_quote","dca_limits",
                          "dca_initial_reference","dca_start_candle_t","ownership_baseline"}:
            raise ValueError("scope")
        UUID(scope["bot_id"]); UUID(scope["account_id"])
        amount = Decimal(scope["entry_budget_quote"])
        _money(amount,"entry_budget_quote",positive=True)
        if (scope["account_ref"] != policy.account_ref or amount > policy.max_order_quote
                or scope["source"]["version_id"] != policy.strategy_version_id
                or scope["source"]["symbol"].replace("/","") != policy.symbol
                or not re.fullmatch(r"[0-9a-f]{64}",scope["ownership_baseline"])):
            raise ValueError("scope")
        if scope["source"].get("kind") == "DCA":
            from app.live_execution.dca_bot_plan import dca_real_limits
            if dca_real_limits(scope["dca_limits"]["budget_total_quote"],scope["dca_limits"]["monthly_limit_quote"]) != scope["dca_limits"]:
                raise ValueError("DCA limits")
            _money(Decimal(scope["dca_initial_reference"]),"DCA reference",positive=True)
            if type(scope["dca_start_candle_t"]) is not int: raise ValueError("DCA start")
        elif any(scope[key] is not None for key in ("dca_limits","dca_initial_reference","dca_start_candle_t")):
            raise ValueError("DCA scope")
        frozen = json.loads(json.dumps(scope,allow_nan=False))
        return {"protocol":CONTINUOUS,"grant_id":str(UUID(grant_id)),"root_policy_id":policy.policy_id,
            "root_policy_digest":content_hash(_canonical(policy)),"draft_policy_id":draft.policy_id,
            "draft_digest":content_hash(_canonical(draft)),"limits_digest":content_hash(continuity_terms(policy)),
            "scope":frozen,"approved_at":at.isoformat(),"expires_at":end.isoformat()}
    except (ValueError,TypeError,KeyError,AttributeError,ArithmeticError):
        raise ValueError("El alcance de la concesión continua no es válido") from None


def validate_continuity_grant(permission: dict, root: LiveSpotPolicy, draft: LiveSpotPolicy, scope: dict) -> None:
    try:
        expected = make_continuity_grant(root,draft,scope,permission["grant_id"],
            now=datetime.fromisoformat(permission["approved_at"]),until=datetime.fromisoformat(permission["expires_at"]))
        if expected != permission: raise ValueError("scope")
    except (ValueError,TypeError,KeyError,AttributeError):
        raise ValueError("La concesión continua no es verificable") from None


def validate_continuity_policy(permission: dict, policy: LiveSpotPolicy) -> None:
    if (not policy.enabled or not policy.reconciliation_anchor_id
            or content_hash(continuity_terms(policy)) != permission["limits_digest"]
            or not datetime.fromisoformat(permission["approved_at"]) < policy.expires_at
            <= datetime.fromisoformat(permission["expires_at"])):
        raise ValueError("La política no respeta la concesión continua")


def make_permission(policy,source:dict,grant_id:str,*,now:datetime) -> dict:
    at = _utc(now,"now")
    if not policy.enabled or not at < policy.expires_at or not policy.reconciliation_anchor_id:
        raise ValueError("La política operativa no está vigente")
    return {"protocol":OPERATIONAL,"grant_id":str(UUID(grant_id)),"policy_id":policy.policy_id,
            "approved_at":at.isoformat(),"expires_at":policy.expires_at.isoformat(),
            "policy_digest":content_hash(_canonical(policy)),"source_digest":content_hash(source)}


def validate_permission(permission:dict,policy,source:dict) -> None:
    try:
        expected = make_permission(policy,source,permission["grant_id"],now=datetime.fromisoformat(permission["approved_at"]))
        if expected != permission: raise ValueError("scope")
    except (KeyError,ValueError,TypeError,AttributeError):
        raise ValueError("La concesión operativa del bot no es verificable") from None
