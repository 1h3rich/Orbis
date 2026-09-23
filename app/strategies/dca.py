from pydantic import BaseModel, Field


class DCAConfig(BaseModel):
    """Parámetros de un DCA futuro; por ahora no existe scheduler ni ejecución."""
    enabled: bool = True

    asset: str
    quote_currency: str

    amount: float = Field(gt=0)

    frequency: str
