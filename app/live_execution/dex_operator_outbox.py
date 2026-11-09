"""Ephemeral signed continuation; durable unique attempt before one POST.

No HTTP route or bot activation. A stored record cannot recreate a continuation.
Raw signatures pass only to the internal transport and are not persisted/logged.
"""
from datetime import datetime
from contextlib import nullcontext
from threading import Lock
from weakref import ref

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_trade import DexTradePlan, DexStopPlan
from app.portfolio.vault import VaultError
from app.portfolio import dex_operator_journal as journal


_SEAL = object()


class PreparedDexBroadcast:
    def __init__(self, seal, vault, session, generation, account_id, record, signed):
        if seal is not _SEAL:raise TypeError('Use prepare_execution')
        self.identifier = record['id']
        self._vault,self._generation,self._account = ref(vault),generation,account_id
        self._session = ref(session)
        self._digest,self._signed = record['plan_digest'],signed
        self._taken,self._lock = False,Lock()

    def __copy__(self):raise TypeError('La concesión de emisión no se copia')
    def __deepcopy__(self,memo):raise TypeError('La concesión de emisión no se copia')
    def __reduce_ex__(self,protocol):raise TypeError('La concesión de emisión no se serializa')

    @property
    def transaction_hash(self) -> str | None:
        """Original hash in RAM; it survives an uncertain transport in its owner."""
        with self._lock:
            return self._signed.tx_hash if self._signed is not None else None

    def broadcast_once(self, post_raw, *, now: datetime, require_active=lambda:None, permission_guard=nullcontext) -> dict:
        vault = self._vault()
        session = self._session()
        if vault is None or session is None:raise DexExecutorError('La bóveda o firma de esta preparación ya no existe')
        with self._lock:
            if self._taken or self._signed is None:raise DexExecutorError('La preparación ya consumió su único intento de envío')
            signed = self._signed
            try:
                # A worker STOP shares this short guard. The durable claim is
                # the boundary: a claimed send is in flight, never retried.
                with permission_guard():
                    require_active()
                    with vault.guard_session_generation(self._generation),session.guard_emission(signed.permission_revision),vault.session() as db:
                        result = journal.mark_broadcast(db,self.identifier,account_id=self._account,
                            plan_digest=self._digest,tx_hash=signed.tx_hash,now=now)
                self._taken = True
                self._signed = None
            except (VaultError,DexExecutorError):
                self._signed = None;self._taken = True
                raise DexExecutorError('La sesión de bóveda cambió; no se emitió la preparación') from None
        # Network never holds REAL, the DB write transaction or signer locks.
        # Failure/timeout leaves the known original hash and durable attempt.
        try:
            reported = post_raw(signed.raw_transaction)
            matched = isinstance(reported,str) and reported.lower() == signed.tx_hash
        except Exception:
            matched = False
        finally:
            del signed
        return {**result,'send_result':'HASH_MATCHED' if matched else 'UNKNOWN','new_order_post_attempted':True}


def prepare_execution(vault, session, *, identifier: str, account_id: str, grant_id: str,
                      plan: DexTradePlan | DexStopPlan, decision_ref: str, now: datetime):
    generation = vault.session_generation
    if generation is None or session.vault is not vault:raise DexExecutorError('La firma no pertenece a esta bóveda desbloqueada')
    with vault.guard_session_generation(generation),vault.session() as db:
        reserved = journal.reserve_intent(db,identifier=identifier,account_id=account_id,grant_id=grant_id,
            plan=plan,decision_ref=decision_ref,now=now)
    if not reserved['reserved']:
        return {**reserved,'replayed_without_post':True,'new_order_post_attempted':False}
    signed = session.sign_swap(plan,now=now) if type(plan) is DexTradePlan else session.sign_stop(plan,now=now)
    with vault.guard_session_generation(generation),vault.session() as db:
        stored = journal.record_signature(db,identifier,account_id=account_id,signed=signed,plan=plan,now=now)
    if not stored['signature_recorded']:raise DexExecutorError('La firma original ya está archivada; sólo puede cotejarse')
    return PreparedDexBroadcast(_SEAL,vault,session,generation,account_id,stored,signed)


def abandon_execution(vault, session, identifier: str, *, account_id: str,
                      plan: DexTradePlan | DexStopPlan, now: datetime) -> dict:
    """Discard only before the durable POST claim; stale continuations then fail.

    SIGNED is still local: raw bytes have no public/export route and every send
    requires a committed BROADCAST_ATTEMPTED transition. Once that transition
    exists, even a timeout cannot be treated as never sent.
    """
    generation = vault.session_generation
    if generation is None or session.vault is not vault:raise DexExecutorError('Conecta la bóveda original para descartar')
    with vault.guard_session_generation(generation),vault.session() as db:
        row,terms,_ = journal.intent_record(db,identifier)
        if row.account_id != account_id or any(terms[key] != value for key,value in journal._recipe(db,plan).items()):
            raise DexExecutorError('La intención descartada no coincide con la reserva de firma')
        result = journal.abandon_unsent(db,identifier,account_id=account_id,now=now)
        session._discard_unsent_reservation(plan)
    return result
