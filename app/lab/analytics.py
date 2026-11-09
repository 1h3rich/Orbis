"""Métricas de riesgo derivadas de curvas LAB inmutables.

No modifica el motor de señales ni los resultados persistidos. Esta versión
puede recalcularse al consultar backtests antiguos sin reescribir su motor.
"""

from __future__ import annotations

from decimal import Decimal, DecimalException, localcontext


RISK_VERSION = "equity-risk-1.1"
SECONDS_PER_YEAR = 365 * 24 * 60 * 60


def _annualized_return(equity: list[dict]) -> tuple[str | None, str | None]:
    """CAGR descriptivo sólo para una curva LAB sin flujos de al menos un año."""
    if len(equity) < 2:
        return None, "Se necesitan al menos dos puntos de capital"
    try:
        times = [point["t"] for point in equity]
        if any(type(value) is not int for value in times) or any(
                later <= earlier for earlier, later in zip(times, times[1:])):
            return None, "Fechas de capital inválidas"
        duration = times[-1] - times[0]
        if duration < SECONDS_PER_YEAR:
            return None, "Periodo inferior a un año; no se anualiza"
        with localcontext() as ctx:
            ctx.prec = 80
            values = [Decimal(str(point["equity"])) for point in equity]
            if any(not value.is_finite() or value <= 0 for value in values):
                return None, "Capital no positivo o no finito; anualización indefinida"
            years = Decimal(duration) / Decimal(SECONDS_PER_YEAR)
            annualized = ((values[-1] / values[0]) ** (Decimal(1) / years) - 1) * 100
            if not annualized.is_finite():
                return None, "Anualización no finita"
            return str(annualized), None
    except (KeyError, TypeError, ValueError, DecimalException, OverflowError):
        return None, "Curva de capital inválida"


def equity_risk(equity: list[dict]) -> dict:
    """Desviación típica muestral (%) de retornos simples entre velas.

    Retorno i = E_i / E_(i-1) - 1. Requiere dos retornos y capital positivo
    en cada denominador; no anualiza ni equipara temporalidades distintas.
    """
    annualized, annualized_reason = _annualized_return(equity)
    result = {"version": RISK_VERSION, "method": "SAMPLE_STD_OF_PER_BAR_EQUITY_RETURNS",
              "returns_count": max(0, len(equity) - 1), "bar_volatility_pct": None,
              "annualized_return_pct": annualized,
              "annualized_method": "CAGR_365D_NO_CASH_FLOWS_LAB"}
    if annualized_reason is not None:
        result["annualized_reason"] = annualized_reason
    if len(equity) < 3:
        result["reason"] = "Se necesitan al menos tres puntos de capital"
        return result
    try:
        with localcontext() as ctx:
            ctx.prec = 80
            values = [Decimal(str(point["equity"])) for point in equity]
            if any(not value.is_finite() for value in values) or any(value <= 0 for value in values[:-1]):
                result["reason"] = "Capital no positivo o no finito; retorno porcentual indefinido"
                return result
            returns = [values[i] / values[i - 1] - 1 for i in range(1, len(values))]
            mean = sum(returns, Decimal(0)) / Decimal(len(returns))
            variance = sum(((value - mean) ** 2 for value in returns), Decimal(0)) / Decimal(len(returns) - 1)
            result["bar_volatility_pct"] = str(variance.sqrt() * 100)
            return result
    except (KeyError, TypeError, ValueError, DecimalException, OverflowError):
        result["reason"] = "Curva de capital inválida"
        return result
