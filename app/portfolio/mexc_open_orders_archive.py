"""Archivo local de órdenes Spot observadas, sin afirmar cobertura global."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import hmac
import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.portfolio.mexc_open_orders_survey import summarize_open_orders
from app.portfolio.models import RealMexcOpenOrdersObservation


class MexcOpenOrdersArchiveError(ValueError):
    pass


_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_SYMBOL = re.compile(r"[A-Z0-9]{3,30}\Z")
_TYPE = re.compile(r"[A-Z_]{2,32}\Z")
_ORDER_FIELDS = ("symbol", "order_id", "client_order_id", "side", "type", "status",
                 "price", "quantity", "executed_quantity", "remaining_quantity")


def _amount(raw: object, *, positive: bool = False) -> Decimal:
    if not isinstance(raw, str) or not 1 <= len(raw) <= 100:
        raise MexcOpenOrdersArchiveError("Cantidad de orden no decimal")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise MexcOpenOrdersArchiveError("Cantidad de orden no decimal") from exc
    if (not value.is_finite() or value < 0 or positive and value == 0
            or len(value.as_tuple().digits) > 40
            or abs(value.as_tuple().exponent) > 100
            or value != 0 and value.adjusted() > 40):
        raise MexcOpenOrdersArchiveError("Cantidad de orden fuera de límites")
    return value


def normalize_snapshot(snapshot: dict, *, account_id: str,
                       now: datetime | None = None) -> tuple[dict, dict]:
    try:
        summary = summarize_open_orders(snapshot, account_id=account_id, now=now)
    except (ValueError, TypeError) as exc:
        raise MexcOpenOrdersArchiveError("Sonda de órdenes no archivables") from exc
    orders = []
    seen_order_ids: set[str] = set()
    seen_client_ids: set[str] = set()
    for item in snapshot["orders"]:
        if (any(field not in item for field in _ORDER_FIELDS)
                or not _SYMBOL.fullmatch(item["symbol"])
                or any(not isinstance(item[field], str) or not _ID.fullmatch(item[field])
                       for field in ("order_id", "client_order_id"))
                or item["order_id"] in seen_order_ids
                or item["client_order_id"] in seen_client_ids
                or item["side"] not in ("BUY", "SELL")
                or not isinstance(item["type"], str) or not _TYPE.fullmatch(item["type"])):
            raise MexcOpenOrdersArchiveError("Orden abierta inválida o duplicada")
        price = _amount(item["price"], positive=item["type"] == "LIMIT")
        quantity = _amount(item["quantity"], positive=True)
        executed = _amount(item["executed_quantity"])
        remaining = _amount(item["remaining_quantity"], positive=True)
        with localcontext() as context:
            context.prec = 300
            if (executed + remaining != quantity
                    or item["status"] == "NEW" and executed != 0
                    or item["status"] == "PARTIALLY_FILLED" and executed == 0):
                raise MexcOpenOrdersArchiveError("Progreso de orden incoherente")
        seen_order_ids.add(item["order_id"])
        seen_client_ids.add(item["client_order_id"])
        orders.append({field: item[field] for field in _ORDER_FIELDS})
    payload = {"source": summary["source"], "accounting": "OBSERVED_NOT_IMPORTED",
               "coverage": summary["coverage"], "symbol": None,
               "observed_at": summary["observed_at"], "orders": orders}
    return payload, summary


def _canonical(payload: dict) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > 2_000_000:
        raise MexcOpenOrdersArchiveError("Demasiadas órdenes para archivar")
    return encoded


def _digest(row: RealMexcOpenOrdersObservation) -> str:
    if row.observed_at.tzinfo is None:
        raise MexcOpenOrdersArchiveError("Fecha de órdenes sin zona horaria")
    raw = json.dumps({"id": row.id, "account_id": row.account_id,
                      "account_ref": row.account_ref,
                      "observed_at": row.observed_at.astimezone(timezone.utc).isoformat(
                          timespec="microseconds"), "payload_json": row.payload_json},
                     ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def read_observation(row: RealMexcOpenOrdersObservation) -> dict:
    if not isinstance(row.payload_digest, str) or not hmac.compare_digest(
            row.payload_digest, _digest(row)):
        raise MexcOpenOrdersArchiveError("Evidencia de órdenes alterada")
    try:
        payload = json.loads(row.payload_json)
        if not isinstance(payload, dict):
            raise ValueError("not an object")
        normalized, summary = normalize_snapshot(payload, account_id=row.account_id,
                                                 now=row.observed_at)
    except (TypeError, ValueError, KeyError) as exc:
        raise MexcOpenOrdersArchiveError("Evidencia de órdenes ilegible") from exc
    if (_canonical(normalized) != row.payload_json
            or datetime.fromisoformat(normalized["observed_at"]).astimezone(timezone.utc)
            != row.observed_at):
        raise MexcOpenOrdersArchiveError("Evidencia de órdenes incoherente")
    return {"id": row.id, **summary, "saved": True}


def add_observation(db: Session, request_id: str, account_id: str,
                    account_ref: str, snapshot: dict) -> dict:
    payload, _ = normalize_snapshot(snapshot, account_id=account_id)
    row = RealMexcOpenOrdersObservation(
        id=request_id, account_id=account_id, account_ref=account_ref,
        observed_at=datetime.fromisoformat(payload["observed_at"]),
        payload_json=_canonical(payload), payload_digest="0" * 64,
    )
    row.payload_digest = _digest(row)
    db.add(row)
    db.flush()
    return read_observation(row)


def list_observations(db: Session, account_id: str, *, limit: int = 50) -> list[dict]:
    rows = db.scalars(select(RealMexcOpenOrdersObservation).where(
        RealMexcOpenOrdersObservation.account_id == account_id).order_by(
            RealMexcOpenOrdersObservation.observed_at.desc(),
            RealMexcOpenOrdersObservation.id.desc()).limit(limit)).all()
    return [read_observation(row) for row in rows]


def compare_observations(db: Session, account_id: str,
                         before_id: str, after_id: str) -> dict:
    """Diferencias visibles, nunca confirmación de fill, cancelación o cobertura."""
    if before_id == after_id:
        raise MexcOpenOrdersArchiveError("Elige dos revisiones distintas")
    before = db.get(RealMexcOpenOrdersObservation, before_id)
    after = db.get(RealMexcOpenOrdersObservation, after_id)
    if before is None or after is None or before.account_id != account_id or after.account_id != account_id:
        raise MexcOpenOrdersArchiveError("Revisión de órdenes no encontrada en esta cuenta")
    read_observation(before)
    read_observation(after)
    if before.account_ref != after.account_ref:
        raise MexcOpenOrdersArchiveError("Las revisiones pertenecen a identidades MEXC distintas")
    if not before.observed_at < after.observed_at:
        raise MexcOpenOrdersArchiveError("Las revisiones no están en orden temporal")
    prior = {item["order_id"]: item for item in json.loads(before.payload_json)["orders"]}
    current = {item["order_id"]: item for item in json.loads(after.payload_json)["orders"]}
    counts: dict[str, dict[str, int]] = {}

    def mark(symbol: str, kind: str) -> None:
        row = counts.setdefault(symbol, {"new": 0, "disappeared": 0,
                                         "progressed": 0, "unchanged": 0,
                                         "inconsistent": 0})
        row[kind] += 1

    for order_id, old in prior.items():
        newer = current.get(order_id)
        if newer is None:
            mark(old["symbol"], "disappeared")
            continue
        invariant = ("symbol", "client_order_id", "side", "type")
        if (any(old[field] != newer[field] for field in invariant)
                or Decimal(old["price"]) != Decimal(newer["price"])
                or Decimal(old["quantity"]) != Decimal(newer["quantity"])):
            mark(old["symbol"], "inconsistent")
            continue
        old_executed = Decimal(old["executed_quantity"])
        new_executed = Decimal(newer["executed_quantity"])
        if new_executed < old_executed or old["status"] == "PARTIALLY_FILLED" and newer["status"] == "NEW":
            mark(old["symbol"], "inconsistent")
        elif new_executed > old_executed:
            mark(old["symbol"], "progressed")
        else:
            mark(old["symbol"], "unchanged")
    for order_id, newer in current.items():
        if order_id not in prior:
            mark(newer["symbol"], "new")
    totals = {kind: sum(row[kind] for row in counts.values()) for kind in
              ("new", "disappeared", "progressed", "unchanged", "inconsistent")}
    blockers = ["UNFILTERED_COVERAGE_UNPROVEN"]
    if totals["new"]:
        blockers.append("NEW_ORDERS_NEED_ATTRIBUTION")
    if totals["disappeared"]:
        blockers.append("DISAPPEARED_ORDERS_NEED_FINAL_STATUS_AND_FILLS")
    if totals["progressed"]:
        blockers.append("PROGRESS_NEEDS_FILL_AUDIT")
    if totals["inconsistent"]:
        blockers.append("INCONSISTENT_ORDER_PROGRESS")
    return {
        "account_id": account_id,
        "before_id": before_id, "after_id": after_id,
        "before_observed_at": before.observed_at.isoformat(),
        "after_observed_at": after.observed_at.isoformat(),
        "coverage": "TWO_UNFILTERED_SNAPSHOTS_UNPROVEN",
        "status": ("INCONSISTENT" if totals["inconsistent"] else
                   "CHANGES_OBSERVED" if any(totals[k] for k in ("new", "disappeared", "progressed"))
                   else "NO_CHANGE_OBSERVED"),
        "counts": totals,
        "symbols": [{"symbol": symbol, **row} for symbol, row in sorted(counts.items())],
        "blockers": blockers,
        "reconciled": False, "orders_enabled": False,
    }
