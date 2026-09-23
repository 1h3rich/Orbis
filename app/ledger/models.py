from dataclasses import dataclass
from datetime import datetime


@dataclass
class Operation:
    """
    Representa una operación financiera registrada por Orbis.
    """

    operation_type: str
    asset: str
    quote_currency: str

    amount_spent: float
    asset_received: float
    price: float

    trading_fee: float = 0.0
    withdrawal_fee: float = 0.0
    network_fee: float = 0.0

    timestamp: datetime | None = None

    def total_fees(self) -> float:
        """
        Devuelve todas las comisiones de la operación.
        """
        return (
            self.trading_fee
            + self.withdrawal_fee
            + self.network_fee
        )

    def total_cost(self) -> float:
        """
        Devuelve el coste total:
        dinero invertido + comisiones.
        """
        return self.amount_spent + self.total_fees()