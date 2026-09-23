def calculate_total_invested(amounts: list[float]) -> float:
    """
    Suma los importes recibidos; una lista vacía produce 0.

    Ejemplo:
    [50, 50, 100] -> 200
    """
    return sum(amounts)


def calculate_average_buy_price(
    total_invested: float,
    total_asset_received: float
) -> float:
    """
    Divide lo invertido entre unidades recibidas; devuelve 0 si no hay unidades.

    Ejemplo:
    1000 € invertidos / 0.01 BTC = 100000 €/BTC
    """
    if total_asset_received <= 0:
        return 0.0

    return total_invested / total_asset_received

def calculate_position_value(
    asset_amount: float,
    current_price: float
) -> float:
    """
    Multiplica unidades por precio; devuelve 0 ante un valor negativo.

    Ejemplo:
    0.01 BTC * 100000 €/BTC = 1000 €
    """
    if asset_amount < 0 or current_price < 0:
        return 0.0

    return asset_amount * current_price


def calculate_profit_loss(
    total_invested: float,
    current_value: float
) -> float:
    """
    Resta el capital invertido al valor actual, sin descontar comisiones.

    Resultado positivo = beneficio.
    Resultado negativo = pérdida.

    Ejemplo:
    1000 € invertidos y valor actual de 1200 € -> +200 €
    """
    return current_value - total_invested

def calculate_return_percentage(
    total_invested: float,
    current_value: float
) -> float:
    """
    Calcula (valor actual - invertido) / invertido * 100.

    Si el capital invertido es cero o negativo, devuelve 0.

    Ejemplo:
    1000 € invertidos y 1200 € actuales -> +20 %
    """
    if total_invested <= 0:
        return 0.0

    profit_loss = current_value - total_invested

    return (profit_loss / total_invested) * 100

def calculate_total_fees(fees: list[float]) -> float:
    """
    Suma las comisiones recibidas, sin distinguir su moneda.

    Puede incluir:
    - comisión de compra
    - comisión de retirada
    - comisión de red
    - otras comisiones
    """
    return sum(fees)

def calculate_net_investment(
    invested_amount: float,
    total_fees: float
) -> float:
    """
    Suma el importe invertido y las comisiones recibidas.

    Ejemplo:
    1000 € invertidos + 10 € en comisiones = 1010 €
    """
    return invested_amount + total_fees

def calculate_portfolio_summary(operations: list) -> dict:
    """
    Resume todas las compras como una sola posición y suma todas las comisiones.

    Solo las operaciones con tipo exacto "BUY" cuentan para importe y unidades;
    las comisiones incluyen cualquier tipo de operación. El precio medio se
    calcula como importe bruto / unidades, sin añadir comisiones. No separa
    activos ni monedas y no calcula valor actual ni rentabilidad.
    """

    buy_operations = [
        operation
        for operation in operations
        if operation.operation_type == "BUY"
    ]

    total_invested = sum(
        operation.amount_spent
        for operation in buy_operations
    )

    total_asset_received = sum(
        operation.asset_received
        for operation in buy_operations
    )

    total_fees = sum(
        operation.trading_fee
        + operation.withdrawal_fee
        + operation.network_fee
        for operation in operations
    )

    average_buy_price = (
        total_invested / total_asset_received
        if total_asset_received > 0
        else 0.0
    )

    return {
        "total_invested": total_invested,
        "total_asset_received": total_asset_received,
        "total_fees": total_fees,
        "average_buy_price": average_buy_price,
    }
