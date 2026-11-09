"""Comprobación reproducible de una simulación DCA para API y PAPER."""

from __future__ import annotations

from datetime import datetime, timezone

from app.automation.dca import SUPPORTED_ENGINE_VERSIONS, simulate_dca, validate_dca
from app.automation.store import AutomationStore
from app.automation.validation import VALIDATION_ENGINE_VERSION, chronological_dca_holdout
from app.lab.engine import LabError, validate_candles
from app.lab.store import LabStore, content_hash


def verify_dca_run(store: AutomationStore, lab: LabStore, saved: dict) -> dict:
    """Comprueba huellas y repite las tres variantes sin alterar el run."""
    run_id = saved["id"]
    checks: dict[str, bool | None] = {"dataset": None, "definition": None,
                                      "stored_result": None, "replay": None}

    def report(status: str, reason: str) -> dict:
        return {"run_id": run_id, "verified": status == "verified",
                "status": status, "reason": reason, "checks": checks}

    try:
        dataset, raw = lab.dataset(saved["dataset_id"])
        checks["dataset"] = dataset["hash"] == saved["dataset_hash"]
        if not checks["dataset"]:
            return report("mismatch", "El histórico ya no coincide con la simulación DCA")
        version = store.version(saved["version_id"])
    except LabError as exc:
        return report("mismatch", str(exc))
    if saved.get("definition_hash") is not None:
        checks["definition"] = content_hash(version["definition"]) == saved["definition_hash"]
        if not checks["definition"]:
            return report("mismatch", "La versión DCA guardada fue alterada")
    metadata = {"id", "version_id", "dataset_id", "dataset_hash", "created_at",
                "engine_version", "definition_hash", "result_hash"}
    stored_result = {key: value for key, value in saved.items() if key not in metadata}
    if saved.get("result_hash") is not None:
        checks["stored_result"] = content_hash(stored_result) == saved["result_hash"]
        if not checks["stored_result"]:
            return report("mismatch", "El resultado DCA guardado fue alterado")
    if saved["engine_version"] not in SUPPORTED_ENGINE_VERSIONS:
        return report("engine_unavailable", "El motor DCA original no está disponible")
    if (saved["engine_version"] == "dca-1.1"
            and version["definition"].get("reference_mode", "initial") != "initial"):
        return report("engine_unavailable", "El motor DCA original no admite esa referencia")
    try:
        bars, gaps = validate_candles(raw, dataset["timeframe"],
                                      now=int(datetime.now(timezone.utc).timestamp()))
        if gaps:
            return report("mismatch", "El histórico contiene huecos")
        replay = simulate_dca(bars, validate_dca(version["definition"]),
                              engine_version=saved["engine_version"], timeframe=dataset["timeframe"])
    except (KeyError, TypeError, ValueError, LabError) as exc:
        return report("mismatch", f"No se pudo repetir DCA: {exc}")
    checks["replay"] = content_hash(replay) == content_hash(stored_result)
    if not checks["replay"]:
        return report("mismatch", "La repetición DCA difiere del resultado guardado")
    if checks["definition"] is None or checks["stored_result"] is None:
        return report("legacy_unpinned", "Coincide, pero el registro antiguo no fijó todas sus huellas")
    return report("verified", "Histórico, versión y resultados DCA coinciden con la repetición")


def verify_dca_validation(store: AutomationStore, lab: LabStore, saved: dict) -> dict:
    """Replay the pinned reserve and reread identities; never grants authority."""
    checks = {"dataset": False, "definition": False, "stored_result": False,
              "replay": False, "stable_inputs": False}

    def report(status: str, reason: str) -> dict:
        return {"run_id": saved.get("id"), "verified": status == "verified",
                "status": status, "reason": reason, "checks": checks}

    if saved.get("engine_version") != VALIDATION_ENGINE_VERSION:
        return report("engine_unavailable", "El motor de validación DCA original no está disponible")
    try:
        dataset, raw = lab.dataset(saved["dataset_id"])
        version = store.version(saved["version_id"])
        checks["dataset"] = dataset["hash"] == saved["dataset_hash"] and not dataset["gaps"]
        checks["definition"] = content_hash(version["definition"]) == saved.get("definition_hash")
        metadata = {"id", "version_id", "dataset_id", "dataset_hash", "created_at",
                    "engine_version", "definition_hash", "result_hash"}
        result = {key: value for key, value in saved.items() if key not in metadata}
        checks["stored_result"] = content_hash(result) == saved.get("result_hash")
        if not all(checks[key] for key in ("dataset", "definition", "stored_result")):
            return report("mismatch", "Histórico, regla o resultado de la reserva DCA alterados")
        bars, gaps = validate_candles(raw, dataset["timeframe"],
                                     now=int(datetime.now(timezone.utc).timestamp()))
        if gaps:
            return report("mismatch", "El histórico de la reserva DCA contiene huecos")
        replay = chronological_dca_holdout(bars, version["definition"], dataset["timeframe"],
            result["fraction"], dca_engine_version=result["dca_engine_version"])
        checks["replay"] = content_hash(replay) == saved["result_hash"]
        if not checks["replay"]:
            return report("mismatch", "La repetición de la reserva DCA difiere del resultado guardado")
        current_dataset, _ = lab.dataset(saved["dataset_id"])
        current_version = store.version(saved["version_id"])
        current = store.validation(saved["id"])
        checks["stable_inputs"] = (current_dataset["hash"] == dataset["hash"]
            and not current_dataset["gaps"]
            and content_hash(current_version["definition"]) == saved["definition_hash"]
            and content_hash(current) == content_hash(saved))
        if not checks["stable_inputs"]:
            return report("mismatch", "Las entradas cambiaron durante la verificación DCA")
    except (LabError, KeyError, TypeError, ValueError) as exc:
        return report("mismatch", f"No se pudo comprobar la reserva DCA: {exc}")
    return report("verified", "Histórico, versión, corte y resultados DCA reproducibles")
