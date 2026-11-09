"""Serialización JSON de importes financieros sin convertirlos a float."""

from decimal import Decimal

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse


def exact_jsonable(value):
    """Convierte estructuras y modelos para JSON, dejando Decimal como texto."""
    return jsonable_encoder(
        value,
        custom_encoder={Decimal: lambda number: format(number, "f")},
    )


def exact_json(value, *, status_code: int = 200) -> JSONResponse:
    """Respuesta financiera; nunca publica un Decimal como número binario."""
    return JSONResponse(status_code=status_code, content=exact_jsonable(value))
