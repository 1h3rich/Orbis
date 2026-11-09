from dataclasses import dataclass
from decimal import Decimal, localcontext


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


def check_price(price) -> GuardResult:
    """
    El precio de la operación debe ser estrictamente positivo.

    Antes esta comprobación vivía en el núcleo (H6); ahora pertenece a Guard
    y se ejecuta antes que cualquier otro cálculo. Devuelve `GuardResult`
    (no lanza), a diferencia de `OrderIntent` (app/core/orders.py) que valida
    rápido con `ValueError`: ambas usan el mismo mensaje exacto
    "Asset price must be greater than zero" como fuente única compartida.
    No cambiar el texto sin actualizar ambos y sus pruebas.
    """
    if price is None:
        return GuardResult(
            False,
            "Asset price must be greater than zero"
        )

    price = to_decimal(price)

    if price <= 0:
        return GuardResult(
            False,
            "Asset price must be greater than zero"
        )

    return GuardResult(True, "Order allowed")


def check_position_exists(available_quantity) -> GuardResult:
    """Solo se puede vender algo que se tiene abierto."""
    available_quantity = to_decimal(available_quantity)

    if available_quantity <= 0:
        return GuardResult(
            False,
            "No open position for asset"
        )

    return GuardResult(True, "Order allowed")


def check_sell_quantity(quantity, available_quantity) -> GuardResult:
    """La cantidad vendida debe ser positiva y no superar el inventario."""
    quantity = to_decimal(quantity)
    available_quantity = to_decimal(available_quantity)

    if quantity <= 0:
        return GuardResult(
            False,
            "Sell quantity must be greater than zero"
        )

    if quantity > available_quantity:
        return GuardResult(
            False,
            "Sell quantity exceeds available position"
        )

    return GuardResult(True, "Order allowed")


def check_no_negative_inventory(quantity, available_quantity) -> GuardResult:
    """Defensa en profundidad: el inventario resultante nunca puede ser negativo."""
    quantity = to_decimal(quantity)
    available_quantity = to_decimal(available_quantity)

    if available_quantity - quantity < 0:
        return GuardResult(
            False,
            "Position inventory cannot go negative"
        )

    return GuardResult(True, "Order allowed")


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

    # Comparar productos evita que la división redondee un exceso de una
    # unidad mínima justo en el límite configurado.
    with localcontext() as context:
        context.prec = 80
        fee_exceeded = estimated_fee * Decimal("100") > max_fee_percentage * order_amount

    if fee_exceeded:
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

    with localcontext() as context:
        context.prec = 80
        exceeded = daily_spent + order_amount > daily_limit
    if exceeded:
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

    with localcontext() as context:
        context.prec = 80
        exceeded = monthly_spent + order_amount > monthly_limit
    if exceeded:
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
    price=None,
    halted=False,
) -> GuardResult:
    """
    Ejecuta todos los controles de seguridad de una compra.

    Los límites diario y mensual son opcionales.
    Si no están configurados, no se aplican.

    ``halted`` es el kill switch conceptual: está apagado por defecto (v1 no
    tiene tabla ni endpoint que lo active) y, si llegara a encenderse, se
    comprueba **primero** y bloquea cualquier orden.
    """

    if halted:
        return GuardResult(
            False,
            "Kill switch is active"
        )

    price_result = check_price(price) if price is not None else GuardResult(True, "")
    if not price_result.allowed:
        return price_result

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


def evaluate_sell(
    quantity,
    price,
    estimated_fee,
    max_fee_percentage,
    available_quantity,
    halted=False,
) -> GuardResult:
    """
    Ejecuta los controles de seguridad de una venta PAPER.

    Comprueba el estado del kill switch, el precio, que exista posición
    abierta, que la cantidad no supere el inventario y que la comisión sea
    aceptable. No aplica límites diario/mensual: esos límites miden compras.
    """

    if halted:
        return GuardResult(
            False,
            "Kill switch is active"
        )

    with localcontext() as context:
        context.prec = 80
        gross_value = to_decimal(quantity) * to_decimal(price)

    checks = [
        check_price(price),
        check_position_exists(available_quantity),
        check_sell_quantity(quantity, available_quantity),
        check_no_negative_inventory(quantity, available_quantity),
        check_fee_limit(
            gross_value,
            estimated_fee,
            max_fee_percentage,
        ),
    ]

    for result in checks:
        if not result.allowed:
            return result

    return GuardResult(
        True,
        "All guard checks passed"
    )
