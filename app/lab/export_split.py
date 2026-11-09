"""Marca evidencia LAB para investigación posterior sin cruzar etiquetas al corte.

El archivo sigue conteniendo todo el histórico: esto no sella una reserva ni
convierte una estrategia ya seleccionada sobre él en validación independiente.
"""

from __future__ import annotations

from app.lab.engine import INTERVALS


PARTITIONS = ("development", "purged_boundary", "validation",
              "incomplete_development", "incomplete_validation")
FOLLOWUP_SECONDS = 604800


def partition_evidence(candles: list[dict], rows: list[dict], timeframe: str) -> dict:
    """Partición fija 70/30 con embargo de cualquier etiqueta futura de 7 d.

    Los índices sólo incluyen casos con seguimiento completo; un caso cerca
    del final del histórico conserva sus etiquetas parciales, pero no se
    recomienda como muestra de entrenamiento o validación de 7 días.
    """
    if timeframe not in INTERVALS or len(candles) < 3:
        raise ValueError("Histórico insuficiente o temporalidad inválida")
    step = INTERVALS[timeframe]
    boundary_index = min(len(candles) - 1, max(1, len(candles) * 7 // 10))
    boundary = candles[boundary_index]["t"]
    if type(boundary) is not int:
        raise ValueError("Hora de corte inválida")
    indices = {name: [] for name in PARTITIONS}
    complete_windows: dict[str, list[tuple[int, int, int]]] = {
        "development": [], "validation": [],
    }
    for index, row in enumerate(rows):
        features = row["features_at_close"]
        labels = row["future_labels"]
        signal = features["signal_time"]
        followup_end = labels["followup_end_time"]
        complete = labels["full_7d_followup"] is True
        if followup_end is None and not complete:
            # La última vela puede producir una señal sin ninguna vela futura.
            followup_end = signal
        if (type(signal) is not int or type(followup_end) is not int
                or followup_end < signal):
            raise ValueError("Horizonte de etiqueta inválido")
        if signal < boundary:
            if signal + step >= boundary or (complete and followup_end + step >= boundary):
                partition = "purged_boundary"
            elif not complete:
                partition = "incomplete_development"
            else:
                partition = "development"
        else:
            partition = "validation" if complete else "incomplete_validation"
        row["partition"] = partition
        indices[partition].append(index)
        if partition in complete_windows:
            complete_windows[partition].append((signal, followup_end, index))

    def nonoverlap(phase: str) -> list[int]:
        """Muestreo cronológico de ventanas completas, sin mirar resultados."""
        selected = []
        next_open = None
        for signal, followup_end, index in sorted(complete_windows[phase]):
            if next_open is None or signal >= next_open:
                selected.append(index)
                next_open = followup_end + step
        return selected

    development_nonoverlap = nonoverlap("development")
    validation_nonoverlap = nonoverlap("validation")
    return {
        "method": "chronological_70_30_purged_7d_v1",
        "nonoverlap_method": "greedy_by_signal_time_complete_7d_v1",
        "boundary_index": boundary_index,
        "boundary_open_t": boundary,
        "horizon_seconds": FOLLOWUP_SECONDS,
        "counts": {name: len(indices[name]) for name in PARTITIONS},
        "development_indices": indices["development"],
        "validation_indices": indices["validation"],
        "development_nonoverlap_indices": development_nonoverlap,
        "validation_nonoverlap_indices": validation_nonoverlap,
        "purged_indices": indices["purged_boundary"],
        "incomplete_indices": indices["incomplete_development"] + indices["incomplete_validation"],
    }
