"""Posiciones PAPER calculadas con ``Decimal`` exacto.

Un *fold* (pliegue) es el tramo vivo de una posición: desde que se abre con una
compra hasta que se cierra al vender todas las unidades. Las reglas son:

* ``BUY``: ``quantity += asset_received``, ``invested += amount_spent``
  (importe bruto, comisión incluida) y ``average_cost = invested / quantity``.
* ``SELL``: el precio medio **no cambia** en una venta parcial; el coste vivo
  baja ``average_cost * quantity_sold`` y el realizado sube
  ``cash_credited - average_cost * quantity_sold``. El efectivo acreditado
  ya es neto de comisión y redondeo: no se descuenta dos veces.
* Al cerrar el tramo (``quantity == 0``) el residuo de redondeo que quede en
  ``invested`` se reconoce en ``realized_pnl`` para que
  ``equity - initial == realized + unrealized`` sea exacto.

El orden de aplicación es ``(timestamp, id)``: el Ledger puede recibir
operaciones con el mismo instante y el ``id`` rompe el empate de forma
determinista.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, localcontext

from app.math.calculations import calculate_average_cost


def _to_decimal(value) -> Decimal:
    """Convierte un valor almacenado a ``Decimal`` sin pasar por float."""
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def _sort_key(operation):
    """Orden total ``(timestamp, id)`` tolerando tz mezclados o ausentes."""
    timestamp = getattr(operation, "timestamp", None)
    if isinstance(timestamp, datetime):
        if timestamp.tzinfo is not None:
            timestamp = timestamp.astimezone(timezone.utc).replace(tzinfo=None)
    else:
        timestamp = datetime.min
    return (timestamp, getattr(operation, "id", 0) or 0)


@dataclass
class Position:
    """Estado de una posición (activo, moneda) dentro de una sesión PAPER."""

    asset: str
    quote_currency: str
    quantity: Decimal = field(default_factory=lambda: Decimal("0"))
    invested: Decimal = field(default_factory=lambda: Decimal("0"))
    average_cost: Decimal = field(default_factory=lambda: Decimal("0"))
    realized_pnl: Decimal = field(default_factory=lambda: Decimal("0"))
    fees: Decimal = field(default_factory=lambda: Decimal("0"))

    @property
    def is_open(self) -> bool:
        """Solo las posiciones con unidades vivas entran en la cartera."""
        return self.quantity > 0


def _fees(operation) -> Decimal:
    with localcontext() as context:
        context.prec = 80
        return (
            _to_decimal(getattr(operation, "trading_fee", 0))
            + _to_decimal(getattr(operation, "withdrawal_fee", 0))
            + _to_decimal(getattr(operation, "network_fee", 0))
        )


def apply_operation(position: Position, operation) -> Position:
    """Aplica una operación BUY/SELL a una posición (función pura)."""
    with localcontext() as context:
        context.prec = 80
        return _apply_operation(position, operation)


def _apply_operation(position: Position, operation) -> Position:
    operation_type = operation.operation_type

    if operation_type == "BUY":
        units = _to_decimal(operation.asset_received)
        if position.quantity <= 0:
            # Nuevo pliegge: no se arrastra coste de un tramo cerrado.
            position.invested = Decimal("0")
        position.quantity += units
        position.invested += _to_decimal(operation.amount_spent)
        position.average_cost = calculate_average_cost(
            position.invested,
            position.quantity,
        )
        position.fees += _fees(operation)
        return position

    if operation_type == "SELL":
        units = _to_decimal(operation.asset_received)
        fee = _fees(operation)
        position.realized_pnl += (
            _to_decimal(operation.amount_spent) - position.average_cost * units
        )
        position.invested -= position.average_cost * units
        position.quantity -= units
        position.fees += fee

        if position.quantity == 0:
            # Cierre del pliegue: el residuo de redondeo es P&L realizado.
            position.realized_pnl -= position.invested
            position.invested = Decimal("0")
        return position

    return position


def build_positions(
    operations,
    mode: str = "PAPER",
    quote_currency: str | None = None,
) -> list[Position]:
    """
    Reconstruye las posiciones de un historial de operaciones.

    Solo se consideran operaciones ``EXECUTED`` del modo indicado con tipo
    ``BUY``/``SELL``, aplicadas en orden ``(timestamp, id)``. Se devuelven
    todas las posiciones (también cerradas) para que el resumen pueda sumar
    comisiones y P&L realizado; el filtro de vivas lo hace el caller con
    ``position.is_open``.
    """
    selected = []
    for operation in operations:
        if getattr(operation, "mode", mode) != mode:
            continue
        if getattr(operation, "status", "EXECUTED") != "EXECUTED":
            continue
        if operation.operation_type not in ("BUY", "SELL"):
            continue
        if quote_currency is not None and operation.quote_currency != quote_currency:
            continue
        selected.append(operation)

    selected.sort(key=_sort_key)

    positions: dict[tuple[str, str], Position] = {}
    for operation in selected:
        key = (operation.asset, operation.quote_currency)
        position = positions.get(key)
        if position is None:
            position = Position(
                asset=operation.asset,
                quote_currency=operation.quote_currency,
            )
            positions[key] = position
        apply_operation(position, operation)

    return [positions[key] for key in sorted(positions)]


def position_by_asset(positions: list[Position]) -> dict[str, Position]:
    """Índice de posiciones por activo para consultas rápidas."""
    return {position.asset: position for position in positions}


def operation_pnl(operations, mode: str = "PAPER") -> dict:
    """Anota el PnL realizado de cada operación reutilizando el motor.

    Recorre el historial en orden ``(timestamp, id)`` aplicando
    ``apply_operation`` sobre ``Position`` por par ``(asset, quote)`` —la
    misma lógica de coste medio que usa ``build_positions`` (prohibido
    duplicarla aquí)— y devuelve ``{id: anotación}`` con todo en
    ``Decimal``:

    * ``BUY``: ``cost_basis`` = coste medio resultante, ``proceeds`` = 0,
      ``fees`` = comisiones de la operación, ``realized_pnl`` = 0,
      ``pnl_pct`` = 0 (la compra no realiza resultado).
    * ``SELL``: ``cost_basis`` = coste medio previo aplicado a lo vendido,
      ``proceeds`` = ``price * quantity`` (bruto, sin descontar comisión),
      ``fees`` = comisiones de la operación, ``realized_pnl`` = incremento
      de ``Position.realized_pnl`` al aplicar la venta
      (``cash_credited - average_cost * quantity``, más el residuo de
      redondeo si cierra el pliegue) y ``pnl_pct`` =
      ``realized_pnl / (cost_basis * quantity) * 100`` (0 si no hay coste).
    * Operaciones no ``EXECUTED``, de otro modo o que no sean ``BUY``/``SELL``:
      no tocan la posición (igual que en ``build_positions``) y se anotan a
      cero salvo ``fees``.

    Por construcción telescópica, ``Σ realized_pnl == Σ Position.realized_pnl``
    de ``build_positions`` sobre el mismo historial y modo.
    """
    operations = list(operations)
    integer_digits = 0
    scale = 0
    for operation in operations:
        for name in ("amount_spent", "asset_received", "price",
                     "trading_fee", "withdrawal_fee", "network_fee"):
            value = _to_decimal(getattr(operation, name, 0))
            integer_digits = max(integer_digits, value.adjusted() + 1)
            scale = max(scale, -value.as_tuple().exponent)
    with localcontext() as context:
        context.prec = 80 if integer_digits + scale + 2 > 28 else 28
        return _operation_pnl(operations, mode)


def _operation_pnl(operations, mode: str) -> dict:
    ordered = sorted(operations, key=_sort_key)

    positions: dict[tuple[str, str], Position] = {}
    annotated: dict = {}
    for operation in ordered:
        key = (operation.asset, operation.quote_currency)
        position = positions.get(key)
        if position is None:
            position = Position(
                asset=operation.asset,
                quote_currency=operation.quote_currency,
            )
            positions[key] = position

        fees = _fees(operation)
        if (
            getattr(operation, "mode", mode) != mode
            or getattr(operation, "status", "EXECUTED") != "EXECUTED"
            or operation.operation_type not in ("BUY", "SELL")
        ):
            annotated[operation.id] = {
                "cost_basis": Decimal("0"),
                "proceeds": Decimal("0"),
                "fees": fees,
                "realized_pnl": Decimal("0"),
                "pnl_pct": Decimal("0"),
            }
            continue

        if operation.operation_type == "BUY":
            apply_operation(position, operation)
            annotated[operation.id] = {
                "cost_basis": position.average_cost,
                "proceeds": Decimal("0"),
                "fees": fees,
                "realized_pnl": Decimal("0"),
                "pnl_pct": Decimal("0"),
            }
            continue

        average_before = position.average_cost
        realized_before = position.realized_pnl
        units = _to_decimal(operation.asset_received)
        price = _to_decimal(operation.price)
        apply_operation(position, operation)
        realized = position.realized_pnl - realized_before
        cost = average_before * units
        annotated[operation.id] = {
            "cost_basis": average_before,
            "proceeds": price * units,
            "fees": fees,
            "realized_pnl": realized,
            "pnl_pct": (
                realized / cost * Decimal("100")
                if cost > 0
                else Decimal("0")
            ),
        }

    return annotated
