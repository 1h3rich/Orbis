"""Detecta movimientos externos MEXC durante una ventana reciente.

Una ventana vacía sólo demuestra ausencia de ingresos/retiros *en esa ventana*.
No acredita saldos anteriores, actividad en otros productos ni órdenes/fills,
y jamás construye un ``LiveSpotState`` reconciliado.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.integrations.mexc_readonly import MexcReadOnlyClient
from app.live_execution.mexc_uid_guard import MexcUidGuardError, assert_known_mexc_uid
from app.live_execution.policy import LiveSpotPolicy, _utc
from app.portfolio.domain import PortfolioError
from app.portfolio.mexc_transfers import _validate
from app.portfolio.vault import RealVault


@dataclass(frozen=True, slots=True)
class MexcTransferWindowAudit:
    clear_for_window: bool
    reason: str
    start_ms: int | None
    end_ms: int | None
    deposit_count: int
    withdrawal_count: int


def audit_spot_transfer_window(policy: LiveSpotPolicy, snapshot: dict, *,
                               now: datetime) -> MexcTransferWindowAudit:
    """Rechaza cualquier traslado observado, incluso pendiente o fallido.

    El código de estado de MEXC no se traduce aquí en un asiento REAL: un
    cambio externo requiere una conciliación causal posterior, no una
    suposición sobre su efecto patrimonial.
    """
    current = _utc(now, "now")

    def deny(reason: str, start: int | None = None, end: int | None = None,
             deposits: int = 0, withdrawals: int = 0) -> MexcTransferWindowAudit:
        return MexcTransferWindowAudit(False, reason, start, end, deposits, withdrawals)

    if not isinstance(policy, LiveSpotPolicy):
        return deny("POLICY_INVALID")
    try:
        observed_at, start, end, deposits, withdrawals, _ = _validate(
            policy.account_ref, snapshot,
        )
    except (PortfolioError, TypeError, ValueError):
        return deny("TRANSFER_WINDOW_INVALID")
    current_ms = int(current.timestamp() * 1000)
    if (observed_at > current or (current - observed_at).total_seconds()
            > policy.max_data_age_seconds or end > current_ms
            or current_ms - end > policy.max_data_age_seconds * 1000
            or observed_at.timestamp() * 1000 < end):
        return deny("TRANSFER_WINDOW_STALE", start, end, len(deposits), len(withdrawals))
    if deposits or withdrawals:
        return deny("TRANSFER_ACTIVITY_REQUIRES_RECONCILIATION", start, end,
                    len(deposits), len(withdrawals))
    return MexcTransferWindowAudit(True, "NO_TRANSFERS_IN_OBSERVED_WINDOW",
                                   start, end, 0, 0)


def observe_and_audit_spot_transfers(vault: RealVault, view: MexcReadOnlyClient,
                                     policy: LiveSpotPolicy, *, days: int,
                                     now: datetime | None = None) -> MexcTransferWindowAudit:
    """Consulta View con UID conocido y estable; no modifica la bóveda.

    La clave View necesita permiso de lectura de ingresos/retiros. Una
    excepción de proveedor nunca se interpreta como ventana vacía.
    """
    if not isinstance(policy, LiveSpotPolicy):
        return MexcTransferWindowAudit(False, "POLICY_INVALID", None, None, 0, 0)
    try:
        if view.account_reference() != policy.account_ref:
            return MexcTransferWindowAudit(False, "VIEW_ACCOUNT_MISMATCH", None, None, 0, 0)
        uid_before = view.account_uid()
    except Exception:
        return MexcTransferWindowAudit(False, "VIEW_UNAVAILABLE", None, None, 0, 0)
    try:
        known = assert_known_mexc_uid(vault, policy.account_ref, uid_before)
    except MexcUidGuardError:
        known = False
    if not known:
        return MexcTransferWindowAudit(False, "VIEW_UID_UNVERIFIED", None, None, 0, 0)
    try:
        snapshot = view.transfers_recent(days=days)
        uid_after = view.account_uid()
    except Exception:
        return MexcTransferWindowAudit(False, "VIEW_UNAVAILABLE", None, None, 0, 0)
    if uid_after != uid_before:
        return MexcTransferWindowAudit(False, "VIEW_UID_CHANGED", None, None, 0, 0)
    try:
        known = assert_known_mexc_uid(vault, policy.account_ref, uid_after)
    except MexcUidGuardError:
        known = False
    if not known:
        return MexcTransferWindowAudit(False, "VIEW_UID_UNVERIFIED", None, None, 0, 0)
    return audit_spot_transfer_window(policy, snapshot,
                                      now=now or datetime.now(timezone.utc))
