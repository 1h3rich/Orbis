"""Coordinación de retirada puntual: View → permiso durable → un POST.

No tiene worker, claves guardadas, reintento ni automatismo al desbloquear.
"""

from datetime import datetime,timezone
import secrets
import time
from typing import Callable

from app.live_execution.mexc_uid_guard import assert_known_mexc_uid
from app.live_execution.mexc_withdrawal_transport import MexcWithdrawalClient,MexcWithdrawalError,WithdrawalRequest
from app.portfolio.mexc_withdrawal_journal import (
    WithdrawalError,acknowledge_withdrawal,amount_text,claim_withdrawal,public_record,withdrawal_record,
)


def fetch_withdrawal_inputs(view,address: str) -> tuple[str,dict]:
    """No presume cobertura global de órdenes ni estado atómico del exchange."""
    reference,uid = view.account_reference(),view.account_uid()
    route = view.bsc_withdrawal_route(address)
    observation = view.balances()
    balances = observation.get("balances")
    if (not isinstance(balances,list) or observation.get("source") != "MEXC_SPOT_PRIVATE_READONLY"
            or any(not isinstance(row,dict) for row in balances)):
        raise WithdrawalError("Saldos View de retirada no verificables")
    usdt = [row for row in balances if row.get("asset") == "USDT"]
    if len(usdt) > 1:
        raise WithdrawalError("Saldo USDT ambiguo")
    if view.account_uid() != uid or view.account_reference() != reference:
        raise WithdrawalError("La identidad View cambió durante la revisión")
    return uid,{"route":route,"balances":usdt[0] if usdt else {"free":"0","locked":"0"},
                "observed_at":observation["observed_at"]}


def cached_permission(access,token: str,view,generation: int) -> Callable[[],None]:
    """Sin adquirir RealAccess dentro de SQLCipher: evita inversión de cerrojos.

    lock/desconexión revocan estas referencias antes de esperar a la bóveda.
    El punto de comprobación final inicia el intento; no revoca un POST abierto.
    """
    def check():
        if (not isinstance(access._token,str) or not secrets.compare_digest(access._token,token)
                or access._vault_generation != generation or access._mexc is not view
                or time.monotonic() >= access._deadline):
            raise WithdrawalError("Permiso REAL revocado o sesión caducada; no se reenviará la retirada")
    return check


def submit_withdrawal_once(access,*,identifier: str,account_id: str,account_ref: str,address: str,
                           plan_digest: str,view,generation: int,assert_active: Callable,
                           client: MexcWithdrawalClient,clock: Callable | None = None) -> dict:
    now = clock or (lambda:datetime.now(timezone.utc))
    assert_active()
    with access.vault.guard_session_generation(generation),access.vault.session() as db:
        row,_,_ = withdrawal_record(db,identifier)
        if row.account_id != account_id or row.account_ref != account_ref or row.payload_digest != plan_digest:
            raise WithdrawalError("El plan no pertenece a esta cuenta")
        if row.status != "PREPARED":
            return {**public_record(db,identifier,now()),"post_attempted":False}
    uid = view.account_uid()
    client.pair_view_account(uid)
    fresh_uid,source = fetch_withdrawal_inputs(view,address)
    if fresh_uid != uid:
        raise WithdrawalError("View y credencial de retirada dejaron de coincidir")
    assert_active()
    if not assert_known_mexc_uid(access.vault,account_ref,uid):
        raise WithdrawalError("UID View no vinculado a esta cuenta")
    with access.vault.guard_session_generation(generation):
        with access.vault.session() as db:
            assert_active()
            grant = claim_withdrawal(db,identifier,account_id=account_id,account_ref=account_ref,
                address=address,plan_digest=plan_digest,source=source,now=now())
            _,terms,_ = withdrawal_record(db,identifier)
        if not grant["claim_granted"]:
            return {**grant,"post_attempted":False}
        try:
            assert_active()
            external_id = client.withdraw_once(WithdrawalRequest(identifier,address,terms["contract"],
                amount_text(int(terms["amount_units"]),terms["decimals"])))
        except (MexcWithdrawalError,WithdrawalError) as exc:
            return {**grant,"post_attempted":True,"review_required":True,"detail":str(exc)}
        # Un bloqueo solicitado durante la red espera a esta evidencia; no se pierde el acuse.
        with access.vault.session() as db:
            result = acknowledge_withdrawal(db,identifier,external_id,now=now())
        return {**result,"post_attempted":True,"review_required":True}
