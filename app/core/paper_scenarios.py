from datetime import datetime, timezone
from decimal import Decimal, localcontext

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database.models import PaperScenario
from app.ledger.repository import get_net_cash_moved, get_total_spent


def get_paper_scenario(db: Session, quote_currency: str) -> PaperScenario | None:
    """
    Devuelve el escenario PAPER **abierto más reciente** de la moneda.

    Las sesiones cerradas quedan en el historial (``GET /paper/sessions``) y
    dejan de aceptar operaciones.
    """
    return (
        db.query(PaperScenario)
        .filter(
            PaperScenario.quote_currency == quote_currency,
            PaperScenario.closed_at.is_(None),
        )
        .order_by(PaperScenario.generation.desc(), PaperScenario.id.desc())
        .first()
    )


def get_paper_scenarios(
    db: Session,
    open_only: bool = True,
    quote_currency: str | None = None,
) -> list[PaperScenario]:
    """Lista los escenarios PAPER (por defecto solo los abiertos) para la interfaz."""
    query = db.query(PaperScenario)

    if open_only:
        query = query.filter(PaperScenario.closed_at.is_(None))
    if quote_currency is not None:
        query = query.filter(PaperScenario.quote_currency == quote_currency)

    return query.order_by(
        PaperScenario.quote_currency.asc(),
        PaperScenario.generation.desc(),
        PaperScenario.id.desc(),
    ).all()


def next_generation(db: Session, quote_currency: str) -> int:
    """Número de sesión siguiente para la moneda (1 si es la primera)."""
    current = (
        db.query(func.max(PaperScenario.generation))
        .filter(PaperScenario.quote_currency == quote_currency)
        .scalar()
    )
    return int(current or 0) + 1


def create_paper_scenario(
    db: Session,
    quote_currency: str,
    initial_balance: Decimal,
    max_order_amount: Decimal,
    max_fee_percentage: Decimal,
    daily_limit: Decimal | None,
    monthly_limit: Decimal | None,
) -> PaperScenario:
    """Abre una sesión PAPER; el capital inicial queda fijo para esa sesión."""
    scenario = PaperScenario(
        quote_currency=quote_currency,
        generation=next_generation(db, quote_currency),
        initial_balance=initial_balance,
        max_order_amount=max_order_amount,
        max_fee_percentage=max_fee_percentage,
        daily_limit=daily_limit,
        monthly_limit=monthly_limit,
    )
    db.add(scenario)
    db.commit()
    db.refresh(scenario)
    return scenario


def close_paper_scenario(db: Session, scenario: PaperScenario) -> PaperScenario:
    """Cierra una sesión; sus operaciones siguen visibles en el historial."""
    if scenario.closed_at is None:
        scenario.closed_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(scenario)
    return scenario


def paper_scenario_snapshot(db: Session, scenario: PaperScenario) -> dict:
    """
    Devuelve la configuración junto con lo gastado y el saldo restante.

    * ``total_spent`` / ``available_balance``: solo compras (semántica
      histórica, no cambia).
    * ``cash_balance``: caja real de la sesión ``initial − ΣBUY + ΣSELL``; es
      la que usa Guard como presupuesto disponible.
    """
    spent = get_total_spent(
        db,
        mode="PAPER",
        quote_currency=scenario.quote_currency,
        since=scenario.created_at,
        until=scenario.closed_at,
    )
    net_cash = get_net_cash_moved(
        db,
        mode="PAPER",
        quote_currency=scenario.quote_currency,
        since=scenario.created_at,
        until=scenario.closed_at,
    )
    with localcontext() as context:
        context.prec = 80
        cash = scenario.initial_balance + net_cash
        available_balance = scenario.initial_balance - spent

    return {
        "quote_currency": scenario.quote_currency,
        "generation": scenario.generation,
        "initial_balance": scenario.initial_balance,
        "total_spent": spent,
        "available_balance": available_balance,
        "cash_balance": cash,
        "max_order_amount": scenario.max_order_amount,
        "max_fee_percentage": scenario.max_fee_percentage,
        "daily_limit": scenario.daily_limit,
        "monthly_limit": scenario.monthly_limit,
        "created_at": scenario.created_at,
        "closed_at": scenario.closed_at,
        "is_open": scenario.closed_at is None,
    }
