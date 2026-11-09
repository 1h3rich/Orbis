"""Comprueba el origen LAB archivado de una sesión PAPER sin modificarla."""

from __future__ import annotations

from app.lab.engine import INTERVALS, LabError, supports_engine_version, validate_candles, validate_strategy
from app.lab.store import LabStore


def verify_paper_source(lab: LabStore, session: dict, definition: dict,
                        history: list[dict]) -> dict:
    """Distingue origen idéntico, divergente y no disponible.

    La prueba lee la versión y el dataset originales, pero no reinterpreta
    eventos PAPER ni presenta una coincidencia como conciliación con mercado.
    """
    checks = {"engine": False, "strategy": False, "dataset": False,
              "seed": False, "continuity": False}
    if not isinstance(definition, dict) or not isinstance(history, list):
        return {"verified": False, "status": "mismatch", "checks": checks,
                "reason": "Definición o histórico PAPER con formato inválido"}
    timeframe = session["timeframe"]
    if timeframe not in INTERVALS:
        return {"verified": False, "status": "mismatch", "checks": checks,
                "reason": "Temporalidad PAPER no admitida"}
    checks["engine"] = supports_engine_version(session["engine_version"], definition)
    if not checks["engine"]:
        return {"verified": False, "status": "mismatch", "checks": checks,
                "reason": "El motor archivado no interpreta esta definición"}
    try:
        source_definition = validate_strategy(lab.version(session["version_id"])["definition"])
        meta, seed = lab.dataset(session["dataset_id"])
    except LabError as exc:
        return {"verified": False, "status": "unverifiable", "checks": checks,
                "reason": f"Origen LAB no disponible o inválido: {exc}"}
    checks["strategy"] = source_definition == definition
    checks["dataset"] = (meta["hash"] == session["dataset_hash"]
                         and meta["symbol"] == session["symbol"]
                         and meta["timeframe"] == timeframe)
    checks["seed"] = len(history) >= len(seed) and history[:len(seed)] == seed
    if 3 <= len(history) <= 5000:
        try:
            bars, gaps = validate_candles(history, timeframe,
                                          now=session["last_t"] + 2 * INTERVALS[timeframe])
            checks["continuity"] = not gaps and bars[-1].t == session["last_t"]
        except LabError:
            pass
    if all(checks.values()):
        return {"verified": True, "status": "verified", "checks": checks,
                "reason": "Versión LAB, dataset y semilla PAPER coinciden"}
    return {"verified": False, "status": "mismatch", "checks": checks,
            "reason": "El origen o el histórico de la sesión PAPER cambió"}
