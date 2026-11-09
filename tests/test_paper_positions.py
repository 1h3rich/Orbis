"""Posiciones PAPER por pliegue: coste medio, realizado y orden (timestamp, id)."""

from datetime import datetime, timedelta
from decimal import Decimal

from app.ledger.models import Operation
from app.math.positions import (
    Position,
    apply_operation,
    build_positions,
    position_by_asset,
)

NOW = datetime(2026, 1, 1, 12, 0, 0)


def operation(
    operation_type="BUY",
    spent="100",
    received="0.001",
    price="100000",
    fee="0",
    timestamp=NOW,
    operation_id=1,
    mode="PAPER",
    status="EXECUTED",
    asset="BTC",
):
    item = Operation(
        operation_type=operation_type,
        asset=asset,
        quote_currency="EUR",
        amount_spent=Decimal(spent),
        asset_received=Decimal(received),
        price=Decimal(price),
        trading_fee=Decimal(fee),
        mode=mode,
        status=status,
        timestamp=timestamp,
    )
    item.id = operation_id
    return item


def test_single_buy_opens_position_with_exact_average_cost():
    [position] = build_positions([operation()])

    assert position.asset == "BTC"
    assert position.is_open is True
    assert position.quantity == Decimal("0.001")
    assert position.invested == Decimal("100")
    assert position.average_cost == Decimal("100000")
    assert position.realized_pnl == Decimal("0")
    assert position.fees == Decimal("0")


def test_partial_sell_keeps_average_cost_constant():
    ops = [
        operation(),
        operation(
            operation_type="SELL",
            spent="44",
            received="0.0004",
            price="110000",
            operation_id=2,
            timestamp=NOW + timedelta(seconds=1),
        ),
    ]

    [position] = build_positions(ops)

    assert position.average_cost == Decimal("100000")
    assert position.quantity == Decimal("0.0006")
    assert position.invested == Decimal("60")
    # (110000 − 100000) × 0.0004 − 0 = 4
    assert position.realized_pnl == Decimal("4")
    assert position.is_open is True


def test_realized_uses_cash_actually_credited_after_sell_rounding():
    ops = [
        operation(spent="1", received="1", price="1"),
        operation(
            operation_type="SELL", spent="0.000000000001",
            received="0.000000000002", price="0.8",
            operation_id=2, timestamp=NOW + timedelta(seconds=1),
        ),
    ]
    [position] = build_positions(ops)
    assert position.realized_pnl == Decimal("-0.000000000001")
    assert position.quantity == Decimal("0.999999999998")
    assert position.invested == Decimal("0.999999999998")
    equity = Decimal("1") - Decimal("1") + Decimal("0.000000000001") + position.quantity
    assert equity - Decimal("1") == position.realized_pnl


def test_full_sell_closes_the_fold_and_keeps_realized():
    ops = [
        operation(),
        operation(operation_type="SELL", spent="44", received="0.0004",
                  price="110000", operation_id=2, timestamp=NOW),
        operation(operation_type="SELL", spent="54", received="0.0006",
                  price="90000", operation_id=3, timestamp=NOW),
    ]

    [position] = build_positions(ops)

    assert position.quantity == Decimal("0")
    assert position.is_open is False
    assert position.invested == Decimal("0")
    # +4 por la venta a 110000 y −6 por la de 90000
    assert position.realized_pnl == Decimal("-2")


def test_new_buy_after_close_starts_a_new_fold():
    ops = [
        operation(),
        operation(operation_type="SELL", spent="100", received="0.001",
                  price="100000", operation_id=2, timestamp=NOW),
        operation(spent="300", received="0.002", price="150000",
                  operation_id=3, timestamp=NOW),
    ]

    [position] = build_positions(ops)

    assert position.quantity == Decimal("0.002")
    assert position.invested == Decimal("300")
    assert position.average_cost == Decimal("150000")
    assert position.realized_pnl == Decimal("0")


def test_operations_are_applied_in_timestamp_then_id_order():
    later = NOW + timedelta(seconds=5)
    shuffled = [
        operation(spent="300", received="0.003", price="100000",
                  operation_id=3, timestamp=later),
        operation(operation_type="SELL", spent="10", received="0.0001",
                  price="100000", operation_id=2, timestamp=NOW),
        operation(operation_id=1, timestamp=NOW),
    ]

    [position] = build_positions(shuffled)

    # Orden real: buy(id1) → sell(id2) → buy(id3)
    #   0.001 − 0.0001 + 0.003 = 0.0039 unidades
    #   100 − 100000×0.0001 + 300 = 390 invertidos → media 100000
    assert position.quantity == Decimal("0.0039")
    assert position.invested == Decimal("390")
    assert position.average_cost == Decimal("100000")
    assert position.realized_pnl == Decimal("0")

    # Si se aplicara en orden de llegada (sell antes que su buy) el resultado
    # sería distinto: el fold depende del orden total, no de la lista.
    reversed_result = build_positions(list(reversed(shuffled)))
    assert [p.quantity for p in reversed_result] == [position.quantity]


def test_positions_include_all_fee_types():
    item = operation(fee="1")
    item.withdrawal_fee = Decimal("0.5")
    item.network_fee = Decimal("0.25")

    [position] = build_positions([item])

    assert position.fees == Decimal("1.75")


def test_fees_accumulate_on_sells():
    ops = [
        operation(fee="1"),
        operation(operation_type="SELL", spent="99.5", received="0.001",
                  price="100000", fee="0.5", operation_id=2, timestamp=NOW),
    ]

    [position] = build_positions(ops)

    assert position.fees == Decimal("1.5")
    # El libro acredita 99,5 netos: 100 brutos menos la comisión de 0,5.
    assert position.realized_pnl == Decimal("-0.5")


def test_only_executed_paper_operations_are_folded():
    ops = [
        operation(mode="LIVE"),
        operation(status="CANCELLED"),
        operation(),
    ]

    positions = build_positions(ops)

    assert [position.asset for position in positions] == ["BTC"]
    assert positions[0].quantity == Decimal("0.001")


def test_positions_are_grouped_by_asset_and_quote():
    ops = [
        operation(asset="BTC"),
        operation(asset="ETH", spent="50", received="0.01", price="5000",
                  operation_id=2, timestamp=NOW),
        operation(asset="BTC", operation_id=3, timestamp=NOW),
    ]

    positions = build_positions(ops)
    index = position_by_asset(positions)

    assert sorted(position.asset for position in positions) == ["BTC", "ETH"]
    assert index["BTC"].quantity == Decimal("0.002")
    assert index["ETH"].quantity == Decimal("0.01")


def test_sell_without_position_excludes_the_position_from_the_report():
    [position] = build_positions([
        operation(operation_type="SELL", spent="10", received="0.001",
                  price="10000"),
    ])

    # Guard impide este caso en la API; el fold lo deja fuera de la cartera.
    assert position.is_open is False
    assert position.quantity < Decimal("0")


def test_position_rejects_negative_quantities_in_is_open():
    position = Position(asset="BTC", quote_currency="EUR", quantity=Decimal("-1"))
    assert position.is_open is False


def test_apply_operation_is_pure_decimals():
    position = Position(asset="BTC", quote_currency="EUR")
    apply_operation(position, operation())

    assert isinstance(position.quantity, Decimal)
    assert isinstance(position.average_cost, Decimal)
    assert isinstance(position.invested, Decimal)
    assert position.average_cost == Decimal("100000")
