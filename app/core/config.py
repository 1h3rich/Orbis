from pydantic import BaseModel


class OrbisConfig(BaseModel):
    """Valores por defecto del proyecto; este modelo aún no se usa en la API."""
    app_name: str = "Orbis"
    version: str = "0.1.0"
    paper_mode: bool = True
    base_currency: str = "EUR"
