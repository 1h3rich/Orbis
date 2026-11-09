"""Diagnóstico de cobertura multitemporal, ajeno al resultado inmutable del backtest."""

from __future__ import annotations

from app.lab.engine import (Candle, IndicatorCache, INTERVALS, LabError, RSI_DIVERGENCE_KINDS,
                            validate_rule_timeframes)


def uses_higher_timeframe(strategy: dict) -> bool:
    """Consulta barata para no reconstruir el histórico en sesiones sólo base."""
    def visit(node: dict) -> bool:
        return (any(visit(child) for child in node["children"]) if "op" in node
                else node.get("timeframe") is not None)

    return visit(strategy["entry"]) or ("exit" in strategy and visit(strategy["exit"]))


def _minimum_complete(rule: dict) -> int | None:
    """Mínimo exacto de calentamiento; None para estructuras sin mínimo suficiente."""
    kind, period = rule["kind"], rule["period"]
    if kind in ("RSI_LT", "RSI_GT", "VOLUME_ABOVE_AVG"):
        return period + 1
    if kind in RSI_DIVERGENCE_KINDS:
        radius = rule["pivot_bars"]
        return max(period, radius) + 2 * radius + 2
    if kind in ("EMA_CROSS_ABOVE_EMA", "EMA_CROSS_BELOW_EMA"):
        return rule["slow_period"] + 1
    if kind in ("MACD_CROSS_ABOVE_SIGNAL", "MACD_CROSS_BELOW_SIGNAL"):
        return rule["slow_period"] + rule["signal_period"]
    if kind in ("CLOSE_ABOVE_EMA", "CLOSE_BELOW_EMA", "CLOSE_ABOVE_SMA",
                "CLOSE_BELOW_SMA", "CLOSE_ABOVE_BB_UPPER", "CLOSE_BELOW_BB_LOWER",
                "ATR_PCT_GT", "ATR_PCT_LT", "BEARISH_STREAK", "BULLISH_STREAK",
                "VOLUME_RISING_STREAK"):
        return period
    return None


def validate_paper_timeframe_capacity(strategy: dict, base_timeframe: str,
                                      first_candle_t: int, *, max_base_bars: int = 5000) -> None:
    """Rechaza reglas superiores que nunca podrán calentarse en una sesión PAPER.

    Supone una continuación sin huecos, el caso más favorable. Una regla que
    falla incluso en ese caso no puede producir una señal antes del límite.
    """
    validate_rule_timeframes(strategy, base_timeframe)
    if max_base_bars < 1 or first_candle_t < 0:
        raise LabError("Límite o inicio de histórico PAPER inválido")
    base_step = INTERVALS[base_timeframe]
    last_possible_t = first_candle_t + (max_base_bars - 1) * base_step

    def visit(node: dict, path: str) -> None:
        if "op" in node:
            for index, child in enumerate(node["children"]):
                visit(child, f"{path}.children[{index}]")
            return
        timeframe = node.get("timeframe")
        if timeframe is None:
            return
        higher_step = INTERVALS[timeframe]
        first_full_start = ((first_candle_t + higher_step - 1) // higher_step) * higher_step
        first_full_last_t = first_full_start + higher_step - base_step
        possible = (0 if last_possible_t < first_full_last_t else
                    1 + (last_possible_t - first_full_last_t) // higher_step)
        required = _minimum_complete(node) or 1
        if possible < required:
            raise LabError(
                f"PAPER no puede evaluar {path}: necesita {required} velas {timeframe} "
                f"completas, pero caben como máximo {possible} en {max_base_bars} velas {base_timeframe}")

    visit(strategy["entry"], "entry")
    if "exit" in strategy:
        visit(strategy["exit"], "exit")


def higher_timeframe_coverage(candles: list[Candle], strategy: dict,
                              base_timeframe: str) -> dict:
    """Cuenta buckets completos y calentamiento por regla sin alterar un run guardado."""
    validate_rule_timeframes(strategy, base_timeframe)
    if not candles:
        raise LabError("No hay velas para diagnosticar la cobertura")
    cache = IndicatorCache(candles)
    rows = []

    def visit(node: dict, path: str) -> None:
        if "op" in node:
            for index, child in enumerate(node["children"]):
                visit(child, f"{path}.children[{index}]")
            return
        timeframe = node.get("timeframe")
        if timeframe is None:
            return
        complete, _, _ = cache.resampled(base_timeframe, timeframe)
        required = _minimum_complete(node)
        rows.append({"path": path, "kind": node["kind"], "period": node["period"],
                     "timeframe": timeframe, "complete_bars": len(complete),
                     "minimum_bars": required,
                     "status": ("COVERAGE_ONLY" if required is None else
                                "INSUFFICIENT" if len(complete) < required else "READY_TO_EVALUATE"),
                     "first_evaluable_at": (complete[required - 1].t + INTERVALS[timeframe]
                                            if required is not None and len(complete) >= required else None)})

    visit(strategy["entry"], "entry")
    if "exit" in strategy:
        visit(strategy["exit"], "exit")
    return {"base_timeframe": base_timeframe, "dataset_start_t": candles[0].t,
            "dataset_end_t": candles[-1].t, "rules": rows,
            "insufficient_rules": sum(row["status"] == "INSUFFICIENT" for row in rows)}
