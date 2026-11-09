"""Owner-reviewed zero-to-self cancellation; one durable grant, one POST.

Raw signed bytes and the operator key live only in RAM. A restored journal
cannot reconstruct this continuation or issue a second send.
"""
from dataclasses import dataclass,field
from datetime import datetime
from threading import Lock
from weakref import ref
from eth_account import Account
from eth_utils import to_checksum_address
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_gas_cancellation import CancellationPlan,verify_cancellation_signature
from app.live_execution.dex_gas_return_outbox import _guard
from app.live_execution.dex_operator_drafts import DexOperatorDrafts
from app.portfolio import dex_gas_cancellation as journal,dex_gas_return as original,dex_native_resolution as native
from app.portfolio.vault import VaultError

_SEAL=object()


@dataclass(frozen=True,slots=True,repr=False)
class _Signature:
    raw:bytes=field(repr=False)
    tx_hash:str=field(repr=False)
    revision:int


def _sign(vault,drafts,*,identifier,account_id,wallet,draft_id,plan,now,generation):
    with vault.guard_session_generation(generation),vault.session() as db:
        row,terms,_=journal._owned(db,identifier,account_id,wallet)
        parent,_,_=original.return_record(db,row.return_id)
        if (row.status!='RESERVED' or parent.status!='BROADCAST_ATTEMPTED'
                or native.status(db,'GAS_RETURN',row.return_id) is not None):
            raise DexExecutorError('La cancelación ya no tiene una reserva pendiente')
        journal._compatible(terms,plan,now)
    with drafts._copy(draft_id,account_id,wallet,now) as (draft,key):
        with drafts._lock:revision=drafts._revision
        with _guard(vault,drafts,generation,revision,draft_id=draft_id,account_id=account_id,wallet=wallet,now=now) as current:
            if current is not draft or current.address!=plan.snapshot.operator:
                raise DexExecutorError('La operadora de cancelación cambió')
        tx=plan.transaction()
        signable={name:to_checksum_address(value) if name=='to' else value if name=='data' else int(value,16)
            for name,value in tx.items() if name not in ('from','type')}
        signed=Account.sign_transaction(signable,bytes(key));raw=bytes(signed.raw_transaction);tx_hash='0x'+signed.hash.hex()
        verify_cancellation_signature(plan,raw,tx_hash)
        with _guard(vault,drafts,generation,revision,draft_id=draft_id,account_id=account_id,wallet=wallet,now=now):
            return _Signature(raw,tx_hash,revision)


class PreparedGasCancellation:
    def __init__(self,seal,vault,drafts,*,generation,account_id,wallet,draft_id,identifier,plan,digest,signed):
        if seal is not _SEAL:raise TypeError('Use prepare_cancellation_execution')
        self.identifier=identifier;self._vault,self._drafts=ref(vault),ref(drafts)
        self._generation,self._account,self._wallet,self._draft_id=generation,account_id,wallet,draft_id
        self._plan,self._digest,self._signed=plan,digest,signed
        self._taken=False;self._lock=Lock()

    def __copy__(self):raise TypeError('La cancelación preparada no se copia')
    def __deepcopy__(self,memo):raise TypeError('La cancelación preparada no se copia')
    def __reduce_ex__(self,protocol):raise TypeError('La cancelación preparada no se serializa')

    @property
    def transaction_hash(self)->str|None:
        with self._lock:return self._signed.tx_hash if self._signed is not None else None

    def broadcast_once(self,post_raw,*,now:datetime,require_active=lambda:None)->dict:
        vault,drafts=self._vault(),self._drafts()
        if vault is None or drafts is None:raise DexExecutorError('La preparación de cancelación ya no existe')
        with self._lock:
            if self._taken or self._signed is None:raise DexExecutorError('La cancelación ya consumió su único intento')
            signed=self._signed
            try:
                require_active();self._plan.validate(now)
                with _guard(vault,drafts,self._generation,signed.revision,draft_id=self._draft_id,account_id=self._account,wallet=self._wallet,now=now),vault.session() as db:
                    result=journal.mark_broadcast(db,self.identifier,account_id=self._account,plan_digest=self._digest,tx_hash=signed.tx_hash,now=now)
            except Exception:
                self._signed=None;self._taken=True
                raise DexExecutorError('La autorización de cancelación cambió; no se emitió este intento') from None
            self._signed=None;self._taken=True
        # No vault, draft, access or SQL lock crosses the network boundary.
        try:
            reported=post_raw(signed.raw);matched=isinstance(reported,str) and reported.lower()==signed.tx_hash
        except Exception:matched=False
        finally:del signed
        return {**result,'send_result':'HASH_MATCHED' if matched else 'UNKNOWN','new_order_post_attempted':True}


def prepare_cancellation_execution(vault,drafts,*,identifier,account_id,wallet_address,draft_id,plan,plan_digest,now):
    generation=vault.session_generation
    if generation is None or type(drafts) is not DexOperatorDrafts or drafts.vault is not vault or type(plan) is not CancellationPlan:
        raise DexExecutorError('La cancelación necesita su bóveda y copia verificadas')
    wallet=journal.canonical_wallet_address(wallet_address)
    with vault.guard_session_generation(generation),vault.session() as db:
        reserved=journal.reserve_cancellation(db,identifier,account_id=account_id,wallet_address=wallet,drafts=drafts,draft_id=draft_id,
            plan=plan,plan_digest=plan_digest,now=now)
    if not reserved['reserved']:return {**reserved,'replayed_without_post':True,'new_order_post_attempted':False}
    try:
        signed=_sign(vault,drafts,identifier=identifier,account_id=account_id,wallet=wallet,draft_id=draft_id,plan=plan,now=now,generation=generation)
        with vault.guard_session_generation(generation),vault.session() as db:
            stored=journal.record_signature(db,identifier,account_id=account_id,plan=plan,raw=signed.raw,tx_hash=signed.tx_hash,now=now)
        return PreparedGasCancellation(_SEAL,vault,drafts,generation=generation,account_id=account_id,wallet=wallet,draft_id=draft_id,
            identifier=identifier,plan=plan,digest=stored['plan_digest'],signed=signed)
    except Exception:
        try:
            with vault.guard_session_generation(generation),vault.session() as db:journal.abandon_unsent(db,identifier,account_id=account_id,now=now)
        except (ValueError,VaultError):pass
        raise
