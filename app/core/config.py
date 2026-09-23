from pydantic import BaseModel


class OrbisConfig(BaseModel):
    app_name: str = "Orbis"
    version: str = "0.1.0"
    paper_mode: bool = True
    base_currency: str = "EUR"
    