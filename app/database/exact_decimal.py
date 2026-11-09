"""Decimal exacto en columnas SQLite de afinidad NUMERIC ya existentes.

SQLite convierte cadenas numéricas en estas columnas a INTEGER/REAL y pierde
precisión. El prefijo hace que las escrituras nuevas permanezcan como TEXT,
sin cambiar el esquema ni las filas antiguas. No se puede recuperar precisión
que SQLite ya perdió en filas heredadas.
"""

from decimal import Decimal, localcontext

from sqlalchemy.types import UserDefinedType


def fits_exact_decimal(number: Decimal, precision: int, scale: int) -> bool:
    """Comprueba dígitos enteros y escala, no solo dígitos significativos."""
    if not number.is_finite():
        return False
    with localcontext() as context:
        context.prec = max(80, precision + scale + 10)
        normalized = number.normalize()
    integer_digits = max(0, normalized.adjusted() + 1)
    decimal_places = max(0, -normalized.as_tuple().exponent)
    return integer_digits <= precision - scale and decimal_places <= scale


class ExactDecimal(UserDefinedType):
    cache_ok = True
    _prefix = "orbis-decimal:"

    def __init__(self, precision: int, scale: int):
        self.precision = precision
        self.scale = scale

    def get_col_spec(self, **_kwargs) -> str:
        return f"NUMERIC({self.precision}, {self.scale})"

    def bind_processor(self, dialect):
        def process(value):
            if value is None:
                return None
            number = Decimal(str(value))
            if not fits_exact_decimal(number, self.precision, self.scale):
                raise ValueError(
                    f"Importe fuera de NUMERIC({self.precision}, {self.scale})"
                )
            if dialect.name == "sqlite":
                return self._prefix + format(number, "f")
            return number

        return process

    def result_processor(self, dialect, _coltype):
        def process(value):
            if value is None:
                return None
            if isinstance(value, str) and value.startswith(self._prefix):
                return Decimal(value[len(self._prefix):])
            if dialect.name != "sqlite":
                return Decimal(str(value))
            # Compatibilidad con las filas NUMERIC heredadas. La conversión a
            # REAL pudo perder dígitos antes de llegar a este lector.
            with localcontext() as context:
                context.prec = max(80, self.precision + self.scale + 10)
                quantum = Decimal(1).scaleb(-self.scale)
                return Decimal(str(value)).quantize(quantum)

        return process
