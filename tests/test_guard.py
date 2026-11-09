from decimal import Decimal, localcontext

from app.guard.risk import (
    check_budget,
    check_order_limit,
    check_fee_limit,
    evaluate_order,
    evaluate_sell,
)


def test_guard_allows_valid_order():
    result = check_budget(50, 100)

    assert result.allowed is True


def test_guard_blocks_insufficient_budget():
    result = check_budget(150, 100)

    assert result.allowed is False
    assert result.reason == "Insufficient budget"


def test_guard_blocks_invalid_amount():
    result = check_budget(0, 100)

    assert result.allowed is False


def test_guard_allows_order_inside_limit():
    result = check_order_limit(50, 100)

    assert result.allowed is True


def test_guard_blocks_order_above_limit():
    result = check_order_limit(150, 100)

    assert result.allowed is False
    assert result.reason == "Order exceeds maximum allowed amount"


def test_guard_allows_acceptable_fee():
    result = check_fee_limit(100, 1, 2)

    assert result.allowed is True


def test_guard_blocks_expensive_fee():
    result = check_fee_limit(100, 3, 2)

    assert result.allowed is False
    assert result.reason == "Estimated fee exceeds allowed percentage"


def test_guard_blocks_negative_fee():
    result = check_fee_limit(100, -1, 2)

    assert result.allowed is False


def test_guard_blocks_fee_that_consumes_order():
    result = check_fee_limit(100, 100, 100)

    assert result.allowed is False
    assert result.reason == "Estimated fee must be lower than order amount"


def test_evaluate_order_allows_safe_order():
    result = evaluate_order(
        order_amount=50,
        available_budget=500,
        max_order_amount=100,
        estimated_fee=0.50,
        max_fee_percentage=2,
    )

    assert result.allowed is True
    assert result.reason == "All guard checks passed"


def test_evaluate_order_blocks_insufficient_budget():
    result = evaluate_order(
        order_amount=150,
        available_budget=100,
        max_order_amount=200,
        estimated_fee=1,
        max_fee_percentage=2,
    )

    assert result.allowed is False
    assert result.reason == "Insufficient budget"


def test_evaluate_order_blocks_order_limit():
    result = evaluate_order(
        order_amount=150,
        available_budget=500,
        max_order_amount=100,
        estimated_fee=1,
        max_fee_percentage=2,
    )

    assert result.allowed is False
    assert result.reason == "Order exceeds maximum allowed amount"


def test_evaluate_order_blocks_high_fee():
    result = evaluate_order(
        order_amount=100,
        available_budget=500,
        max_order_amount=200,
        estimated_fee=5,
        max_fee_percentage=2,
    )

    assert result.allowed is False
    assert result.reason == "Estimated fee exceeds allowed percentage"


def test_sell_fee_limit_uses_unrounded_gross_value():
    quantity = Decimal("123456789012345678.123456789012")
    with localcontext() as context:
        context.prec = 80
        gross = quantity * Decimal("0.9")
        fee = gross * Decimal("0.02") + Decimal("0.000000000001")
    result = evaluate_sell(
        quantity=quantity, price=Decimal("0.9"), estimated_fee=fee,
        max_fee_percentage=Decimal("2"), available_quantity=quantity,
    )
    assert not result.allowed
    assert result.reason == "Estimated fee exceeds allowed percentage"
