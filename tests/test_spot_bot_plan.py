"""Decisiones de bot LONG con capital REAL independiente y controles causales."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, localcontext
from uuid import uuid4

import pytest

from app.live_execution.policy import sell_base_reservation, LiveSpotIntent
from app.live_execution.spot_bot_plan import (
    SpotBotCursor, SpotBotPlanError, SpotBotPosition, plan_spot_bot,
)
from tests.test_live_spot_journal import NOW
from tests.test_mexc_live_submit import case
from tests.test_spot_bot_source import bot_source, observation


def plan_case(tmp_path):
    paper, session, paper_state, _, _ = bot_source(tmp_path)
    source = observation(paper, session, paper_state)
    now = source.observed_at
    policy, _, state, rules = case()
    policy = replace(policy, strategy_version_id=source.version_id, reconciliation_anchor_id=str(uuid4()),
                     max_order_quote=Decimal("150"), expires_at=now + timedelta(days=1), max_slippage_bps=Decimal(0))
    state = replace(state, observed_at=now, quote_at=now, best_bid=Decimal("99"), best_ask=Decimal("100"),
                    free_base=Decimal("5"), free_quote=Decimal("200"))
    rules = replace(rules, observed_at=now, fee_rate_upper=Decimal("0.001"))
    cursor = SpotBotCursor(source.candle_t - 3600, now)
    position = SpotBotPosition(Decimal(0), Decimal(0), None, None)
    return policy, state, rules, source, position, cursor, now


def plan(args, *, budget="20"):
    policy, state, rules, source, position, cursor, now = args
    return plan_spot_bot(policy, state, rules, source, position, cursor,
                          entry_budget_quote=Decimal(budget), now=now)


def owned(args, *, cost="100", units="1"):
    policy, state, rules, source, _, cursor, now = args
    position = SpotBotPosition(Decimal(units), Decimal(cost), source.candle_t - 3600, str(uuid4()))
    return policy, state, rules, source, position, cursor, now


def test_entry_uses_only_real_budget_ask_and_published_fee_not_paper_allocation(tmp_path):
    args = plan_case(tmp_path)
    result = plan(args)
    assert result.action == "BUY" and result.reason == "ENTRY_RULE"
    assert result.limit_price_quote == Decimal(100)
    assert result.quantity_base == Decimal("0.199600")
    assert result.estimated_fee_quote == Decimal("0.03992")
    assert result.quantity_base * result.limit_price_quote + result.estimated_fee_quote <= 20
    assert args[3].definition["initial_capital"] == "1000"
    assert result.quantity_base != Decimal("100") / Decimal("100.1")


@pytest.mark.parametrize("price,reason", [("96", "STOP_LOSS"), ("109", "TAKE_PROFIT")])
def test_exit_uses_real_fill_cost_and_does_not_sell_preexisting_exchange_units(tmp_path, price, reason):
    args = list(owned(plan_case(tmp_path)))
    args[1] = replace(args[1], best_bid=Decimal(price), best_ask=Decimal(price) + 1)
    result = plan(args)
    assert result.action == "SELL" and result.reason == reason
    assert result.quantity_base < 1  # Balance MEXC tiene 5; el bot sólo posee 1.
    intent = LiveSpotIntent(str(uuid4()), args[0].policy_id, args[0].strategy_version_id,
                             args[0].account_ref, args[0].symbol, "SELL", result.quantity_base,
                             result.limit_price_quote, result.estimated_fee_quote, args[-1])
    assert sell_base_reservation(intent) <= 1


def test_same_candle_does_not_repeat_entry_and_does_not_prevent_intrabar_stop(tmp_path):
    args = list(plan_case(tmp_path))
    args[5] = replace(args[5], last_candle_t=args[3].candle_t)
    assert plan(args).reason == "WAITING_ENTRY"
    args = list(owned(args))
    args[1] = replace(args[1], best_bid=Decimal("96"), best_ask=Decimal("97"))
    assert plan(args).reason == "STOP_LOSS"


def test_exit_rule_and_holding_limit_use_new_closed_candle_only(tmp_path):
    args = list(owned(plan_case(tmp_path)))
    args[3] = replace(args[3], exit_match=True)
    assert plan(args).reason == "EXIT_RULE"
    args[5] = replace(args[5], last_candle_t=args[3].candle_t)
    assert plan(args).reason == "POSITION_OPEN"
    args[5] = replace(args[5], last_candle_t=args[3].candle_t - 3600)
    args[3] = replace(args[3], exit_match=False, definition={**args[3].definition,"max_holding_bars":1})
    assert plan(args).reason == "TIME_LIMIT"


def test_trailing_stop_uses_previous_observed_high_not_fictitious_candle_high(tmp_path):
    args = list(owned(plan_case(tmp_path)))
    args[3] = replace(args[3], definition={**args[3].definition,"trailing_stop":"0.02", "take_profit":"0.9"})
    args[5] = replace(args[5], trailing_high=Decimal("105"))
    args[1] = replace(args[1], best_bid=Decimal("103"), best_ask=Decimal("104"))
    assert plan(args).reason == "POSITION_OPEN"
    args[1] = replace(args[1], best_bid=Decimal("102"), best_ask=Decimal("103"))
    assert plan(args).reason == "TRAILING_STOP"


@pytest.mark.parametrize("change,reason", [
    ("stop", "REAL_ACCOUNT_UNAVAILABLE"), ("expiry", "REAL_ACCOUNT_UNAVAILABLE"),
    ("gap", "REAL_RISK_OBSERVATION_GAP"), ("candles", "CANDLE_CONTINUITY_LOST"),
    ("balance", "OWNED_POSITION_EXCEEDS_FREE_BALANCE"), ("pending", "ORDER_PENDING"),
])
def test_plan_pauses_or_waits_instead_of_inventing_coverage(tmp_path, change, reason):
    args = list(owned(plan_case(tmp_path)))
    if change == "stop": args[1] = replace(args[1], stopped=True)
    if change == "expiry": args[0] = replace(args[0], expires_at=args[-1])
    if change == "gap": args[5] = replace(args[5], last_checked_at=args[-1] - timedelta(seconds=31))
    if change == "candles": args[5] = replace(args[5], last_candle_t=args[3].candle_t - 7200)
    if change == "balance": args[1] = replace(args[1], free_base=Decimal("0.9"))
    if change == "pending": args[1] = replace(args[1], pending_quote=Decimal("1"))
    result = plan(args)
    assert result.action in ("PAUSE", "WAIT") and result.reason == reason


def test_quantity_is_multiple_of_both_exchange_step_and_decimal_precision(tmp_path):
    args = list(plan_case(tmp_path))
    args[2] = replace(args[2], base_step=Decimal("0.000015"), base_precision=5)
    result = plan(args)
    assert result.action == "BUY" and result.quantity_base % Decimal("0.000015") == 0
    assert result.quantity_base % Decimal("0.00001") == 0


def test_zero_sell_price_and_small_capital_never_produce_invalid_order(tmp_path):
    args = list(owned(plan_case(tmp_path)))
    args[1] = replace(args[1], best_bid=Decimal("96"), best_ask=Decimal("97"))
    args[0] = replace(args[0], max_slippage_bps=Decimal("10000"))
    assert plan(args).reason == "SELL_PRICE_UNUSABLE"
    args = list(plan_case(tmp_path))
    assert plan(args, budget="0.1").reason == "BELOW_MINIMUM_ORDER"


def test_dust_is_not_presented_as_fully_liquidated_or_sold_from_other_holdings(tmp_path):
    args = list(owned(plan_case(tmp_path), cost="0.0001", units="0.000001"))
    args[5] = replace(args[5], last_candle_t=args[3].candle_t)
    assert plan(args).reason == "WAITING_ENTRY"


def test_unrelated_version_and_floats_are_rejected(tmp_path):
    args = list(plan_case(tmp_path))
    args[3] = replace(args[3], version_id="other")
    with pytest.raises(SpotBotPlanError): plan(args)
    with pytest.raises(ValueError):
        SpotBotPosition(1.0, Decimal(100), 0, str(uuid4()))
