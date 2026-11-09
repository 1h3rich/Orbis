"""Puerta duradera para probar una única orden firmada en GRVT testnet.

Hay rutas API locales y una interfaz para un intento firmado TESTNET. Tras una caída,
se observa por ID; jamás se debe
reclamar el mismo POST otra vez. No asienta fills ni activa dinero real.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import time
from typing import Callable

import httpx

from app.live_execution.grvt_testnet_journal import (
    GrvtTestnetCancelRecord, GrvtTestnetJournalError, GrvtTestnetRecord,
    claim_testnet_cancel, claim_testnet_post, prepare_testnet_order,
    mark_testnet_cancel_audited, record_testnet_ack,
    record_testnet_cancel_observation,
    record_testnet_observation, require_testnet_cancel,
    stop_testnet_subaccount, _same_request,
)
from app.live_execution.grvt_testnet_fills import (
    GrvtTestnetFillAudit, audit_testnet_fills,
)
from app.live_execution.grvt_testnet_funding import (
    GrvtTestnetFundingAudit, audit_testnet_funding,
)
from app.live_execution.grvt_testnet_transfers import (
    GrvtTestnetTransferAudit, audit_testnet_transfers,
)
from app.live_execution.grvt_testnet_withdrawals import (
    GrvtTestnetWithdrawalAudit, audit_testnet_withdrawals,
)
from app.live_execution.grvt_testnet_deposits import (
    GrvtTestnetDepositAudit, audit_testnet_deposits,
)
from app.live_execution.grvt_testnet_interest import (
    GrvtTestnetInterestAudit, audit_testnet_interest,
)
from app.live_execution.grvt_testnet_account import (
    GrvtTestnetAccountSnapshot, audit_testnet_account,
)
from app.live_execution.grvt_testnet_evidence import (
    GrvtTestnetEvidenceRecord, save_testnet_audit,
)
from app.live_execution.grvt_testnet_order import (
    GrvtTestnetLimits, GrvtTestnetOrderClient,
    GrvtTestnetOrderObservation, _validate_order,
)
from app.live_execution.grvt_testnet_risk import (
    GrvtTestnetRiskError, _public_post, evaluate_testnet_risk,
    fetch_testnet_risk,
)
from app.live_execution.grvt_testnet_preflight import (
    optional_testnet_preflight, record_testnet_preflight, verified_preflight_for_claim,
)
from app.live_execution.grvt_testnet_signer import GrvtTestnetLimitDraft
from app.live_execution.grvt_testnet_quote import GrvtTestnetPriceGuard
from app.portfolio.vault import RealVault
from app.portfolio.models import GrvtTestnetOrderIntent, GrvtTestnetStop


@dataclass(frozen=True, slots=True)
class GrvtTestnetObservedAudit:
    record: GrvtTestnetRecord
    fills: GrvtTestnetFillAudit
    funding: GrvtTestnetFundingAudit
    transfers: GrvtTestnetTransferAudit
    withdrawals: GrvtTestnetWithdrawalAudit
    deposits: GrvtTestnetDepositAudit
    interest: GrvtTestnetInterestAudit
    account: GrvtTestnetAccountSnapshot
    evidence: GrvtTestnetEvidenceRecord


@dataclass(frozen=True, slots=True)
class GrvtTestnetCancelAudit:
    cancellation: GrvtTestnetCancelRecord
    audit: GrvtTestnetObservedAudit


def _datetime_ns(value: int) -> datetime:
    seconds, nanoseconds = divmod(value, 1_000_000_000)
    return datetime.fromtimestamp(seconds, timezone.utc) + timedelta(microseconds=nanoseconds // 1000)


def submit_testnet_once(vault: RealVault, client: GrvtTestnetOrderClient,
                        order: dict, limits: GrvtTestnetLimits, *,
                        clock_ns: Callable[[], int] = time.time_ns,
                        public_post: Callable[[str, dict], httpx.Response] = _public_post,
                        signal_source: dict | None = None,
                        pre_claim_guard: Callable[[], GrvtTestnetPriceGuard] | None = None,
                        ) -> GrvtTestnetRecord:
    """Prepara, reclama y envía; un fallo deja POST_CLAIMED hasta consultar."""
    stamp_ns = clock_ns()
    if type(stamp_ns) is not int or stamp_ns <= 0:
        raise ValueError("Reloj testnet inválido")
    last_ns = stamp_ns

    def checked_ns() -> int:
        nonlocal last_ns
        current = clock_ns()
        if type(current) is not int or current < last_ns:
            raise GrvtTestnetRiskError("Reloj testnet retrocedió durante el envío")
        last_ns = current
        return current

    _validate_order(order, limits, stamp_ns)
    leg = order["legs"][0]
    draft = GrvtTestnetLimitDraft(
        order["sub_account_id"], leg["instrument"],
        "BUY" if leg["is_buying_asset"] else "SELL",
        Decimal(leg["size"]), Decimal(leg["limit_price"]),
        order["reduce_only"], order["builder"],
        order["metadata"]["client_order_id"],
        order["signature"]["nonce"], int(order["signature"]["expiration"]),
    )
    state, rules = fetch_testnet_risk(client, draft.instrument, draft.sub_account_id,
                                      public_post=public_post, clock_ns=checked_ns)
    decision = evaluate_testnet_risk(draft, limits, state, rules, now_ns=checked_ns())
    if not decision.allowed:
        raise GrvtTestnetRiskError(f"Preflight GRVT testnet bloqueado: {decision.reason}")
    from app.live_execution.grvt_testnet_continuity import check_testnet_history_continuity
    history = check_testnet_history_continuity(vault, client, draft.sub_account_id, now_ns=checked_ns())
    refreshed, refreshed_rules = fetch_testnet_risk(
        client, draft.instrument, draft.sub_account_id,
        public_post=public_post, clock_ns=checked_ns,
    )
    refreshed_decision = evaluate_testnet_risk(
        draft, limits, refreshed, refreshed_rules, now_ns=checked_ns(),
    )
    if not refreshed_decision.allowed:
        raise GrvtTestnetRiskError(f"Segundo preflight GRVT testnet bloqueado: {refreshed_decision.reason}")
    if history is not None and refreshed.event_time_ns < history["observed_until_ns"]:
        raise GrvtTestnetRiskError("Cuenta GRVT testnet anterior al corte del historial; vuelve a consultar")
    # Fondos, exposición y reglas deben seguir iguales. En una reducción,
    # P/L y márgenes pueden variar con el mercado: ambas lecturas deben
    # superar su guardia y mantener equity = saldo + P/L no realizado.
    if (refreshed.event_time_ns < state.event_time_ns
            or refreshed.observed_at_ns < state.observed_at_ns
            or refreshed_rules.fetched_at_ns < rules.fetched_at_ns
            or (refreshed.sub_account_id, refreshed.settle_currency,
                refreshed.margin_type, refreshed.sub_account_mode,
                refreshed.settle_balance, refreshed.positions,
                refreshed.open_order_count) !=
               (state.sub_account_id, state.settle_currency,
                state.margin_type, state.sub_account_mode,
                state.settle_balance, state.positions,
                state.open_order_count)
            or not draft.reduce_only and (refreshed.total_equity,refreshed.initial_margin,
                refreshed.maintenance_margin,refreshed.unrealized_pnl) !=
                (state.total_equity,state.initial_margin,state.maintenance_margin,state.unrealized_pnl)
            or (refreshed_rules.instrument, refreshed_rules.max_size,
                refreshed_rules.tiers) !=
               (rules.instrument, rules.max_size, rules.tiers)):
        raise GrvtTestnetRiskError("El estado o las reglas GRVT testnet cambiaron durante el preflight")
    # Una cotización opcional debe comprobarse después de ambos preflights,
    # pero antes de escribir PREPARED/POST_CLAIMED. Su fallo no consume el ID.
    price_guard = pre_claim_guard() if pre_claim_guard is not None else None
    prepared_ns = checked_ns()
    now = _datetime_ns(prepared_ns)
    prepared = prepare_testnet_order(vault, order, limits, now=now, now_ns=prepared_ns)
    if prepared.status != "PREPARED":
        raise GrvtTestnetJournalError("Orden testnet no preparada para primer envío")
    claim_ns = checked_ns()
    _validate_order(order, limits, claim_ns)
    record_testnet_preflight(vault, order, limits, refreshed, refreshed_rules,
                             refreshed_decision,
                             now=_datetime_ns(claim_ns), now_ns=claim_ns,
                             signal_source=signal_source, price_guard=price_guard,
                             history_continuity=history)
    claim_testnet_post(vault, order, limits, now=_datetime_ns(claim_ns),
                       now_ns=claim_ns)
    # La parada escribe bajo el mismo bloqueo. Si confirma antes, no hay POST;
    # si llega durante la red, sólo confirma después de terminar este intento.
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            row = db.get(GrvtTestnetOrderIntent, order["metadata"]["client_order_id"])
            if (row is None or row.status != "POST_CLAIMED"
                    or not _same_request(row, order, limits)):
                raise GrvtTestnetJournalError("Parada o estado testnet cambió antes del POST")
            send_ns = checked_ns()
            _validate_order(order, limits, send_ns)
            from app.live_execution.grvt_testnet_journal import require_submission_stop
            require_submission_stop(db,row,now_ns=send_ns,order=order)
            verified_preflight_for_claim(db,row,now_ns=send_ns)
            ack = client.submit_signed_limit(order, limits)
            db.commit()
        except Exception:
            db.rollback()
            raise
    # Un ajuste del reloj tras un acuse no debe borrar la evidencia del POST.
    acknowledged_ns = clock_ns()
    if type(acknowledged_ns) is not int:
        acknowledged_ns = last_ns
    return record_testnet_ack(vault, ack, now=_datetime_ns(max(last_ns, acknowledged_ns)))


def observe_testnet_order(vault: RealVault, client: GrvtTestnetOrderClient,
                          sub_account_id: str, client_order_id: str, *,
                          clock_ns: Callable[[], int] = time.time_ns) -> GrvtTestnetRecord:
    """Reconsulta tras acuse o incertidumbre; no cuenta fills como patrimonio."""
    observation = client.lookup_order(sub_account_id, client_order_id)
    return record_testnet_observation(vault, observation,
                                      now=_datetime_ns(clock_ns()))


def stop_and_cancel_testnet_once(vault: RealVault,
                                  client: GrvtTestnetOrderClient,
                                  sub_account_id: str,
                                  client_order_id: str, *,
                                  clock_ns: Callable[[], int] = time.time_ns,
                                  ) -> GrvtTestnetCancelRecord:
    """Persiste parada y marca la cancelación antes de un único POST testnet.

    Si el POST es incierto, no se reenvía. Su TTL máximo no demuestra que una
    orden posterior no llegue, y el intento original permanece bloqueado.
    """
    stamp = clock_ns()
    if type(stamp) is not int or stamp <= 0:
        raise GrvtTestnetJournalError("Reloj de cancelación testnet inválido")
    now = _datetime_ns(stamp)
    stop_testnet_subaccount(vault, sub_account_id, now=now)
    claim_testnet_cancel(vault, sub_account_id, client_order_id, now=now)
    observation = client.cancel_by_client_id(sub_account_id, client_order_id)
    record_testnet_observation(vault, observation,
                                now=_datetime_ns(clock_ns()))
    return record_testnet_cancel_observation(vault, observation,
                                              now=_datetime_ns(clock_ns()))


def observe_testnet_cancel(vault: RealVault, client: GrvtTestnetOrderClient,
                            sub_account_id: str, client_order_id: str, *,
                            clock_ns: Callable[[], int] = time.time_ns,
                            ) -> GrvtTestnetCancelRecord:
    """Consulta tras acuse o incertidumbre; no prueba cierre contable."""
    observation = client.lookup_order(sub_account_id, client_order_id)
    record_testnet_observation(vault, observation,
                                now=_datetime_ns(clock_ns()))
    return record_testnet_cancel_observation(vault, observation,
                                              now=_datetime_ns(clock_ns()))


def audit_testnet_intent(vault: RealVault, client: GrvtTestnetOrderClient,
                         sub_account_id: str, client_order_id: str, *,
                         clock_ns: Callable[[], int] = time.time_ns,
                         ) -> GrvtTestnetObservedAudit:
    """Observa orden, fills y cuenta posterior; jamás desbloquea otro envío."""
    observation = client.lookup_order(sub_account_id, client_order_id)
    return _audit_testnet_observation(vault, client, observation,
                                       clock_ns=clock_ns)


def audit_testnet_cancel(vault: RealVault, client: GrvtTestnetOrderClient,
                         sub_account_id: str, client_order_id: str, *,
                         clock_ns: Callable[[], int] = time.time_ns,
                         ) -> GrvtTestnetCancelAudit:
    """Audita una cancelación reclamada sin inferir cierre ni liberar riesgo."""
    require_testnet_cancel(vault, sub_account_id, client_order_id)
    observation = client.lookup_order(sub_account_id, client_order_id)
    record_testnet_observation(vault, observation,
                               now=_datetime_ns(clock_ns()))
    record_testnet_cancel_observation(
        vault, observation, now=_datetime_ns(clock_ns()))
    audit = _audit_testnet_observation(vault, client, observation,
                                       clock_ns=clock_ns)
    cancellation = mark_testnet_cancel_audited(
        vault, observation, audit.evidence.id,
        now=_datetime_ns(clock_ns()))
    return GrvtTestnetCancelAudit(cancellation, audit)


def _audit_testnet_observation(vault: RealVault,
                               client: GrvtTestnetOrderClient,
                               observation: GrvtTestnetOrderObservation, *,
                               clock_ns: Callable[[], int],
                               ) -> GrvtTestnetObservedAudit:
    record = record_testnet_observation(vault, observation,
                                        now=_datetime_ns(clock_ns()))
    sub_account_id = observation.sub_account_id
    preflight = optional_testnet_preflight(vault, sub_account_id,
                                            observation.client_order_id)
    history_start = preflight["observed_at_ns"] if preflight is not None else None
    fills = audit_testnet_fills(client, observation, now_ns=clock_ns(),
                                start_time_ns=history_start)
    funding = audit_testnet_funding(
        client, sub_account_id,
        start_time_ns=history_start or int(observation.create_time_ns),
        now_ns=clock_ns(),
    )
    transfers = audit_testnet_transfers(
        client, sub_account_id,
        start_time_ns=history_start or int(observation.create_time_ns),
        now_ns=clock_ns(),
    )
    withdrawals = audit_testnet_withdrawals(
        client, sub_account_id,
        start_time_ns=history_start or int(observation.create_time_ns),
        now_ns=clock_ns(),
    )
    deposits = audit_testnet_deposits(
        client, sub_account_id,
        start_time_ns=history_start or int(observation.create_time_ns),
        now_ns=clock_ns(),
    )
    interest = audit_testnet_interest(
        client, sub_account_id,
        start_time_ns=history_start or int(observation.create_time_ns),
        now_ns=clock_ns(),
    )
    account = audit_testnet_account(client, observation, fills, now_ns=clock_ns(),
                                    funding=funding, transfers=transfers,
                                    withdrawals=withdrawals, interest=interest)
    evidence = save_testnet_audit(vault, observation, fills, funding, transfers,
                                  withdrawals, deposits, interest,
                                  account,
                                  now=_datetime_ns(clock_ns()))
    return GrvtTestnetObservedAudit(record, fills, funding, transfers,
                                    withdrawals, deposits, interest,
                                    account, evidence)
