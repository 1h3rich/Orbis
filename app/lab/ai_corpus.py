"""Deterministic, local corpus of explicitly selected LAB annotations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from app.lab.ai_research import COMPARISON_SCHEMA
from app.lab.engine import LabError
from app.lab.store import content_hash
from app.security.sensitive_text import contains_sensitive_ai_text

SCHEMA = "orbis.lab.ai-corpus.v1"
MAX_RECORDS = 50
MAX_BYTES = 4 * 1024 * 1024
EVIDENCE_SCHEMAS = {
    "research": ("orbis.lab.ai-summary.v1",),
    "backtest": ("orbis.lab.ai-backtest-summary.v1", COMPARISON_SCHEMA),
}


def _canonical(value: dict) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (ValueError, TypeError) as exc:
        raise LabError("Un análisis seleccionado no se puede exportar como JSONL") from exc


def build_ai_corpus(records: Iterable[dict]) -> dict:
    lines = []
    size = 0
    for record in records:
        evidence = record["evidence"]
        if (record["kind"] not in EVIDENCE_SCHEMAS
                or evidence.get("schema") not in EVIDENCE_SCHEMAS[record["kind"]]
                or evidence.get("environment") != "LAB"
                or evidence.get("theoretical_only") is not True
                or evidence.get("source_run") != record["run_id"]):
            raise LabError("Un análisis seleccionado no corresponde a su evidencia LAB")
        if (contains_sensitive_ai_text(record["question"], question=True)
                or contains_sensitive_ai_text(record["answer"], question=True)):
            raise LabError("Un análisis seleccionado contiene un identificador sensible")
        sample = {
            "schema": SCHEMA,
            "environment": "LAB",
            "purpose": "interpret_historical_research",
            "input": {"question": record["question"], "evidence": evidence},
            "annotation": {"text": record["answer"], "origin": "model_generated",
                           "model": record["model"], "usage": record["usage"]},
            "provenance": {"analysis_id": record["id"], "archived_at": record["created_at"],
                           "kind": record["kind"], "run_id": record["run_id"],
                           "archive_sha256": record["hash"], "evidence_sha256": content_hash(evidence)},
            "limitations": {"theoretical_only": True, "annotation_verified": False,
                            "eligible_for_point_in_time_signals": False},
        }
        material = _canonical(sample)
        if contains_sensitive_ai_text(material.decode("utf-8")):
            raise LabError("Un análisis seleccionado contiene un identificador sensible")
        sample["row_sha256"] = hashlib.sha256(material).hexdigest()
        line = _canonical(sample) + b"\n"
        size += len(line)
        if size > MAX_BYTES:
            raise LabError("La selección supera el tamaño máximo de exportación")
        lines.append(line)
    data = b"".join(lines)
    return {"schema": SCHEMA, "record_count": len(lines), "sha256": hashlib.sha256(data).hexdigest(),
            "jsonl": data.decode("utf-8")}
