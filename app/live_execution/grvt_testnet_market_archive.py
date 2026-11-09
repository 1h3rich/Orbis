"""Append-only authenticated Testnet captures in the encrypted portfolio vault."""
from collections import Counter
from decimal import localcontext
import json
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
from app.live_execution.grvt_testnet_bracket_journal import _public
from app.live_execution.grvt_testnet_market_comparison import read_market_probe
from app.live_execution.grvt_testnet_market_probe import MarketProbeError
from app.live_execution.grvt_testnet_oco import _encode
from app.portfolio.models import (
    GrvtTestnetBracketClaim, GrvtTestnetBracketGroup, GrvtTestnetMarketSample,
)

MAX_MARKET_SAMPLES = 32
_SCHEMA = "orbis.grvt.testnet.market-archive.v1"
Lookup = tuple[Literal["client_order_id", "order_id"], str] | None


def _group(db: Session, parent: str) -> tuple[dict, str, int]:
    row = db.get(GrvtTestnetBracketGroup, parent)
    if row is None or row.status not in ("POST_CLAIMED", "ACKNOWLEDGED"):
        raise MarketProbeError("No hay un grupo TESTNET enviado para estas muestras")
    try:
        group = _public(db, row)
        claim = db.get(GrvtTestnetBracketClaim, parent)
        return group, row.payload_digest, int(claim.claimed_at_ns)
    except (GrvtTestnetBracketError, AttributeError, ValueError):
        raise MarketProbeError("El grupo TESTNET de la muestra no es verificable") from None


def _validated_sample(sample_json: str, parent: str, source: tuple[dict, str, int],
                      lookup: Lookup) -> dict:
    group, _, claimed_at = source
    with localcontext() as context:
        context.prec = 100
        sample = read_market_probe(sample_json, parent, group)
    if sample["orders"][0]["create_time_ns"] < claimed_at:
        raise ValueError("sample precedes posted entry")
    orders = sample["orders"]
    if lookup is None:
        if len(orders) != 3:
            raise ValueError("unexpected IOC")
    elif len(orders) != 4 or orders[3][lookup[0]] != lookup[1]:
        raise ValueError("IOC selector changed")
    return sample


def _record(row: GrvtTestnetMarketSample, parent: str, source: tuple[dict, str, int]) -> tuple[dict, Lookup]:
    try:
        data = json.loads(row.payload_json)
        if (_encode(data) != (row.payload_json, row.payload_digest)
                or set(data) != {"schema", "id", "parent", "plan_sha256", "lookup", "sample"}
                or data["schema"] != _SCHEMA or data["id"] != row.id or str(UUID(row.id)) != row.id
                or data["parent"] != parent or row.parent_client_order_id != parent
                or data["plan_sha256"] != source[1]):
            raise ValueError("archive identity changed")
        raw_lookup = data["lookup"]
        lookup = None
        if raw_lookup is not None:
            if (type(raw_lookup) is not list or len(raw_lookup) != 2
                    or raw_lookup[0] not in ("client_order_id", "order_id") or type(raw_lookup[1]) is not str):
                raise ValueError("invalid IOC selector")
            lookup = tuple(raw_lookup)
        sample = _validated_sample(_encode(data["sample"])[0], parent, source, lookup)
        if sample["observed_at_ns"] != row.observed_at_ns:
            raise ValueError("archive timestamp changed")
        return sample, lookup
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
        raise MarketProbeError("Muestra TESTNET guardada no verificable") from None


def find_market_sample(db: Session, parent: str, identifier: str) -> tuple[dict, Lookup] | None:
    row = db.get(GrvtTestnetMarketSample, identifier)
    if row is None:
        return None
    if row.parent_client_order_id != parent:
        raise MarketProbeError("La muestra guardada pertenece a otro grupo TESTNET")
    return _record(row, parent, _group(db, parent))


def append_market_sample(db: Session, parent: str, identifier: str, sample: dict, *,
                         lookup: Lookup) -> dict:
    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    previous = find_market_sample(db, parent, identifier)
    if previous is not None:
        if previous[1] != lookup:
            raise MarketProbeError("El identificador de captura ya se usó para otro IOC")
        return previous[0]
    count = db.scalar(select(func.count()).select_from(GrvtTestnetMarketSample).where(
        GrvtTestnetMarketSample.parent_client_order_id == parent))
    if count >= MAX_MARKET_SAMPLES:
        raise MarketProbeError("El grupo alcanzó el máximo de 32 muestras guardadas")
    try:
        source = _group(db, parent)
        sample = _validated_sample(_encode(sample)[0], parent, source, lookup)
        raw, digest = _encode({"schema": _SCHEMA, "id": identifier, "parent": parent,
                               "plan_sha256": source[1], "lookup": lookup, "sample": sample})
    except (ValueError, KeyError, TypeError, AttributeError):
        raise MarketProbeError("No se pudo verificar la muestra para guardarla") from None
    db.add(GrvtTestnetMarketSample(id=identifier, parent_client_order_id=parent,
                                  observed_at_ns=sample["observed_at_ns"],
                                  payload_json=raw, payload_digest=digest))
    db.commit()
    return sample


def list_market_samples(db: Session, parent: str) -> list[dict]:
    source = _group(db, parent)
    query = select(GrvtTestnetMarketSample).where(
        GrvtTestnetMarketSample.parent_client_order_id == parent).order_by(
        GrvtTestnetMarketSample.observed_at_ns, GrvtTestnetMarketSample.id).limit(MAX_MARKET_SAMPLES + 1)
    results = []
    for row in db.scalars(query.execution_options(yield_per=1)):
        sample, _ = _record(row, parent, source)
        results.append({"id": row.id, "observed_at_ns": row.observed_at_ns, "remaining_size": sample["remaining_size"],
                        "has_ioc": len(sample["orders"]) == 4})
    if len(results) > MAX_MARKET_SAMPLES:
        raise MarketProbeError("El archivo del grupo supera el límite de muestras")
    return results


def verify_all_market_samples(db: Session) -> None:
    counts: Counter[str] = Counter()
    sources = {}
    for row in db.scalars(select(GrvtTestnetMarketSample).execution_options(yield_per=1)):
        parent = row.parent_client_order_id
        counts[parent] += 1
        if counts[parent] > MAX_MARKET_SAMPLES:
            raise MarketProbeError("El archivo del grupo supera el límite de muestras")
        if parent not in sources:
            sources[parent] = _group(db, parent)
        _record(row, parent, sources[parent])
