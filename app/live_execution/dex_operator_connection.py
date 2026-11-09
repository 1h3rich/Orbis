"""Verified generated operator → existing durable gas grant → RAM-only signer.

This bridge grants no bot activation or generic signing API. It never exports
key bytes, creates/replenishes a grant, performs RPC or broadcasts a transaction.
"""
from dataclasses import dataclass
from datetime import datetime,timedelta
import hashlib,json,math
from threading import RLock
from uuid import UUID

from sqlalchemy import select

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_gas_funding import FundingState, operator_gas_coverage
from app.live_execution.dex_operator import DexOperatorSession
from app.live_execution.dex_operator_drafts import DexOperatorDrafts
from app.live_execution.dex_permission import MAX_PERMISSION_SECONDS
from app.portfolio import dex_gas_funding as funding
from app.portfolio.models import RealDexOperatorIntent
from app.portfolio.vault import RealVault,VaultError
from app.portfolio.wallet_funding import _time
from app.portfolio.wallet_proof import canonical_wallet_address


@dataclass(frozen=True,slots=True)
class _Scope:
    request_id:str
    request_digest:str
    account_id:str
    binding_id:str
    grant_id:str
    draft_id:str
    wallet_ref:str
    generation:int
    revision:int
    expires_at:datetime


def _wallet_ref(wallet:str)->str:
    # Ephemeral comparison only; no address or key is stored in the scope.
    return hashlib.sha256(wallet.encode('ascii')).hexdigest()


class DexOperatorConnection:
    def __init__(self,vault:RealVault,drafts:DexOperatorDrafts,session:DexOperatorSession)->None:
        if type(drafts) is not DexOperatorDrafts or type(session) is not DexOperatorSession or drafts.vault is not vault or session.vault is not vault:
            raise DexExecutorError('La preparación y el firmante deben pertenecer a la misma bóveda')
        self.vault,self.drafts,self.session=vault,drafts,session
        self._lock=RLock();self._scope:_Scope|None=None;self._requests:dict[str,str]={}

    @staticmethod
    def _empty()->dict:
        return {'connected':False,'credential_storage':'NONE','automatic_execution':False}

    def _view(self,now:datetime)->dict:
        scope=self._scope
        if scope is None:return self._empty()
        state=self.session.status()
        if (not state['connected'] or self.session.permission_revision!=scope.revision
                or self.vault.session_generation!=scope.generation or _time(now)>=scope.expires_at):
            self.session.revoke_revision(scope.revision);self._scope=None;return self._empty()
        return {**state,'request_id':scope.request_id,'account_id':scope.account_id,'binding_id':scope.binding_id,
            'grant_id':scope.grant_id,'draft_id':scope.draft_id,'expires_at':scope.expires_at.isoformat()}

    def connect(self,*,request_id:str,account_id:str,binding_id:str,draft_id:str,wallet_address:str,
                snapshot:FundingState,ttl_seconds:int,now:datetime,order_gas_wei:int=0)->dict:
        now=_time(now)
        request_id,account_id,binding_id,draft_id=[str(UUID(v)) for v in (request_id,account_id,binding_id,draft_id)]
        wallet=canonical_wallet_address(wallet_address)
        if type(ttl_seconds) is not int or not 1<=ttl_seconds<=MAX_PERMISSION_SECONDS:raise DexExecutorError('Vigencia de firma no válida')
        parameters={'account_id':account_id,'binding_id':binding_id,'draft_id':draft_id,'wallet_ref':_wallet_ref(wallet),'ttl_seconds':ttl_seconds}
        if order_gas_wei:
            parameters['order_gas_wei']=order_gas_wei
        digest=hashlib.sha256(json.dumps(parameters,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        generation=self.vault.session_generation
        try:
            # The same order is used for status/disconnect. No RPC or expensive
            # key derivation runs while these short in-process locks are held.
            with self.vault.guard_session_generation(generation),self.drafts._lock,self._lock:
                draft=self.drafts._match(draft_id,account_id,wallet,generation,now)
                with self.vault.session() as db:
                    parent,grant,budget,recipient=funding._live(db,binding_id=binding_id,account_id=account_id,wallet=wallet,
                        drafts=self.drafts,draft_id=draft_id,snapshot=snapshot,now=now)
                    unfinished=list(db.scalars(select(RealDexOperatorIntent.id).where(RealDexOperatorIntent.grant_id==parent.grant_id,
                        RealDexOperatorIntent.status.not_in(('FINALIZED','REVERTED','ABANDONED_UNSENT')))))
                    if unfinished:
                        if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()<71:raise DexExecutorError('Resuelve las intenciones anteriores antes de conectar la firma')
                        from app.portfolio.dex_operator_outcomes import operator_outcome
                        if any(not operator_outcome(db,item,as_of=now,include_external=True).settled for item in unfinished):raise DexExecutorError('Resuelve las intenciones anteriores antes de conectar la firma')
                    # Archive capture can expire ORM attributes while checking
                    # an external acceptance. No ORM row may escape this lease.
                    grant_id=parent.grant_id
                active=self._view(now)
                if self._scope is not None and self._scope.request_id==request_id:
                    if self._scope.request_digest!=digest:raise DexExecutorError('La solicitud de firma ya tiene otros datos')
                    return active
                if request_id in self._requests:raise DexExecutorError('Esta conexión ya terminó; usa una solicitud nueva')
                if len(self._requests)>=1000:raise DexExecutorError('Límite de conexiones locales alcanzado')
                if active['connected'] or self.session.status()['connected']:
                    raise DexExecutorError('Desconecta la firma actual antes de conectar otra')
                if not draft.backup_verified:raise DexExecutorError('Comprueba primero la copia de la operadora')
                ttl=min(ttl_seconds,math.floor(draft.deadline-self.drafts._clock()),
                    math.floor((draft.expires_at-now).total_seconds()),math.floor(grant['limits']['expires_at']-now.timestamp()))
                coverage=operator_gas_coverage(snapshot,budget,order_gas_wei)
                gas=coverage.available_wei
                if ttl<1 or gas<coverage.stop_reserve_wei:
                    raise DexExecutorError('La vigencia o el gas no cubre la reserva STOP de la firma')
                if gas<coverage.required_wei:
                    raise DexExecutorError('Gas insuficiente para una orden y la reserva STOP')
                # Connect and attach revocation atomically under the draft
                # lock; an old timer may never erase a newer signer revision.
                try:
                    self.session.connect(private_key=bytes(draft.key),snapshot=snapshot.executor,ttl_seconds=ttl,
                        gas_budget_wei=gas,backup_confirmed=True,now=now)
                    self.drafts._remember_signer(draft,self.session)
                    if self.drafts._live(generation,now) is not draft:raise DexExecutorError('La preparación caducó durante la conexión')
                except Exception:
                    self.session.revoke();raise
                self._requests[request_id]=digest
                self._scope=_Scope(request_id,digest,account_id,binding_id,grant_id,draft_id,_wallet_ref(wallet),generation,
                    self.session.permission_revision,now+timedelta(seconds=ttl))
                return self._view(now)
        except VaultError:
            raise DexExecutorError('La sesión REAL cambió; no se conectó la firma') from None

    def status(self,*,account_id:str,wallet_address:str,now:datetime)->dict:
        now=_time(now);account_id=str(UUID(account_id));wallet=canonical_wallet_address(wallet_address)
        generation=self.vault.session_generation
        try:
            with self.vault.guard_session_generation(generation),self.drafts._lock,self._lock:
                scope=self._scope
                if scope is None:return self._empty()
                if (scope.account_id,scope.wallet_ref)!=(account_id,_wallet_ref(wallet)):return self._empty()
                draft=self.drafts._live(generation,now)
                if draft is None or draft.identifier!=scope.draft_id:
                    self.session.revoke_revision(scope.revision);self._scope=None;return self._empty()
                return self._view(now)
        except VaultError:return self._empty()

    def disconnect(self,*,account_id:str,wallet_address:str)->dict:
        account_id=str(UUID(account_id));wallet=canonical_wallet_address(wallet_address)
        with self._lock:
            scope=self._scope
            if scope is not None:
                if (scope.account_id,scope.wallet_ref)!=(account_id,_wallet_ref(wallet)):
                    raise DexExecutorError('La firma pertenece a otra cuenta o wallet')
                self.session.revoke_revision(scope.revision);self._scope=None
            return self._empty()

    def revoke(self)->None:
        """Local lifecycle only; no chain STOP, sales, receipts or budget edits."""
        with self._lock:
            if self._scope is not None:self.session.revoke_revision(self._scope.revision)
            self._scope=None

    def revoke_revision(self,revision:int)->None:
        """A stopped worker cannot revoke a later connection's permission."""
        with self._lock:
            if self._scope is not None and self._scope.revision==revision:
                self.session.revoke_revision(revision);self._scope=None
