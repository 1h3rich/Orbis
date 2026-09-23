from decimal import Decimal

from sqlalchemy.orm import Session

from app.database.models import PaperScenario
from app.ledger.repository import get_total_spent


def get_paper_scenario(db: Session, quote_currency: str) -> PaperScenario | None:
    """Busca la configuración PAPER de la moneda cotizada indicada."""
    return db.query(PaperScenario).filter(
        PaperScenario.quote_currency == quote_currency
    ).first()


def get_paper_scenarios(db: Session) -> list[PaperScenario]:
    """Lista los escenarios PAPER ordenados por moneda para la interfaz."""
    return db.query(PaperScenario).order_by(PaperScenario.quote_currency).all()


def create_paper_scenario(
    db: Session,
    quote_currency: str,
    initial_balance: Decimal,
    max_order_amount: Decimal,
    max_fee_percentage: Decimal,
    daily_limit: Decimal | None,
    monthly_limit: Decimal | None,
) -> PaperScenario:
    """Crea un escenario una sola vez; el capital inicial queda fijo."""
    scenario = PaperScenario(
        quote_currency=quote_currency,
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


def paper_scenario_snapshot(db: Session, scenario: PaperScenario) -> dict:
    """Devuelve la configuración junto con lo gastado y el saldo restante."""
    spent = get_total_spent(
        db,
        mode="PAPER",
        quote_currency=scenario.quote_currency,
        since=scenario.created_at,
    )
    return {
        "quote_currency": scenario.quote_currency,
        "initial_balance": scenario.initial_balance,
        "total_spent": spent,
        "available_balance": scenario.initial_balance - spent,
        "max_order_amount": scenario.max_order_amount,
        "max_fee_percentage": scenario.max_fee_percentage,
        "daily_limit": scenario.daily_limit,
        "monthly_limit": scenario.monthly_limit,
        "created_at": scenario.created_at,
    }
