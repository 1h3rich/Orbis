from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from app.live_execution.mexc_instrument import (
    MexcInstrumentError, fetch_spot_rules, parse_exchange_info, preflight_limit_order,
)
from app.live_execution.policy import LiveSpotIntent, LiveSpotPolicy


NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
ACCOUNT = "b" * 64
POLICY_ID = "d0b23409-b940-42ba-957f-cc90f4fe8e23"


def payload():
    return {"serverTime": 1791892800000, "symbols": [{
        "symbol": "BTCUSDT", "status": "1", "baseAsset": "BTC", "quoteAsset": "USDT",
        "baseAssetPrecision": 6, "quotePrecision": 2,
        "orderTypes": ["LIMIT", "MARKET"], "isSpotTradingAllowed": True,
        "permissions": ["SPOT"], "tradeSideType": 1,
        "baseSizePrecision": "0.0001", "quoteAmountPrecision": "5",
        "maxQuoteAmount": "500000", "makerCommission": "0.0001",
        "takerCommission": "0.001",
        "filters": [{"filterType": "PERCENT_PRICE_BY_SIDE",
                     "bidMultiplierUp": "0.2", "askMultiplierDown": "0.2"}],
    }]}


def case():
    policy = LiveSpotPolicy(
        policy_id=POLICY_ID, strategy_version_id="v1", account_ref=ACCOUNT,
        symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT",
        max_order_quote=Decimal("200"), max_daily_turnover_quote=Decimal("1000"),
        max_exposure_quote=Decimal("1000"), max_daily_loss_quote=Decimal("50"),
        max_fee_bps=Decimal("20"), max_slippage_bps=Decimal("50"),
        max_data_age_seconds=60, expires_at=NOW + timedelta(hours=1), enabled=True,
    )
    intent = LiveSpotIntent(
        intent_id=str(uuid4()), policy_id=policy.policy_id,
        strategy_version_id="v1", account_ref=ACCOUNT, symbol="BTCUSDT",
        side="BUY", quantity_base=Decimal("0.0010"),
        limit_price_quote=Decimal("100000.00"),
        estimated_fee_quote=Decimal("0.10"), signal_at=NOW,
    )
    rules = parse_exchange_info(payload(), "BTCUSDT", observed_at=NOW)
    return policy, intent, rules


def check(policy=None, intent=None, rules=None, *, last=Decimal("100000"), at=NOW):
    base_policy, base_intent, base_rules = case()
    return preflight_limit_order(policy or base_policy, intent or base_intent,
                                 rules or base_rules, last_price=last,
                                 last_price_at=at, now=NOW)


def test_parses_current_exchange_info_and_exact_order_passes():
    rules = fetch_spot_rules("BTCUSDT", now=NOW, http_get=lambda symbol: payload())
    assert rules.base_step == Decimal("0.0001")
    assert rules.fee_rate_upper == Decimal("0.001")
    assert check(rules=rules).reason == "LOCAL_PREFLIGHT_ONLY"


@pytest.mark.parametrize(("field", "value"), [
    ("status", "2"), ("isSpotTradingAllowed", False),
    ("symbol", "ETHUSDT"), ("quoteAsset", "EUR"),
    ("orderTypes", ["MARKET"]), ("permissions", []),
    ("tradeSideType", "unknown"), ("baseSizePrecision", "0"),
    ("quoteAmountPrecision", "NaN"), ("maxQuoteAmount", "1"),
    ("takerCommission", "Infinity"), ("quotePrecision", True),
    ("filters", [{"filterType": "UNKNOWN"}]),
])
def test_bad_or_incomplete_rules_fail_closed(field, value):
    data = payload()
    data["symbols"][0][field] = value
    with pytest.raises(MexcInstrumentError):
        parse_exchange_info(data, "BTCUSDT", observed_at=NOW)


def test_duplicate_or_missing_instrument_is_rejected():
    data = payload()
    data["symbols"].append(deepcopy(data["symbols"][0]))
    with pytest.raises(MexcInstrumentError):
        parse_exchange_info(data, "BTCUSDT", observed_at=NOW)
    with pytest.raises(MexcInstrumentError):
        parse_exchange_info({"symbols": []}, "BTCUSDT", observed_at=NOW)


@pytest.mark.parametrize(("intent_change", "reason"), [
    ({"quantity_base": Decimal("0.00101")}, "BASE_STEP"),
    ({"quantity_base": Decimal("0.0000001")}, "BASE_PRECISION"),
    ({"limit_price_quote": Decimal("100000.001")}, "PRICE_PRECISION"),
    ({"quantity_base": Decimal("0.0001"), "limit_price_quote": Decimal("100")}, "MIN_NOTIONAL"),
    ({"quantity_base": Decimal("6")}, "MAX_NOTIONAL"),
    ({"estimated_fee_quote": Decimal("0.09")}, "FEE_ESTIMATE_TOO_LOW"),
    ({"limit_price_quote": Decimal("130000"), "estimated_fee_quote": Decimal("0.13")}, "EXCHANGE_PRICE_BOUND"),
])
def test_order_rules(intent_change, reason):
    _, intent, _ = case()
    assert check(intent=replace(intent, **intent_change)).reason == reason


def test_side_and_freshness_gates():
    policy, intent, rules = case()
    assert check(rules=replace(rules, buy_allowed=False)).reason == "SIDE_DISABLED"
    assert check(intent=replace(intent, side="SELL"),
                 rules=replace(rules, sell_allowed=False)).reason == "SIDE_DISABLED"
    assert check(rules=replace(rules, observed_at=NOW - timedelta(seconds=61))).reason == "STALE_INSTRUMENT_OR_TRADE"
    assert check(at=NOW - timedelta(seconds=61)).reason == "STALE_INSTRUMENT_OR_TRADE"
    assert check(rules=replace(rules, symbol="ETHUSDT")).reason == "INSTRUMENT_SCOPE_MISMATCH"
    assert check(intent=replace(intent, account_ref="c" * 64)).reason == "INSTRUMENT_SCOPE_MISMATCH"
    assert check(last=Decimal("NaN")).reason == "INVALID_LAST_PRICE"
    assert check(last=Decimal("1E+9999")).reason == "INVALID_LAST_PRICE"


def test_buy_only_and_sell_only_direction_from_exchange():
    buy = payload()
    buy["symbols"][0]["tradeSideType"] = "2"
    buy_rules = parse_exchange_info(buy, "BTCUSDT", observed_at=NOW)
    assert buy_rules.buy_allowed and not buy_rules.sell_allowed
    sell = payload()
    sell["symbols"][0]["tradeSideType"] = "3"
    sell_rules = parse_exchange_info(sell, "BTCUSDT", observed_at=NOW)
    assert sell_rules.sell_allowed and not sell_rules.buy_allowed


def test_public_fetch_does_not_need_credentials_and_sanitizes_failure():
    def fail(_):
        raise RuntimeError("sensitive upstream contents")
    with pytest.raises(MexcInstrumentError, match="unavailable") as exc:
        fetch_spot_rules("BTCUSDT", now=NOW, http_get=fail)
    assert "sensitive" not in str(exc.value)
