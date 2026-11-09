"""Verificación reproducible de un backtest LAB para API y consumidores PAPER."""

from __future__ import annotations

from datetime import datetime, timezone

from app.lab.engine import CURRENT_ENGINE_VERSION, LabError, run_backtest, supports_engine_version, validate_candles
from app.lab.store import LabStore, content_hash


def verify_backtest_run(store: LabStore, saved: dict) -> dict:
    """Comprueba huellas y repite el motor; nunca altera el run guardado."""
    run_id = saved["id"]
    checks: dict[str, bool | None] = {"dataset": None, "strategy": None,
                                      "stored_result": None, "replay": None}

    def report(status: str, reason: str) -> dict:
        return {"run_id": run_id, "verified": status == "verified",
                "status": status, "reason": reason, "checks": checks}

    try:
        dataset, raw = store.dataset(saved["dataset_id"])
        checks["dataset"] = (dataset["hash"] == saved.get("dataset_hash")
                             and dataset["source"] == saved.get("dataset_source"))
        if not checks["dataset"]:
            return report("mismatch", "El dataset guardado no coincide con el del backtest")
        version = store.version(saved["version_id"])
    except LabError as exc:
        return report("mismatch", str(exc))

    if saved.get("strategy_hash") is not None:
        checks["strategy"] = content_hash(version["definition"]) == saved["strategy_hash"]
        if not checks["strategy"]:
            return report("mismatch", "La versión de estrategia ya no coincide")

    metadata = {"id", "dataset_id", "version_id", "created_at", "engine_version",
                "dataset_hash", "dataset_source", "strategy_hash", "result_hash"}
    stored_result = {key: value for key, value in saved.items() if key not in metadata}
    if saved.get("result_hash") is not None:
        checks["stored_result"] = content_hash(stored_result) == saved["result_hash"]
        if not checks["stored_result"]:
            return report("mismatch", "El resultado persistido fue alterado")

    if not supports_engine_version(saved["engine_version"], version["definition"]):
        return report("engine_unavailable", "La versión original del motor no está disponible para repetir la prueba")
    try:
        bars, gaps = validate_candles(raw, dataset["timeframe"],
                                      now=int(datetime.now(timezone.utc).timestamp()))
        if gaps:
            return report("mismatch", "El dataset contiene huecos")
        replay = run_backtest(bars, version["definition"], dataset["timeframe"],
                              engine_version=saved["engine_version"])
        replay_origin = saved.get("replay_of_run_id")
        if replay_origin is not None:
            if not isinstance(replay_origin, str) or replay_origin == run_id:
                return report("mismatch", "La procedencia de la nueva prueba es inválida")
            origin = store.run(replay_origin)
            if (origin["dataset_id"] != saved["dataset_id"]
                    or origin["version_id"] != saved["version_id"]
                    or origin.get("dataset_hash") != saved["dataset_hash"]
                    or (origin.get("dataset_source") is not None
                        and origin["dataset_source"] != saved["dataset_source"])
                    or (origin.get("strategy_hash") is not None
                        and origin["strategy_hash"] != saved["strategy_hash"])):
                return report("mismatch", "La prueba de origen ya no coincide con las entradas")
            replay = {**replay, "replay_of_run_id": replay_origin}
    except LabError as exc:
        return report("mismatch", f"No se pudo repetir la prueba: {exc}")
    checks["replay"] = content_hash(replay) == content_hash(stored_result)
    legacy_metric_shape = False
    stored_metrics, replay_metrics = stored_result.get("metrics"), replay.get("metrics")
    if (not checks["replay"] and saved["engine_version"] != CURRENT_ENGINE_VERSION
            and isinstance(stored_metrics, dict) and isinstance(replay_metrics, dict)
            and "ambiguous_exits" not in stored_metrics
            and set(replay_metrics) - set(stored_metrics) == {"ambiguous_exits"}):
        # Un resultado antiguo puede carecer de esta métrica añadida después.
        # La comparación de TODO lo demás sigue siendo exacta, pero nunca
        # se eleva ese registro legado a la categoría «verificado».
        comparable = {**replay, "metrics": {key: value for key, value in replay_metrics.items()
                                             if key != "ambiguous_exits"}}
        legacy_metric_shape = content_hash(comparable) == content_hash(stored_result)
        if legacy_metric_shape:
            checks["replay"] = True
    if not checks["replay"]:
        return report("mismatch", "La repetición difiere del resultado persistido")
    if legacy_metric_shape:
        return report("legacy_unpinned", "Coinciden los campos antiguos; falta una métrica añadida después y no se certifica el registro")
    if checks["strategy"] is None or checks["stored_result"] is None:
        return report("legacy_unpinned", "La prueba coincide, pero el registro antiguo no fijó todas sus huellas")
    return report("verified", "Dataset, estrategia y resultado coinciden con la repetición")
