"""Libro de unidades propias de un bot, reconstruible desde fills REAL.

No recibe saldos iniciales del exchange ni cartera PAPER. El coordinador debe
aportar únicamente fills propios, finales, asentados y conciliados. Esta capa
pura conserva comisiones en base/quote y rechaza que el bot venda activos que
no adquirió.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Literal
from uuid import UUID

from app.live_execution.policy import _derived_money, _money, _utc
from app.live_execution.spot_bot_plan import SpotBotPlanError, SpotBotPosition


@dataclass(frozen=True, slots=True)
class OwnedSpotFill:
    fill_id: str
    intent_id: str
    side: Literal["BUY", "SELL"]
    quantity_base: Decimal
    gross_quote: Decimal
    fee: Decimal
    fee_currency: Literal["BASE", "QUOTE"]
    occurred_at: datetime
    entry_candle_t: int | None = None

    def __post_init__(self):
        if not isinstance(self.fill_id, str) or not self.fill_id or len(self.fill_id) > 128:
            raise SpotBotPlanError("Fill propio sin identificador")
        try:
            UUID(self.intent_id)
        except (ValueError, TypeError, AttributeError):
            raise SpotBotPlanError("Fill propio sin intención verificable") from None
        if self.side not in ("BUY", "SELL") or self.fee_currency not in ("BASE", "QUOTE"):
            raise SpotBotPlanError("Dirección o comisión del fill propio inválida")
        _money(self.quantity_base, "quantity_base", positive=True)
        _money(self.gross_quote, "gross_quote", positive=True)
        _money(self.fee, "fee")
        if self.side == "BUY" and type(self.entry_candle_t) is not int:
            raise SpotBotPlanError("Compra propia sin vela de entrada")
        object.__setattr__(self, "occurred_at", _utc(self.occurred_at, "occurred_at"))


@dataclass(frozen=True, slots=True)
class AssignedSpotSale:
    """An exact share of a verified manual sale, separate from orders of the bot."""
    fill_id: str
    occurred_at: datetime
    consumed_units: Decimal
    fill_units: Decimal
    net_quote: Decimal

    def __post_init__(self):
        _derived_money(self.consumed_units, "consumed_units")
        _derived_money(self.fill_units, "fill_units")
        _derived_money(self.net_quote, "net_quote")
        if not 0 < self.consumed_units <= self.fill_units:
            raise SpotBotPlanError("Las unidades atribuidas están fuera del intervalo de la venta")
        object.__setattr__(self, "occurred_at", _utc(self.occurred_at, "occurred_at"))


@dataclass(frozen=True, slots=True)
class SpotBotLedger:
    position: SpotBotPosition
    net_cash_flow_quote: Decimal
    realized_pnl_quote: Decimal
    fill_count: int
    manual_sale_count: int = 0


def fold_owned_spot_fills(fills: list[OwnedSpotFill | AssignedSpotSale], *,
                         initial: SpotBotLedger | None = None) -> SpotBotLedger:
    """Promedio ponderado sólo del bot; orden estable de fills y cero duplicados.

    Una venta consume también cualquier comisión en base. Con comisión en
    quote disminuyen sus ingresos. El coste de una compra incluye comisión
    quote o se reparte sobre menos unidades si se cobra en base. Un cierre
    parcial conserva exactamente el coste restante, sin usar coma flotante.
    """
    seen = set()
    previous = None
    units = cost = net_cash = pnl = Decimal(0)
    entry_t = entry_intent = None
    count = manual_count = 0
    if initial is not None:
        units, cost = initial.position.units, initial.position.cost_quote
        entry_t, entry_intent = initial.position.entry_candle_t, initial.position.last_entry_intent_id
        net_cash, pnl, count = initial.net_cash_flow_quote, initial.realized_pnl_quote, initial.fill_count
        manual_count = initial.manual_sale_count
    with localcontext() as context:
        context.prec = 300
        for fill in fills:
            if not isinstance(fill, (OwnedSpotFill, AssignedSpotSale)) or fill.fill_id in seen:
                raise SpotBotPlanError("Los fills propios están duplicados o no son válidos")
            if previous is not None and fill.occurred_at < previous:
                raise SpotBotPlanError("Los fills propios no están ordenados")
            seen.add(fill.fill_id)
            previous = fill.occurred_at
            if isinstance(fill, AssignedSpotSale):
                manual_count += 1
                consumed = fill.consumed_units
                proceeds = (fill.net_quote if consumed == fill.fill_units
                            else fill.net_quote * consumed / fill.fill_units)
            else:
                count += 1
                base_fee = fill.fee if fill.fee_currency == "BASE" else Decimal(0)
                quote_fee = fill.fee if fill.fee_currency == "QUOTE" else Decimal(0)
                if fill.side == "BUY":
                    acquired = fill.quantity_base - base_fee
                    if acquired <= 0:
                        raise SpotBotPlanError("La comisión consume toda la compra del bot")
                    if units == 0 or fill.intent_id != entry_intent:
                        # El planificador ya autorizó agregar polvo a otra compra.
                        entry_t, entry_intent = fill.entry_candle_t, fill.intent_id
                    units += acquired
                    cost += fill.gross_quote + quote_fee
                    net_cash -= fill.gross_quote + quote_fee
                    continue
                consumed = fill.quantity_base + base_fee
                proceeds = fill.gross_quote - quote_fee
            if consumed > units or proceeds < 0:
                raise SpotBotPlanError("La venta excede las unidades propias o sus ingresos")
            allocated = cost if consumed == units else cost * consumed / units
            units -= consumed
            cost -= allocated
            pnl += proceeds - allocated
            net_cash += proceeds
            if units == 0:
                cost, entry_t, entry_intent = Decimal(0), None, None
    return SpotBotLedger(SpotBotPosition(units, cost, entry_t, entry_intent), net_cash, pnl, count, manual_count)
