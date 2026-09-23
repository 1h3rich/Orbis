from pydantic import BaseModel, Field


class DCAConfig(BaseModel):
    enabled: bool = True

    asset: str
    quote_currency: str

    amount: float = Field(gt=0)

    frequency: str