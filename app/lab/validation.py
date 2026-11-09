"""Evaluación cronológica sin reajustar parámetros con el tramo reservado.

No es walk-forward ni elimina el sesgo de selección si el usuario diseñó
la estrategia mirando ya el periodo fuera de muestra.
"""

from __future__ import annotations

from decimal import Decimal

from app.lab.engine import CURRENT_ENGINE_VERSION, Candle, LabError, number, run_backtest


VALIDATION_ENGINE_VERSION = "holdout-1.0"
ROLLING_ENGINE_VERSION = "rolling-fixed-1.0"


def chronological_split(candles: list[Candle], train_fraction: str = "0.7") -> tuple[Decimal, int]:
    """Mismo corte cerrado para validación simple y búsqueda de parámetros."""
    fraction = number(train_fraction, positive=True)
    if not Decimal("0.5") <= fraction <= Decimal("0.8"):
        raise LabError("La fracción de desarrollo debe estar entre 0.5 y 0.8")
    if len(candles) < 60:
        raise LabError("La validación requiere al menos 60 velas cerradas")
    split = int(Decimal(len(candles)) * fraction)
    if split < 30 or len(candles) - split < 30:
        raise LabError("Cada tramo necesita al menos 30 velas")
    return fraction, split


def chronological_holdout(candles: list[Candle], strategy: dict, timeframe: str,
                          train_fraction: str = "0.7", *,
                          engine_version: str = CURRENT_ENGINE_VERSION) -> dict:
    fraction, split = chronological_split(candles, train_fraction)
    train = run_backtest(candles[:split], strategy, timeframe,
                         engine_version=engine_version)
    # Los indicadores disponen del prefijo anterior al corte, conocido en
    # ese momento; capital y posiciones arrancan de cero en la reserva.
    test = run_backtest(candles, strategy, timeframe, entry_start_index=split,
                        engine_version=engine_version)
    warnings = ["Separación cronológica; no optimización ni walk-forward",
                "El tramo reservado reutiliza sólo el histórico anterior para calentar indicadores, no sus posiciones ni capital",
                "La posición abierta al final de desarrollo se cierra artificialmente por fin de tramo",
                "Los tramos duran distinto; sus rentabilidades acumuladas no son directamente comparables",
                "Cambiar el corte después de mirar resultados también contamina la reserva",
                "Si la estrategia se eligió mirando todo este dataset, el tramo reservado no es independiente"]
    if train["metrics"]["trades"] < 30 or test["metrics"]["trades"] < 30:
        warnings.append("Menos de 30 operaciones en algún tramo: estimaciones muy inestables")
    return {"method": "CHRONOLOGICAL_HOLDOUT", "fraction": str(fraction),
            "split_index": split, "train_bars": split, "test_bars": len(candles) - split,
            "train_start_t": candles[0].t, "train_end_t": candles[split - 1].t,
            "test_start_t": candles[split].t, "test_end_t": candles[-1].t,
            "train": train, "out_of_sample": test, "warnings": warnings}


def rolling_fixed_validation(candles: list[Candle], strategy: dict, timeframe: str,
                             folds: int = 3, *,
                             engine_version: str = CURRENT_ENGINE_VERSION) -> dict:
    """Tramos OOS disjuntos con entrenamiento creciente y estrategia inalterada.

    No se optimizan parámetros: cada fold reinicia capital y posiciones.
    El prefijo completo sólo calienta indicadores causales del test.
    """
    if type(folds) is not int or not 2 <= folds <= 5:
        raise LabError("Se requieren entre 2 y 5 ventanas de reserva")
    if len(candles) < 30 * (folds + 1):
        raise LabError("Cada tramo inicial y de reserva requiere al menos 30 velas cerradas")
    width = len(candles) // (folds + 1)
    windows = []
    for index in range(folds):
        split = width * (index + 1)
        end = width * (index + 2) if index < folds - 1 else len(candles)
        training = run_backtest(candles[:split], strategy, timeframe,
                                engine_version=engine_version)
        test = run_backtest(candles[:end], strategy, timeframe,
                            entry_start_index=split,
                            engine_version=engine_version)
        windows.append({"index": index + 1, "train_bars": split, "test_bars": end - split,
                        "train_start_t": candles[0].t, "train_end_t": candles[split - 1].t,
                        "test_start_t": candles[split].t, "test_end_t": candles[end - 1].t,
                        "train_metrics": training["metrics"], "out_of_sample": test})
    test_trades = sum(window["out_of_sample"]["metrics"]["trades"] for window in windows)
    positive = sum(Decimal(window["out_of_sample"]["metrics"]["net_profit"]) > 0
                   for window in windows)
    warnings = ["Versión y parámetros fijos; no se ha reoptimizado en cada ventana",
                "La versión puede haber sido diseñada mirando este histórico; eso contamina todos los tramos reservados",
                "Cada reserva reinicia capital y posiciones; sus beneficios no se suman como una cartera continua",
                "El cierre al final de cada ventana es artificial",
                "Los indicadores usan sólo el prefijo temporal conocido en cada vela"]
    if any(window["out_of_sample"]["metrics"]["trades"] < 30 for window in windows):
        warnings.append("Menos de 30 operaciones en alguna reserva: estimación inestable")
    return {"method": "ROLLING_FIXED_VERSION", "folds": folds,
            "train_bars": width, "test_bars": len(candles) - width,
            "tested_trades": test_trades, "positive_folds": positive,
            "windows": windows, "warnings": warnings}
