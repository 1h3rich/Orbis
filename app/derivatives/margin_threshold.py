"""Umbral teórico de mantenimiento aislado; nunca precio de liquidación.

Se resuelve equity(mark) = mantenimiento(mark) en cada tramo publicado. La
cotización, las reglas, el colateral y la financiación asentada son una foto;
funding futuro, cambios de reglas, gaps y liquidación del venue no se predicen.
"""

from __future__ import annotations

from decimal import Decimal, localcontext

from app.derivatives.isolated import IsolatedPerpPosition
from app.derivatives.paper_math import MarginRules, PaperPerpError


def estimate_maintenance_threshold(position: IsolatedPerpPosition,
                                   rules: MarginRules,
                                   observed_mark: Decimal) -> Decimal | None:
    """Devuelve el primer cruce adverso válido de un tramo, o None.

No extrapola a través de una discontinuidad de brackets. ``None`` significa
que no hay solución verificable con este modelo, no ausencia de riesgo.
    """
    if position.instrument != rules.instrument:
        raise PaperPerpError("Posición y reglas de margen de instrumentos distintos")
    position.unrealized_pnl(observed_mark)
    with localcontext() as context:
        context.prec = 80
        for previous, current in zip(rules.tiers, rules.tiers[1:]):
            # Sin continuidad no hay un umbral único interpretable al pasar
            # entre tramos; la alarma en la marca actual sigue disponible.
            at_boundary = current.floor
            before = (at_boundary * previous.maintenance_rate
                      - previous.cumulative_maintenance_amount)
            after = (at_boundary * current.maintenance_rate
                     - current.cumulative_maintenance_amount)
            if previous.cap != at_boundary or before != after:
                return None
        notional = abs(position.size) * observed_mark
        if position.equity(observed_mark) <= rules.maintenance_requirement(notional):
            return None
        candidates: list[Decimal] = []
        for tier in rules.tiers:
            denominator = position.size - abs(position.size) * tier.maintenance_rate
            if denominator == 0:
                continue
            numerator = (position.size * position.entry_price + position.entry_fee
                         + position.funding_paid - position.collateral
                         - tier.cumulative_maintenance_amount)
            mark = numerator / denominator
            if mark <= 0:
                continue
            candidate_notional = abs(position.size) * mark
            if candidate_notional < tier.floor or (tier.cap is not None
                                                  and candidate_notional >= tier.cap):
                continue
            if position.size > 0 and mark < observed_mark:
                candidates.append(mark)
            elif position.size < 0 and mark > observed_mark:
                candidates.append(mark)
        if not candidates:
            return None
        return max(candidates) if position.size > 0 else min(candidates)
