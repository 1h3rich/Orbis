"""Reapertura explícita de una cuenta detenida, sin resucitar políticas previas."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import UUID

from sqlalchemy import select

from app.integrations.mexc_readonly import MexcReadOnlyClient
from app.live_execution.journal import _stored_policy, _register_spot_policy_in_transaction
from app.live_execution.mexc_execution import MexcExecutionError
from app.live_execution.mexc_execution_state import fetch_spot_execution_inputs
from app.live_execution.mexc_live_order import MexcSpotLiveClient
from app.live_execution.mexc_uid_guard import assert_known_mexc_uid
from app.live_execution.policy import LiveSpotPolicy, _utc
from app.portfolio.mexc_parity_archive import read_observation
from app.portfolio.models import RealLiveSpotIntent, RealLiveSpotPolicy, RealLiveStop, RealMexcAccountLink, RealMexcParityObservation
from app.portfolio.repository import account_book_revision
from app.portfolio.vault import RealVault


def resume_spot_execution(
    vault: RealVault, view: MexcReadOnlyClient, trade: MexcSpotLiveClient,
    draft_id: str, anchor_id: str, active_policy_id: str, *,
    clock: Callable[[], datetime] | None = None,
    quote_get: Callable | None = None,
    rules_fetch: Callable | None = None,
    persist_in_transaction: Callable | None = None,
    expires_before:datetime|None=None,
) -> LiveSpotPolicy:
    """Revisa saldo/actividad sin levantar STOP y confirma ambos cambios juntos.

    Exige una apertura posterior a la parada y ninguna intención pendiente.
    La política nueva fija la fecha de esa parada en su contenido inmutable.
    Guardar la política y levantar STOP comparten una única transacción.
    """
    now = clock or (lambda: datetime.now(timezone.utc))
    try:
        anchor_id, active_policy_id = str(UUID(anchor_id)), str(UUID(active_policy_id))
    except (ValueError, TypeError, AttributeError):
        raise MexcExecutionError("Identificador de reapertura inválido") from None
    generation = vault.session_generation
    if generation is None:
        raise MexcExecutionError("La bóveda REAL está bloqueada")
    with vault.guard_session_generation(generation):
        with vault.session() as db:
            db.connection().exec_driver_sql("BEGIN")
            draft = _stored_policy(db, draft_id)
            anchor = db.get(RealMexcParityObservation, anchor_id)
            if draft.enabled or anchor is None or anchor.account_ref != draft.account_ref:
                raise MexcExecutionError("El borrador y la apertura no corresponden a esta cuenta")
            read_observation(anchor)
            from app.live_execution.mexc_continuity_review import opening_boundary
            boundary = opening_boundary(db,anchor)
            expires_at = min(draft.expires_at,boundary+timedelta(days=1))
            if expires_before is not None: expires_at = min(expires_at,_utc(expires_before,"expires_before"))
            anchor_digest, account_id = anchor.report_digest, anchor.account_id
            stop = db.get(RealLiveStop, draft.account_ref)
            existing = db.get(RealLiveSpotPolicy, active_policy_id)
            if existing is not None:
                policy = _stored_policy(db, active_policy_id)
                expected = replace(draft, policy_id=active_policy_id, enabled=True,
                                   reconciliation_anchor_id=anchor_id, allow_risk_reducing_exits=True,
                                   resumed_after_stop_at=policy.resumed_after_stop_at,
                                   expires_at=expires_at)
                if (policy != expected or policy.resumed_after_stop_at is None
                        or stop is None or stop.stopped or existing.created_at < stop.updated_at):
                    raise MexcExecutionError("El ID de reapertura ya se usó o hay una parada posterior")
                return policy
            if stop is None or not stop.stopped:
                raise MexcExecutionError("La cuenta no está detenida")
            stop_at = stop.updated_at
            checked_at = _utc(now(), "now")
            if (anchor.checked_at < stop_at or not 0 <= (checked_at - anchor.checked_at).total_seconds()
                    <= draft.max_data_age_seconds):
                raise MexcExecutionError("Guarda una apertura nueva después de la parada")
            pending = db.scalar(select(RealLiveSpotIntent.id).where(
                RealLiveSpotIntent.account_ref == draft.account_ref,
                RealLiveSpotIntent.status.in_(("RESERVED", "SUBMISSION_UNKNOWN"))).limit(1))
            if pending is not None:
                raise MexcExecutionError("Sincroniza las órdenes pendientes antes de reabrir")
            policy = replace(draft, policy_id=active_policy_id, enabled=True,
                             reconciliation_anchor_id=anchor_id, allow_risk_reducing_exits=True,
                             resumed_after_stop_at=stop_at,
                             expires_at=expires_at)
        inputs = fetch_spot_execution_inputs(
            vault, view, policy, clock=now, quote_get=quote_get,
            rules_fetch=rules_fetch, review_stopped=True)
        if not inputs.state.stopped:
            raise MexcExecutionError("La parada cambió durante la revisión")
        try:
            uid = view.account_uid()
            if (view.account_reference() != policy.account_ref
                    or not assert_known_mexc_uid(vault, policy.account_ref, uid)):
                raise ValueError("View identity changed")
            trade.pair_view_account(uid)
            if view.account_uid() != uid:
                raise ValueError("View identity changed")
        except Exception:
            raise MexcExecutionError("No se pudo revalidar MEXC View y Trade") from None
        completed_at = _utc(now(), "now")
        if (completed_at >= policy.expires_at or completed_at < checked_at
                or any(stamp > completed_at or (completed_at - stamp).total_seconds()
                       > policy.max_data_age_seconds for stamp in (
                           inputs.state.observed_at, inputs.state.quote_at, inputs.rules.observed_at))):
            raise MexcExecutionError("La revisión caducó antes de reabrir")
        with vault.session() as db:
            try:
                db.connection().exec_driver_sql("BEGIN IMMEDIATE")
                stop = db.get(RealLiveStop, policy.account_ref)
                anchor = db.get(RealMexcParityObservation, anchor_id)
                link = db.get(RealMexcAccountLink, policy.account_ref)
                pending = db.scalar(select(RealLiveSpotIntent.id).where(
                    RealLiveSpotIntent.account_ref == policy.account_ref,
                    RealLiveSpotIntent.status.in_(("RESERVED", "SUBMISSION_UNKNOWN"))).limit(1))
                if (stop is None or not stop.stopped or stop.updated_at != stop_at
                        or anchor is None or anchor.report_digest != anchor_digest or pending is not None
                        or link is None or link.account_id != account_id
                        or account_book_revision(db, account_id) != anchor.book_revision):
                    raise MexcExecutionError("La parada, el saldo o una orden cambiaron durante la revisión")
                read_observation(anchor)
                from app.portfolio.mexc_withdrawal_journal import assert_no_pending_withdrawals,WithdrawalError
                try:
                    assert_no_pending_withdrawals(db,policy.account_ref,completed_at)
                except WithdrawalError as exc:
                    raise MexcExecutionError(str(exc)) from None
                _register_spot_policy_in_transaction(db, policy, now=completed_at)
                if persist_in_transaction is not None:
                    persist_in_transaction(db, policy, inputs, completed_at)
                stop.stopped = False
                stop.updated_at = completed_at
                db.commit()
                return policy
            except Exception:
                db.rollback()
                raise
