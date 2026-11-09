"""Barrido discreto: selección en desarrollo y una reserva cronológica intacta.

No muta la estrategia guardada ni interpreta la mejor combinación como
garantía. Sólo ajusta stop/objetivo; reglas, costes y capital quedan fijos.
"""

from __future__ import annotations

from decimal import Decimal

from app.lab.engine import CURRENT_ENGINE_VERSION, Candle, LabError, number, run_backtest, validate_strategy
from app.lab.validation import chronological_split


OPTIMIZER_VERSION = "grid-stop-target-1.1"
LEGACY_WALK_FORWARD_OPTIMIZER_VERSION = "walk-forward-grid-stop-target-1.0"
WALK_FORWARD_OPTIMIZER_VERSION = "walk-forward-grid-stop-target-1.1"
SUPPORTED_OPTIMIZER_VERSIONS = frozenset(("grid-stop-target-1.0", OPTIMIZER_VERSION,
                                          LEGACY_WALK_FORWARD_OPTIMIZER_VERSION,
                                          WALK_FORWARD_OPTIMIZER_VERSION))
HOLDOUT_OPTIMIZER_VERSIONS = frozenset(("grid-stop-target-1.0", OPTIMIZER_VERSION))
MIN_TRAIN_TRADES = 10


def _rates(values: list[str], name: str, maximum: Decimal) -> list[str]:
    if not isinstance(values, list) or not 1 <= len(values) <= 3:
        raise LabError(f"{name}: introduce entre 1 y 3 valores")
    clean = [number(value, positive=True) for value in values]
    if any(value >= maximum for value in clean) or len(set(clean)) != len(clean):
        raise LabError(f"{name}: valores duplicados o fuera de rango")
    return [str(value) for value in sorted(clean)]


def _sensitivity(candidates: list[dict], selected: dict | None,
                 stops: list[str], targets: list[str]) -> dict | None:
    """Vecinos inmediatos de la cuadrícula, exclusivamente en desarrollo."""
    if selected is None:
        return None
    stop_index = stops.index(selected["stop_loss"])
    target_index = targets.index(selected["take_profit"])
    neighbor_keys = set()
    for delta in (-1, 1):
        if 0 <= stop_index + delta < len(stops):
            neighbor_keys.add((stops[stop_index + delta], selected["take_profit"]))
        if 0 <= target_index + delta < len(targets):
            neighbor_keys.add((selected["stop_loss"], targets[target_index + delta]))
    neighbors = [candidate for candidate in candidates
                 if (candidate["stop_loss"], candidate["take_profit"]) in neighbor_keys]
    eligible = [candidate for candidate in neighbors if candidate["eligible"]]
    positive = [candidate for candidate in eligible
                if Decimal(candidate["train_metrics"]["net_profit"]) > 0]
    return {"neighbor_count": len(neighbors), "eligible_neighbors": len(eligible),
            "positive_neighbors": len(positive),
            "worst_neighbor_net_profit": (min((candidate["train_metrics"]["net_profit"]
                                               for candidate in neighbors), key=Decimal)
                                          if neighbors else None),
            "neighbors": [{"stop_loss": candidate["stop_loss"],
                           "take_profit": candidate["take_profit"],
                           "eligible": candidate["eligible"],
                           "trades": candidate["train_metrics"]["trades"],
                           "net_profit": candidate["train_metrics"]["net_profit"]}
                          for candidate in neighbors]}


def _grid(candles: list[Candle], definition: dict, timeframe: str,
          stops: list[str], targets: list[str], *, engine_version: str) -> tuple[list[dict], dict | None]:
    candidates = []
    for stop in stops:
        for target in targets:
            variant = {**definition, "stop_loss": stop, "take_profit": target}
            training = run_backtest(candles, variant, timeframe,
                                    engine_version=engine_version)
            metrics = training["metrics"]
            candidates.append({"stop_loss": stop, "take_profit": target,
                               "stop_loss_pct": str(Decimal(stop) * 100),
                               "take_profit_pct": str(Decimal(target) * 100),
                               "train_metrics": metrics,
                               "eligible": metrics["trades"] >= MIN_TRAIN_TRADES})
    eligible = [candidate for candidate in candidates if candidate["eligible"]]
    selected = min(eligible, key=lambda candidate: (-Decimal(candidate["train_metrics"]["net_profit"]),
                                                    Decimal(candidate["stop_loss"]), Decimal(candidate["take_profit"]))) if eligible else None
    return candidates, selected


def optimize_stops(candles: list[Candle], strategy: dict, timeframe: str,
                   stop_losses: list[str], take_profits: list[str],
                   train_fraction: str = "0.7", *,
                   engine_version: str = CURRENT_ENGINE_VERSION,
                   optimizer_version: str = OPTIMIZER_VERSION) -> dict:
    """Clasifica por neto de desarrollo entre candidatos con muestra mínima."""
    if not isinstance(optimizer_version, str) or optimizer_version not in HOLDOUT_OPTIMIZER_VERSIONS:
        raise LabError("Versión del optimizador no disponible")
    definition = validate_strategy(strategy)
    fraction, split = chronological_split(candles, train_fraction)
    stops = _rates(stop_losses, "Stops", Decimal(1))
    target_max = Decimal(1) if definition["direction"] == "SHORT" else Decimal(10)
    targets = _rates(take_profits, "Objetivos", target_max)
    candidates, selected = _grid(candles[:split], definition, timeframe, stops, targets,
                                 engine_version=engine_version)
    out_of_sample = None
    if selected is not None:
        variant = {**definition, "stop_loss": selected["stop_loss"],
                   "take_profit": selected["take_profit"]}
        # El prefijo sólo calienta indicadores. Capital y posiciones empiezan
        # en cero al corte; no se prueban los demás candidatos en la reserva.
        out_of_sample = run_backtest(candles, variant, timeframe,
                                     entry_start_index=split,
                                     engine_version=engine_version)
    warnings = ["La reserva se consulta sólo para el candidato elegido en desarrollo",
                "Elegir otra cuadrícula o corte después de ver la reserva contamina la validación",
                "No es walk-forward ni prueba de rendimiento futuro",
                "Los costes y la calidad del histórico limitan cualquier conclusión"]
    if selected is None:
        warnings.append(f"Ningún candidato alcanzó {MIN_TRAIN_TRADES} operaciones en desarrollo; reserva no evaluada")
    elif out_of_sample["metrics"]["trades"] < 30:
        warnings.append("Menos de 30 operaciones en reserva: estimación muy inestable")
    result = {"method": "GRID_STOP_TARGET_HOLDOUT", "optimizer_version": optimizer_version,
            "fraction": str(fraction), "split_index": split,
            "train_bars": split, "test_bars": len(candles) - split,
            "train_start_t": candles[0].t, "train_end_t": candles[split - 1].t,
            "test_start_t": candles[split].t, "test_end_t": candles[-1].t,
            "min_train_trades": MIN_TRAIN_TRADES, "candidates": candidates,
            "selected": selected, "out_of_sample": out_of_sample, "warnings": warnings}
    if optimizer_version == OPTIMIZER_VERSION:
        sensitivity = _sensitivity(candidates, selected, stops, targets)
        result["sensitivity"] = sensitivity
        if sensitivity is not None:
            if sensitivity["neighbor_count"] == 0:
                warnings.append("Una sola combinación: no hay sensibilidad a parámetros evaluable")
            elif sensitivity["eligible_neighbors"] < sensitivity["neighbor_count"]:
                warnings.append("Algún parámetro vecino no alcanzó la muestra mínima en desarrollo")
            if sensitivity["positive_neighbors"] < sensitivity["eligible_neighbors"]:
                warnings.append("Algún parámetro vecino elegible no fue rentable en desarrollo")
            warnings.append("Vecinos positivos no prueban robustez futura ni independencia de los casos")
    return result


def walk_forward_stops(candles: list[Candle], strategy: dict, timeframe: str,
                       stop_losses: list[str], take_profits: list[str], folds: int = 3, *,
                       engine_version: str = CURRENT_ENGINE_VERSION,
                       optimizer_version: str = WALK_FORWARD_OPTIMIZER_VERSION,
                       final_holdout_fraction: str = "0.2") -> dict:
    """Reelige en prefijos crecientes y prueba reservas futuras disjuntas.

    La reserva de un fold pasa a ser entrenamiento para el siguiente, nunca
    para la elección del mismo fold. No hay un holdout final intacto adicional.
    """
    if optimizer_version not in (LEGACY_WALK_FORWARD_OPTIMIZER_VERSION,
                                 WALK_FORWARD_OPTIMIZER_VERSION):
        raise LabError("Versión walk-forward no disponible")
    if type(folds) is not int or not 2 <= folds <= 5:
        raise LabError("Se requieren entre 2 y 5 ventanas walk-forward")
    final_fraction = None
    development = candles
    if optimizer_version == WALK_FORWARD_OPTIMIZER_VERSION:
        final_fraction = number(final_holdout_fraction, positive=True)
        if not Decimal("0.1") <= final_fraction <= Decimal("0.4"):
            raise LabError("La reserva final debe ser entre 10 % y 40 %")
        final_count = int(Decimal(len(candles)) * final_fraction)
        if final_count < 30:
            raise LabError("La reserva final requiere al menos 30 velas cerradas")
        development = candles[:-final_count]
    elif final_holdout_fraction != "0.2":
        raise LabError("La versión antigua no admite reserva final")
    if len(development) < 30 * (folds + 1):
        raise LabError("Cada tramo inicial y de reserva requiere al menos 30 velas cerradas")
    definition = validate_strategy(strategy)
    stops = _rates(stop_losses, "Stops", Decimal(1))
    target_max = Decimal(1) if definition["direction"] == "SHORT" else Decimal(10)
    targets = _rates(take_profits, "Objetivos", target_max)
    width = len(development) // (folds + 1)
    windows = []
    for index in range(folds):
        split = width * (index + 1)
        end = width * (index + 2) if index < folds - 1 else len(development)
        candidates, selected = _grid(development[:split], definition, timeframe, stops, targets,
                                     engine_version=engine_version)
        test = None
        if selected is not None:
            variant = {**definition, "stop_loss": selected["stop_loss"],
                       "take_profit": selected["take_profit"]}
            test = run_backtest(development[:end], variant, timeframe,
                                entry_start_index=split, engine_version=engine_version)
        windows.append({"index": index + 1, "train_bars": split, "test_bars": end - split,
                        "train_start_t": development[0].t, "train_end_t": development[split - 1].t,
                        "test_start_t": development[split].t, "test_end_t": development[end - 1].t,
                        "candidates": candidates, "selected": selected,
                        "sensitivity": _sensitivity(candidates, selected, stops, targets),
                        "out_of_sample": test})
    evaluated = [window for window in windows if window["out_of_sample"] is not None]
    tested_trades = sum(window["out_of_sample"]["metrics"]["trades"] for window in evaluated)
    positive = sum(Decimal(window["out_of_sample"]["metrics"]["net_profit"]) > 0
                   for window in evaluated)
    warnings = ["Cada elección utiliza sólo velas anteriores a su reserva",
                "Las reservas son disjuntas, pero una reserva anterior entra en el entrenamiento posterior",
                "Cada ventana reinicia capital y posiciones; sus beneficios no son una cartera continua",
                "Una estrategia o cuadrícula diseñada mirando el histórico contamina la evaluación",
                "El cierre al final de cada ventana es artificial; no predice rendimiento futuro"]
    if optimizer_version == LEGACY_WALK_FORWARD_OPTIMIZER_VERSION:
        warnings.insert(2, "No hay una reserva final intacta además de estas ventanas")
    else:
        warnings.insert(2, "La reserva final no interviene en la elección de parámetros ni en las ventanas walk-forward")
    if len(evaluated) < folds:
        warnings.append("Alguna ventana no tuvo candidato con diez operaciones de desarrollo; no se evaluó su reserva")
    if any(window["out_of_sample"]["metrics"]["trades"] < 30 for window in evaluated):
        warnings.append("Menos de 30 operaciones en alguna reserva: estimación inestable")
    result = {"method": "WALK_FORWARD_GRID_STOP_TARGET",
            "optimizer_version": optimizer_version,
            "folds": folds, "grid": {"stop_losses": stops, "take_profits": targets},
            "train_bars": width, "test_bars": len(development) - width,
            "eligible_folds": len(evaluated), "tested_trades": tested_trades,
            "positive_folds": positive, "windows": windows, "warnings": warnings}
    if optimizer_version == WALK_FORWARD_OPTIMIZER_VERSION:
        candidates, selected = _grid(development, definition, timeframe, stops, targets,
                                     engine_version=engine_version)
        final_test = None
        if selected is not None:
            variant = {**definition, "stop_loss": selected["stop_loss"],
                       "take_profit": selected["take_profit"]}
            final_test = run_backtest(candles, variant, timeframe,
                                      entry_start_index=len(development),
                                      engine_version=engine_version)
        result["final_fraction"] = str(final_fraction)
        result["development_bars"] = len(development)
        result["final_holdout"] = {
            "start_t": candles[len(development)].t, "end_t": candles[-1].t,
            "bars": len(candles) - len(development),
            "selected": selected, "candidates": candidates,
            "sensitivity": _sensitivity(candidates, selected, stops, targets),
            "out_of_sample": final_test,
        }
        warnings.append("La elección final se hace en todo el desarrollo, no con resultados de la reserva final")
        if selected is None:
            warnings.append("Sin candidato con diez operaciones de desarrollo; reserva final no evaluada")
        elif final_test["metrics"]["trades"] < 30:
            warnings.append("Menos de 30 operaciones en la reserva final: estimación inestable")
    return result
