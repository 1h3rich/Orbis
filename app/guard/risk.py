from dataclasses import dataclass


@dataclass
class GuardResult:
    """Resultado de una comprobación: permiso y motivo legible para el usuario."""
    allowed: bool
    reason: str


def check_budget(
    order_amount: float,
    available_budget: float
) -> GuardResult:
    """Rechaza importes no positivos o mayores que el presupuesto disponible."""

    if order_amount <= 0:
        return GuardResult(
            allowed=False,
            reason="Order amount must be greater than zero"
        )

    if order_amount > available_budget:
        return GuardResult(
            allowed=False,
            reason="Insufficient budget"
        )

    return GuardResult(True, "Order allowed")


def check_order_limit(
    order_amount: float,
    max_order_amount: float
) -> GuardResult:
    """Rechaza importes no positivos o superiores al máximo por operación."""

    if order_amount <= 0:
        return GuardResult(
            allowed=False,
            reason="Order amount must be greater than zero"
        )

    if order_amount > max_order_amount:
        return GuardResult(
            allowed=False,
            reason="Order exceeds maximum allowed amount"
        )

    return GuardResult(True, "Order allowed")


def check_fee_limit(
    order_amount: float,
    estimated_fee: float,
    max_fee_percentage: float
) -> GuardResult:
    """Compara la comisión estimada con el porcentaje máximo del importe.

    El cálculo presupone que comisión e importe usan la misma moneda.
    """

    if order_amount <= 0:
        return GuardResult(
            allowed=False,
            reason="Order amount must be greater than zero"
        )

    if estimated_fee < 0:
        return GuardResult(
            allowed=False,
            reason="Estimated fee cannot be negative"
        )

    fee_percentage = (
        estimated_fee / order_amount
    ) * 100

    if fee_percentage > max_fee_percentage:
        return GuardResult(
            allowed=False,
            reason="Estimated fee exceeds allowed percentage"
        )

    return GuardResult(True, "Order allowed")


def evaluate_order(
    order_amount: float,
    available_budget: float,
    max_order_amount: float,
    estimated_fee: float,
    max_fee_percentage: float,
) -> GuardResult:
    """Aplica las tres comprobaciones y devuelve el primer rechazo.

    Esta función evalúa una propuesta; aún no está conectada al endpoint
    que registra operaciones ni a ningún ejecutor de órdenes.
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

    for result in checks:
        if not result.allowed:
            return result

    return GuardResult(
        allowed=True,
        reason="All guard checks passed"
    )
