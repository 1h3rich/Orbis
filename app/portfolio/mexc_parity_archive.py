"""Evidencia local cifrada de paridad MEXC, sin semántica de conciliación."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.portfolio.models import RealMexcParityObservation


class MexcParityArchiveError(ValueError):
    pass


def _canonical_report(report: dict) -> str:
    try:
        encoded = json.dumps(report, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise MexcParityArchiveError("Informe de paridad no serializable") from exc
    if len(encoded.encode("utf-8")) > 2_000_000:
        raise MexcParityArchiveError("Informe de paridad demasiado grande")
    return encoded


def _digest(row: RealMexcParityObservation) -> str:
    if row.checked_at.tzinfo is None:
        raise MexcParityArchiveError("Fecha de paridad sin zona horaria")
    raw = json.dumps({
        "id": row.id, "account_id": row.account_id,
        "account_ref": row.account_ref, "book_revision": row.book_revision,
        "checked_at": row.checked_at.astimezone(timezone.utc).isoformat(timespec="microseconds"),
        "report_json": row.report_json,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def read_observation(row: RealMexcParityObservation) -> dict:
    if not hmac.compare_digest(row.report_digest, _digest(row)):
        raise MexcParityArchiveError("Evidencia de paridad alterada")
    try:
        report = json.loads(row.report_json)
    except (TypeError, ValueError) as exc:
        raise MexcParityArchiveError("Evidencia de paridad ilegible") from exc
    if (not isinstance(report, dict) or report.get("account_id") != row.account_id
            or report.get("book_revision") != row.book_revision
            or report.get("status") != "PARITY_OBSERVED_NOT_RECONCILED"
            or report.get("blockers") != []
            or report.get("reconciled") is not False
            or report.get("orders_enabled") is not False):
        raise MexcParityArchiveError("Evidencia de paridad incoherente")
    try:
        reported_at = datetime.fromisoformat(report["checked_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MexcParityArchiveError("Fecha de paridad ilegible") from exc
    if reported_at.tzinfo is None or reported_at.astimezone(timezone.utc) != row.checked_at:
        raise MexcParityArchiveError("Fecha de paridad incoherente")
    return {"id": row.id, **report, "saved": True}


def add_observation(db: Session, request_id: str, account_id: str,
                    account_ref: str, report: dict) -> dict:
    if report.get("status") != "PARITY_OBSERVED_NOT_RECONCILED" or report.get("blockers") != []:
        raise MexcParityArchiveError("Sólo se guarda paridad estable sin bloqueos")
    checked_at = datetime.fromisoformat(report["checked_at"])
    if checked_at.tzinfo is None:
        raise MexcParityArchiveError("Fecha de paridad sin zona horaria")
    row = RealMexcParityObservation(
        id=request_id, account_id=account_id, account_ref=account_ref,
        book_revision=report["book_revision"], checked_at=checked_at,
        report_json=_canonical_report(report), report_digest="0" * 64,
    )
    row.report_digest = _digest(row)
    db.add(row)
    db.flush()
    return read_observation(row)


def list_observations(db: Session, account_id: str, *, limit: int = 50) -> list[dict]:
    rows = db.scalars(select(RealMexcParityObservation).where(
        RealMexcParityObservation.account_id == account_id).order_by(
            RealMexcParityObservation.checked_at.desc(),
            RealMexcParityObservation.id.desc()).limit(limit)).all()
    return [read_observation(row) for row in rows]
