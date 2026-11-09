"""Sólo lecturas ficticias; ningún test usa claves o saldo real."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import hmac
from uuid import uuid4

import pytest

from app.integrations.mexc_readonly import MexcReadOnlyClient
from app.live_execution.mexc_instrument import MexcSpotRules
from app.live_execution.mexc_live_observation import (
    MexcLiveObservationError, fetch_live_evidence, fetch_live_quote, value_spot_account,
)
from app.live_execution.policy import LiveSpotPolicy


NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
KEY = "fixture-view-key"
SECRET = "fixture-view-secret"
ACCOUNT = hmac.new(SECRET.encode(), KEY.encode(), hashlib.sha256).hexdigest()


def policy():
    return LiveSpotPolicy(
        policy_id=str(uuid4()), strategy_version_id="v1", account_ref=ACCOUNT,
        symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT",
        max_order_quote=Decimal("150"), max_daily_turnover_quote=Decimal("500"),
        max_exposure_quote=Decimal("1000"), max_daily_loss_quote=Decimal("50"),
        max_fee_bps=Decimal("20"), max_slippage_bps=Decimal("50"),
        max_data_age_seconds=30, expires_at=NOW + timedelta(hours=1), enabled=True,
    )


def rules():
    return MexcSpotRules(
        symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT", observed_at=NOW,
        buy_allowed=True, sell_allowed=True, base_precision=6, quote_precision=2,
        base_step=Decimal("0.000001"), min_quote=Decimal("1"),
        max_quote=Decimal("10000"), fee_rate_upper=Decimal("0.001"),
        bid_multiplier_up=None, ask_multiplier_down=None,
    )


def quote(*, timestamp=NOW, symbol="BTCUSDT", bid="99999", ask="100000"):
    return {"symbol": symbol, "closeTime": int(timestamp.timestamp() * 1000),
            "bidPrice": bid, "askPrice": ask, "lastPrice": "99999.5"}


class FakeView:
    def __init__(self, *, account=ACCOUNT, observed_at=NOW, balances=None,
                 open_orders=None):
        self.account = account
        self.observed_at = observed_at
        self.rows = balances if balances is not None else [
            {"asset": "BTC", "free": "0.02", "locked": "0.001", "total": "0.021"},
            {"asset": "USDT", "free": "200", "locked": "10", "total": "210"},
        ]
        self.open_rows = open_orders if open_orders is not None else []
        self.calls = []

    def account_reference(self):
        self.calls.append("account_reference")
        return self.account

    def account_uid(self):
        self.calls.append("account_uid")
        return "123456789"

    def balances(self):
        self.calls.append("balances")
        return {"source": "MEXC_SPOT_PRIVATE_READONLY",
                "accounting": "OBSERVED_NOT_IMPORTED",
                "observed_at": self.observed_at.isoformat(), "balances": self.rows}

    def open_orders(self, symbol):
        self.calls.append("open_orders")
        return {"source": "MEXC_SPOT_PRIVATE_READONLY",
                "accounting": "OBSERVED_NOT_IMPORTED", "symbol": symbol,
                "coverage": "ALL_OPEN_ORDERS_FOR_ONE_SYMBOL",
                "observed_at": self.observed_at.isoformat(), "orders": self.open_rows}


def test_reference_is_stable_without_persisting_the_view_secret():
    client = MexcReadOnlyClient(KEY, SECRET, http_get=lambda *_: {})
    assert client.account_reference() == ACCOUNT
    assert SECRET not in repr(client)


def test_fresh_uncached_evidence_preserves_source_and_exact_decimals():
    view = FakeView()
    calls = []

    def quote_get(symbol):
        calls.append(("quote", symbol))
        return quote()

    def rules_fetch(symbol, now):
        calls.append(("rules", symbol, now))
        return rules()

    evidence = fetch_live_evidence(
        view, policy(), clock=lambda: NOW + timedelta(seconds=1),
        quote_get=quote_get, rules_fetch=rules_fetch,
    )
    assert evidence.view_uid == "123456789" and "123456789" not in repr(evidence)
    assert evidence.account_ref == ACCOUNT and evidence.symbol == "BTCUSDT"
    assert evidence.free_base == Decimal("0.02")
    assert evidence.locked_quote == Decimal("10")
    assert evidence.bid == Decimal("99999") and evidence.ask == Decimal("100000")
    assert [(item.asset, item.total) for item in evidence.balances] == [
        ("BTC", Decimal("0.021")), ("USDT", Decimal("210")),
    ]
    assert [item[0] for item in calls] == ["rules", "quote", "rules", "quote"]
    assert view.calls == ["account_reference", "account_uid", "balances", "open_orders", "account_uid"] * 2


@pytest.mark.parametrize("changed", ("balance", "order", "uid"))
def test_live_evidence_rejects_account_changing_between_reads(changed):
    class ChangingView(FakeView):
        def __init__(self):
            super().__init__()
            self.reads = 0

        def balances(self):
            result = super().balances()
            self.reads += 1
            if changed == "balance" and self.reads == 2:
                result["balances"] = [{"asset": "BTC", "free": "0.03", "locked": "0.001", "total": "0.031"},
                                      {"asset": "USDT", "free": "200", "locked": "10", "total": "210"}]
            return result

        def open_orders(self, symbol):
            result = super().open_orders(symbol)
            if changed == "order" and self.reads == 2:
                result["orders"] = [{"symbol": symbol, "status": "NEW",
                                    "order_id": "new-order", "client_order_id": "new-client"}]
            return result

        def account_uid(self):
            result = super().account_uid()
            return "987654321" if changed == "uid" and self.reads == 1 else result

    with pytest.raises(MexcLiveObservationError, match="cambió durante"):
        fetch_live_evidence(
            ChangingView(), policy(), clock=lambda: NOW + timedelta(seconds=1),
            quote_get=lambda _: quote(), rules_fetch=lambda *_: rules(),
        )


@pytest.mark.parametrize("change", (
    {"buy_allowed": False},
    {"base_step": Decimal("0.00001")},
    {"min_quote": Decimal("10")},
    {"fee_rate_upper": Decimal("0.002")},
    {"bid_multiplier_up": Decimal("0.9")},
))
def test_live_evidence_rejects_instrument_rules_changing_between_reads(change):
    reads = 0

    def changing_rules(symbol, now):
        nonlocal reads
        reads += 1
        return replace(rules(), **change) if reads == 2 else rules()

    with pytest.raises(MexcLiveObservationError, match="cambió durante"):
        fetch_live_evidence(FakeView(), policy(),
                            clock=lambda: NOW + timedelta(seconds=1),
                            quote_get=lambda _: quote(), rules_fetch=changing_rules)
    assert reads == 2


def test_live_evidence_accepts_same_rules_with_newer_observation_time():
    reads = 0

    def refreshed_rules(symbol, now):
        nonlocal reads
        reads += 1
        return replace(rules(), observed_at=NOW + timedelta(seconds=reads - 1))

    evidence = fetch_live_evidence(FakeView(), policy(),
                                   clock=lambda: NOW + timedelta(seconds=2),
                                   quote_get=lambda _: quote(), rules_fetch=refreshed_rules)
    assert evidence.rules.observed_at == NOW + timedelta(seconds=1)
    assert reads == 2


def test_live_evidence_expires_if_double_sweep_finishes_too_late():
    calls = 0

    def clock():
        nonlocal calls
        calls += 1
        return NOW + timedelta(seconds=1 if calls <= 10 else 31)

    with pytest.raises(MexcLiveObservationError, match="caducado antes de terminar"):
        fetch_live_evidence(FakeView(), policy(), clock=clock,
                            quote_get=lambda _: quote(), rules_fetch=lambda *_: rules())


def test_wrong_view_key_is_rejected_before_network_observations():
    view = FakeView(account="b" * 64)
    with pytest.raises(MexcLiveObservationError, match="otra referencia"):
        fetch_live_evidence(view, policy(), clock=lambda: NOW + timedelta(seconds=1),
                            quote_get=lambda _: quote(), rules_fetch=lambda *_: rules())
    assert view.calls == ["account_reference"]


@pytest.mark.parametrize("row,reason", [
    (quote(timestamp=NOW - timedelta(seconds=31)), "caducado"),
    (quote(symbol="ETHUSDT"), "otro mercado"),
    (quote(bid="100001"), "cruzado"),
    (quote(ask="NaN"), "Precio MEXC inválido"),
])
def test_quote_rejects_stale_wrong_crossed_or_invalid_data(row, reason):
    with pytest.raises(MexcLiveObservationError, match=reason):
        fetch_live_quote("BTCUSDT", now=NOW, max_age_seconds=30,
                         http_get=lambda _: row)


def test_quote_can_arrive_during_request_but_must_be_known_at_response_time():
    during = quote(timestamp=NOW + timedelta(seconds=1))
    result = fetch_live_quote("BTCUSDT", now=NOW, max_age_seconds=30,
                              http_get=lambda _: during,
                              clock=lambda: NOW + timedelta(seconds=2))
    assert result[-1] == NOW + timedelta(seconds=1)
    with pytest.raises(MexcLiveObservationError, match="futuro"):
        fetch_live_quote("BTCUSDT", now=NOW, max_age_seconds=30,
                         http_get=lambda _: quote(timestamp=NOW + timedelta(seconds=3)),
                         clock=lambda: NOW + timedelta(seconds=2))
    with pytest.raises(MexcLiveObservationError, match="retrocedió"):
        fetch_live_quote("BTCUSDT", now=NOW, max_age_seconds=30,
                         http_get=lambda _: quote(), clock=lambda: NOW - timedelta(seconds=1))


def test_stale_or_duplicate_private_balance_is_rejected():
    for view in (
        FakeView(observed_at=NOW - timedelta(seconds=31)),
        FakeView(balances=[{"asset": "USDT", "free": "200", "locked": "0", "total": "200"},
                           {"asset": "USDT", "free": "200", "locked": "0", "total": "200"}]),
        FakeView(balances=[{"asset": "USDT", "free": "200", "locked": "10", "total": "200"}]),
    ):
        with pytest.raises(MexcLiveObservationError):
            fetch_live_evidence(view, policy(), clock=lambda: NOW,
                                quote_get=lambda _: quote(),
                                rules_fetch=lambda *_: rules())


def test_live_preflight_rejects_tiny_locked_amount_missing_from_reported_total():
    view = FakeView(balances=[
        {"asset": "USDT", "free": "200", "locked": "1e-40", "total": "200"},
    ])
    with pytest.raises(MexcLiveObservationError, match="libre más bloqueado"):
        fetch_live_evidence(view, policy(), clock=lambda: NOW,
                            quote_get=lambda _: quote(), rules_fetch=lambda *_: rules())


def test_entire_mexc_account_requires_fresh_direct_prices_and_exact_decimals():
    view = FakeView(balances=[
        {"asset": "BTC", "free": "0.02", "locked": "0.001", "total": "0.021"},
        {"asset": "USDT", "free": "200", "locked": "10", "total": "210"},
        {"asset": "ETH", "free": "1.5", "locked": "0.5", "total": "2"},
    ])
    active = policy()
    evidence = fetch_live_evidence(view, active, clock=lambda: NOW + timedelta(seconds=1),
                                   quote_get=lambda _: quote(), rules_fetch=lambda *_: rules())
    seen = []

    def eth_quote(symbol):
        seen.append(symbol)
        return {"symbol": symbol, "closeTime": int(NOW.timestamp() * 1000),
                "bidPrice": "2500", "askPrice": "2501", "lastPrice": "2500.5"}

    valuation = value_spot_account(evidence, active, clock=lambda: NOW + timedelta(seconds=1),
                                   quote_get=eth_quote)
    assert seen == ["ETHUSDT"]
    assert valuation.quote_asset == "USDT"
    assert valuation.non_quote_exposure_quote == Decimal("7099.979")
    assert valuation.total_quote == Decimal("7309.979")
    assert [(row.asset, row.value_quote) for row in valuation.balances] == [
        ("BTC", Decimal("2099.979")), ("ETH", Decimal("5000")),
        ("USDT", Decimal("210")),
    ]

    with pytest.raises(MexcLiveObservationError, match="otro mercado"):
        value_spot_account(evidence, active, clock=lambda: NOW + timedelta(seconds=1),
                           quote_get=lambda _: quote(symbol="LTCUSDT"))
    with pytest.raises(MexcLiveObservationError, match="caducado"):
        value_spot_account(evidence, active, clock=lambda: NOW + timedelta(seconds=1),
                           quote_get=lambda symbol: eth_quote(symbol) |
                           {"closeTime": int((NOW - timedelta(seconds=31)).timestamp() * 1000)})


def test_account_valuation_rejects_stale_balance_even_with_fresh_asset_price():
    view = FakeView()
    active = policy()
    evidence = fetch_live_evidence(view, active, clock=lambda: NOW + timedelta(seconds=1),
                                   quote_get=lambda _: quote(), rules_fetch=lambda *_: rules())
    with pytest.raises(MexcLiveObservationError, match="caducados"):
        value_spot_account(evidence, active, clock=lambda: NOW + timedelta(seconds=31))
