"""Dedicated owner-only native return continuation; never generic signing.

One durable claim precedes one POST. Raw bytes and the draft key stay in RAM;
restoring a journal row cannot recreate this noncopyable continuation.
"""
from contextlib import contextmanager
from dataclasses import dataclass,field
from datetime import datetime
from threading import Lock
from weakref import ref

from eth_account import Account
from eth_utils import to_checksum_address

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_gas_return import GasReturnPlan,verify_return_signature
from app.live_execution.dex_operator_drafts import DexOperatorDrafts
from app.portfolio import dex_gas_return as journal
from app.portfolio.vault import VaultError
from app.portfolio.wallet_funding import _time

_SEAL=object()


@dataclass(frozen=True,slots=True,repr=False)
class _Signature:
    raw:bytes=field(repr=False)
    tx_hash:str=field(repr=False)
    revision:int


@contextmanager
def _guard(vault,drafts,generation,revision,*,draft_id,account_id,wallet,now):
    with vault.guard_session_generation(generation),drafts._lock:
        draft=drafts._match(draft_id,account_id,wallet,generation,_time(now))
        attached=drafts._attached_signer
        signer=attached[0]() if attached is not None else None
        if (drafts._revision!=revision or not draft.backup_verified
                or signer is not None and signer.status()['connected']):
            raise DexExecutorError('La preparación de devolución cambió o la firma de trading sigue conectada')
        yield draft


def _sign(vault,drafts,*,identifier,account_id,wallet,draft_id,plan,now,generation):
    with vault.guard_session_generation(generation),vault.session() as db:
        row,terms,_=journal.return_record(db,identifier)
        if row.account_id!=account_id or row.status!='RESERVED':raise DexExecutorError('La devolución no tiene reserva original')
        journal._compatible(db,terms,plan,now)
    with drafts._copy(draft_id,account_id,wallet,now) as (draft,key):
        with drafts._lock:revision=drafts._revision
        with _guard(vault,drafts,generation,revision,draft_id=draft_id,account_id=account_id,wallet=wallet,now=now) as current:
            if current is not draft or current.address!=plan.snapshot.operator:raise DexExecutorError('La operadora de devolución cambió')
        tx=plan.transaction()
        signable={name:to_checksum_address(value) if name=='to' else value if name=='data' else int(value,16)
            for name,value in tx.items() if name not in ('from','type')}
        signed=Account.sign_transaction(signable,bytes(key));raw=bytes(signed.raw_transaction);tx_hash='0x'+signed.hash.hex()
        verify_return_signature(plan,raw,tx_hash)
        with _guard(vault,drafts,generation,revision,draft_id=draft_id,account_id=account_id,wallet=wallet,now=now):
            return _Signature(raw,tx_hash,revision)


class PreparedGasReturn:
    def __init__(self,seal,vault,drafts,*,generation,account_id,wallet,draft_id,identifier,plan,digest,signed):
        if seal is not _SEAL:raise TypeError('Use prepare_return_execution')
        self.identifier=identifier;self._vault,self._drafts=ref(vault),ref(drafts)
        self._generation,self._account,self._wallet,self._draft_id=generation,account_id,wallet,draft_id
        self._plan,self._digest,self._signed=plan,digest,signed
        self._taken=False;self._lock=Lock()

    def __copy__(self):raise TypeError('La devolución preparada no se copia')
    def __deepcopy__(self,memo):raise TypeError('La devolución preparada no se copia')
    def __reduce_ex__(self,protocol):raise TypeError('La devolución preparada no se serializa')

    @property
    def transaction_hash(self):
        with self._lock:return self._signed.tx_hash if self._signed is not None else None

    def broadcast_once(self,post_raw,*,now:datetime,require_active=lambda:None):
        vault,drafts=self._vault(),self._drafts()
        if vault is None or drafts is None:raise DexExecutorError('La preparación de devolución ya no existe')
        with self._lock:
            if self._taken or self._signed is None:raise DexExecutorError('La devolución ya consumió su único intento')
            signed=self._signed
            try:
                require_active();self._plan.validate(now)
                with _guard(vault,drafts,self._generation,signed.revision,draft_id=self._draft_id,account_id=self._account,wallet=self._wallet,now=now),vault.session() as db:
                    result=journal.mark_broadcast(db,self.identifier,account_id=self._account,plan_digest=self._digest,tx_hash=signed.tx_hash,now=now)
            except Exception:
                self._signed=None;self._taken=True
                raise DexExecutorError('La autorización de devolución cambió; no se emitió este intento') from None
            self._signed=None;self._taken=True
        # No access/vault/draft/DB lock crosses this network boundary.
        try:
            reported=post_raw(signed.raw);matched=isinstance(reported,str) and reported.lower()==signed.tx_hash
        except Exception:matched=False
        finally:del signed
        return {**result,'send_result':'HASH_MATCHED' if matched else 'UNKNOWN','new_order_post_attempted':True}


def prepare_return_execution(vault,drafts,*,identifier,account_id,wallet_address,draft_id,plan,plan_digest,now):
    generation=vault.session_generation
    if generation is None or type(drafts) is not DexOperatorDrafts or drafts.vault is not vault or type(plan) is not GasReturnPlan:
        raise DexExecutorError('La devolución necesita su bóveda y preparación verificadas')
    wallet=journal.canonical_wallet_address(wallet_address)
    with vault.guard_session_generation(generation),vault.session() as db:
        reserved=journal.reserve_return(db,identifier,account_id=account_id,wallet_address=wallet,drafts=drafts,draft_id=draft_id,
            plan=plan,plan_digest=plan_digest,now=now)
    if not reserved['reserved']:return {**reserved,'replayed_without_post':True,'new_order_post_attempted':False}
    try:
        signed=_sign(vault,drafts,identifier=identifier,account_id=account_id,wallet=wallet,draft_id=draft_id,plan=plan,now=now,generation=generation)
        with vault.guard_session_generation(generation),vault.session() as db:
            stored=journal.record_signature(db,identifier,account_id=account_id,plan=plan,raw=signed.raw,tx_hash=signed.tx_hash,now=now)
        return PreparedGasReturn(_SEAL,vault,drafts,generation=generation,account_id=account_id,wallet=wallet,draft_id=draft_id,
            identifier=identifier,plan=plan,digest=stored['plan_digest'],signed=signed)
    except Exception:
        try:
            with vault.guard_session_generation(generation),vault.session() as db:journal.abandon_unsent(db,identifier,account_id=account_id,now=now)
        except (ValueError,VaultError):pass
        raise
