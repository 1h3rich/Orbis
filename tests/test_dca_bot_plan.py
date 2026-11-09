"""Calendario, caídas y exposición calculados con presupuesto/fills REAL."""

from dataclasses import replace
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.live_execution.dca_bot_plan import plan_dca_bot
from app.live_execution.spot_bot_plan import SpotBotPosition
from tests.test_mexc_live_submit import case
from tests.test_paper_dca import rule


def fixture():
    policy,_,market,rules = case()
    day = int(market.quote_at.timestamp())//86400*86400
    now = datetime.fromtimestamp(day+1,timezone.utc)
    policy = replace(policy,reconciliation_anchor_id=str(uuid4()),expires_at=now+timedelta(days=1))
    market = replace(market,observed_at=now,quote_at=now,best_bid=Decimal("99"),best_ask=Decimal("100"),
                     free_quote=Decimal("1000"),free_base=Decimal("10"))
    rules = replace(rules,observed_at=now)
    source = SimpleNamespace(binding=lambda:{"kind":"DCA"},version_id=policy.strategy_version_id,symbol="BTC/USDT",
        timeframe="1d",definition=rule(),candle_t=day-86400,observed_at=now,
        candles=(SimpleNamespace(c=Decimal("80")),))
    config = {"entry_budget_quote":"20","dca_limits":{"budget_total_quote":"100","monthly_limit_quote":"40"},
              "dca_initial_reference":"100","dca_start_candle_t":day-2*86400}
    cursor = {"last_candle_t":day-2*86400,"last_checked_at":now.isoformat()}
    position = SpotBotPosition(Decimal(0),Decimal(0),None,None)
    return [policy,market,rules,source,position,cursor,config,[],[],now]


def plan(args): return plan_dca_bot(*args[:-1],now=args[-1])


def consumed(args,kind="BASE",drop="0.1"):
    period = args[-1].strftime("%Y-%m-%d")
    return {"side":"BUY","candle_t":args[3].candle_t,"dca":{"kind":kind,"key":kind+":"+(period if kind == "BASE" else drop),
        "drop_pct":drop,"reference":"100"}}


def test_base_then_dips_are_distinct_durable_activations_not_repeated_candles():
    args = fixture()
    first = plan(args)
    assert first.reason == "DCA_BASE" and first.action == "BUY"
    assert first.quantity_base*first.limit_price_quote+first.estimated_fee_quote <= 20
    args[7] = [consumed(args)]
    assert plan(args).reason == "DCA_DIP" and plan(args).dca["drop_pct"] == "0.1"
    args[7].append(consumed(args,"DIP"))
    assert plan(args).dca["drop_pct"] == "0.2"
    args[7].append(consumed(args,"DIP","0.2"))
    assert plan(args).action == "WAIT"


def test_real_allocated_cash_and_monthly_fills_override_fictitious_budget():
    args = fixture()
    args[8] = [{"side":"BUY","occurred_at":args[-1].isoformat(),"gross_quote":"39.8","fee":"0.2","fee_asset":"USDT"}]
    assert args[3].definition["budget_total"] == "1000"
    assert plan(args).action == "WAIT"
    args[8][0]["gross_quote"] = "40"
    assert plan(args).reason == "DCA_EXECUTED_BUDGET_EXCEEDED"


def test_month_boundary_counts_actual_execution_month_not_decision_or_rearm():
    args = fixture()
    previous = args[-1].replace(day=1,hour=0,minute=0,second=0,microsecond=0)-timedelta(seconds=1)
    args[8] = [{"side":"BUY","occurred_at":previous.isoformat(),"gross_quote":"40","fee":"0","fee_asset":"USDT"}]
    assert plan(args).action == "BUY"
    args[6]["dca_limits"]["budget_total_quote"] = "40"
    assert plan(args).action == "WAIT"  # Cambiar mes no recarga la asignación.


def test_base_fee_not_counted_twice_and_future_fill_never_used():
    args = fixture()
    args[8] = [{"side":"BUY","occurred_at":args[-1].isoformat(),"gross_quote":"20","fee":"0.01","fee_asset":"BTC"}]
    assert plan(args).action == "BUY"
    args[8][0]["occurred_at"] = (args[-1]+timedelta(seconds=1)).isoformat()
    assert plan(args).reason == "DCA_FILL_IN_FUTURE"


def test_exposure_counts_only_own_units_and_allocated_cash_not_all_exchange_money():
    args = fixture()
    args[3].definition = rule(max_exposure="0.1")
    result = plan(args)
    assert result.action == "BUY"
    assert result.quantity_base*result.limit_price_quote+result.estimated_fee_quote < 11
    args[4] = SpotBotPosition(Decimal("1"),Decimal("90"),args[3].candle_t,str(uuid4()))
    assert plan(args).action == "WAIT"


def test_cooldown_and_extra_limit_and_high_watermark_use_actual_attempt_history():
    args = fixture(); args[3].definition = rule(cooldown_bars=2,reference_mode="high_watermark")
    args[7] = [consumed(args),consumed(args,"DIP")]
    assert plan(args).action == "WAIT"
    args[3].definition = rule(max_extra_activations=1)
    assert plan(args).action == "WAIT"


@pytest.mark.parametrize("change,reason",[("stale","DCA_MARKET_DATA_STALE"),("skip","DCA_WAITING_NEXT_CANDLE"),
    ("expired","DCA_SIGNAL_EXPIRED"),("balance","OWNED_POSITION_EXCEEDS_FREE_BALANCE"),
    ("scope","DCA_SCOPE_CHANGED"),("pending","ORDER_PENDING"),("disabled","BUY_DISABLED"),
    ("identity","DCA_ACCOUNT_UNAVAILABLE"),("stop","DCA_ACCOUNT_UNAVAILABLE"),("regressed","DCA_CLOCK_REVERSED"),
    ("gap","DCA_CANDLE_CONTINUITY_LOST")])
def test_controls_never_produce_an_order(change,reason):
    args = fixture()
    if change == "stale":
        args[1] = replace(args[1],quote_at=args[-1]-timedelta(seconds=301))
        args[5]["last_checked_at"] = (args[-1]-timedelta(seconds=302)).isoformat()
    if change == "skip": args[5]["dca_skip_through_t"] = args[3].candle_t
    if change == "expired": args[-1] += timedelta(seconds=61); args[1] = replace(args[1],observed_at=args[-1],quote_at=args[-1]); args[2] = replace(args[2],observed_at=args[-1])
    if change == "balance": args[4] = SpotBotPosition(Decimal("20"),Decimal("100"),args[3].candle_t,str(uuid4()))
    if change == "scope": args[3].symbol = "ETH/USDT"
    if change == "pending": args[1] = replace(args[1],pending_quote=Decimal("1"))
    if change == "disabled": args[2] = replace(args[2],buy_allowed=False)
    if change == "identity": args[1] = replace(args[1],account_verified=False)
    if change == "stop": args[1] = replace(args[1],stopped=True)
    if change == "regressed": args[1] = replace(args[1],quote_at=args[-1]-timedelta(seconds=1))
    if change == "gap": args[5]["last_candle_t"] -= 86400
    assert plan(args).reason == reason
    assert plan(args).action != "BUY"
