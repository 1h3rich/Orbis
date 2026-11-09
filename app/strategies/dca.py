from decimal import Decimal

from pydantic import BaseModel, Field


class DCAConfig(BaseModel):
    """Parámetros de un DCA futuro; por ahora no existe scheduler ni ejecución."""

    enabled: bool = True

    asset: str
    quote_currency: str

    # Dinero por aportación, no unidades: se guarda y se compara en Decimal
    # para que límites y presupuestos no arrastren errores de float.
    amount: Decimal = Field(gt=0)

    frequency: str
