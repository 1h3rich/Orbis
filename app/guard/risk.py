from dataclasses import dataclass
from decimal import Decimal


@dataclass
class GuardResult:
    allowed: bool
    reason: str


def to_decimal(value) -> Decimal:
    """
    Convierte valores numéricos a Decimal
    sin arrastrar errores binarios de float.
    """
    return Decimal(str(value))


def check_budget(
    order_amount,
    available_budget,
) -> GuardResult:

    order_amount = to_decimal(order_amount)
    available_budget = to_decimal(available_budget)

    if order_amount <= 0:
        return GuardResult(
            False,
            "Order amount must be greater than zero"
        )

    if order_amount > available_budget:
        return GuardResult(
            False,
            "Insufficient budget"
        )

    return GuardResult(True, "Order allowed")


def check_order_limit(
    order_amount,
    max_order_amount,
) -> GuardResult:

    order_amount = to_decimal(order_amount)
    max_order_amount = to_decimal(max_order_amount)

    if order_amount <= 0:
        return GuardResult(
            False,
            "Order amount must be greater than zero"
        )

    if order_amount > max_order_amount:
        return GuardResult(
            False,
            "Order exceeds maximum allowed amount"
        )

    return GuardResult(True, "Order allowed")


def check_fee_limit(
    order_amount,
    estimated_fee,
    max_fee_percentage,
) -> GuardResult:

    order_amount = to_decimal(order_amount)
    estimated_fee = to_decimal(estimated_fee)
    max_fee_percentage = to_decimal(max_fee_percentage)

    if order_amount <= 0:
        return GuardResult(
            False,
            "Order amount must be greater than zero"
        )

    if estimated_fee < 0:
        return GuardResult(
            False,
            "Estimated fee cannot be negative"
        )

    if estimated_fee >= order_amount:
        return GuardResult(
            False,
            "Estimated fee must be lower than order amount"
        )

    fee_percentage = (
        estimated_fee / order_amount
    ) * Decimal("100")

    if fee_percentage > max_fee_percentage:
        return GuardResult(
            False,
            "Estimated fee exceeds allowed percentage"
        )

    return GuardResult(True, "Order allowed")


def check_daily_limit(
    order_amount,
    daily_spent,
    daily_limit,
) -> GuardResult:
    """
    Comprueba que la nueva operación no provoque
    que se supere el límite diario.
    """

    order_amount = to_decimal(order_amount)
    daily_spent = to_decimal(daily_spent)
    daily_limit = to_decimal(daily_limit)

    if daily_spent < 0:
        return GuardResult(
            False,
            "Daily spent cannot be negative"
        )

    if daily_limit < 0:
        return GuardResult(
            False,
            "Daily limit cannot be negative"
        )

    if daily_spent + order_amount > daily_limit:
        return GuardResult(
            False,
            "Daily spending limit exceeded"
        )

    return GuardResult(True, "Order allowed")


def check_monthly_limit(
    order_amount,
    monthly_spent,
    monthly_limit,
) -> GuardResult:
    """
    Comprueba que la nueva operación no provoque
    que se supere el límite mensual.
    """

    order_amount = to_decimal(order_amount)
    monthly_spent = to_decimal(monthly_spent)
    monthly_limit = to_decimal(monthly_limit)

    if monthly_spent < 0:
        return GuardResult(
            False,
            "Monthly spent cannot be negative"
        )

    if monthly_limit < 0:
        return GuardResult(
            False,
            "Monthly limit cannot be negative"
        )

    if monthly_spent + order_amount > monthly_limit:
        return GuardResult(
            False,
            "Monthly spending limit exceeded"
        )

    return GuardResult(True, "Order allowed")


def evaluate_order(
    order_amount,
    available_budget,
    max_order_amount,
    estimated_fee,
    max_fee_percentage,
    daily_spent=0,
    daily_limit=None,
    monthly_spent=0,
    monthly_limit=None,
) -> GuardResult:
    """
    Ejecuta todos los controles de seguridad.

    Los límites diario y mensual son opcionales.
    Si no están configurados, no se aplican.
    """

    checks = [
        check_budget(
            order_amount,
            available_budget
        ),
        check_order_limit(
            order_amount,
            max_order_amount
        ),
        check_fee_limit(
            order_amount,
            estimated_fee,
            max_fee_percentage
        ),
    ]

    if daily_limit is not None:
        checks.append(
            check_daily_limit(
                order_amount,
                daily_spent,
                daily_limit,
            )
        )

    if monthly_limit is not None:
        checks.append(
            check_monthly_limit(
                order_amount,
                monthly_spent,
                monthly_limit,
            )
        )

    for result in checks:
        if not result.allowed:
            return result

    return GuardResult(
        True,
        "All guard checks passed"
    )
