"""Cálculos numéricos de Orbis.

Núcleo PAPER en ``Decimal`` exacto: ``calculate_buy_summaries`` y
``calculate_average_cost`` (vía ``app/math/positions.py``). Las 7 funciones
históricas en ``float`` que siguen (
``calculate_total_invested``, ``calculate_average_buy_price``,
``calculate_position_value``, ``calculate_profit_loss``,
``calculate_return_percentage``, ``calculate_total_fees``,
``calculate_net_investment``) se conservan solo por compatibilidad con
consumidores y pruebas existentes: están marcadas como históricas en sus
docstrings, ningún código nuevo de la aplicación las usa (verificado por
búsqueda: solo las referencian ``tests/test_math.py`` y sus definiciones) y
no deben emplearse en código nuevo del motor PAPER. No se cambian sus firmas
para no romper pruebas.
"""

from decimal import Decimal, localcontext


def calculate_total_invested(amounts: list[float]) -> float:
    """
    Histórica (float): no usar en código nuevo del motor PAPER (usar Decimal).

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
    Histórica (float): no usar en código nuevo del motor PAPER (usar Decimal).

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
    Histórica (float): no usar en código nuevo del motor PAPER (usar Decimal).

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
    Histórica (float): no usar en código nuevo del motor PAPER (usar Decimal).

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
    Histórica (float): no usar en código nuevo del motor PAPER (usar Decimal).

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
    Histórica (float): no usar en código nuevo del motor PAPER (usar Decimal).

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
    Histórica (float): no usar en código nuevo del motor PAPER (usar Decimal).

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


def calculate_average_cost(invested, quantity):
    """
    Coste medio unitario de una posición.

    Acepta ``float`` o ``Decimal`` y devuelve siempre el mismo tipo que ha
    recibido, de modo que las funciones históricas en ``float`` siguen
    funcionando igual y el núcleo PAPER puede usar ``Decimal`` exacto.

    Si no hay unidades, devuelve cero en el tipo de entrada.

    Ejemplo:
    100 € invertidos / 0.001 BTC = 100000 €/BTC
    """
    uses_decimal = isinstance(invested, Decimal) or isinstance(quantity, Decimal)
    zero = Decimal("0") if uses_decimal else 0.0

    if quantity is None or quantity <= 0:
        return zero

    if uses_decimal:
        decimal_invested = invested if isinstance(invested, Decimal) else Decimal(str(invested))
        decimal_quantity = quantity if isinstance(quantity, Decimal) else Decimal(str(quantity))
        with localcontext() as context:
            # Los libros antiguos calcularon costes corrientes con 28
            # dígitos. Elevarlos indiscriminadamente modifica P/L históricos
            # por residuos de división periódica; sólo hace falta ampliar el
            # contexto si el importe y su escala no caben en 28.
            integer_digits = max(0, decimal_invested.adjusted() + 1)
            scale = max(0, -decimal_invested.as_tuple().exponent)
            context.prec = 80 if integer_digits + scale + 2 > 28 else 28
            return decimal_invested / decimal_quantity
    return invested / quantity


def calculate_buy_summaries(operations: list, mode: str) -> list[dict]:
    """Resume compras ejecutadas por activo y moneda para un modo concreto.

    Muestra importes históricos de compra, no saldos actuales ni P/L. Las
    comisiones se atribuyen solo a las compras del mismo par y modo.
    """
    with localcontext() as context:
        context.prec = 80
        return _calculate_buy_summaries(operations, mode)


def _calculate_buy_summaries(operations: list, mode: str) -> list[dict]:
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
