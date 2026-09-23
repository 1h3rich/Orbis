from decimal import Decimal


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
    Devuelve el resumen de compras PAPER si corresponde a un único par.

    Se conserva para consumidores existentes. Para varios pares o para LIVE,
    usa `calculate_buy_summaries` y elige explícitamente el modo.
    """
    summaries = calculate_buy_summaries(operations, mode="PAPER")
    if len(summaries) > 1:
        raise ValueError("Cannot summarize multiple asset/currency pairs")
    if summaries:
        return {
            key: value for key, value in summaries[0].items()
            if key not in ("asset", "quote_currency")
        }
    return {
        "total_invested": Decimal("0"),
        "total_asset_received": Decimal("0"),
        "total_fees": Decimal("0"),
        "average_buy_price": Decimal("0"),
    }


def calculate_buy_summaries(operations: list, mode: str) -> list[dict]:
    """Resume compras ejecutadas por activo y moneda para un modo concreto.

    Muestra importes históricos de compra, no saldos actuales ni P/L. Las
    comisiones se atribuyen solo a las compras del mismo par y modo.
    """
    groups: dict[tuple[str, str], dict] = {}

    for operation in operations:
        if (
            operation.operation_type != "BUY"
            or operation.status != "EXECUTED"
            or operation.mode != mode
        ):
            continue

        key = (operation.asset, operation.quote_currency)
        if key not in groups:
            groups[key] = {
                "asset": operation.asset,
                "quote_currency": operation.quote_currency,
                "total_invested": Decimal("0"),
                "total_asset_received": Decimal("0"),
                "total_fees": Decimal("0"),
                "average_buy_price": Decimal("0"),
            }

        group = groups[key]
        group["total_invested"] += Decimal(str(operation.amount_spent))
        group["total_asset_received"] += Decimal(str(operation.asset_received))
        group["total_fees"] += sum(
            (
                Decimal(str(operation.trading_fee)),
                Decimal(str(operation.withdrawal_fee)),
                Decimal(str(operation.network_fee)),
            ),
            Decimal("0"),
        )

    for group in groups.values():
        if group["total_asset_received"] > 0:
            group["average_buy_price"] = (
                group["total_invested"] / group["total_asset_received"]
            )

    return [groups[key] for key in sorted(groups)]
