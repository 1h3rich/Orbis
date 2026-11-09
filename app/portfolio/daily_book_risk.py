"""Riesgo diario derivado del libro REAL, nunca de una lectura completa de venue.

Esta pieza alimentará una futura conciliación MEXC, pero por sí sola no
autoriza ni construye ``LiveSpotState.reconciled=True``. Excluye aportaciones,
retiros y traslados del volumen negociado y del P/L realizado.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from decimal import Decimal, ROUND_HALF_EVEN, localcontext

from app.portfolio.domain import (
    CALC_PRECISION, PortfolioEvent, PortfolioError, fold_events, utc_datetime,
)


@dataclass(frozen=True, slots=True)
class DailyBookRisk:
    account_id: str
    quote_currency: str
    day_start: datetime
    calculated_at: datetime
    trade_count: int
    turnover_quote: Decimal
    realized_pnl_quote: Decimal
    base_asset: str | None = None
    position_quantity_base: Decimal | None = None
    position_cost_basis_quote: Decimal | None = None
    average_cost_quote_per_base: Decimal | None = None
    source: str = "REAL_BOOK_ONLY"
    reconciled_with_venue: bool = False
    execution_ready: bool = False


def calculate_daily_book_risk(account_id: str, events: list[PortfolioEvent],
                              quote_currency: str, *, now: datetime,
                              base_asset: str | None = None) -> DailyBookRisk:
    """Reproduce coste medio hasta ``now`` y resta el P/L acumulado previo al día UTC.

    Un evento retroactivo anterior al día puede cambiar el coste de una venta
    de hoy; por eso no se suman aisladamente resultados por fila.
    """
    current = utc_datetime(now)
    if (not isinstance(account_id, str) or not account_id
            or not isinstance(quote_currency, str) or not 2 <= len(quote_currency) <= 12
            or not quote_currency.isascii() or not quote_currency.isalnum()
            or quote_currency != quote_currency.upper()
            or not isinstance(events, list)
            or any(not isinstance(event, PortfolioEvent) for event in events)):
        raise PortfolioError("Solicitud de riesgo diario inválida")
    if (base_asset is not None and (not isinstance(base_asset, str)
            or not 2 <= len(base_asset) <= 12 or not base_asset.isascii()
            or not base_asset.isalnum() or base_asset != base_asset.upper()
            or base_asset == quote_currency)):
        raise PortfolioError("Activo base del riesgo diario inválido")
    day_start = datetime.combine(current.date(), time.min, tzinfo=timezone.utc)
    if any(event.account_id != account_id for event in events):
        raise PortfolioError("El diario contiene otra cuenta REAL")
    preceding = [event for event in events if event.occurred_at < day_start]
    through_now = [event for event in events if event.occurred_at <= current]
    before = fold_events(account_id, preceding)
    after = fold_events(account_id, through_now)
    with localcontext() as context:
        context.prec = CALC_PRECISION
        if any(event.accounting is not None for event in events):
            context.rounding = ROUND_HALF_EVEN
        before_pnl = sum((position.realized_pnl for position in before.positions.values()
                          if position.quote_currency == quote_currency), Decimal(0))
        after_pnl = sum((position.realized_pnl for position in after.positions.values()
                         if position.quote_currency == quote_currency), Decimal(0))
        trades = [event for event in through_now
                  if event.occurred_at >= day_start
                  and event.financial_input.kind in ("BUY", "SELL")
                  and event.financial_input.quote_currency == quote_currency]
        turnover = sum((event.financial_input.gross_amount for event in trades), Decimal(0))
        pnl = after_pnl - before_pnl
        position = after.positions.get((base_asset, quote_currency)) if base_asset else None
        quantity = position.quantity if position else Decimal(0)
        cost = position.cost_basis if position else Decimal(0)
        # Cero unidades no implica coste medio cero: no existe una posición
        # cuyo coste pueda respaldar una futura venta.
        average = position.average_cost if position and quantity > 0 else None
    return DailyBookRisk(
        account_id, quote_currency, day_start, current,
        len(trades), turnover, pnl,
        base_asset=base_asset,
        position_quantity_base=quantity if base_asset else None,
        position_cost_basis_quote=cost if base_asset else None,
        average_cost_quote_per_base=average if base_asset else None,
    )
