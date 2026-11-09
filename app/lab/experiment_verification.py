"""Shared replay of published LAB validation/optimization records.

Reports retain the API's established classifications; no execution authority.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from app.lab.engine import LabError, supports_engine_version, validate_candles
from app.lab.optimization import (SUPPORTED_OPTIMIZER_VERSIONS,
    LEGACY_WALK_FORWARD_OPTIMIZER_VERSION, WALK_FORWARD_OPTIMIZER_VERSION,
    optimize_stops, walk_forward_stops)
from app.lab.store import LabStore, content_hash
from app.lab.validation import (ROLLING_ENGINE_VERSION, VALIDATION_ENGINE_VERSION,
    chronological_holdout, rolling_fixed_validation)


def verify_lab_experiment(store: LabStore, saved: dict, kind: Literal["validation", "optimization"]) -> dict:
    """Comprueba el experimento exacto; un motor no fijado nunca se certifica."""
    checks: dict[str, bool | None] = {"dataset": None, "strategy": None,
                                      "stored_result": None, "replay": None}

    def report(status: str, reason: str) -> dict:
        return {"run_id": saved["id"], "verified": status == "verified",
                "status": status, "reason": reason, "checks": checks}

    try:
        dataset, raw = store.dataset(saved["dataset_id"])
        checks["dataset"] = dataset["hash"] == saved["dataset_hash"]
        if not checks["dataset"]:
            return report("mismatch", "El dataset guardado no coincide con el experimento")
        version = store.version(saved["version_id"])
    except LabError as exc:
        return report("mismatch", str(exc))
    if saved.get("strategy_hash") is not None:
        checks["strategy"] = content_hash(version["definition"]) == saved["strategy_hash"]
        if not checks["strategy"]:
            return report("mismatch", "La versión de estrategia ya no coincide")
    metadata = {"id", "dataset_id", "version_id", "dataset_hash", "created_at",
                "engine_version", "strategy_hash", "result_hash", "lab_engine_version"}
    stored_result = {key: value for key, value in saved.items() if key not in metadata}
    if saved.get("result_hash") is not None:
        checks["stored_result"] = content_hash(stored_result) == saved["result_hash"]
        if not checks["stored_result"]:
            return report("mismatch", "El resultado persistido fue alterado")
    if kind == "validation":
        expected_engine = (ROLLING_ENGINE_VERSION if stored_result.get("method") == "ROLLING_FIXED_VERSION"
                           else VALIDATION_ENGINE_VERSION)
        if saved["engine_version"] != expected_engine:
            return report("engine_unavailable", "El motor de validación original no está disponible")
        if saved.get("lab_engine_version") is None:
            return report("legacy_unpinned", "El registro antiguo no fijó el motor de backtesting")
        if not supports_engine_version(saved["lab_engine_version"], version["definition"]):
            return report("engine_unavailable", "El motor de backtesting original no está disponible")
    elif (not supports_engine_version(saved["engine_version"], version["definition"])
          or saved["optimizer_version"] not in SUPPORTED_OPTIMIZER_VERSIONS):
        return report("engine_unavailable", "El motor u optimizador original no está disponible")
    try:
        bars, gaps = validate_candles(raw, dataset["timeframe"],
                                      now=int(datetime.now(timezone.utc).timestamp()))
        if gaps:
            return report("mismatch", "El dataset contiene huecos")
        if kind == "validation":
            if stored_result.get("method") == "ROLLING_FIXED_VERSION":
                replay = rolling_fixed_validation(bars, version["definition"],
                                                  dataset["timeframe"], stored_result["folds"],
                                                  engine_version=saved["lab_engine_version"])
            else:
                replay = chronological_holdout(bars, version["definition"],
                                               dataset["timeframe"], stored_result["fraction"],
                                               engine_version=saved["lab_engine_version"])
        else:
            if stored_result.get("method") == "WALK_FORWARD_GRID_STOP_TARGET":
                if saved["optimizer_version"] not in (LEGACY_WALK_FORWARD_OPTIMIZER_VERSION,
                                                      WALK_FORWARD_OPTIMIZER_VERSION):
                    return report("mismatch", "La versión no corresponde al método walk-forward")
                grid = stored_result["grid"]
                replay = walk_forward_stops(bars, version["definition"], dataset["timeframe"],
                                            grid["stop_losses"], grid["take_profits"],
                                            stored_result["folds"],
                                            engine_version=saved["engine_version"],
                                            optimizer_version=saved["optimizer_version"],
                                            final_holdout_fraction=stored_result.get("final_fraction", "0.2"))
            else:
                candidates = stored_result["candidates"]
                stops = sorted({row["stop_loss"] for row in candidates})
                targets = sorted({row["take_profit"] for row in candidates})
                replay = optimize_stops(bars, version["definition"], dataset["timeframe"],
                                        stops, targets, stored_result["fraction"],
                                        engine_version=saved["engine_version"],
                                        optimizer_version=saved["optimizer_version"])
    except (KeyError, TypeError, ValueError, LabError) as exc:
        return report("mismatch", f"No se pudo repetir el experimento: {exc}")
    checks["replay"] = content_hash(replay) == content_hash(stored_result)
    if not checks["replay"]:
        return report("mismatch", "La repetición difiere del resultado persistido")
    if checks["strategy"] is None or checks["stored_result"] is None:
        return report("legacy_unpinned", "Coincide, pero el registro antiguo no fijó todas sus huellas")
    return report("verified", "Dataset, estrategia y resultado coinciden con la repetición")
