from dataclasses import dataclass
from datetime import datetime


@dataclass
class Operation:
    """
    Representa una operación en memoria; no es el modelo persistido por SQLAlchemy.

    Los importes y comisiones son floats y no llevan validación de moneda aquí.
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
        """Suma las comisiones de trading, retirada y red de esta operación."""
        return (
            self.trading_fee
            + self.withdrawal_fee
            + self.network_fee
        )

    def total_cost(self) -> float:
        """Devuelve importe gastado más comisiones, en la misma unidad asumida."""
        return self.amount_spent + self.total_fees()
