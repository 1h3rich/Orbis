"""Lecturas View para recuperar un envío, comprobar recepción y preparar su asiento."""

from datetime import datetime,timezone

from app.live_execution.mexc_uid_guard import MexcTransferRecoveryView
from app.live_execution.mexc_withdrawal import cached_permission
from app.portfolio.mexc_withdrawal_journal import WithdrawalError,withdrawal_record
from app.portfolio.mexc_withdrawal_observation import save_withdrawal_observation


def read_context(access,token,account_id,identifier):
    with access._lock:
        access.require(token)
        view,ref,generation = access._mexc,access._mexc_account_ref,access._vault_generation
        if view is None or ref is None:
            raise WithdrawalError("Conecta MEXC View de la misma cuenta para revisar la retirada")
        active = cached_permission(access,token,view,generation)
    with access.vault.guard_session_generation(generation),access.vault.session() as db:
        plan,_,_ = withdrawal_record(db,identifier)
        if plan.account_id != account_id or plan.status not in ("SUBMISSION_UNKNOWN","ACKNOWLEDGED"):
            raise WithdrawalError("La retirada no corresponde a esta cuenta o no se intentó")
        recovery = MexcTransferRecoveryView(account_id, plan.account_ref, ref)
        age = (datetime.now(timezone.utc)-plan.created_at).total_seconds()
    return view,recovery,generation,active,age


def observe_withdrawal(access,token,account_id,identifier):
    view,recovery,generation,active,age = read_context(access,token,account_id,identifier)
    before = view.account_uid()
    if view.account_reference() != recovery.credential_ref:
        raise WithdrawalError("La referencia MEXC cambió antes de observar la retirada")
    active()
    if recovery.credential_ref != recovery.archive_ref:
        with access.vault.guard_session_generation(generation),access.vault.session() as db:
            db.connection().exec_driver_sql("BEGIN")
            recovery.verify(db, before)
    snapshot = view.transfers_recent(days=7 if age < 7*86400 else 30 if age < 30*86400 else 90,
                                    include_source_ids=True,include_withdrawal_evm=True)
    if view.account_uid() != before or view.account_reference() != recovery.credential_ref:
        raise WithdrawalError("La identidad View cambió durante la observación de retirada")
    with access.vault.guard_session_generation(generation),access.vault.session() as db:
        active()
        result,chain_hash = save_withdrawal_observation(
            db,identifier,account_id,recovery.archive_ref,snapshot,before,now=datetime.now(timezone.utc),
            recovery=recovery,
        )
    return {**result,"tx_hash":chain_hash},(view,recovery,generation,active,before)


def current_balance(view,expected_uid):
    if view.account_uid() != expected_uid:
        raise WithdrawalError("La identidad View cambió antes de conciliar")
    result = view.balances()
    if result.get("source") != "MEXC_SPOT_PRIVATE_READONLY" or not isinstance(result.get("balances"),list):
        raise WithdrawalError("Saldo View no verificable")
    rows = [item for item in result["balances"] if isinstance(item,dict) and item.get("asset") == "USDT"]
    if len(rows)>1 or view.account_uid() != expected_uid:
        raise WithdrawalError("Saldo o identidad USDT ambiguos")
    return {"balances":rows[0] if rows else {"free":"0","locked":"0"},"observed_at":result["observed_at"]}
