from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.database.exact_decimal import fits_exact_decimal


class StrategyCreate(BaseModel):
    name: str
    strategy_type: str
    enabled: bool = True
    config: dict[str, Any]


class OperationCreate(BaseModel):
    """
    Datos necesarios para registrar manualmente
    una operación financiera en el Ledger.
    """

    model_config = ConfigDict(extra="forbid")

    operation_type: str
    asset: str
    quote_currency: str

    amount_spent: Decimal = Field(ge=0)
    asset_received: Decimal = Field(ge=0)
    price: Decimal = Field(ge=0)

    trading_fee: Decimal = Field(
        default=Decimal("0"),
        ge=0
    )

    withdrawal_fee: Decimal = Field(
        default=Decimal("0"),
        ge=0
    )

    network_fee: Decimal = Field(
        default=Decimal("0"),
        ge=0
    )

    mode: Literal["PAPER", "LIVE"] = "PAPER"
    status: str = "EXECUTED"

    exchange: str | None = None
    strategy_id: int | None = None

    @field_validator("amount_spent", "asset_received", "price", "trading_fee",
                     "withdrawal_fee", "network_fee")
    @classmethod
    def ledger_decimal(cls, value: Decimal) -> Decimal:
        if not fits_exact_decimal(value, 30, 12):
            raise ValueError("Importe fuera de escala: máximo 18 enteros y 12 decimales")
        return value


class PaperBuyRequest(BaseModel):
    """
    Propuesta PAPER; el servidor obtiene capital y límites del escenario.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    asset: str
    quote_currency: str

    order_amount: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)
    estimated_fee: Decimal = Field(
        default=Decimal("0"),
        ge=0
    )
    exchange: str | None = None


class PaperScenarioCreate(BaseModel):
    """Configuración inicial de un escenario PAPER para una moneda."""

    model_config = ConfigDict(extra="forbid")

    quote_currency: str = Field(pattern=r"^[A-Z0-9]{2,12}$")
    initial_balance: Decimal = Field(gt=0)
    max_order_amount: Decimal = Field(gt=0)
    max_fee_percentage: Decimal = Field(ge=0, lt=100)
    daily_limit: Decimal | None = Field(default=None, gt=0)
    monthly_limit: Decimal | None = Field(default=None, gt=0)

    @field_validator("initial_balance", "max_order_amount", "daily_limit", "monthly_limit")
    @classmethod
    def balance_decimal(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and not fits_exact_decimal(value, 30, 12):
            raise ValueError("Importe fuera de escala: máximo 18 enteros y 12 decimales")
        return value

    @field_validator("max_fee_percentage")
    @classmethod
    def fee_percentage_decimal(cls, value: Decimal) -> Decimal:
        if not fits_exact_decimal(value, 10, 4):
            raise ValueError("Porcentaje fuera de escala: máximo 6 enteros y 4 decimales")
        return value


class PaperSellRequest(BaseModel):
    """
    Propuesta de venta PAPER; el servidor obtiene inventario, capital y
    límites del escenario almacenado.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    asset: str
    quote_currency: str

    quantity: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)
    fee: Decimal = Field(
        default=Decimal("0"),
        ge=0
    )
    exchange: str | None = None


PaperSpotProvider = Literal[
    "auto", "kraken", "binance", "coinbase", "okx", "bybit",
    "mexc", "kucoin", "gate", "bitget", "bitstamp",
]


class PaperMarketBuyRequest(BaseModel):
    """Compra PAPER a precio público validado por el servidor, sin `price`."""

    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    asset: str = Field(pattern=r"^[A-Za-z0-9]{1,12}$")
    quote_currency: str = Field(pattern=r"^[A-Za-z0-9]{2,12}$")
    order_amount: Decimal = Field(gt=0)
    estimated_fee: Decimal = Field(default=Decimal("0"), ge=0)
    provider: PaperSpotProvider = "auto"


class PaperMarketSellRequest(BaseModel):
    """Venta PAPER a precio público validado por el servidor, sin `price`."""

    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    asset: str = Field(pattern=r"^[A-Za-z0-9]{1,12}$")
    quote_currency: str = Field(pattern=r"^[A-Za-z0-9]{2,12}$")
    quantity: Decimal = Field(gt=0)
    fee: Decimal = Field(default=Decimal("0"), ge=0)
    provider: PaperSpotProvider = "auto"


class PaperPriceRequest(BaseModel):
    """Publica la cotización manual de un par para valorar la cartera."""

    model_config = ConfigDict(extra="forbid")

    asset: str
    quote_currency: str = Field(pattern=r"^[A-Z0-9]{2,12}$")
    price: Decimal = Field(gt=0)

    @field_validator("price")
    @classmethod
    def quote_decimal(cls, value: Decimal) -> Decimal:
        if not fits_exact_decimal(value, 30, 12):
            raise ValueError("Precio fuera de escala: máximo 18 enteros y 12 decimales")
        return value
