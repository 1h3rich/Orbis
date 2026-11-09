"""Intención de orden PAPER.

``OrderIntent`` es el único vehículo de una propuesta hacia el núcleo de
ejecución: la capa HTTP la construye y ya no toca el Ledger. El lado
(``side``) forma parte de la intención y del digesto de idempotencia, de modo
que un mismo ``request_id`` no puede servir tanto para comprar como para
vender.
"""

from dataclasses import dataclass
from decimal import Decimal


VALID_SIDES = ("BUY", "SELL")


def to_decimal(value) -> Decimal:
    """Convierte un valor recibido por la API a ``Decimal`` exacto."""
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


@dataclass(frozen=True)
class OrderIntent:
    """Propuesta de una operación PAPER completa y validada."""

    side: str
    asset: str
    quote_currency: str
    price: Decimal
    fee: Decimal
    quantity: Decimal | None = None
    order_amount: Decimal | None = None
    strategy_id: int | None = None
    source: str = "MANUAL"
    exchange: str | None = None

    def __post_init__(self):
        if self.side not in VALID_SIDES:
            raise ValueError(f"Invalid side: {self.side}")
        # Validación rápida de precio en el borde (falla con ValueError).
        # Fuente única del mensaje compartida con Guard (`check_price` en
        # app/guard/risk.py devuelve GuardResult con el mismo texto exacto):
        # "Asset price must be greater than zero". No cambiar el texto sin
        # actualizar ambos y sus pruebas.
        if to_decimal(self.price) <= 0:
            raise ValueError("Asset price must be greater than zero")
        if to_decimal(self.fee) < 0:
            raise ValueError("Fee cannot be negative")
        if self.side == "BUY":
            if self.order_amount is None or to_decimal(self.order_amount) <= 0:
                raise ValueError("BUY requires an order amount greater than zero")
        elif self.quantity is None or to_decimal(self.quantity) <= 0:
            raise ValueError("SELL requires a quantity greater than zero")

    @property
    def gross_amount(self) -> Decimal:
        """Importe bruto de la operación (compra reservada o venta × precio)."""
        if self.side == "BUY":
            return to_decimal(self.order_amount)
        return to_decimal(self.quantity) * to_decimal(self.price)

    @property
    def net_amount(self) -> Decimal:
        """Caja neta: lo que sale (BUY) o lo que se acredita (SELL)."""
        return self.gross_amount - to_decimal(self.fee)
