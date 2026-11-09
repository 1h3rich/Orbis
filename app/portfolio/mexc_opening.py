"""Inventario inicial manual desde una lectura MEXC View, nunca conciliación.

La cantidad procede del exchange; el coste histórico lo declara el usuario.
No se inventan compras, fecha de adquisición, P/L ni permisos de trading.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from uuid import NAMESPACE_URL, uuid5

from app.portfolio.domain import AccountSnapshot, EventInput, PortfolioError, _symbol
from app.portfolio.reconciliation import compare_mexc_spot_inventory

OPENING_QUOTES = frozenset(("USDT", "USDC", "EUR", "USD", "DAI", "FDUSD"))


def prepare_mexc_opening(account_id: str, observed: dict, *, quote_currency: str,
                         costs: dict[str, str], occurred_at: datetime | None = None,
                         now: datetime | None = None) -> dict:
    """Valida una fotografía completa y prepara todos sus asientos de apertura.

    `costs` debe contener exactamente los activos no monetarios con saldo
    positivo. Un valor cero es una declaración explícita, no un coste omitido.
    """
    quote = _symbol(quote_currency, "quote currency")
    if quote not in OPENING_QUOTES:
        raise PortfolioError("El saldo inicial requiere una divisa de referencia admitida")
    if not isinstance(costs, dict) or len(costs) > 200:
        raise PortfolioError("Costes iniciales MEXC inválidos")
    comparison = compare_mexc_spot_inventory(AccountSnapshot(account_id), observed, now=now)
    observation_time = datetime.fromisoformat(comparison["observed_at"]).astimezone(timezone.utc)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if observation_time > current:
        raise PortfolioError("La observación MEXC no puede ser futura")
    event_time = occurred_at or observation_time
    if not isinstance(event_time, datetime) or event_time.tzinfo is None:
        raise PortfolioError("Fecha de apertura MEXC inválida")
    event_time = event_time.astimezone(timezone.utc)
    if event_time > observation_time or (observation_time - event_time).total_seconds() > 120:
        raise PortfolioError("La vista previa MEXC caducó; vuelve a revisarla")
    balances = {row["asset"]: Decimal(row["mexc_spot_quantity"])
                for row in comparison["assets"] if Decimal(row["mexc_spot_quantity"]) > 0}
    if not balances:
        raise PortfolioError("MEXC no muestra saldos Spot positivos para abrir")
    expected_cost_assets = set(balances) - {quote}
    if set(costs) != expected_cost_assets:
        raise PortfolioError("Declara el coste de cada activo MEXC no monetario, sin omisiones")

    lines = []
    entries: list[tuple[str, EventInput]] = []
    for asset, quantity in sorted(balances.items()):
        if len(asset) > 12:
            raise PortfolioError("Un símbolo MEXC no cabe en el libro REAL; no se importó nada")
        request_id = str(uuid5(NAMESPACE_URL, f"orbis:mexc-opening:{account_id}:{event_time.isoformat()}:{asset}"))
        if asset == quote:
            item = EventInput("CONTRIBUTION", quote, event_time, amount=quantity,
                              note="Saldo inicial MEXC Spot observado; no representa beneficio")
            lines.append({"asset": asset, "quantity": format(quantity, "f"),
                          "kind": "OPENING_CASH", "declared_unit_cost": None,
                          "declared_total_cost": None})
        else:
            raw_cost = costs[asset]
            if not isinstance(raw_cost, str) or not 1 <= len(raw_cost) <= 100:
                raise PortfolioError("Coste inicial MEXC inválido")
            try:
                unit_cost = Decimal(raw_cost)
            except InvalidOperation:
                raise PortfolioError("Coste inicial MEXC no decimal") from None
            item = EventInput("ADJUSTMENT_IN", quote, event_time, asset=asset,
                              quantity=quantity, unit_price=unit_cost,
                              note="Inventario inicial MEXC Spot; coste declarado, no compra verificada")
            with localcontext() as context:
                context.prec = 300
                total = quantity * unit_cost
            lines.append({"asset": asset, "quantity": format(quantity, "f"),
                          "kind": "OPENING_ASSET", "declared_unit_cost": format(unit_cost, "f"),
                          "declared_total_cost": format(total, "f")})
        entries.append((request_id, item))
    return {"account_id": account_id, "quote_currency": quote,
            "observed_at": observation_time.isoformat(), "occurred_at": event_time.isoformat(),
            "lines": lines, "entries": entries,
            "accounting": "OPENING_INVENTORY_WITH_USER_DECLARED_COST_NOT_RECONCILED"}
