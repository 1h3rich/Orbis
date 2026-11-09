"""Cartera PAPER: cruza el Ledger con los precios de valoración (I/O + precios).

Todo el cálculo es ``Decimal`` exacto y sale del Ledger; los precios actuales
pueden venir del proveedor configurado en ``OrbisConfig.price_provider``, y si
no hay cotización publicada se recurre al último precio negociado en la sesión
y, en último término, al coste medio (valoración a coste).
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext

from sqlalchemy.orm import Session

from app.core.config import get_orbis_config
from app.core.paper_scenarios import paper_scenario_snapshot
from app.database.models import PaperScenario
from app.ledger.repository import get_net_cash_moved, list_paper_operations
from app.math.positions import build_positions
from app.pricing import create_price_provider


def _last_trade_prices(operations) -> dict[str, Decimal]:
    """Último precio negociado por activo; las operaciones vienen ordenadas."""
    prices: dict[str, Decimal] = {}
    for operation in operations:
        prices[operation.asset] = Decimal(str(operation.price))
    return prices


def _last_trade_times(operations) -> dict[str, str]:
    """Fecha de la ejecución usada como referencia, no de una cotización."""
    times: dict[str, str] = {}
    for operation in operations:
        timestamp = operation.timestamp
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        times[operation.asset] = timestamp.isoformat()
    return times


def _decimal_price(value) -> Decimal | None:
    if value is None:
        return None
    try:
        price = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return price if price.is_finite() and price > 0 else None


def resolve_price_with_meta(
    provider,
    asset: str,
    quote_currency: str,
    fallbacks: dict[str, Decimal],
    average_cost: Decimal,
    last_trade_as_of: str | None = None,
) -> tuple[Decimal, dict]:
    """Valora una posición y revela si su referencia es vigente o aproximada."""
    if provider is not None:
        meta_method = getattr(provider, "get_price_with_meta", None)
        if callable(meta_method):
            try:
                meta = meta_method(asset, quote_currency)
            except Exception:
                meta = None
            if isinstance(meta, dict):
                price = _decimal_price(meta.get("price"))
                if price is not None:
                    return price, {
                        "price_source": str(meta.get("source") or "provider"),
                        "price_as_of": meta.get("as_of"),
                        "price_age_seconds": meta.get("age_seconds"),
                        "price_stale": bool(meta.get("stale", True)),
                    }
        else:
            try:
                price = _decimal_price(provider.get_price(asset, quote_currency))
            except Exception:
                price = None
            if price is not None:
                # Sin metadatos no se puede afirmar que el precio sea reciente.
                return price, {
                    "price_source": "provider",
                    "price_as_of": None,
                    "price_age_seconds": None,
                    "price_stale": True,
                }

    price = _decimal_price(fallbacks.get(asset))
    if price is not None:
        age_seconds = None
        if last_trade_as_of is not None:
            age_seconds = max(
                0.0,
                (datetime.now(timezone.utc) - datetime.fromisoformat(last_trade_as_of)).total_seconds(),
            )
        return price, {
            "price_source": "last_trade",
            "price_as_of": last_trade_as_of,
            "price_age_seconds": age_seconds,
            "price_stale": True,
        }
    price = _decimal_price(average_cost)
    if price is not None:
        return price, {
            "price_source": "average_cost",
            "price_as_of": None,
            "price_age_seconds": None,
            "price_stale": True,
        }
    raise ValueError(f"No positive valuation price available for {asset}/{quote_currency}")


def resolve_price(
    provider,
    asset: str,
    quote_currency: str,
    fallbacks: dict[str, Decimal],
    average_cost: Decimal,
) -> Decimal | None:
    """Interfaz de precio mantenida para consumidores que no leen metadatos."""
    try:
        price, _meta = resolve_price_with_meta(
            provider, asset, quote_currency, fallbacks, average_cost
        )
        return price
    except ValueError:
        return None


def build_paper_portfolio(
    db: Session,
    scenario: PaperScenario,
    price_provider=None,
) -> dict:
    """Calcula el saldo, las posiciones y el P/L de una sesión PAPER."""
    with localcontext() as context:
        context.prec = 80
        return _build_paper_portfolio(db, scenario, price_provider)


def _build_paper_portfolio(db: Session, scenario: PaperScenario, price_provider) -> dict:

    snapshot = paper_scenario_snapshot(db, scenario)
    operations = list_paper_operations(
        db,
        quote_currency=scenario.quote_currency,
        start=scenario.created_at,
        end=scenario.closed_at,
    )

    provider = price_provider
    if provider is None:
        provider = create_price_provider(get_orbis_config().price_provider, db)

    fallbacks = _last_trade_prices(operations)
    last_trade_times = _last_trade_times(operations)
    positions = build_positions(operations, mode="PAPER")

    entries = []
    positions_value = Decimal("0")
    realized_pnl = Decimal("0")
    unrealized_pnl = Decimal("0")
    total_fees = Decimal("0")

    for position in positions:
        total_fees += position.fees
        realized_pnl += position.realized_pnl

        if not position.is_open:
            continue

        current_price, price_meta = resolve_price_with_meta(
            provider,
            position.asset,
            position.quote_currency,
            fallbacks,
            position.average_cost,
            last_trade_times.get(position.asset),
        )
        value = position.quantity * current_price
        position_unrealized = value - position.invested
        positions_value += value
        unrealized_pnl += position_unrealized

        entries.append(
            {
                "asset": position.asset,
                "quote_currency": position.quote_currency,
                "quantity": position.quantity,
                "average_cost": position.average_cost,
                "invested": position.invested,
                "current_price": current_price,
                **price_meta,
                "value": value,
                "unrealized_pnl": position_unrealized,
                "realized_pnl": position.realized_pnl,
                "return_percentage": (
                    (position_unrealized / position.invested * Decimal("100"))
                    if position.invested > 0
                    else Decimal("0")
                ),
                "fees": position.fees,
            }
        )

    cash_balance = snapshot["cash_balance"]
    equity = cash_balance + positions_value
    total_pnl = realized_pnl + unrealized_pnl
    initial_balance = scenario.initial_balance

    hundred = Decimal("100")
    if equity > 0:
        cash_weight = cash_balance / equity * hundred
    else:
        cash_weight = Decimal("0")
    for entry in entries:
        if equity > 0:
            entry["weight_percentage"] = entry["value"] / equity * hundred
        else:
            entry["weight_percentage"] = Decimal("0")

    return {
        "quote_currency": scenario.quote_currency,
        "initial_balance": initial_balance,
        "cash_balance": cash_balance,
        "cash_weight": cash_weight,
        "positions_value": positions_value,
        "equity": equity,
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "total_pnl": total_pnl,
        "return_percentage": (
            (total_pnl / initial_balance * Decimal("100"))
            if initial_balance > 0
            else Decimal("0")
        ),
        "total_fees": total_fees,
        "valuation_status": (
            "CASH_ONLY" if not entries else
            "ESTIMATED" if any(entry["price_stale"] for entry in entries) else
            "CURRENT_REFERENCE"
        ),
        "positions": entries,
    }
