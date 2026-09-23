from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


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
