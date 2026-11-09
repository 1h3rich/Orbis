"""Exchange of declarative DCA rules without local identity or permissions."""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Literal, Never, TypedDict

from app.automation.dca import dca_engine_for_rule, validate_dca
from app.lab.engine import LabError
from app.lab.store import content_hash


BUNDLE_SCHEMA = "orbis.automation.dca.v1"
MAX_DCA_BUNDLE_BYTES = 32_768


class DcaBundleReview(TypedDict):
    name: str
    definition: dict
    engine_version: str
    source_version: int | None
    integrity: Literal["TEMPLATE", "CHECKSUM_VERIFIED"]
    review_hash: str


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise LabError("El JSON DCA contiene campos duplicados")
        value[key] = item
    return value


def _invalid_constant(value: str) -> Never:
    raise LabError("El JSON DCA contiene un número no finito")


def _plan(name: object, definition: object) -> tuple[str, dict]:
    if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 100
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 or 0xD800 <= ord(char) <= 0xDFFF for char in name)):
        raise LabError("Nombre DCA inválido")
    if not isinstance(definition, dict):
        raise LabError("Regla DCA inválida")
    canonical = validate_dca(definition)
    if (definition.keys() - canonical.keys()
            or any(set(tier) != {"drop_pct", "amount"} for tier in definition.get("dips", []))):
        raise LabError("El plan contiene campos que ORBIS no admite")
    return name.strip(), canonical


def export_dca_bundle(source: dict) -> dict:
    name, canonical = _plan(source["name"], source["definition"])
    if canonical != source["definition"]:
        raise LabError("La versión DCA guardada no es canónica")
    plan = {"name": name, "version": source["version"], "engine_version": dca_engine_for_rule(canonical),
            "definition": canonical}
    payload = {"schema": BUNDLE_SCHEMA, "scope": "RULE_ONLY", "plan": plan}
    return payload | {"sha256": content_hash(payload)}


def review_dca_bundle(raw: bytes) -> DcaBundleReview:
    if not raw or len(raw) > MAX_DCA_BUNDLE_BYTES:
        raise LabError("El archivo DCA debe ocupar entre 1 byte y 32 KiB")
    try:
        value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except LabError:
        raise
    except (ValueError, RecursionError) as exc:
        raise LabError("El archivo DCA no contiene JSON UTF-8 válido") from exc
    if not isinstance(value, dict):
        raise LabError("Formato DCA no admitido")
    integrity: Literal["TEMPLATE", "CHECKSUM_VERIFIED"]
    if set(value) == {"name", "definition"}:
        name, definition = _plan(value["name"], value["definition"])
        integrity, version = "TEMPLATE", None
    elif (set(value) == {"schema", "scope", "plan", "sha256"} and value["schema"] == BUNDLE_SCHEMA
          and value["scope"] == "RULE_ONLY" and isinstance(value["plan"], dict)):
        plan = value["plan"]
        if (set(plan) != {"name", "version", "engine_version", "definition"}
                or type(plan["version"]) is not int or not 1 <= plan["version"] <= 2**63 - 1):
            raise LabError("Versión de origen DCA inválida")
        name, definition = _plan(plan["name"], plan["definition"])
        if plan["engine_version"] != dca_engine_for_rule(definition):
            raise LabError("Motor DCA de origen incompatible")
        canonical = export_dca_bundle({"name": name, "version": plan["version"], "definition": definition})
        if canonical != value:
            raise LabError("La huella o los datos del plan DCA no coinciden")
        integrity, version = "CHECKSUM_VERIFIED", plan["version"]
    else:
        raise LabError("Formato DCA no admitido. Usa un JSON de ORBIS DCA o una plantilla de regla")
    return {"name": name, "definition": definition, "engine_version": dca_engine_for_rule(definition),
            "source_version": version, "integrity": integrity, "review_hash": sha256(raw).hexdigest()}
