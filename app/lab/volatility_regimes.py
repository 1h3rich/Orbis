"""Segmentación descriptiva por volatilidad conocida al cierre de la señal."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext

from app.lab.engine import INTERVALS, LabError
from app.lab.research import HORIZONS


VERSION = "volatility-regimes-1.2"


def _distribution(values: list[Decimal]) -> dict:
    if not values:
        return {"count": 0, "mean_pct": None, "median_pct": None,
                "positive_rate": None}
    ordered = sorted(values)
    middle = len(ordered) // 2
    median = (ordered[middle] if len(ordered) % 2 else
              (ordered[middle - 1] + ordered[middle]) / 2)
    return {"count": len(values),
            "mean_pct": str(sum(values, Decimal(0)) / Decimal(len(values))),
            "median_pct": str(median),
            "positive_rate": str(Decimal(sum(value > 0 for value in values)) / Decimal(len(values)))}


def analyze_volatility_regimes(candles: list[dict], cases: list[dict], timeframe: str,
                               *, lookback: int = 20, threshold_pct: str = "2",
                               train_fraction: str = "0.7") -> dict:
    """Mide la desviación muestral de retornos previos, nunca de etiquetas futuras.

    Usa `lookback` retornos cierre-a-cierre terminados en el cierre de la
    señal. Un hueco o calentamiento insuficiente deja el caso sin clasificar.
    El umbral es fijo y elegido por el usuario; no se optimiza con resultados.
    """
    if timeframe not in INTERVALS or type(lookback) is not int or not 2 <= lookback <= 300:
        raise LabError("Temporalidad o ventana de volatilidad inválida")
    try:
        threshold = Decimal(threshold_pct)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise LabError("Umbral de volatilidad inválido") from exc
    if not threshold.is_finite() or not 0 < threshold <= 1000:
        raise LabError("Umbral de volatilidad inválido")
    try:
        fraction = Decimal(train_fraction)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise LabError("Fracción de desarrollo inválida") from exc
    if not fraction.is_finite() or not Decimal("0.5") <= fraction <= Decimal("0.9"):
        raise LabError("Fracción de desarrollo inválida")
    if not candles or not isinstance(cases, list):
        raise LabError("Histórico o casos ausentes")

    step = INTERVALS[timeframe]
    buckets = {name: {"cases": 0, "horizons": {str(h): [] for h in HORIZONS},
                      "non_overlapping": {str(h): [] for h in HORIZONS},
                      "development": {str(h): [] for h in HORIZONS},
                      "out_of_sample": {str(h): [] for h in HORIZONS},
                      "development_non_overlapping": {str(h): [] for h in HORIZONS},
                      "out_of_sample_non_overlapping": {str(h): [] for h in HORIZONS}}
               for name in ("BELOW", "AT_OR_ABOVE", "INSUFFICIENT_HISTORY")}
    with localcontext() as context:
        context.prec = 80
        times: dict[int, int] = {}
        closes: list[Decimal] = []
        for index, candle in enumerate(candles):
            try:
                stamp = candle["t"]
                close = Decimal(candle["c"])
            except (KeyError, TypeError, InvalidOperation) as exc:
                raise LabError("Vela inválida para segmentar volatilidad") from exc
            if (type(stamp) is not int or stamp in times or close <= 0
                    or not close.is_finite() or index and stamp <= candles[index - 1]["t"]):
                raise LabError("Histórico inválido para segmentar volatilidad")
            times[stamp] = index
            closes.append(close)
        cutoff_index = int(Decimal(len(candles)) * fraction)
        if not 0 < cutoff_index < len(candles):
            raise LabError("Histórico insuficiente para reserva cronológica")
        cutoff_t = candles[cutoff_index]["t"]
        prefix = [Decimal(0)]
        squares = [Decimal(0)]
        consecutive = [0] * len(candles)
        for index in range(1, len(candles)):
            continuous = candles[index]["t"] - candles[index - 1]["t"] == step
            consecutive[index] = consecutive[index - 1] + 1 if continuous else 0
            change = closes[index] / closes[index - 1] - 1 if continuous else Decimal(0)
            prefix.append(prefix[-1] + change)
            squares.append(squares[-1] + change * change)
        classified: list[tuple[int, str, dict[str, Decimal | None]]] = []
        for case in cases:
            try:
                stamp = case["features_at_close"]["signal_time"]
                labels = case["future_labels"]["outcomes_pct"]
                index = times[stamp]
            except (KeyError, TypeError, ValueError) as exc:
                raise LabError("Caso no corresponde al histórico") from exc
            if consecutive[index] < lookback:
                bucket = "INSUFFICIENT_HISTORY"
            else:
                total = prefix[index] - prefix[index - lookback]
                total_squares = squares[index] - squares[index - lookback]
                variance = (total_squares - total * total / Decimal(lookback)) / Decimal(lookback - 1)
                if variance < 0 and abs(variance) < Decimal("1e-60"):
                    variance = Decimal(0)
                if variance < 0:
                    raise LabError("Varianza de volatilidad inválida")
                volatility_pct = variance.sqrt() * 100
                bucket = "BELOW" if volatility_pct < threshold else "AT_OR_ABOVE"
            buckets[bucket]["cases"] += 1
            parsed_labels: dict[str, Decimal | None] = {}
            for horizon in HORIZONS:
                key = str(horizon)
                value = labels[key]
                if value is not None:
                    try:
                        parsed = Decimal(value)
                    except (InvalidOperation, TypeError, ValueError) as exc:
                        raise LabError("Etiqueta futura inválida") from exc
                    if not parsed.is_finite():
                        raise LabError("Etiqueta futura inválida")
                    buckets[bucket]["horizons"][key].append(parsed)
                    parsed_labels[key] = parsed
                else:
                    parsed_labels[key] = None
            classified.append((stamp, bucket, parsed_labels))
        # Selección global por horizonte: dos regímenes distintos tampoco
        # pueden reutilizar la misma ventana futura dentro de esta tabla.
        classified.sort(key=lambda item: item[0])
        embargoed_by_horizon: dict[str, int] = {}
        for horizon in HORIZONS:
            key = str(horizon)
            next_signal_time: int | None = None
            next_by_phase: dict[str, int | None] = {"development": None, "out_of_sample": None}
            embargoed = 0
            for stamp, bucket, labels in classified:
                value = labels[key]
                if value is None:
                    continue
                if next_signal_time is None or stamp >= next_signal_time:
                    buckets[bucket]["non_overlapping"][key].append(value)
                    next_signal_time = stamp + horizon
                # La etiqueta de desarrollo debe terminar *antes* de la
                # primera vela de reserva. Los casos de frontera se embargan.
                phase = ("development" if stamp + horizon < cutoff_t else
                         "out_of_sample" if stamp >= cutoff_t else None)
                if phase is None:
                    embargoed += 1
                    continue
                buckets[bucket][phase][key].append(value)
                next_in_phase = next_by_phase[phase]
                if next_in_phase is None or stamp >= next_in_phase:
                    buckets[bucket][phase + "_non_overlapping"][key].append(value)
                    next_by_phase[phase] = stamp + horizon
            embargoed_by_horizon[key] = embargoed
        result = {name: {"cases": group["cases"],
                         "small_sample": group["cases"] < 30,
                         "horizons": {key: {**_distribution(values),
                                            "non_overlapping": _distribution(group["non_overlapping"][key]),
                                            "development": {**_distribution(group["development"][key]),
                                                            "non_overlapping": _distribution(
                                                                group["development_non_overlapping"][key])},
                                            "out_of_sample": {**_distribution(group["out_of_sample"][key]),
                                                              "non_overlapping": _distribution(
                                                                  group["out_of_sample_non_overlapping"][key])}}
                                      for key, values in group["horizons"].items()}}
                  for name, group in buckets.items()}
    return {"version": VERSION, "timeframe": timeframe, "lookback_returns": lookback,
            "threshold_pct_per_bar": str(threshold), "cases": len(cases),
            "chronological_reserve": {"train_fraction": str(fraction),
                                      "cutoff_t": cutoff_t,
                                      "embargoed_cases_by_horizon": embargoed_by_horizon,
                                      "status": "EXPLORATORY_NOT_SEALED"},
            "regimes": result, "comparison_basis": "DESCRIPTIVE_NOT_INDEPENDENT",
            "assumptions": [
                "Volatilidad muestral de retornos cierre-a-cierre conocida al cierre de la señal",
                "Un hueco reinicia la ventana; los casos sin historia suficiente no se fuerzan a un régimen",
                "El umbral por vela depende de la temporalidad y no se ajusta a resultados futuros",
                "Los resultados posteriores son etiquetas; casos cercanos pueden solaparse",
                "El subconjunto sin solape se elige cronológicamente entre todos los regímenes por horizonte; no prueba independencia",
                "La reserva empieza en una vela fijada por fracción cronológica; desarrollo excluye etiquetas que tocan o cruzan esa frontera",
                "La reserva no está sellada: cambiar ventana, umbral o fracción tras verla invalida una pretensión fuera de muestra",
                "Precio bruto, sin costes ni rentabilidad ejecutable",
            ]}
