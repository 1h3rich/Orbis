from typing import Any

from pydantic import BaseModel, Field


class StrategyCreate(BaseModel):
    """Entrada para crear o reemplazar una estrategia; `config` admite cualquier clave."""
    name: str
    strategy_type: str
    enabled: bool = True
    config: dict[str, Any]

class OperationCreate(BaseModel):
    """
    Entrada HTTP para registrar una operación en el Ledger.

    Los números no pueden ser negativos. El tipo, activo y moneda aún no
    tienen un conjunto de valores restringido ni validación cruzada.
    """

    operation_type: str
    asset: str
    quote_currency: str

    amount_spent: float = Field(ge=0)
    asset_received: float = Field(ge=0)
    price: float = Field(ge=0)

    trading_fee: float = Field(default=0.0, ge=0)
    withdrawal_fee: float = Field(default=0.0, ge=0)
    network_fee: float = Field(default=0.0, ge=0)
