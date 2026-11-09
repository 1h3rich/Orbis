from datetime import datetime, timezone
from decimal import Decimal, localcontext

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.database import Base
from app.database.exact_decimal import ExactDecimal


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


class PaperScenario(Base):
    """
    Sesión PAPER: capital y límites del simulador para una moneda cotizada.

    Cada apertura crea una fila nueva con ``generation`` incremental. Solo
    puede haber **un escenario abierto por moneda** (índice único parcial
    ``uq_paper_scenarios_open_quote``); cerrar una sesión marca ``closed_at``
    y conserva el historial.

    ``create_all`` no modifica tablas ya creadas, así que ``orbis.db`` necesita
    DDL manual (ver ``scripts/check_schema.py``, que detecta los cambios).
    """

    __tablename__ = "paper_scenarios"
    __table_args__ = (
        Index(
            "uq_paper_scenarios_open_quote",
            "quote_currency",
            unique=True,
            sqlite_where=text("closed_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    quote_currency: Mapped[str] = mapped_column(String(12), nullable=False)
    generation: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    initial_balance: Mapped[Decimal] = mapped_column(ExactDecimal(30, 12), nullable=False)
    max_order_amount: Mapped[Decimal] = mapped_column(ExactDecimal(30, 12), nullable=False)
    max_fee_percentage: Mapped[Decimal] = mapped_column(ExactDecimal(10, 4), nullable=False)
    daily_limit: Mapped[Decimal | None] = mapped_column(ExactDecimal(30, 12))
    monthly_limit: Mapped[Decimal | None] = mapped_column(ExactDecimal(30, 12))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    @property
    def is_open(self) -> bool:
        """Una sesión está abierta mientras ``closed_at`` sea ``NULL``."""
        return self.closed_at is None


class PriceQuote(Base):
    """Último precio manualmente publicado por par; lo usa ManualPriceProvider."""

    __tablename__ = "price_quotes"
    __table_args__ = (UniqueConstraint("asset", "quote_currency"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset: Mapped[str] = mapped_column(String, nullable=False)
    quote_currency: Mapped[str] = mapped_column(String(12), nullable=False)
    price: Mapped[Decimal] = mapped_column(ExactDecimal(30, 12), nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False, default="MANUAL")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class PaperRequest(Base):
    """Resultado persistido de una solicitud PAPER para reintentos seguros."""

    __tablename__ = "paper_requests"

    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    executed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False)
    asset: Mapped[str] = mapped_column(String, nullable=False)
    amount_spent: Mapped[Decimal] = mapped_column(ExactDecimal(30, 12), nullable=False)
    asset_received: Mapped[Decimal] = mapped_column(ExactDecimal(30, 12), nullable=False)
    price: Mapped[Decimal] = mapped_column(ExactDecimal(30, 12), nullable=False)
    fee: Mapped[Decimal] = mapped_column(ExactDecimal(30, 12), nullable=False)
    operation_id: Mapped[int | None] = mapped_column(
        ForeignKey("operations.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class PaperMarketEvidence(Base):
    """Cotización pública usada por una solicitud PAPER gestionada por servidor."""

    __tablename__ = "paper_market_evidence"

    request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("paper_requests.request_id"), primary_key=True,
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[str] = mapped_column(String(64), nullable=False)
    market_time_at: Mapped[str | None] = mapped_column(String(64))
    market_time_status: Mapped[str] = mapped_column(String(16), nullable=False)
    price_basis: Mapped[str] = mapped_column(String(16), nullable=False)
    price: Mapped[str] = mapped_column(String(80), nullable=False)
    approximate: Mapped[bool] = mapped_column(Boolean, nullable=False)


class Operation(Base):
    """
    Operación financiera almacenada permanentemente
    en el Ledger de Orbis.

    Las escrituras nuevas usan Decimal exacto codificado como TEXT dentro de
    las columnas NUMERIC existentes; las filas heredadas siguen siendo legibles.
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
        ExactDecimal(30, 12),
        nullable=False
    )

    asset_received: Mapped[Decimal] = mapped_column(
        ExactDecimal(30, 12),
        nullable=False
    )

    price: Mapped[Decimal] = mapped_column(
        ExactDecimal(30, 12),
        nullable=False
    )

    trading_fee: Mapped[Decimal] = mapped_column(
        ExactDecimal(30, 12),
        default=Decimal("0"),
        nullable=False
    )

    @property
    def total_fees(self) -> Decimal:
        """Suma de comisiones (trading + withdrawal + network) en Decimal."""
        with localcontext() as context:
            context.prec = 80
            return (
                Decimal(str(self.trading_fee or 0))
                + Decimal(str(self.withdrawal_fee or 0))
                + Decimal(str(self.network_fee or 0))
            )

    withdrawal_fee: Mapped[Decimal] = mapped_column(
        ExactDecimal(30, 12),
        default=Decimal("0"),
        nullable=False
    )

    network_fee: Mapped[Decimal] = mapped_column(
        ExactDecimal(30, 12),
        default=Decimal("0"),
        nullable=False
    )


class UiMarketState(Base):
    """Estado del Estudio por mercado+provider (dibujos/indicadores/config/planes).

    Clave natural: (market, provider, timeframe). ``market`` es el símbolo
    canónico (``BTC/EUR``): nunca se pinta un dibujo de ETH en BTC porque la
    carga filtra por mercado normalizado. ``payload`` es JSON.
    """

    __tablename__ = "ui_market_state"
    __table_args__ = (
        Index(
            "ix_ui_market_state_market",
            "market",
            "provider",
            "timeframe",
            "kind",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market: Mapped[str] = mapped_column(String(24), nullable=False)
    provider: Mapped[str] = mapped_column(String(24), nullable=False, default="kraken")
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False, default="1h")
    kind: Mapped[str] = mapped_column(String(24), nullable=False, default="drawings")
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class UiWatchlist(Base):
    """Watchlist persistente del usuario (mercados con provider)."""

    __tablename__ = "ui_watchlist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market: Mapped[str] = mapped_column(String(24), nullable=False)
    provider: Mapped[str] = mapped_column(String(24), nullable=False, default="kraken")
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class UiPreferences(Base):
    """Preferencias UI por clave (modo, provider default, config Estudio...)."""

    __tablename__ = "ui_preferences"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
