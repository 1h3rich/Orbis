"""DCA chronological reserve: known price context, independent financial state."""

from __future__ import annotations

from decimal import Context, DivisionByZero, InvalidOperation, Overflow, ROUND_HALF_EVEN, localcontext

from app.automation.dca import CALC_PRECISION, RESERVE_ENGINE_VERSIONS, dca_engine_for_rule, simulate_dca, validate_dca
from app.lab.engine import Candle, LabError
from app.lab.validation import chronological_split


VALIDATION_ENGINE_VERSION = "dca-holdout-1.0"


def chronological_dca_holdout(candles: list[Candle], definition: dict, timeframe: str,
                              train_fraction: str = "0.7", *,
                              dca_engine_version: str | None = None) -> dict:
    """Reinitialize funds/activations at the cut, preserve causal references."""
    # A research artifact must reproduce even when called from code using
    # different rounding, exponent limits or traps for order-size arithmetic.
    context = Context(prec=CALC_PRECISION, rounding=ROUND_HALF_EVEN,
                      Emin=-999999, Emax=999999, capitals=1, clamp=0,
                      flags=[], traps=[InvalidOperation, DivisionByZero, Overflow])
    with localcontext(context):
        return _chronological_dca_holdout(candles, definition, timeframe,
                                          train_fraction, dca_engine_version)


def _chronological_dca_holdout(candles: list[Candle], definition: dict, timeframe: str,
                               train_fraction: str, dca_engine_version: str | None) -> dict:
    rule = validate_dca(definition)
    if dca_engine_version is None:
        dca_engine_version = dca_engine_for_rule(rule)
    if dca_engine_version not in RESERVE_ENGINE_VERSIONS:
        raise LabError("Motor DCA no disponible para esta validación")
    fraction, split = chronological_split(candles, train_fraction)
    # The public simulator validates the complete continuous candle sequence.
    # The reserve has a single cash-only context point immediately before it.
    test = simulate_dca(candles, rule, timeframe=timeframe,
                        engine_version=dca_engine_version, entry_start_index=split)
    train = simulate_dca(candles[:split], rule, timeframe=timeframe,
                         engine_version=dca_engine_version)
    warnings = [
        "Reserva cronológica; no optimización ni garantía de resultados futuros",
        "Cada tramo inicia presupuesto completo, sin unidades, consumos mensuales ni activaciones heredadas",
        "La reserva conserva sólo referencias de precios del prefijo conocido; la primera señal usa su última vela cerrada",
        "Los tramos duran distinto; sus rentabilidades acumuladas no son directamente comparables",
        "La compra inicial se intenta al principio de cada tramo; no liquida ni hereda la posición del otro",
        "Mirar la reserva para elegir reglas o cambiar el corte contamina la independencia de la prueba",
    ]
    if "volume_gate" in rule:
        warnings.append("La media de volumen puede usar las velas ya cerradas anteriores al corte")
    if any(part["results"][mode]["metrics"]["buys"] < 30
           for part in (train, test) for mode in ("conditional", "fixed")):
        warnings.append("Menos de 30 compras en algún tramo: muestra pequeña; no demuestra robustez")
    return {"method": "DCA_CHRONOLOGICAL_HOLDOUT", "fraction": str(fraction),
            "dca_engine_version": dca_engine_version, "split_index": split,
            "train_bars": split, "test_bars": len(candles) - split,
            "train_start_t": candles[0].t, "train_end_t": candles[split - 1].t,
            "test_start_t": candles[split].t, "test_end_t": candles[-1].t,
            "train": train, "out_of_sample": test, "warnings": warnings}
