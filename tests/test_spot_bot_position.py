"""Coste/beneficio propio del bot, sin atribuirle inventario ajeno ni PAPER."""

from dataclasses import replace
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.live_execution.spot_bot_plan import SpotBotPlanError
from app.live_execution.spot_bot_position import AssignedSpotSale, OwnedSpotFill, fold_owned_spot_fills


NOW = datetime(2026, 10, 30, tzinfo=timezone.utc)


def fill(side, quantity, gross, fee="0", currency="QUOTE", **kwargs):
    return OwnedSpotFill(str(uuid4()), str(uuid4()), side, Decimal(quantity), Decimal(gross),
                         Decimal(fee), currency, kwargs.pop("at", NOW),
                         kwargs.pop("entry_t", 0) if side == "BUY" else None, **kwargs)


def test_book_starts_with_no_owned_assets_not_the_exchange_balance():
    ledger = fold_owned_spot_fills([])
    assert ledger.position.units == ledger.position.cost_quote == 0
    assert ledger.net_cash_flow_quote == ledger.realized_pnl_quote == 0
    with pytest.raises(SpotBotPlanError, match="excede las unidades"):
        fold_owned_spot_fills([fill("SELL", "1", "100")])


def test_quote_fees_and_partial_exit_conserve_remaining_cost():
    ledger = fold_owned_spot_fills([
        fill("BUY", "2", "200", "2"), fill("SELL", "1", "120", "1")])
    assert ledger.position.units == 1 and ledger.position.cost_quote == 101
    assert ledger.realized_pnl_quote == 18 and ledger.net_cash_flow_quote == -83
    assert ledger.fill_count == 2


def test_buy_and_sell_base_fees_do_not_overstate_owned_units_or_profit():
    buy = fill("BUY", "2", "200", "0.1", "BASE")
    sale = fill("SELL", "1.8", "216", "0.1", "BASE")
    ledger = fold_owned_spot_fills([buy, sale])
    assert ledger.position.units == ledger.position.cost_quote == 0
    assert ledger.position.entry_candle_t is None and ledger.position.last_entry_intent_id is None
    assert ledger.realized_pnl_quote == ledger.net_cash_flow_quote == 16


def test_assigned_sale_splits_net_proceeds_over_quantity_and_base_fee():
    opening = fold_owned_spot_fills([fill("BUY", "2", "200")])
    # One BTC sold for 121 USDT with a 0.1 BTC fee. The bot supplies half of the debit.
    sale = AssignedSpotSale("manual-fill", NOW, Decimal("0.55"), Decimal("1.1"), Decimal("121"))
    ledger = fold_owned_spot_fills([sale], initial=opening)
    assert ledger.position.units == Decimal("1.45") and ledger.position.cost_quote == 145
    assert ledger.realized_pnl_quote == Decimal("5.5") and ledger.net_cash_flow_quote == Decimal("-139.5")
    assert ledger.fill_count == ledger.manual_sale_count == 1


def test_tiny_base_fee_prevents_false_full_liquidation():
    buy = fill("BUY", "1", "100", "0.000000000000000000000000000000000000001", "BASE")
    with pytest.raises(SpotBotPlanError, match="excede las unidades"):
        fold_owned_spot_fills([buy, fill("SELL", "1", "100")])


def test_duplicate_fills_and_reverse_times_are_not_silently_ignored():
    buy = fill("BUY", "1", "100")
    with pytest.raises(SpotBotPlanError, match="duplicados"):
        fold_owned_spot_fills([buy, buy])
    with pytest.raises(SpotBotPlanError, match="ordenados"):
        fold_owned_spot_fills([buy, fill("SELL", "0.1", "10", at=NOW - timedelta(seconds=1))])


def test_partial_cost_can_exceed_input_precision_without_becoming_unreadable():
    ledger = fold_owned_spot_fills([fill("BUY", "3", "100"), fill("SELL", "1", "40")])
    assert ledger.position.units == 2
    assert len(ledger.position.cost_quote.as_tuple().digits) == 300
    assert ledger.realized_pnl_quote > 6
