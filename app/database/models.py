from sqlalchemy import Boolean, Float, Integer, String, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.database.database import Base


class Strategy(Base):
    """Definición persistida de una estrategia; no contiene estado de ejecución."""
    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True
    )

    name: Mapped[str] = mapped_column(
        String,
        nullable=False
    )

    strategy_type: Mapped[str] = mapped_column(
        String,
        nullable=False
    )

    enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True
    )

    config: Mapped[dict] = mapped_column(
        JSON,
        nullable=False
    )

class Operation(Base):
    """
    Registro básico de operación en SQLite, separado de la dataclass del Ledger.

    Conserva importes y comisiones, pero aún no incluye fecha, exchange,
    estrategia, estado de orden ni identificadores externos.
    """

    __tablename__ = "operations"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True
    )

    operation_type: Mapped[str] = mapped_column(
        String,
        nullable=False
    )

    asset: Mapped[str] = mapped_column(
        String,
        nullable=False
    )

    quote_currency: Mapped[str] = mapped_column(
        String,
        nullable=False
    )

    amount_spent: Mapped[float] = mapped_column(
        Float,
        nullable=False
    )

    asset_received: Mapped[float] = mapped_column(
        Float,
        nullable=False
    )

    price: Mapped[float] = mapped_column(
        Float,
        nullable=False
    )

    trading_fee: Mapped[float] = mapped_column(
        Float,
        default=0.0
    )

    withdrawal_fee: Mapped[float] = mapped_column(
        Float,
        default=0.0
    )

    network_fee: Mapped[float] = mapped_column(
        Float,
        default=0.0
    )
