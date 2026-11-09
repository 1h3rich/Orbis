"""Incoming market → causal journal → one bounded send → finalized receipt.

Constructing a worker starts no thread or transaction. Only its explicit cycles
may operate; saved ACTIVE rows cannot recreate the RAM claim or signer lease.
RPC and HTTP never hold the vault, signer or STOP permission guard.
"""
from dataclasses import dataclass
from datetime import datetime,timezone
from threading import Event,Lock,RLock,Thread,current_thread
import math,time
from typing import Callable,ContextManager,Protocol
from uuid import UUID,uuid4

from app.lab.engine import INTERVALS
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_bot_market import advance_market,market_overlap,reference_quote
from app.live_execution.dex_dca_plan import ENGINE as DCA_ENGINE,execution_ttl
from app.live_execution.dex_operator_connection import DexOperatorConnection
from app.live_execution.dex_operator_outbox import prepare_execution,abandon_execution
from app.live_execution.dex_operator_transport import BnbOperatorReadClient,BnbOperatorSender,collect_operator_receipt
from app.live_execution.dex_state import read_executor_state
from app.live_execution.dex_trade import read_exit_cap,read_trade_plan,_inputs
from app.live_execution.evm_transaction import quantity
from app.marketdata.adapters.mexc import MexcAdapter
from app.portfolio import dex_bot_journal as bots,dex_operator_journal as journal
from app.portfolio.dex_deployments import deployment_record
from app.portfolio.vault import RealVault,VaultError
from app.portfolio.wallet_funding import _time
from app.portfolio.wallet_proof import canonical_wallet_address

from app.live_execution.dex_versions import DCA_POLICY_OUTCOME,EXTERNAL_DCA_ENGINES
DCA_ENGINES=(DCA_ENGINE,'orbis.dex.bot.dca.v2',DCA_POLICY_OUTCOME,*EXTERNAL_DCA_ENGINES)


class Market(Protocol):
    def get_candles(self,symbol:str,*,timeframe:str,since:int,before:int,limit:int)->list[dict]|None:...
    def get_paper_quote(self,symbol:str)->dict|None:...


@dataclass(frozen=True,slots=True)
class DexExecutionOptions:
    gas:int=400000
    gas_price:int=10**9
    max_gas_wei:int=10**15
    slippage_bps:int=25

    def validate(self)->None:
        _inputs(gas=self.gas,gas_price=self.gas_price,max_gas_wei=self.max_gas_wei)
        if type(self.slippage_bps) is not int or not 1<=self.slippage_bps<=500:
            raise DexExecutorError('Deslizamiento del bot DEX no válido')


class _Halt(DexExecutorError):
    def __init__(self,reason):self.reason=reason;super().__init__(reason)


class DexBotWorker:
    def __init__(self,vault:RealVault,connection:DexOperatorConnection,*,run_id:str,account_id:str,claim:str,
                 wallet_address:str,market:Market|None=None,options:DexExecutionOptions|None=None,
                 rpc_factory:Callable[[],ContextManager]=BnbOperatorReadClient,
                 sender_factory:Callable|None=None,clock:Callable[[],datetime]|None=None,on_exit:Callable[[],None]|None=None,
                 research_digest:str|None=None):
        if type(connection) is not DexOperatorConnection or connection.vault is not vault:
            raise DexExecutorError('El trabajador requiere su conexión de firma original')
        self._vault,self._connection=vault,connection
        self._run_id,self._account,self._claim=[str(UUID(value)) for value in (run_id,account_id,claim)]
        self._wallet=canonical_wallet_address(wallet_address)
        self._clock=clock or (lambda:datetime.now(timezone.utc))
        self._options=options or DexExecutionOptions();self._options.validate()
        self._market=market or MexcAdapter();self._rpc_factory=rpc_factory
        self._on_exit=on_exit
        self._sender_factory=sender_factory or (lambda plan:BnbOperatorSender(plan,clock=self._clock))
        self._generation=vault.session_generation;self._revision=connection.session.permission_revision
        self._cycle_lock=Lock();self._state_lock=RLock();self._permission_lock=RLock();self._stop_event=Event()
        self._shutdown_event=Event();self._wake_event=Event();self._started=False;self._pending_deadline=0.0
        self._pending:tuple[str,str]|None=None;self._prepared_id=None;self._prepared_plan=None;self._thread=None
        self._reason=None;self._last_action=None;self._last_receipt=None;self._stop_persisted=False;self._archive_error=False
        now=_time(self._clock())
        self._research_digest=research_digest
        with vault.guard_session_generation(self._generation),vault.session() as db:
            row,terms,_,archive=bots.run_record(db,self._run_id)
            live=connection.status(account_id=self._account,wallet_address=self._wallet,now=now)
            if (row.account_id!=self._account or row.status!='ACTIVE'
                    or journal._identity(db,'bot-claim',self._claim)!=terms['claim_ref']
                    or journal._identity(db,'wallet',self._wallet)!=terms['wallet_digest']
                    or not live['connected'] or live['request_id']!=terms['connection_request_id']):
                raise DexExecutorError('El trabajador no posee la activación original del bot')
            self._grant=row.grant_id;self._binding=row.binding_id;self._request=live['request_id']
            self._expires=_time(datetime.fromisoformat(terms['expires_at']));self._nonce=int(terms['creation_nonce'])
            self._mode=terms.get('permission',{}).get('mode','SESSION')
            self._fee=deployment_record(db,archive['grant']['deployment_id'])[1]['fee']
            from app.portfolio.dex_research import authorization_qualification,authorization_digest
            proof=authorization_qualification(db,self._run_id,expected_digest=research_digest)
            if proof is not None:self._research_digest=authorization_digest(db,self._run_id)

    def _check_research(self,db)->None:
        if self._research_digest is not None:
            from app.portfolio.dex_research import authorization_qualification
            authorization_qualification(db,self._run_id,expected_digest=self._research_digest)

    def _require_active(self)->None:
        if self._stop_event.is_set():raise _Halt(self._reason or 'MANUAL')
        if self._vault.session_generation!=self._generation:raise _Halt('SESSION_CHANGED')
        now=_time(self._clock())
        if now>=self._expires:raise _Halt('PERMISSION_EXPIRED')
        live=self._connection.status(account_id=self._account,wallet_address=self._wallet,now=now)
        if (not live['connected'] or live['request_id']!=self._request or live['grant_id']!=self._grant
                or self._connection.session.permission_revision!=self._revision):raise _Halt('SESSION_CHANGED')

    def status(self)->dict:
        with self._state_lock:
            running=self.alive
            result={'run_id':self._run_id,'running':running,'execution_enabled':False,'automatic_execution':False,
                'permission_mode':self._mode,'expires_at':self._expires.isoformat(),
                'reason':self._reason,'last_action':self._last_action,'pending_receipt':self._pending is not None,
                'last_receipt_status':self._last_receipt,'stop_recorded':self._stop_persisted,
                'review_required':self._archive_error}
        if running:
            try:self._require_active()
            except DexExecutorError:pass
            else:result['execution_enabled']=result['automatic_execution']=True
        return result

    @property
    def alive(self)->bool:
        """Thread liveness only; this property never reads SQL or permissions."""
        with self._state_lock:return self._thread is not None and self._thread.is_alive()

    @property
    def permission_mode(self)->str:return self._mode

    def _persist_stop(self,*,wait:bool=False)->None:
        if self._stop_persisted or self._reason not in bots.CONTROL_REASONS:return
        try:
            guard=(self._vault.guard_session_generation(self._generation) if wait
                else self._vault.try_guard_session_generation(self._generation))
            with guard as allowed:
                if wait or allowed:
                    with self._vault.session() as db:
                        bots.stop_run(db,self._run_id,account_id=self._account,now=_time(self._clock()),reason=self._reason)
                    with self._state_lock:self._stop_persisted=True;self._archive_error=False
                else:
                    with self._state_lock:self._archive_error=True
        except Exception:
            # Local revocation already happened. A corrupt/unavailable archive
            # cannot keep a key alive or expose SQL/provider diagnostics.
            with self._state_lock:self._archive_error=True

    def stop(self,reason:str='MANUAL')->dict:
        if reason not in bots.CONTROL_REASONS:raise DexExecutorError('Motivo de parada no admitido')
        with self._permission_lock:
            if not self._stop_event.is_set():
                with self._state_lock:self._reason=reason
                self._stop_event.set()
            self._wake_event.set()
        self._connection.revoke_revision(self._revision)
        self._persist_stop()
        return self.status()

    def start(self,*,interval_seconds:float=5)->dict:
        if (type(interval_seconds) not in (int,float) or not math.isfinite(interval_seconds)
                or not .1<=interval_seconds<=10):raise DexExecutorError('Intervalo del trabajador no válido')
        self._require_active()
        with self._state_lock:
            if self._started:raise DexExecutorError('El trabajador ya se inició; una parada requiere un nuevo rearme')
            self._started=True
            self._thread=Thread(target=self._loop,args=(interval_seconds,),name='orbis-dex-bot',daemon=True)
            thread=self._thread
        try:thread.start()
        except Exception:
            self.stop('THREAD_START_FAILED')
            raise DexExecutorError('No se pudo arrancar el trabajador DEX') from None
        return self.status()

    def _loop(self,interval:float)->None:
        try:
            while not self._shutdown_event.is_set():
                # STOP may observe an already granted send for two minutes,
                # with no signing permission. Nothing is inferred on timeout.
                if self._stop_event.is_set() and (self._pending is None or time.monotonic()>=self._pending_deadline
                        or self._vault.session_generation!=self._generation):break
                self.run_once()
                if self._stop_event.is_set() and self._pending is None:break
                self._wake_event.wait(interval);self._wake_event.clear()
        finally:
            if not self._stop_event.is_set():self.stop('INTERNAL_ERROR')
            # The signature is already revoked. Outside cycle/permission locks,
            # finish the metadata write even if a reader briefly held the vault.
            # The original generation guard forbids writing into a reopened vault.
            self._persist_stop(wait=True)
            if self._on_exit is not None:
                try:self._on_exit()
                except Exception:pass

    def shutdown(self)->dict:
        """Cooperative shutdown; a caller must never join under REAL locks."""
        self._shutdown_event.set();return self.stop('SHUTDOWN')

    def join(self,*,timeout:float=5)->None:
        if type(timeout) not in (int,float) or not math.isfinite(timeout) or not 0<=timeout<=30:
            raise DexExecutorError('Espera del trabajador no válida')
        with self._state_lock:thread=self._thread
        if thread is not None and thread is not current_thread():thread.join(timeout)

    def _observe_pending(self)->None:
        identifier,tx_hash=self._pending
        if self._vault.session_generation!=self._generation:raise _Halt('SESSION_CHANGED')
        with self._rpc_factory() as rpc:proof=collect_operator_receipt(rpc,self._wallet,self._nonce,tx_hash)
        if proof is None or quantity(proof['finalized']['number'])<quantity(proof['receipt']['blockNumber']):return
        with self._vault.guard_session_generation(self._generation),self._vault.session() as db:
            checked=journal.check_intent(db,identifier,account_id=self._account,evidence=proof,now=_time(self._clock()))
        with self._state_lock:self._pending=None;self._last_receipt=checked['status']
        if checked['status']=='REVERTED':raise _Halt('RECEIPT_INVALID')

    def _abandon_unsent(self)->None:
        if self._prepared_id is None:return
        try:
            abandon_execution(self._vault,self._connection.session,self._prepared_id,account_id=self._account,
                plan=self._prepared_plan,now=_time(self._clock()))
        except Exception:
            with self._state_lock:self._archive_error=True
        finally:self._prepared_id=None;self._prepared_plan=None

    def run_once(self)->dict:
        if not self._cycle_lock.acquire(blocking=False):return self.status()
        stage='INTERNAL_ERROR'
        try:
            if self._pending is not None:
                stage='RECEIPT_INVALID';self._observe_pending();return self.status()
            self._require_active()
            with self._vault.guard_session_generation(self._generation),self._vault.session() as db:
                self._check_research(db)
                row,_,state,archive=bots.run_record(db,self._run_id)
                if row.status!='ACTIVE':raise _Halt('RUN_PAUSED')
                head=state['head']['digest'];history=archive['bars'];timeframe=archive['review']['source']['timeframe']
                overlap_count=market_overlap(archive['review']['definition'])
            stage='MARKET_UNAVAILABLE';now=_time(self._clock());step=INTERVALS[timeframe]
            due=int(now.timestamp())//step*step-step
            raw=(self._market.get_candles('BNB/USDT',timeframe=timeframe,since=history[-overlap_count].t,before=due+step,limit=overlap_count+1)
                if history[-1].t<due else None)
            stage='DATA_INVALID'
            candles=advance_market(history,raw,timeframe=timeframe,now=_time(self._clock()),overlap_count=overlap_count).candles
            stage='MARKET_UNAVAILABLE';raw_quote=self._market.get_paper_quote('BNB/USDT')
            quote=reference_quote(raw_quote,observed_at=_time(self._clock()));self._require_active()
            stage='RPC_UNAVAILABLE'
            with self._rpc_factory() as rpc:
                snapshot=read_executor_state(rpc,self._wallet,self._nonce,self._fee,now=_time(self._clock()))
            # The full ownership/runtime observation uses 25 reads. Quote and
            # partial-exit sizing get their own bounded client, pinned to that
            # same numbered block and rechecked hash; no RPC limit is raised.
            with self._rpc_factory() as rpc:
                cap=read_exit_cap(rpc,snapshot,now=_time(self._clock())) if snapshot.owned_wbnb else 0
                args=dict(account_id=self._account,claim=self._claim,candles=[bar.json() for bar in candles],quote=quote,
                    snapshot=snapshot,exit_cap_units=cap,expected_head=head)
                stage='STATE_CHANGED'
                with self._vault.guard_session_generation(self._generation),self._vault.session() as db:
                    self._check_research(db)
                    plan=bots.preview_decision(db,self._run_id,**args,now=_time(self._clock()))
                stage='QUOTE_UNAVAILABLE';execution=None
                if plan.action in {'BUY','SELL'}:
                    options=self._options
                    execution_now=_time(self._clock());extra={}
                    if plan.evidence.get('engine') in DCA_ENGINES:
                        extra['ttl_seconds']=execution_ttl(plan,now=execution_now,policy_expires_at=snapshot.policy_limits.expires_at)
                    execution=read_trade_plan(rpc,self._wallet,self._nonce,self._fee,snapshot=snapshot,buy=plan.action=='BUY',
                        amount=plan.amount_units,slippage_bps=options.slippage_bps,gas=options.gas,gas_price=options.gas_price,
                        max_gas_wei=options.max_gas_wei,now=execution_now,**extra)
            self._require_active();stage='STATE_CHANGED'
            with self._vault.guard_session_generation(self._generation),self._vault.session() as db:
                self._check_research(db)
                decision=bots.record_decision(db,self._run_id,**args,execution=execution,now=_time(self._clock()))
            with self._state_lock:self._last_action=decision['plan']['action']
            if decision['plan']['action']=='PAUSE':raise _Halt('RUN_PAUSED')
            if execution is None:return self.status()
            self._require_active()
            self._prepared_id,self._prepared_plan=str(uuid4()),execution
            prepared=prepare_execution(self._vault,self._connection.session,identifier=self._prepared_id,account_id=self._account,
                grant_id=self._grant,plan=execution,decision_ref=decision['decision_ref'],now=_time(self._clock()))
            if isinstance(prepared,dict):raise _Halt('STATE_CHANGED')
            original_hash=prepared.transaction_hash
            with self._vault.guard_session_generation(self._generation),self._vault.session() as db:
                self._check_research(db)
            with self._sender_factory(execution) as sender:
                result=prepared.broadcast_once(sender.send,now=_time(self._clock()),require_active=self._require_active,
                    permission_guard=lambda:self._permission_lock)
                with self._state_lock:
                    self._pending=(prepared.identifier,original_hash);self._pending_deadline=time.monotonic()+120
                self._prepared_id=None;self._prepared_plan=None
            if result['send_result']=='UNKNOWN':self.stop('OUTCOME_UNKNOWN')
        except _Halt as exc:self.stop(exc.reason)
        except VaultError:self.stop('SESSION_CHANGED')
        except Exception:self.stop(stage)
        finally:
            self._abandon_unsent()
            if self._stop_event.is_set():self._persist_stop()
            self._cycle_lock.release()
        return self.status()
