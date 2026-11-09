"""Una consulta ambigua no se transforma en una nueva orden."""

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.integrations.mexc_readonly import MexcReadOnlyError
from app.live_execution.mexc_order_observer import (
    MexcOrderObservationError, observe_spot_intent,
)
from app.live_execution.policy import LiveSpotIntent


INTENT_ID = str(uuid4())
INTENT = LiveSpotIntent(
    intent_id=INTENT_ID, policy_id=str(uuid4()), strategy_version_id="v1",
    account_ref="a" * 64, symbol="BTCUSDT", side="BUY",
    quantity_base=Decimal("0.01"), limit_price_quote=Decimal("65000"),
    estimated_fee_quote=Decimal("0.65"),
    signal_at=datetime(2026, 10, 13, tzinfo=timezone.utc),
)


class FakeReader:
    def __init__(self, *, row=None, fills=None, fails=False):
        self.calls = []
        self.row = row
        self.fills = fills
        self.fails = fails

    def order_by_client_id(self, symbol, client_order_id):
        self.calls.append((symbol, client_order_id))
        if self.fails:
            raise MexcReadOnlyError("not found or timeout")
        return self.row or {
            "source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
            "type": "LIMIT", "symbol": symbol, "client_order_id": client_order_id,
            "side": "BUY", "price": "65000.00", "quantity": "0.01",
            "order_id": "external-1", "status": "PARTIALLY_FILLED",
            "executed_quantity": "0.002",
            "cumulative_quote": "130",
        }

    def trades_for_order(self, symbol, order_id):
        self.calls.append((symbol, order_id))
        if self.fails:
            raise MexcReadOnlyError("fills unavailable")
        return {"source": "MEXC_SPOT_PRIVATE_READONLY",
                "accounting": "OBSERVED_NOT_IMPORTED",
                "coverage": "ORDER_ID_BELOW_PAGE_LIMIT", "symbol": symbol,
                "order_id": order_id,
                "trades": self.fills if self.fills is not None else [{
            "id": "fill-1", "order_id": order_id, "side": "BUY", "quantity": "0.002",
            "quote_quantity": "130", "commission": "0.01", "commission_asset": "USDT",
        }]}


def test_observes_exact_intent_without_creating_a_new_one():
    reader = FakeReader()
    observed = observe_spot_intent(reader, INTENT)
    assert reader.calls == [("BTCUSDT", UUID(INTENT_ID).hex), ("BTCUSDT", "external-1")]
    assert observed.intent_id == INTENT_ID
    assert observed.external_order_id == "external-1"
    assert observed.executed_base == Decimal("0.002")
    assert observed.cumulative_quote == Decimal("130")
    assert observed.fees_by_asset == (("USDT", Decimal("0.01")),)
    assert observed.final is False


@pytest.mark.parametrize("field,value", [
    ("source", "OTHER_EXCHANGE"), ("accounting", "IMPORTED"),
    ("type", "MARKET"), ("symbol", "ETHUSDT"),
    ("client_order_id", "another-order"),
])
def test_order_response_identity_must_match_exact_request(field, value):
    reader = FakeReader()
    row = reader.order_by_client_id(INTENT.symbol, UUID(INTENT_ID).hex)
    reader.row = row | {field: value}
    with pytest.raises(MexcOrderObservationError, match="no verificable"):
        observe_spot_intent(reader, INTENT)
    assert len(reader.calls) == 2  # fixture construction + order lookup, no fills


@pytest.mark.parametrize("field,value", [
    ("source", "OTHER_EXCHANGE"), ("accounting", "IMPORTED"),
    ("coverage", "RECENT_MONTH_SINGLE_PAGE_INCOMPLETE"),
    ("symbol", "ETHUSDT"), ("order_id", "another-order"),
])
def test_fill_page_requires_exact_order_scoped_coverage(field, value):
    class WrongPage(FakeReader):
        def trades_for_order(self, symbol, order_id):
            return super().trades_for_order(symbol, order_id) | {field: value}

    with pytest.raises(MexcOrderObservationError, match="no verificables"):
        observe_spot_intent(WrongPage(), INTENT)


def test_fill_itself_cannot_belong_to_another_order():
    wrong_fill = {"id": "fill-1", "order_id": "other-order", "side": "BUY",
                  "quantity": "0.002", "quote_quantity": "130", "commission": "0.01",
                  "commission_asset": "USDT"}
    with pytest.raises(MexcOrderObservationError, match="no coincide"):
        observe_spot_intent(FakeReader(fills=[wrong_fill]), INTENT)


@pytest.mark.parametrize("field,value", [
    ("side", "SELL"), ("price", "65001"), ("quantity", "0.02"),
])
def test_rejects_order_that_does_not_match_intent(field, value):
    row = FakeReader().order_by_client_id(INTENT.symbol, UUID(INTENT_ID).hex)
    reader = FakeReader(row=row | {field: value})
    with pytest.raises(MexcOrderObservationError, match="no coincide"):
        observe_spot_intent(reader, INTENT)


def test_missing_order_or_network_failure_remains_unknown():
    reader = FakeReader(fails=True)
    with pytest.raises(MexcOrderObservationError, match="no verificable"):
        observe_spot_intent(reader, INTENT)
    assert reader.calls == [("BTCUSDT", UUID(INTENT_ID).hex)]


def test_incomplete_or_stale_fills_do_not_reconcile_order():
    reader = FakeReader(fills=[])
    with pytest.raises(MexcOrderObservationError, match="no concuerdan"):
        observe_spot_intent(reader, INTENT)
    assert reader.calls == [("BTCUSDT", UUID(INTENT_ID).hex), ("BTCUSDT", "external-1")]


def test_fee_assets_are_kept_separate():
    rows = [{"id": "fill-1", "order_id": "external-1", "side": "BUY",
             "quantity": "0.001", "quote_quantity": "65", "commission": "0.000001",
             "commission_asset": "BTC"},
            {"id": "fill-2", "order_id": "external-1", "side": "BUY",
             "quantity": "0.001", "quote_quantity": "65", "commission": "0.01",
             "commission_asset": "USDT"}]
    reader = FakeReader(fills=rows)
    observed = observe_spot_intent(reader, INTENT)
    assert observed.fees_by_asset == (("BTC", Decimal("0.000001")),
                                      ("USDT", Decimal("0.01")))
