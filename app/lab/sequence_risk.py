"""Diagnóstico reproducible del orden de P/L cerrado, no pronóstico de mercado.

Reordenar importes ya realizados mantiene el beneficio final fijo. Sólo mide
sensibilidad del drawdown al orden bajo tamaños originales de operación; no
recalcula señales, exposición, capital por orden ni velas intraposición.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext
import hashlib

from app.lab.engine import LabError


VERSION = "PERMUTED_CLOSED_TRADE_PNL_V1"
MIN_TRADES = 20
MAX_WORK = 1_000_000


def closed_trade_drawdown(pnls: list[Decimal], initial: Decimal) -> tuple[Decimal, bool]:
    """Drawdown de capital sólo después de cierres; no incluye riesgo intratrade."""
    with localcontext() as context:
        context.prec = 80
        capital = peak = initial
        worst = Decimal(0)
        for pnl in pnls:
            capital += pnl
            if capital <= 0:
                return Decimal(100), True
            peak = max(peak, capital)
            worst = max(worst, (peak - capital) / peak * 100)
        return worst, False


def _draw(seed: int, path: int, position: int, nonce: int, choices: int) -> int:
    """Índice uniforme con rechazo de sesgo módulo, fijado por SHA-256."""
    payload = (seed.to_bytes(8, "big") + path.to_bytes(4, "big")
               + position.to_bytes(4, "big") + nonce.to_bytes(4, "big"))
    value = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")
    ceiling = 1 << 64
    if value >= ceiling - ceiling % choices:
        return _draw(seed, path, position, nonce + 1, choices)
    return value % choices


def _quantile(sorted_values: list[Decimal], numerator: int, denominator: int) -> str:
    index = max(0, (numerator * len(sorted_values) + denominator - 1) // denominator - 1)
    return str(sorted_values[index])


def sequence_risk(trades: list[dict], *, initial_capital: str,
                  paths: int = 250, seed: int = 0) -> dict:
    """Permuta P/L neto sin reemplazo; no deriva una distribución futura."""
    if (type(paths) is not int or not 10 <= paths <= 2000
            or type(seed) is not int or not 0 <= seed < 1 << 64
            or not isinstance(trades, list) or not MIN_TRADES <= len(trades) <= 10_000
            or paths * len(trades) > MAX_WORK):
        raise LabError("Se requieren al menos 20 cierres y como máximo un millón de pasos")
    try:
        with localcontext() as context:
            context.prec = 80
            initial = Decimal(initial_capital)
            pnls = [Decimal(trade["pnl"]) for trade in trades]
            if initial <= 0 or not initial.is_finite() or any(not pnl.is_finite() for pnl in pnls):
                raise LabError("Capital o P/L LAB no verificable")
            observed, observed_ruin = closed_trade_drawdown(pnls, initial)
            drawdowns: list[Decimal] = []
            ruined = 0
            for path in range(paths):
                shuffled = pnls.copy()
                for position in range(len(shuffled) - 1, 0, -1):
                    chosen = _draw(seed, path, position, 0, position + 1)
                    shuffled[position], shuffled[chosen] = shuffled[chosen], shuffled[position]
                drawdown, ruin = closed_trade_drawdown(shuffled, initial)
                drawdowns.append(drawdown)
                ruined += int(ruin)
            drawdowns.sort()
            return {"method": VERSION, "trades": len(pnls), "paths": paths, "seed": seed,
                    "initial_capital": str(initial), "net_pnl": str(sum(pnls, Decimal(0))),
                    "observed_closed_trade_drawdown_pct": str(observed),
                    "observed_ruin": observed_ruin,
                    "drawdown_pct": {"p05": _quantile(drawdowns, 5, 100),
                                     "median": _quantile(drawdowns, 50, 100),
                                     "p95": _quantile(drawdowns, 95, 100),
                                     "worst": str(drawdowns[-1])},
                    "ruin_paths": ruined, "future_prediction": False,
                    "warnings": ["Reordena los P/L cerrados del mismo backtest; no genera precios nuevos",
                                 "No recalcula tamaño, señales, solapamiento ni pérdidas intraposición",
                                 "No es un intervalo de confianza ni pronóstico de rentabilidad"]}
    except (KeyError, TypeError, ValueError, InvalidOperation, ArithmeticError) as exc:
        raise LabError("Operaciones LAB no verificables para sensibilidad de secuencia") from exc
