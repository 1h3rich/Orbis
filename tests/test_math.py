from app.math.calculations import (
    calculate_total_invested,
    calculate_average_buy_price,
    calculate_position_value,
    calculate_profit_loss,
    calculate_return_percentage,
    calculate_total_fees,
    calculate_net_investment,
)

def test_calculate_total_invested():
    """
    Comprueba que Orbis suma correctamente
    todas las cantidades invertidas.
    """
    amounts = [50, 50, 100]

    result = calculate_total_invested(amounts)

    assert result == 200


def test_calculate_average_buy_price():
    """
    Comprueba el cálculo del precio medio de compra.
    """
    total_invested = 1000
    total_asset_received = 0.01

    result = calculate_average_buy_price(
        total_invested,
        total_asset_received
    )

    assert result == 100000

def test_calculate_position_value():
    """
    Comprueba el cálculo del valor actual de una posición.
    """
    asset_amount = 0.015
    current_price = 80000

    result = calculate_position_value(
        asset_amount,
        current_price
    )

    assert result == 1200

def test_calculate_profit_loss():
    """
    Comprueba el cálculo de beneficio o pérdida.
    """
    result = calculate_profit_loss(
        total_invested=1000,
        current_value=1200
    )

    assert result == 200

def test_calculate_positive_return_percentage():
    """
    Comprueba la rentabilidad cuando existe beneficio.
    """
    result = calculate_return_percentage(
        total_invested=1000,
        current_value=1200
    )

    assert result == 20


def test_calculate_negative_return_percentage():
    """
    Comprueba la rentabilidad cuando existe pérdida.
    """
    result = calculate_return_percentage(
        total_invested=1000,
        current_value=800
    )

    assert result == -20

def test_calculate_total_fees():
    """
    Comprueba la suma de todas las comisiones.
    """
    fees = [0.50, 1.25, 2.00]

    result = calculate_total_fees(fees)

    assert result == 3.75

def test_calculate_net_investment():
    """
    Comprueba el coste real incluyendo comisiones.
    """
    result = calculate_net_investment(
        invested_amount=1000,
        total_fees=10
    )

    assert result == 1010