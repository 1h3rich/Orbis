"""DCA REAL BUY-only: reglas compartidas, presupuestos explícitos y fills propios."""

from datetime import datetime,timezone
from decimal import Decimal,localcontext

from app.automation.dca import (
    _period_key, adjusted_dca_request, dca_trigger_candidates,
    dca_signal_window_key, live_dca_signal, validate_dca,
)
from app.live_execution.dca_bot_source import DcaBotObservation
from app.live_execution.mexc_instrument import MexcSpotRules
from app.live_execution.policy import LiveSpotPolicy,LiveSpotState,_money,_utc
from app.live_execution.spot_bot_plan import SpotBotPlan,SpotBotPosition,_base_step,_floor


def dca_real_limits(total:str|Decimal,monthly:str|Decimal) -> dict[str,str]:
    total,monthly = Decimal(total),Decimal(monthly)
    _money(total,"dca_budget_total_quote",positive=True)
    _money(monthly,"dca_monthly_limit_quote",positive=True)
    return {"budget_total_quote":str(total),"monthly_limit_quote":str(monthly)}


def plan_dca_bot(policy:LiveSpotPolicy,market:LiveSpotState,rules:MexcSpotRules,source:DcaBotObservation,
                 position:SpotBotPosition,cursor:dict,config:dict,decisions:list[dict],executions:list[dict],
                 *,now:datetime) -> SpotBotPlan:
    now = _utc(now,"now")
    rule = validate_dca(source.definition)
    limits = dca_real_limits(**{"total":config["dca_limits"]["budget_total_quote"],
                               "monthly":config["dca_limits"]["monthly_limit_quote"]})
    reference = Decimal(cursor.get("dca_reference",config["dca_initial_reference"]))
    def result(action,reason,*,details=None,quantity=Decimal(0),price=Decimal(0),fee=Decimal(0)):
        return SpotBotPlan(action,reason,source.candle_t,quantity,price,fee,None,
                           {"reference":str(reference),**(details or {})})
    if (source.binding().get("kind") != "DCA" or source.timeframe != "1d"
            or source.version_id != policy.strategy_version_id or source.symbol.replace("/","") != policy.symbol
            or market.account_ref != policy.account_ref or market.symbol != policy.symbol
            or rules.symbol != policy.symbol or rules.base_asset != policy.base_asset or rules.quote_asset != "USDT"
            or policy.quote_asset != "USDT" or not policy.enabled or not policy.reconciliation_anchor_id):
        return result("PAUSE","DCA_SCOPE_CHANGED")
    if market.stopped or not market.reconciled or not market.account_verified or not market.instrument_verified:
        return result("PAUSE","DCA_ACCOUNT_UNAVAILABLE")
    if "volume_gate" in rule and source.market_observation is None:
        return result("PAUSE","DCA_VOLUME_MARKET_NOT_ARCHIVED")
    checked_at = _utc(datetime.fromisoformat(cursor["last_checked_at"]),"last_checked_at")
    if source.candle_t < cursor["last_candle_t"] or now < checked_at or market.quote_at < checked_at:
        return result("PAUSE","DCA_CLOCK_REVERSED")
    if ((source.candle_t-cursor["last_candle_t"]) % 86400
            or source.candle_t > cursor["last_candle_t"]+86400):
        return result("PAUSE","DCA_CANDLE_CONTINUITY_LOST")
    if (now >= policy.expires_at or market.best_bid <= 0 or market.best_ask <= market.best_bid
            or any(stamp > now or (now-stamp).total_seconds() > policy.max_data_age_seconds
                   for stamp in (market.observed_at,market.quote_at,rules.observed_at))):
        return result("PAUSE","DCA_MARKET_DATA_STALE")
    closed_at = datetime.fromtimestamp(source.candle_t+86400,timezone.utc)
    if closed_at > now or source.observed_at > now or source.observed_at < closed_at:
        return result("PAUSE","DCA_OBSERVATION_INVALID")
    if source.candle_t <= cursor.get("dca_skip_through_t",config["dca_start_candle_t"]):
        return result("WAIT","DCA_WAITING_NEXT_CANDLE")
    if (now-closed_at).total_seconds() > policy.max_data_age_seconds:
        return result("WAIT","DCA_SIGNAL_EXPIRED")
    if position.units > market.free_base:
        return result("PAUSE","OWNED_POSITION_EXCEEDS_FREE_BALANCE")
    if market.pending_base or market.pending_quote:
        return result("WAIT","ORDER_PENDING")
    if not rules.buy_allowed:
        return result("PAUSE","BUY_DISABLED")
    period = _period_key(int(now.timestamp()),rule["frequency"])
    month = now.strftime("%Y-%m")
    consumed,triggered = set(),set()
    last_extra = None
    with localcontext() as context:
        context.prec = 300
        for item in decisions:
            detail = item.get("dca")
            if not isinstance(detail,dict) or item.get("side") != "BUY":
                return result("PAUSE","DCA_DECISION_INVALID")
            consumed.add(detail["key"])
            reference = max(reference,Decimal(detail["reference"]))
            if detail["kind"] == "DIP":
                triggered.add(detail["drop_pct"])
                last_extra = max(last_extra or 0,item["candle_t"]//86400)
        if rule.get("reference_mode","initial") == "high_watermark":
            reference = max(reference,source.candles[-1].c)
        spent = monthly_spent = Decimal(0)
        for fill in executions:
            if fill["side"] != "BUY":
                return result("PAUSE","DCA_SELL_NOT_SUPPORTED")
            when = datetime.fromisoformat(fill["occurred_at"])
            if when > now:
                return result("PAUSE","DCA_FILL_IN_FUTURE")
            cost = Decimal(fill["gross_quote"])+(Decimal(fill["fee"]) if fill["fee_asset"] == "USDT" else 0)
            spent += cost
            if when.strftime("%Y-%m") == month: monthly_spent += cost
        cash = max(Decimal(0),Decimal(limits["budget_total_quote"])-spent)
        monthly_room = max(Decimal(0),min(Decimal(limits["monthly_limit_quote"]),Decimal(rule["monthly_limit"]))-monthly_spent)
        if spent > Decimal(limits["budget_total_quote"]) or monthly_spent > min(Decimal(limits["monthly_limit_quote"]),Decimal(rule["monthly_limit"])):
            return result("PAUSE","DCA_EXECUTED_BUDGET_EXCEEDED")
        candidates = dca_trigger_candidates(rule,signal_close=source.candles[-1].c,reference=reference,
            period=period,last_period=period if "BASE:"+period in consumed else None,
            triggered=triggered,extra_count=len(triggered),index=source.candle_t//86400,last_extra_index=last_extra,
            allow_average=True, allow_volume=True)
        factor, evidence = Decimal(1), {}
        if candidates and dca_signal_window_key(rule):
            factor, rejection, evidence = live_dca_signal(rule, source.candles)
            if rejection is not None:
                return result("WAIT", rejection, details=evidence)
        tick = Decimal(1).scaleb(-rules.quote_precision)
        price = _floor(market.best_ask*(1+policy.max_slippage_bps/10000),tick)
        if price <= 0: return result("PAUSE","BUY_PRICE_UNUSABLE")
        fee_rate = max(rules.fee_rate_upper,policy.max_fee_bps/10000)
        marked = position.units*market.best_bid
        equity = cash+marked
        maximum = Decimal(rule["max_exposure"])
        room = max(Decimal(0),maximum*equity-marked)
        ratio = market.best_bid/(price*(1+fee_rate))
        exposure_room = room/(ratio+maximum*(1-ratio))
        reason = "DCA_WAITING_TRIGGER"
        for kind,requested,signal in candidates:
            key = "BASE:"+period if kind == "BASE" else "DIP:"+signal["drop_pct"]
            if key in consumed: continue
            if evidence:
                signal.update(base_requested=str(requested), **evidence)
                requested = adjusted_dca_request(requested, factor)
            budget = min(requested,Decimal(config["entry_budget_quote"]),policy.max_order_quote,
                         cash,monthly_room,market.free_quote,exposure_room,rules.max_quote)
            quantity = _floor(budget/(price*(1+fee_rate)),_base_step(rules))
            if quantity <= 0 or quantity*price < rules.min_quote:
                reason = "DCA_BUDGET_EXPOSURE_OR_MINIMUM"
                continue
            details = {"kind":kind,"key":key,"period":period,"requested":str(requested),
                       "budget_limit":str(budget),**signal}
            return result("BUY","DCA_"+kind,details=details,quantity=quantity,price=price,fee=quantity*price*fee_rate)
        return result("WAIT",reason)
