from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field


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

    mode: str = "PAPER"
    source: str = "MANUAL"
    status: str = "EXECUTED"

    exchange: str | None = None
    strategy_id: int | None = None


class PaperBuyRequest(BaseModel):
    """
    Solicitud de compra simulada.

    Los límites diario y mensual son opcionales.
    Si no se configuran, Guard no aplica esos controles.
    """

    asset: str
    quote_currency: str

    order_amount: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)

    available_budget: Decimal = Field(ge=0)
    max_order_amount: Decimal = Field(gt=0)

    estimated_fee: Decimal = Field(
        default=Decimal("0"),
        ge=0
    )

    max_fee_percentage: Decimal = Field(ge=0)

    daily_limit: Decimal | None = Field(
        default=None,
        ge=0
    )

    monthly_limit: Decimal | None = Field(
        default=None,
        ge=0
    )

    exchange: str | None = None
    strategy_id: int | None = None