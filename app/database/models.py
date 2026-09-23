from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    Numeric,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.database import Base


class Strategy(Base):
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
    Operación financiera almacenada permanentemente
    en el Ledger de Orbis.

    Las cantidades financieras utilizan Decimal/Numeric
    para evitar errores de precisión de float.
    """

    __tablename__ = "operations"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True
    )

    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )

    operation_type: Mapped[str] = mapped_column(
        String,
        nullable=False
    )

    mode: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="PAPER"
    )

    source: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="MANUAL"
    )

    status: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="EXECUTED"
    )

    exchange: Mapped[str | None] = mapped_column(
        String,
        nullable=True
    )

    strategy_id: Mapped[int | None] = mapped_column(
        ForeignKey("strategies.id"),
        nullable=True
    )

    asset: Mapped[str] = mapped_column(
        String,
        nullable=False
    )

    quote_currency: Mapped[str] = mapped_column(
        String,
        nullable=False
    )

    amount_spent: Mapped[Decimal] = mapped_column(
        Numeric(30, 12),
        nullable=False
    )

    asset_received: Mapped[Decimal] = mapped_column(
        Numeric(30, 12),
        nullable=False
    )

    price: Mapped[Decimal] = mapped_column(
        Numeric(30, 12),
        nullable=False
    )

    trading_fee: Mapped[Decimal] = mapped_column(
        Numeric(30, 12),
        default=Decimal("0"),
        nullable=False
    )

    withdrawal_fee: Mapped[Decimal] = mapped_column(
        Numeric(30, 12),
        default=Decimal("0"),
        nullable=False
    )

    network_fee: Mapped[Decimal] = mapped_column(
        Numeric(30, 12),
        default=Decimal("0"),
        nullable=False
    )