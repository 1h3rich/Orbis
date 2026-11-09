"""Revisión causal cifrada para preparar continuidad, sin ampliar autoridad.

Dos barridos completos se solapan desde la apertura anterior. La frontera se
fija ANTES del primer barrido, no al acabar: un futuro consumidor deberá volver
a comprobar desde esa frontera y no descartar el intervalo de las consultas.
Este módulo sólo lee MEXC View y guarda evidencia local; no recibe Trade.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Callable
from uuid import UUID

from sqlalchemy.orm import Session

from app.integrations.mexc_readonly import MexcReadOnlyClient
from app.live_execution.journal import LiveJournalError, _canonical, _stored_policy
from app.live_execution.mexc_execution_state import MexcExecutionStateError, _new_events_are_booked, fetch_spot_execution_inputs
from app.live_execution.mexc_trade_window_audit import audit_spot_trade_window
from app.live_execution.policy import LiveSpotPolicy, _utc
from app.portfolio.daily_book_risk import calculate_daily_book_risk
from app.portfolio.domain import fold_events
from app.portfolio.mexc_observations import _validate as validate_trades
from app.portfolio.mexc_parity import assess_mexc_spot_parity
from app.portfolio.mexc_parity_archive import add_observation, read_observation
from app.portfolio.mexc_transfers import _validate as validate_transfers
from app.portfolio.models import RealLiveStop, RealMexcAccountLink, RealMexcParityObservation
from app.portfolio.repository import account_book_revision, list_events, verify_account_event_digests
from app.portfolio.vault import RealVault


PROTOCOL = "MEXC_CAUSAL_REVIEW_V1"


class MexcContinuityError(ValueError):
    """La revisión no demuestra continuidad; nunca equivale a permiso de envío."""


def _policy_digest(policy: LiveSpotPolicy) -> str:
    return hashlib.sha256(_canonical(policy).encode()).hexdigest()


def _stamp(raw: str) -> datetime:
    return _utc(datetime.fromisoformat(raw), "review timestamp")


def _stop_revision(db: Session, account_ref: str) -> tuple | None:
    stop = db.get(RealLiveStop, account_ref)
    return (stop.stopped, stop.updated_at) if stop is not None else None


def opening_boundary(db: Session, row: RealMexcParityObservation, *, _ancestors:frozenset=frozenset()) -> datetime:
    report = read_observation(row)
    if "causal" not in report: return row.checked_at
    verify_continuity_review(db,None,row,_ancestors=_ancestors)
    return _stamp(report["causal"]["boundary_at"])


def verify_continuity_review(db: Session, vault: RealVault|None, row: RealMexcParityObservation,
                             *,_ancestors:frozenset=frozenset()) -> dict:
    """Reproduce evidencia histórica contra el prefijo comprometido del libro."""
    try:
        if row.id in _ancestors or len(_ancestors) >= 128: raise ValueError("opening cycle")
        report = read_observation(row)
        causal = report["causal"]
        if set(causal) != {"protocol","policy_id","policy_digest","opening_id","opening_digest",
                           "boundary_at","checks","authorizes_renewal"}:
            raise ValueError("fields")
        if causal["protocol"] != PROTOCOL or causal["authorizes_renewal"] is not False:
            raise ValueError("authority")
        policy = _stored_policy(db, causal["policy_id"])
        opening = db.get(RealMexcParityObservation, causal["opening_id"])
        link = db.get(RealMexcAccountLink, row.account_ref)
        boundary = _stamp(causal["boundary_at"])
        if opening is None: raise ValueError("opening")
        opening_at = opening_boundary(db,opening,_ancestors=_ancestors|{row.id})
        if (opening is None or opening.id == row.id or link is None or link.account_id != row.account_id
                or policy.account_ref != row.account_ref or opening.account_id != row.account_id
                or opening.account_ref != row.account_ref or not policy.enabled
                or policy.reconciliation_anchor_id != opening.id or causal["policy_digest"] != _policy_digest(policy)
                or opening.report_digest != causal["opening_digest"]
                or not opening_at <= boundary <= row.checked_at < policy.expires_at
                or row.checked_at-opening_at > timedelta(days=1)
                or policy.expires_at > opening_at+timedelta(days=1)):
            raise ValueError("scope")
        read_observation(opening)
        verify_account_event_digests(db, row.account_id)
        if (account_book_revision(db, row.account_id, recorded_through=opening_at) != opening.book_revision
                or account_book_revision(db, row.account_id, recorded_through=boundary) != row.book_revision):
            raise ValueError("book prefix")
        _new_events_are_booked(db, policy, opening, link, recorded_through=boundary,opening_at=opening_at)
        history = list_events(db, row.account_id, recorded_through=boundary)
        book = fold_events(row.account_id, history)
        checks = causal["checks"]
        if not isinstance(checks, list) or len(checks) != 2 or _stamp(checks[0]["started_at"]) != boundary:
            raise ValueError("checks")
        previous_end = boundary
        opening_ms, boundary_ms = int(opening_at.timestamp()*1000), int(boundary.timestamp()*1000)
        for check in checks:
            if set(check) != {"started_at","checked_at","opening_id","book_revision","balances",
                              "open_orders_at","survey","trades","transfers","risk"}:
                raise ValueError("receipt")
            if (set(check["balances"]) != {"source","accounting","observed_at","balances"}
                    or any(set(item) != {"asset","free","locked","total"}
                           for item in check["balances"]["balances"])
                    or set(check["trades"]) != {"source","accounting","symbol","limit","pages",
                        "coverage","observed_at","start_ms","end_ms","trades"}
                    or any(set(item) != {"id","order_id","side","time_ms","commission_asset",
                                        "price","quantity","quote_quantity","commission"}
                           for item in check["trades"]["trades"])):
                raise ValueError("unrecognized provider fields")
            start, end = _stamp(check["started_at"]), _stamp(check["checked_at"])
            if (not previous_end <= start <= end <= row.checked_at
                    or (row.checked_at-start).total_seconds() > policy.max_data_age_seconds
                    or check["opening_id"] != opening.id or check["book_revision"] != row.book_revision):
                raise ValueError("receipt time")
            previous_end = end
            survey = check["survey"]
            if survey != {"source":"MEXC_SPOT_PRIVATE_READONLY","accounting":"OBSERVED_NOT_IMPORTED",
                          "coverage":"UNFILTERED_COVERAGE_UNPROVEN","symbol":None,
                          "orders":[],"observed_at":survey["observed_at"]}:
                raise ValueError("open orders")
            _, trades_at, _, _, first, last, _, coverage = validate_trades(row.account_ref, check["trades"])
            transfer_at, transfer_first, transfer_last, deposits, withdrawals, _ = validate_transfers(row.account_ref, check["transfers"])
            if (coverage != "RECENT_MONTH_WINDOW_BELOW_PAGE_LIMIT" or first != opening_ms
                    or check["trades"]["symbol"] != policy.symbol or last is None or last < boundary_ms
                    or transfer_first > opening_ms or transfer_last < boundary_ms
                    or any(item["time_ms"] >= opening_ms or item["status"] != final
                           for items, final in ((deposits,5),(withdrawals,7)) for item in items)):
                raise ValueError("activity coverage")
            stamps = (trades_at, transfer_at, _stamp(check["balances"]["observed_at"]),
                      _stamp(check["open_orders_at"]), _stamp(survey["observed_at"]))
            if (any(not start <= stamp <= end for stamp in stamps)
                    or transfer_last > int(transfer_at.timestamp()*1000)
                    or any(value > int(end.timestamp()*1000) for value in (last,transfer_last))
                    or any(int(end.timestamp()*1000)-value > policy.max_data_age_seconds*1000
                           for value in (last,transfer_last))):
                raise ValueError("private freshness")
            audit = audit_spot_trade_window(vault, policy, check["trades"], now=end, db=db)
            if not audit.consistent_for_window:
                raise ValueError("fills")
            risk = calculate_daily_book_risk(row.account_id, history, policy.quote_asset, now=end, base_asset=policy.base_asset)
            if json.loads(_canonical(risk)) != check["risk"]:
                raise ValueError("risk")
        expected = assess_mexc_spot_parity(book, checks[0]["balances"], checks[1]["balances"],
            book_revision_before=row.book_revision, book_revision_after=row.book_revision, now=row.checked_at)
        if {key:value for key,value in report.items() if key not in ("id","saved","causal")} != expected:
            raise ValueError("balances")
        return report
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError, LiveJournalError, MexcExecutionStateError) as exc:
        raise MexcContinuityError("La evidencia causal MEXC no es verificable") from exc


def read_continuity_review(vault: RealVault, review_id: str) -> dict:
    with vault.session() as db:
        db.connection().exec_driver_sql("BEGIN")
        row = db.get(RealMexcParityObservation, str(UUID(review_id)))
        if row is None:
            raise MexcContinuityError("Revisión causal no encontrada")
        return verify_continuity_review(db, vault, row)


def record_continuity_review(
    vault: RealVault, view: MexcReadOnlyClient, policy_id: str, *, review_id: str,
    assert_active: Callable[[], None], clock: Callable[[], datetime] | None = None,
    quote_get: Callable | None = None, rules_fetch: Callable | None = None,
    assert_active_in_transaction:Callable[[Session],None]|None=None,
) -> dict:
    """Revisión durable y repetible: nunca renueva fechas ni inicia un bot.

    Repetir el ID conserva la fecha/evidencia original sin nuevas consultas.
    El coordinador futuro deberá exigir frescura y otra concesión explícita
    antes de usar este resultado para renovar una apertura.
    """
    identifier, policy_id = str(UUID(review_id)), str(UUID(policy_id))
    now = clock or (lambda: datetime.now(timezone.utc))
    assert_active()
    generation = vault.session_generation
    if generation is None:
        raise MexcContinuityError("La bóveda REAL está bloqueada")
    with vault.guard_session_generation(generation):
        with vault.session() as db:
            prior = db.get(RealMexcParityObservation, identifier)
            if prior is not None:
                result = verify_continuity_review(db, vault, prior)
                if result["causal"]["policy_id"] != policy_id:
                    raise MexcContinuityError("El ID de revisión ya pertenece a otra política")
                return result
            policy = _stored_policy(db, policy_id)
            stop_revision = _stop_revision(db, policy.account_ref)
            opening = db.get(RealMexcParityObservation, policy.reconciliation_anchor_id)
            if opening is None:
                raise MexcContinuityError("Falta la apertura de la revisión causal")
            read_observation(opening)
            opening_digest = opening.report_digest
        checks = []
        for _ in range(2):
            assert_active()
            receipt = {}
            try:
                fetch_spot_execution_inputs(vault, view, policy, clock=now, quote_get=quote_get,
                    rules_fetch=rules_fetch, audit_receipt=receipt)
            except MexcExecutionStateError:
                raise MexcContinuityError("La cuenta no supera la revisión causal MEXC") from None
            checks.append(receipt)
        assert_active()
        finished = _utc(now(), "now")
        with vault.session() as db:
            try:
                db.connection().exec_driver_sql("BEGIN IMMEDIATE")
                if assert_active_in_transaction is not None: assert_active_in_transaction(db)
                else: assert_active()
                if _stop_revision(db, policy.account_ref) != stop_revision:
                    raise MexcContinuityError("La parada cambió durante la revisión causal")
                if finished >= policy.expires_at:
                    raise MexcContinuityError("La política caducó durante la revisión causal")
                opening = db.get(RealMexcParityObservation, policy.reconciliation_anchor_id)
                if (opening is None or opening.report_digest != opening_digest
                        or _stored_policy(db, policy_id) != policy):
                    raise MexcContinuityError("La política o la apertura cambiaron")
                revision = account_book_revision(db, opening.account_id)
                if any(check["book_revision"] != revision for check in checks):
                    raise MexcContinuityError("El libro cambió durante la revisión causal")
                history = list_events(db, opening.account_id)
                report = assess_mexc_spot_parity(fold_events(opening.account_id, history),
                    checks[0]["balances"], checks[1]["balances"], book_revision_before=revision,
                    book_revision_after=revision, now=finished)
                report["causal"] = {"protocol":PROTOCOL,"policy_id":policy_id,
                    "policy_digest":_policy_digest(policy),"opening_id":opening.id,
                    "opening_digest":opening.report_digest,"boundary_at":checks[0]["started_at"],
                    "checks":checks,"authorizes_renewal":False}
                add_observation(db, identifier, opening.account_id, policy.account_ref, report)
                result = verify_continuity_review(db, vault, db.get(RealMexcParityObservation, identifier))
                if assert_active_in_transaction is not None: assert_active_in_transaction(db)
                else: assert_active()
                db.commit()
                return result
            except Exception:
                db.rollback()
                raise
