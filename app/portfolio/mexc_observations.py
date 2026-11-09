"""Archivo cifrado de fills MEXC observados, separado del libro contable."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from uuid import uuid4

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.portfolio.domain import PortfolioError, decimal_value, utc_datetime
from app.portfolio.models import MexcObservationBatch, MexcObservedFill


_REF = re.compile(r"^[0-9a-f]{64}$")
_SYMBOL = re.compile(r"^[A-Z0-9]{3,30}$")
_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_ASSET = re.compile(r"^[A-Z0-9]{2,24}$")


def _money(raw: object, *, positive: bool) -> Decimal:
    if not isinstance(raw, str) or len(raw) > 100:
        raise PortfolioError("Observed MEXC amount must be a decimal string")
    try:
        value = decimal_value(Decimal(raw), positive=positive)
    except (InvalidOperation, PortfolioError) as exc:
        raise PortfolioError("Observed MEXC amount is invalid") from exc
    if value < 0 or abs(value.as_tuple().exponent) > 100:
        raise PortfolioError("Observed MEXC amount has invalid magnitude")
    return Decimal(0) if value == 0 else value


def _canonical(value: Decimal) -> str:
    raw = format(value, "f")
    return (raw.rstrip("0").rstrip(".") if "." in raw else raw) or "0"


def observed_fill_digest_matches(fill: MexcObservedFill) -> bool:
    """Contrasta campos archivados con su huella original, sin red externa."""
    try:
        payload = {
            "symbol": fill.symbol, "trade_id": fill.trade_id,
            "order_id": fill.order_id, "side": fill.side,
            "time_ms": fill.time_ms, "commission_asset": fill.commission_asset,
            **{name: _canonical(getattr(fill, name)) for name in
               ("price", "quantity", "quote_quantity", "commission")},
        }
        digest = hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        return digest == fill.payload_digest
    except (TypeError, ValueError, ArithmeticError, AttributeError):
        return False


def _validate(account_ref: str, snapshot: dict) -> tuple[str, datetime, int, list[dict], int | None, int | None, int, str]:
    if not isinstance(account_ref, str) or not _REF.fullmatch(account_ref):
        raise PortfolioError("Invalid MEXC account reference")
    if not isinstance(snapshot, dict) or snapshot.get("source") != "MEXC_SPOT_PRIVATE_READONLY" or snapshot.get("accounting") != "OBSERVED_NOT_IMPORTED":
        raise PortfolioError("Observed MEXC source is invalid")
    coverage = snapshot.get("coverage")
    if coverage not in ("RECENT_MONTH_SINGLE_PAGE_INCOMPLETE",
                        "RECENT_MONTH_WINDOW_BELOW_PAGE_LIMIT", "ORDER_ID_BELOW_PAGE_LIMIT"):
        raise PortfolioError("Observed MEXC coverage is invalid")
    symbol = snapshot.get("symbol")
    limit = snapshot.get("limit")
    rows = snapshot.get("trades")
    if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
        raise PortfolioError("Observed MEXC symbol is invalid")
    pages = snapshot.get("pages", 1)
    start_ms, end_ms = snapshot.get("start_ms"), snapshot.get("end_ms")
    order_id = snapshot.get("order_id")
    if coverage == "RECENT_MONTH_WINDOW_BELOW_PAGE_LIMIT":
        if (type(start_ms) is not int or type(end_ms) is not int or not 0 < start_ms < end_ms
                or type(pages) is not int or not 1 <= pages <= 8 or limit != 1000
                or order_id is not None):
            raise PortfolioError("Observed MEXC window is invalid")
    elif start_ms is not None or end_ms is not None or pages != 1:
        raise PortfolioError("Observed MEXC page has unexpected window")
    if coverage == "ORDER_ID_BELOW_PAGE_LIMIT":
        if (not isinstance(order_id, str) or not _ID.fullmatch(order_id)
                or limit != 1000 or not isinstance(rows, list) or len(rows) >= 1000):
            raise PortfolioError("Observed MEXC order page is incomplete")
    elif order_id is not None:
        raise PortfolioError("Observed MEXC page has unexpected order ID")
    if (type(limit) is not int or not 1 <= limit <= 1000 or not isinstance(rows, list)
            or len(rows) > limit * pages):
        raise PortfolioError("Observed MEXC page is invalid")
    try:
        observed_at = utc_datetime(datetime.fromisoformat(snapshot["observed_at"]))
    except (KeyError, ValueError, TypeError, PortfolioError) as exc:
        raise PortfolioError("Observed MEXC timestamp is invalid") from exc
    parsed = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise PortfolioError("Observed MEXC fill is invalid")
        trade_id, order_id = row.get("id"), row.get("order_id")
        fee_asset, side, time_ms = row.get("commission_asset"), row.get("side"), row.get("time_ms")
        if (not isinstance(trade_id, str) or not _ID.fullmatch(trade_id) or trade_id in seen
                or not isinstance(order_id, str) or not _ID.fullmatch(order_id)
                or snapshot.get("order_id") is not None and order_id != snapshot["order_id"]
                or not isinstance(fee_asset, str) or not _ASSET.fullmatch(fee_asset)
                or side not in ("BUY", "SELL") or type(time_ms) is not int
                or not 0 < time_ms <= 8_640_000_000_000_000
                or start_ms is not None and time_ms < start_ms
                or end_ms is not None and time_ms > end_ms):
            raise PortfolioError("Observed MEXC fill identity is invalid")
        seen.add(trade_id)
        amounts = {key: _money(row.get(key), positive=key != "commission")
                   for key in ("price", "quantity", "quote_quantity", "commission")}
        payload = {"symbol": symbol, "trade_id": trade_id, "order_id": order_id,
                   "side": side, "time_ms": time_ms, "commission_asset": fee_asset,
                   **{key: _canonical(value) for key, value in amounts.items()}}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        parsed.append({**payload, **amounts, "payload_digest": digest})
    return symbol, observed_at, limit, parsed, start_ms, end_ms, pages, coverage


def save_observation(db: Session, account_ref: str, snapshot: dict) -> dict:
    """Inserta sólo fills nuevos; una colisión con contenido distinto aborta todo."""
    try:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        result = save_observation_in_transaction(db, account_ref, snapshot)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def save_observation_in_transaction(db: Session, account_ref: str, snapshot: dict) -> dict:
    """Archiva fills dentro de una transacción IMMEDIATE propiedad del llamante.

    No confirma ni deshace: permite enlazar fills y snapshot de orden
    atómicamente. Sólo debe llamarse después de adquirir el bloqueo de escritura.
    """
    symbol, observed_at, limit, rows, start_ms, end_ms, pages, coverage = _validate(account_ref, snapshot)
    added = 0
    for row in rows:
        existing = db.query(MexcObservedFill).filter_by(
            account_ref=account_ref, symbol=symbol, trade_id=row["trade_id"]
        ).first()
        if existing is not None:
            if not observed_fill_digest_matches(existing):
                raise PortfolioError("Archived MEXC fill integrity failed")
            if existing.payload_digest != row["payload_digest"]:
                raise PortfolioError("MEXC fill changed since its first observation")
            continue
        db.add(MexcObservedFill(
            id=str(uuid4()), account_ref=account_ref, symbol=symbol,
            trade_id=row["trade_id"], order_id=row["order_id"], side=row["side"],
            time_ms=row["time_ms"], price=row["price"], quantity=row["quantity"],
            quote_quantity=row["quote_quantity"], commission=row["commission"],
            commission_asset=row["commission_asset"], payload_digest=row["payload_digest"],
            first_seen_at=observed_at,
        ))
        added += 1
    times = [row["time_ms"] for row in rows]
    db.add(MexcObservationBatch(
        id=str(uuid4()), account_ref=account_ref, symbol=symbol,
        observed_at=observed_at, requested_limit=limit, returned_count=len(rows),
        oldest_time_ms=min(times) if times else None,
        newest_time_ms=max(times) if times else None,
        requested_start_ms=start_ms, requested_end_ms=end_ms,
        request_count=pages, coverage=coverage,
    ))
    return {"new_fills": added, "observed_fills": len(rows), "coverage": coverage,
            "start_ms": start_ms, "end_ms": end_ms, "requests": pages}


def list_observations(db: Session) -> list[dict]:
    """Índice por par con agregados SQL; no carga cada captura en memoria."""
    batch_groups = db.query(
        MexcObservationBatch.account_ref, MexcObservationBatch.symbol,
        func.max(MexcObservationBatch.observed_at), func.count(MexcObservationBatch.id),
    ).group_by(MexcObservationBatch.account_ref, MexcObservationBatch.symbol).order_by(
        func.max(MexcObservationBatch.observed_at).desc(),
        MexcObservationBatch.account_ref, MexcObservationBatch.symbol,
    ).all()
    fill_counts = {(ref, symbol): count for ref, symbol, count in db.query(
        MexcObservedFill.account_ref, MexcObservedFill.symbol,
        func.count(MexcObservedFill.id),
    ).group_by(MexcObservedFill.account_ref, MexcObservedFill.symbol).all()}
    windows = {(ref, symbol): (first, last) for ref, symbol, first, last in db.query(
        MexcObservationBatch.account_ref, MexcObservationBatch.symbol,
        func.min(MexcObservationBatch.requested_start_ms),
        func.max(MexcObservationBatch.requested_end_ms),
    ).filter(
        MexcObservationBatch.coverage == "RECENT_MONTH_WINDOW_BELOW_PAGE_LIMIT",
        MexcObservationBatch.requested_start_ms.is_not(None),
        MexcObservationBatch.requested_end_ms.is_not(None),
        MexcObservationBatch.requested_start_ms <= MexcObservationBatch.requested_end_ms,
    ).group_by(MexcObservationBatch.account_ref, MexcObservationBatch.symbol).all()}
    return [{"account_ref": ref, "symbol": symbol,
             "latest_observed_at": observed.isoformat(), "batch_count": batch_count,
             "fill_count": fill_counts.get((ref, symbol), 0),
             "complete_window_start_ms": windows.get((ref, symbol), (None, None))[0],
             "complete_window_end_ms": windows.get((ref, symbol), (None, None))[1]}
            for ref, symbol, observed, batch_count in batch_groups]


def archived_window_coverage(db: Session, account_ref: str, symbol: str,
                             start_ms: int, end_ms: int) -> dict:
    """Cobertura local por par de ventanas MEXC completas ya archivadas.

    Una página simple o filtrada por orden no acredita ningún intervalo. El
    resultado no demuestra historial anterior al rango ni conciliación REAL.
    """
    if (not isinstance(account_ref, str) or not _REF.fullmatch(account_ref)
            or not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol)
            or type(start_ms) is not int or type(end_ms) is not int
            or not 0 < start_ms <= end_ms <= 8_640_000_000_000_000):
        raise PortfolioError("Invalid MEXC coverage request")
    fill_count = 0
    fills = db.query(MexcObservedFill).filter(
        MexcObservedFill.account_ref == account_ref,
        MexcObservedFill.symbol == symbol,
        MexcObservedFill.time_ms >= start_ms,
        MexcObservedFill.time_ms <= end_ms,
    ).yield_per(500)
    for fill in fills:
        if not observed_fill_digest_matches(fill):
            raise PortfolioError("Archived MEXC fill integrity failed")
        fill_count += 1
    batches = db.query(MexcObservationBatch).filter(
        MexcObservationBatch.account_ref == account_ref,
        MexcObservationBatch.symbol == symbol,
        MexcObservationBatch.coverage == "RECENT_MONTH_WINDOW_BELOW_PAGE_LIMIT",
        MexcObservationBatch.requested_start_ms <= end_ms,
        MexcObservationBatch.requested_end_ms >= start_ms,
    ).all()
    intervals = sorted((max(start_ms, row.requested_start_ms),
                        min(end_ms, row.requested_end_ms)) for row in batches
                       if row.requested_start_ms is not None
                       and row.requested_end_ms is not None
                       and row.requested_start_ms <= row.requested_end_ms)
    gaps: list[dict[str, int]] = []
    next_ms = start_ms
    for first, last in intervals:
        if first > next_ms:
            gaps.append({"start_ms": next_ms, "end_ms": first - 1})
        next_ms = max(next_ms, last + 1)
    if next_ms <= end_ms:
        gaps.append({"start_ms": next_ms, "end_ms": end_ms})
    return {"scope": "ONE_SYMBOL_ARCHIVED_WINDOWS_ONLY", "account_ref": account_ref,
            "symbol": symbol, "start_ms": start_ms, "end_ms": end_ms,
            "complete_window_count": len(intervals), "verified_fill_count": fill_count,
            "fully_covered": not gaps,
            "gaps": gaps, "reconciled": False, "can_submit_real_order": False}


def read_observations(db: Session, account_ref: str, symbol: str, *, limit: int = 500) -> dict:
    if not isinstance(account_ref, str) or not _REF.fullmatch(account_ref):
        raise PortfolioError("Invalid MEXC account reference")
    if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol) or type(limit) is not int or not 1 <= limit <= 1000:
        raise PortfolioError("Invalid MEXC observation request")
    query = db.query(MexcObservedFill).filter_by(account_ref=account_ref, symbol=symbol)
    total = query.count()
    rows = query.order_by(MexcObservedFill.time_ms.desc(), MexcObservedFill.trade_id.desc()).limit(limit).all()
    if any(not observed_fill_digest_matches(row) for row in rows):
        raise PortfolioError("Archived MEXC fill integrity failed")
    return {"account_ref": account_ref, "symbol": symbol, "total": total,
            "truncated": total > limit, "coverage": "RECENT_PAGES_ONLY_NOT_COMPLETE",
            "trades": [{"id": row.trade_id, "order_id": row.order_id, "side": row.side,
                        "time_ms": row.time_ms, "price": format(row.price, "f"),
                        "quantity": format(row.quantity, "f"),
                        "quote_quantity": format(row.quote_quantity, "f"),
                        "commission": format(row.commission, "f"),
                        "commission_asset": row.commission_asset,
                        "first_seen_at": row.first_seen_at.isoformat()} for row in rows]}
