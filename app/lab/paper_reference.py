"""Referencia histórica verificable para una sesión PAPER.

Una semilla actualizada es otro dataset: conserva procedencia, no resultados.
"""

from __future__ import annotations

from app.lab.engine import LabError
from app.lab.store import LabStore


def paper_lab_reference(lab: LabStore, dataset_id: str, dataset_hash: str,
                        version_id: str, source_run_id: str | None = None
                        ) -> tuple[dict | None, str | None, int]:
    """Referencia fijada por LAB, o último backtest compatible para sesiones antiguas."""
    runs = lab.runs()
    latest_by_dataset = {}
    for run in runs:
        if run["version_id"] != version_id:
            continue
        key = (run["dataset_id"], run["dataset_hash"])
        current = latest_by_dataset.get(key)
        if current is None or (run["created_at"], run["id"]) > (current["created_at"], current["id"]):
            latest_by_dataset[key] = run
    if source_run_id is not None:
        pinned = next((run for run in runs if run["id"] == source_run_id), None)
        if pinned is None:
            raise LabError("El backtest LAB de origen ya no está disponible")
        if pinned["version_id"] != version_id:
            raise LabError("El backtest LAB de origen pertenece a otra versión")
        target = (pinned["dataset_id"], pinned["dataset_hash"])
        if target == (dataset_id, dataset_hash):
            return pinned, "EXACT", 0
    else:
        target = None
    exact = latest_by_dataset.get((dataset_id, dataset_hash))
    if exact and source_run_id is None:
        return exact, "EXACT", 0
    frontier = [(dataset_id, dataset_hash, (dataset_id,))]
    visited = {dataset_id}
    depth = 0
    while frontier:
        depth += 1
        upcoming = []
        origins = set()
        for child_id, child_hash, path in frontier:
            for edge in lab.dataset_lineage(child_id):
                if edge["child_hash"] != child_hash:
                    raise LabError("La procedencia del histórico tiene una huella incompatible")
                parent_id = edge["parent_id"]
                if parent_id in path:
                    raise LabError("La procedencia del histórico contiene un ciclo")
                if parent_id in visited:
                    continue
                parent, _ = lab.dataset(parent_id)
                if parent["hash"] != edge["parent_hash"]:
                    raise LabError("La procedencia del histórico tiene una huella incompatible")
                visited.add(parent_id)
                origins.add((parent_id, edge["parent_hash"]))
                upcoming.append((parent_id, edge["parent_hash"], (*path, parent_id)))
        if target in origins:
            return pinned, "REFRESHED_FROM", depth
        linked = [latest_by_dataset[key] for key in origins if key in latest_by_dataset] if source_run_id is None else []
        if linked:
            return (max(linked, key=lambda run: (run["created_at"], run["id"])),
                    "REFRESHED_FROM", depth)
        frontier = upcoming
    if source_run_id is not None:
        raise LabError("El backtest LAB de origen no pertenece al histórico PAPER ni a su procedencia verificada")
    return None, None, 0
