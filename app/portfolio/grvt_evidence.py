"""Archivo GRVT cifrado: evidencia observada, nunca operación contable."""

from __future__ import annotations

import calendar
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.portfolio.domain import PortfolioError, utc_datetime
from app.portfolio.models import GrvtOrderEvidence


_REF = re.compile(r"^[0-9a-f]{64}$")
_NUMBER = re.compile(r"^[0-9]{1,30}$")
_ID = re.compile(r"^[A-Za-z0-9x]{1,80}$")
_INSTRUMENT = re.compile(r"^[A-Za-z0-9_:\-]{3,80}$")
_TRADE = re.compile(r"^[A-Za-z0-9_:\-]{1,100}$")
_CURRENCY = re.compile(r"^[A-Z0-9]{2,24}$")
_TOP = frozenset(("source", "accounting", "observed_at", "account_ref",
                  "history_pages_drained", "fill_sizes_match_order_state",
                  "complete_for_execution", "order", "fills"))
_ORDER = frozenset(("sub_account_id", "order_id", "client_order_id", "status",
                    "reduce_only", "legs", "update_time_ns", "create_time_ns"))
_LEG = frozenset(("instrument", "side", "size", "traded_size", "limit_price"))
_FILL = frozenset(("trade_id", "event_time_ns", "instrument", "side", "role",
                   "size", "price", "fee", "builder_fee", "fee_currency", "realized_pnl"))


def _decimal(value: object, *, positive: bool = False, nonnegative: bool = False) -> Decimal:
    if not isinstance(value, str) or not 1 <= len(value) <= 80:
        raise PortfolioError("GRVT evidence has invalid decimal text")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise PortfolioError("GRVT evidence has invalid decimal text") from exc
    if (not number.is_finite() or len(number.as_tuple().digits) > 40
            or not -40 <= number.as_tuple().exponent <= 40
            or positive and number <= 0 or nonnegative and number < 0):
        raise PortfolioError("GRVT evidence amount is out of range")
    return number


def _validate(snapshot: dict) -> tuple[datetime, str, str, str, str, int, bool, str, str]:
    if not isinstance(snapshot, dict) or set(snapshot) != _TOP:
        raise PortfolioError("GRVT evidence has unexpected fields")
    if (snapshot["source"] != "GRVT_MAINNET_PRIVATE_READONLY"
            or snapshot["accounting"] != "OBSERVED_NOT_IMPORTED"
            or snapshot["history_pages_drained"] is not True
            or snapshot["complete_for_execution"] is not False
            or type(snapshot["fill_sizes_match_order_state"]) is not bool
            or not isinstance(snapshot["account_ref"], str)
            or not _REF.fullmatch(snapshot["account_ref"])):
        raise PortfolioError("GRVT evidence provenance is invalid")
    try:
        observed_at = utc_datetime(datetime.fromisoformat(snapshot["observed_at"]))
    except (TypeError, ValueError, PortfolioError) as exc:
        raise PortfolioError("GRVT evidence observation time is invalid") from exc
    observed_ns = calendar.timegm(observed_at.utctimetuple()) * 1_000_000_000 + observed_at.microsecond * 1000
    order, fills = snapshot["order"], snapshot["fills"]
    if not isinstance(order, dict) or set(order) != _ORDER:
        raise PortfolioError("GRVT evidence order is invalid")
    sub_id, client_id, order_id = (order[key] for key in (
        "sub_account_id", "client_order_id", "order_id"))
    if (not isinstance(sub_id, str) or not _NUMBER.fullmatch(sub_id)
            or not isinstance(client_id, str) or not _NUMBER.fullmatch(client_id)
            or len(client_id) > 20 or not isinstance(order_id, str)
            or not _ID.fullmatch(order_id)
            or order["status"] not in ("PENDING", "OPEN", "FILLED", "REJECTED", "CANCELLED")
            or type(order["reduce_only"]) is not bool
            or not isinstance(order["create_time_ns"], str)
            or not _NUMBER.fullmatch(order["create_time_ns"])
            or not isinstance(order["update_time_ns"], str)
            or not _NUMBER.fullmatch(order["update_time_ns"])
            or not 0 < int(order["create_time_ns"]) <= int(order["update_time_ns"])
            or int(order["update_time_ns"]) > observed_ns + 2_000_000_000):
        raise PortfolioError("GRVT evidence order identity is invalid")
    legs = order["legs"]
    if not isinstance(legs, list) or not 1 <= len(legs) <= 20:
        raise PortfolioError("GRVT evidence has invalid legs")
    totals = {}
    for leg in legs:
        if (not isinstance(leg, dict) or set(leg) != _LEG
                or not isinstance(leg["instrument"], str)
                or not _INSTRUMENT.fullmatch(leg["instrument"])
                or leg["side"] not in ("BUY", "SELL")):
            raise PortfolioError("GRVT evidence leg is invalid")
        key = (leg["instrument"], leg["side"])
        if key in totals:
            raise PortfolioError("GRVT evidence has duplicate legs")
        size = _decimal(leg["size"], positive=True)
        traded = _decimal(leg["traded_size"], nonnegative=True)
        if traded > size:
            raise PortfolioError("GRVT evidence traded size exceeds order")
        if leg["limit_price"] is not None:
            _decimal(leg["limit_price"], nonnegative=True)
        totals[key] = [Decimal(0), traded]
    if not isinstance(fills, list) or len(fills) > 10000:
        raise PortfolioError("GRVT evidence fill set is invalid")
    seen = set()
    for fill in fills:
        if not isinstance(fill, dict) or set(fill) != _FILL:
            raise PortfolioError("GRVT evidence fill is invalid")
        trade_id = fill["trade_id"]
        key = (fill["instrument"], fill["side"])
        if (not isinstance(trade_id, str) or not _TRADE.fullmatch(trade_id)
                or trade_id in seen or key not in totals
                or fill["role"] not in ("MAKER", "TAKER")
                or not isinstance(fill["event_time_ns"], str)
                or not _NUMBER.fullmatch(fill["event_time_ns"])
                or not int(order["create_time_ns"]) <= int(fill["event_time_ns"]) <= observed_ns + 2_000_000_000
                or not isinstance(fill["fee_currency"], str)
                or not _CURRENCY.fullmatch(fill["fee_currency"])):
            raise PortfolioError("GRVT evidence fill identity is invalid")
        seen.add(trade_id)
        totals[key][0] += _decimal(fill["size"], positive=True)
        _decimal(fill["price"], positive=True)
        _decimal(fill["fee"])
        _decimal(fill["builder_fee"], nonnegative=True)
        _decimal(fill["realized_pnl"])
    matches = all(actual == expected for actual, expected in totals.values())
    if matches is not snapshot["fill_sizes_match_order_state"]:
        raise PortfolioError("GRVT evidence size comparison is invalid")
    payload = json.dumps({"account_ref": snapshot["account_ref"], "order": order,
                          "fills": fills, "fill_sizes_match_order_state": matches},
                         sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(payload.encode("ascii")).hexdigest()
    return observed_at, snapshot["account_ref"], sub_id, client_id, order_id, len(fills), matches, payload, digest


def save_evidence(db: Session, snapshot: dict) -> dict:
    observed, account_ref, sub_id, client_id, order_id, count, matches, payload, digest = _validate(snapshot)
    try:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        query = db.query(GrvtOrderEvidence).filter_by(
            account_ref=account_ref, sub_account_id=sub_id, client_order_id=client_id)
        latest = query.order_by(GrvtOrderEvidence.observed_at.desc(), GrvtOrderEvidence.id.desc()).first()
        if latest is not None:
            if latest.order_id != order_id:
                raise PortfolioError("GRVT order ID changed for the same client ID")
            previous = _read_payload(latest)
            old_order = previous["order"]
            if (int(snapshot["order"]["update_time_ns"]) < int(old_order["update_time_ns"])
                    or observed < latest.observed_at):
                raise PortfolioError("GRVT evidence regressed")
            old_fills = {item["trade_id"]: item for item in previous["fills"]}
            new_fills = {item["trade_id"]: item for item in snapshot["fills"]}
            if any(new_fills.get(key) != value for key, value in old_fills.items()):
                raise PortfolioError("GRVT evidence omitted or changed fills")
            if latest.sizes_match and not matches:
                raise PortfolioError("GRVT evidence lost a verified size match")
            if old_order["status"] in ("FILLED", "REJECTED", "CANCELLED") and snapshot["order"]["status"] != old_order["status"]:
                raise PortfolioError("GRVT terminal order state changed")
            if old_order["status"] in ("FILLED", "REJECTED", "CANCELLED") and snapshot["order"]["legs"] != old_order["legs"]:
                raise PortfolioError("GRVT terminal order quantities changed")
        duplicate = query.filter_by(payload_digest=digest).first()
        if duplicate is not None:
            db.rollback()
            return {"id": duplicate.id, "archived": False, "fill_count": duplicate.fill_count}
        row = GrvtOrderEvidence(id=str(uuid4()), account_ref=account_ref,
                                sub_account_id=sub_id, client_order_id=client_id,
                                order_id=order_id, status=snapshot["order"]["status"],
                                observed_at=observed, fill_count=count, sizes_match=matches,
                                payload_json=payload, payload_digest=digest)
        db.add(row)
        db.commit()
        return {"id": row.id, "archived": True, "fill_count": count}
    except Exception:
        db.rollback()
        raise


def _read_payload(row: GrvtOrderEvidence) -> dict:
    if hashlib.sha256(row.payload_json.encode("ascii")).hexdigest() != row.payload_digest:
        raise PortfolioError("GRVT evidence integrity check failed")
    try:
        payload = json.loads(row.payload_json)
    except (ValueError, TypeError) as exc:
        raise PortfolioError("GRVT evidence payload is corrupt") from exc
    if (not isinstance(payload, dict) or set(payload) != {"account_ref", "order", "fills", "fill_sizes_match_order_state"}
            or payload["account_ref"] != row.account_ref
            or payload["order"]["sub_account_id"] != row.sub_account_id
            or payload["order"]["client_order_id"] != row.client_order_id
            or payload["order"]["order_id"] != row.order_id
            or payload["order"]["status"] != row.status
            or len(payload["fills"]) != row.fill_count
            or payload["fill_sizes_match_order_state"] is not row.sizes_match):
        raise PortfolioError("GRVT evidence payload does not match index")
    return payload


def list_evidence(db: Session, *, limit: int = 100) -> list[dict]:
    if type(limit) is not int or not 1 <= limit <= 500:
        raise PortfolioError("Invalid GRVT evidence limit")
    rows = db.query(GrvtOrderEvidence).order_by(
        GrvtOrderEvidence.observed_at.desc(), GrvtOrderEvidence.id.desc()).limit(limit).all()
    return [{"id": row.id, "account_ref": row.account_ref,
             "sub_account_id": row.sub_account_id, "client_order_id": row.client_order_id,
             "status": row.status, "observed_at": row.observed_at.isoformat(),
             "fill_count": row.fill_count, "fill_sizes_match_order_state": row.sizes_match}
            for row in rows]


def read_evidence(db: Session, identifier: str) -> dict:
    try:
        identifier = str(UUID(identifier))
    except (TypeError, ValueError) as exc:
        raise PortfolioError("Invalid GRVT evidence ID") from exc
    row = db.get(GrvtOrderEvidence, identifier)
    if row is None:
        raise PortfolioError("GRVT evidence not found")
    payload = _read_payload(row)
    return {"id": row.id, "source": "GRVT_MAINNET_PRIVATE_READONLY",
            "accounting": "OBSERVED_NOT_IMPORTED", "complete_for_execution": False,
            "observed_at": row.observed_at.isoformat(), **payload}
