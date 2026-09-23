from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass
class Operation:
    """
    Representa una operación financiera dentro
    del dominio de Orbis.
    """

    operation_type: str
    asset: str
    quote_currency: str

    amount_spent: Decimal
    asset_received: Decimal
    price: Decimal

    trading_fee: Decimal = Decimal("0")
    withdrawal_fee: Decimal = Decimal("0")
    network_fee: Decimal = Decimal("0")

    mode: str = "PAPER"
    source: str = "MANUAL"
    status: str = "EXECUTED"

    exchange: str | None = None
    strategy_id: int | None = None
    timestamp: datetime | None = None

    def total_fees(self) -> Decimal:
        """
        Devuelve todas las comisiones asociadas
        a la operación.
        """
        return (
            self.trading_fee
            + self.withdrawal_fee
            + self.network_fee
        )

    def total_cost(self) -> Decimal:
        """
        Devuelve el coste total:
        capital utilizado + comisiones.
        """
        return self.amount_spent + self.total_fees()