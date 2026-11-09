"""Trabajos de cierre TESTNET: permiso corto, estado visible y claves efímeras."""

from __future__ import annotations

from dataclasses import dataclass,field
from threading import Event,RLock,Thread
from typing import Callable,Literal,TypedDict
import time

from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
from app.live_execution.grvt_testnet_strategy_close import (
    CloseStatus,StrategyCloseRequest,StrategyCloseProgress,PublicPost,read_strategy_close,
    request_strategy_close,run_strategy_close,_validate_signer,
)
from app.portfolio.vault import RealVault,VaultError
from app.derivatives.paper_store import PaperPerpStore
from app.lab.store import LabStore
from app.live_execution.grvt_testnet_order import GrvtTestnetOrderClient
from app.live_execution.grvt_filled_close import FilledCloseRequest
from app.live_execution.grvt_native_partial_close import NativePartialCloseRequest,SCHEMA as NATIVE_PARTIAL_SCHEMA
from app.live_execution.grvt_native_recovery_close import NativeRecoveryCloseRequest,SCHEMA as NATIVE_RECOVERY_SCHEMA
from app.live_execution.grvt_exit_watch_archive import EventKind,begin_exit_watch,append_exit_watch,exit_watch_history


JobStatus=CloseStatus|Literal['STARTING','STOP_REQUESTED','NEEDS_REVIEW','WATCHING','GAP','INTERRUPTED']
_TERMINAL={'COMPLETE','EXPIRED','ATTEMPT_LIMIT','REVIEW_UNSENT','STOPPED','NEEDS_REVIEW','GAP'}


class WatchState(TypedDict):
    record_id: str|None
    archive_error: bool
    deadline_ns: int
    max_attempts: int
    observations: int
    last_market_event_ns: int|None
    last_reason: str|None
    mode: Literal['WATCH','RECOVERY']


class CloseRunState(TypedDict):
    active: bool
    status: JobStatus
    stop_requested: bool
    updated_at_ns: int
    last_result: StrategyCloseProgress|None
    permission_remaining_seconds: int
    watch: WatchState|None
    permission_revision: list[int]|None


class CloseSnapshot(TypedDict):
    network: Literal['TESTNET']
    authorization: Literal[False]
    request: dict|None
    run: CloseRunState|None
    control_revision: list[int]


class _Stopped(RuntimeError): pass
class _Expired(RuntimeError): pass


@dataclass
class _Job:
    parent: str
    generation: int
    ticket: tuple[int,int]
    active: bool=True
    status: JobStatus='STARTING'
    grant: StrategyCloseRequest|FilledCloseRequest|NativePartialCloseRequest|NativeRecoveryCloseRequest|None=None
    last_result: StrategyCloseProgress|None=None
    updated_at_ns: int=0
    stop: Event=field(default_factory=Event,repr=False)
    thread: Thread|None=field(default=None,repr=False)
    watch: WatchState|None=None
    stop_cause: str='NONE'
    recovery_only: bool=False


class GrvtStrategyCloseService:
    def __init__(self,vault: RealVault,*,client_factory: Callable[[str],GrvtTestnetOrderClient],public_post: PublicPost,
            clock_ns: Callable[[],int]=time.time_ns,waiter: Callable[[Event,float],bool]|None=None,
            stream_factory: Callable|None=None):
        self.vault=vault; self._client_factory=client_factory; self._public=public_post
        self._clock=clock_ns; self._wait=waiter or (lambda event,seconds:event.wait(seconds))
        self._jobs: dict[str,_Job]={}; self._lock=RLock()
        self._epoch=0; self._parent_epochs: dict[str,int]={}
        self._saved_states: dict[tuple[str,int],CloseRunState|None]={}
        if stream_factory is None:
            from app.marketdata.grvt_stream import GrvtTickerStream
            stream_factory=lambda symbol,clock:GrvtTickerStream((symbol,),clock_ns=clock,network='TESTNET')
        self._stream_factory=stream_factory

    def permission_ticket(self,parent: str) -> tuple[int,int]:
        with self._lock: return self._epoch,self._parent_epochs.get(parent,0)

    def require_permission(self,parent: str,ticket: tuple[int,int]) -> None:
        if self.permission_ticket(parent)!=ticket:
            raise GrvtTestnetBracketError('Permiso de cierre retirado')

    def state(self,parent: str) -> CloseRunState|None:
        now=self._clock()
        with self._lock:
            job=self._jobs.get(parent)
            if job is None: return None
            return {'active':job.active,'status':job.status,'stop_requested':job.stop.is_set(),
                'updated_at_ns':job.updated_at_ns,'last_result':job.last_result.copy() if job.last_result else None,
                'permission_remaining_seconds':max(0,((job.grant['deadline_ns'] if job.grant else
                    job.watch['deadline_ns'] if job.watch else now)-now)//1_000_000_000),
                'watch':job.watch.copy() if job.watch else None,'permission_revision':list(job.ticket)}

    def snapshot(self,parent: str) -> CloseSnapshot:
        with self._lock:
            job=self._jobs.get(parent)
            grant=job.grant.copy() if job and job.grant else None
            active=bool(job and job.active)
        # Un observador activo aún puede no haber decidido cerrar. Su estado
        # RAM no espera un cerrojo de bóveda retenido por autenticación/RPC.
        if grant is None and not active: grant=read_strategy_close(self.vault,parent)
        if grant is not None and grant['schema']=='orbis.grvt.testnet.strategy.close.v2':
            grant={key:grant[key] for key in ('schema','parent_client_order_id','created_at_ns','deadline_ns','max_attempts')}|{
                'exit_reason':grant['evidence']['decision']['reason'],'strategy_digest':grant['evidence']['strategy_digest']}
        elif grant is not None and grant['schema'] in (NATIVE_PARTIAL_SCHEMA,NATIVE_RECOVERY_SCHEMA):
            grant={key:grant[key] for key in ('schema','parent_client_order_id','created_at_ns','deadline_ns','max_attempts')}|{
                'exit_reason':grant['evidence']['reason']}
        run=self.state(parent)
        if job is None:
            generation=self.vault.session_generation
            if generation is None: raise VaultError('Bóveda bloqueada')
            key=(parent,generation)
            if key not in self._saved_states:
                history=exit_watch_history(self.vault,parent,limit=1)['items']
                saved=history[0] if history else None
                self._saved_states[key]=None if saved is None else {
                    'active':False,'status':saved['status'],'stop_requested':saved['stop_requested'],
                    'updated_at_ns':saved['last_event_at_ns'],'last_result':None,
                    'permission_remaining_seconds':0,'permission_revision':None,
                    'watch':{'record_id':saved['id'],'archive_error':False,'deadline_ns':saved['deadline_ns'],
                        'max_attempts':saved['max_attempts'],'observations':saved['observations'],
                        'last_market_event_ns':saved['last_market_event_ns'],'last_reason':saved['last_reason'],
                        'mode':saved.get('mode','WATCH')}}
            run=self._saved_states[key]
        return {'network':'TESTNET','authorization':False,'request':grant,'run':run,
            'control_revision':list(self.permission_ticket(parent))}

    def start_watch(self,parent: str,*,max_attempts: int,ttl_seconds: int,api_key: str,private_key: str,
            expected_signer: str,expected_generation: int|None=None,expected_ticket: tuple[int,int]|None=None,
            deadline_ns: int|None=None,recovery_only: bool=False) -> CloseSnapshot:
        from app.live_execution.grvt_bracket_strategy_snapshot import read_bracket_strategy
        from app.live_execution.grvt_testnet_bracket_journal import read_bracket
        _validate_signer(private_key,expected_signer)
        if (type(max_attempts) is not int or not 1<=max_attempts<=3 or type(ttl_seconds) is not int
                or not 30<=ttl_seconds<=86400 or type(recovery_only) is not bool
                or recovery_only and ttl_seconds>300): raise GrvtTestnetBracketError('Límites de seguimiento inválidos')
        generation=expected_generation if expected_generation is not None else self.vault.session_generation
        if generation is None: raise GrvtTestnetBracketError('Bóveda bloqueada')
        with self._lock:
            ticket=expected_ticket if expected_ticket is not None else self.permission_ticket(parent)
            self.require_permission(parent,ticket)
            prior=self._jobs.get(parent)
            if prior and prior.active: raise GrvtTestnetBracketError('Ya hay un trabajo en curso')
            if prior and prior.status=='COMPLETE': return self.snapshot(parent)
            now=self._clock()
            if deadline_ns is not None and (type(deadline_ns) is not int or not now<deadline_ns<=now+ttl_seconds*1_000_000_000):
                raise GrvtTestnetBracketError('Plazo de seguimiento heredado inválido')
            job=_Job(parent,generation,ticket,updated_at_ns=now,recovery_only=recovery_only,watch={
                'record_id':None,'archive_error':False,
                'deadline_ns':deadline_ns if deadline_ns is not None else now+ttl_seconds*1_000_000_000,'max_attempts':max_attempts,
                'observations':0,'last_market_event_ns':None,'last_reason':None,'mode':'RECOVERY' if recovery_only else 'WATCH'})
            self._jobs[parent]=job
            self._saved_states.clear()
        try:
            with self.vault.guard_session_generation(generation):
                self.require_permission(parent,ticket)
                group=read_bracket(self.vault,parent)
                grant=read_strategy_close(self.vault,parent)
                if grant is not None:
                    if grant['schema'] not in ('orbis.grvt.testnet.strategy.close.v2',NATIVE_PARTIAL_SCHEMA,NATIVE_RECOVERY_SCHEMA):
                        raise GrvtTestnetBracketError('El grupo tiene otro origen de cierre')
                    if deadline_ns is not None and deadline_ns!=grant['deadline_ns']:
                        raise GrvtTestnetBracketError('El plazo del cierre ya estaba fijado')
                    with self._lock:
                        job.grant=grant
                        job.watch.update(deadline_ns=grant['deadline_ns'],max_attempts=grant['max_attempts'])
                elif (read_bracket_strategy(self.vault,parent) is None or group['emergency_attempts']
                        or group['reconciliation'] is not None or group['status'] not in ('ACKNOWLEDGED','POST_CLAIMED')
                        or not recovery_only and job.watch['deadline_ns']+30_000_000_000>=group['legs'][1]['expiration_ns']):
                    raise GrvtTestnetBracketError('Seguimiento sin protección nativa suficiente')
                self.require_permission(parent,ticket)
                job.watch['record_id']=begin_exit_watch(self.vault,parent,max_attempts=job.watch['max_attempts'],
                    deadline_ns=job.watch['deadline_ns'],clock_ns=self._clock,mode='RECOVERY' if recovery_only else 'WATCH')
            thread=Thread(target=self._run,args=(job,api_key,private_key,expected_signer),
                name='orbis-grvt-exits-'+parent[-6:],daemon=True)
            with self._lock: job.thread=thread
            thread.start(); return self.snapshot(parent)
        except Exception:
            with self._lock:
                if self._jobs.get(parent) is job: self._jobs.pop(parent)
            raise

    def start(self,paper: PaperPerpStore,lab: LabStore,parent: str,*,action_request_id: str,max_attempts: int,ttl_seconds: int,
            api_key: str,private_key: str,expected_signer: str,expected_generation: int|None=None,
            expected_ticket: tuple[int,int]|None=None) -> CloseSnapshot:
        _validate_signer(private_key,expected_signer)
        generation=expected_generation if expected_generation is not None else self.vault.session_generation
        if generation is None: raise GrvtTestnetBracketError('Bóveda bloqueada')
        with self._lock:
            ticket=expected_ticket if expected_ticket is not None else self.permission_ticket(parent)
            self.require_permission(parent,ticket)
            prior=self._jobs.get(parent)
            if prior is not None and prior.active:
                raise GrvtTestnetBracketError('Ya hay un cierre en curso')
            if (prior and prior.status=='COMPLETE' and prior.grant
                    and prior.grant['schema']=='orbis.grvt.testnet.strategy.close.v1'
                    and prior.grant['close_source']['action_request_id']==action_request_id):
                return self.snapshot(parent)
            job=_Job(parent,generation,ticket,updated_at_ns=self._clock()); self._jobs[parent]=job
        try:
            with self.vault.guard_session_generation(generation):
                self.require_permission(parent,ticket)
                grant=request_strategy_close(paper,lab,self.vault,parent,action_request_id=action_request_id,
                    max_attempts=max_attempts,deadline_ns=self._clock()+ttl_seconds*1_000_000_000,clock_ns=self._clock)
            with self._lock: job.grant=grant
            if self._clock()>grant['deadline_ns']:
                with self._lock: job.active=False; job.status='EXPIRED'
                return self.snapshot(parent)
            thread=Thread(target=self._run,args=(job,api_key,private_key,expected_signer),
                name='orbis-grvt-close-'+parent[-6:],daemon=True)
            with self._lock: job.thread=thread
            thread.start()
            return self.snapshot(parent)
        except Exception:
            with self._lock:
                if self._jobs.get(parent) is job: self._jobs.pop(parent)
            raise

    def _persist(self,job: _Job,kind: EventKind,data: dict,*,opportunistic: bool=False) -> None:
        if not job.watch or not job.watch['record_id']: return
        try:
            if opportunistic:
                with self.vault.try_guard_session_generation(job.generation) as available:
                    if not available:
                        job.watch['archive_error']=True
                        return
                    append_exit_watch(self.vault,job.watch['record_id'],kind,data,clock_ns=self._clock)
            else:
                with self.vault.guard_session_generation(job.generation):
                    append_exit_watch(self.vault,job.watch['record_id'],kind,data,clock_ns=self._clock)
        except Exception:
            job.watch['archive_error']=True
            if not opportunistic: raise

    def stop(self,parent: str,*,cause: str='USER_STOP') -> CloseRunState|None:
        with self._lock:
            self._parent_epochs[parent]=self._parent_epochs.get(parent,0)+1
            job=self._jobs.get(parent)
            if job and job.active:
                if job.stop_cause=='NONE': job.stop_cause=cause
                job.stop.set()
                if job.status not in _TERMINAL: job.status='STOP_REQUESTED'
        if job and job.active: self._persist(job,'STOP_REQUESTED',{'cause':cause},opportunistic=True)
        return self.state(parent)

    def stop_all(self,cause: str='REVOKED') -> None:
        jobs=[]
        with self._lock:
            self._epoch+=1; self._parent_epochs.clear()
            for job in self._jobs.values():
                if job.active:
                    if job.stop_cause=='NONE': job.stop_cause=cause
                    job.stop.set()
                    if job.status not in _TERMINAL: job.status='STOP_REQUESTED'
                    jobs.append(job)
        for job in jobs: self._persist(job,'STOP_REQUESTED',{'cause':cause},opportunistic=True)

    def _run(self,job: _Job,api_key: str,private_key: str,expected_signer: str) -> None:
        from app.live_execution.grvt_exit_watcher import watch_filled_exit,ExitWatchGap
        client=None
        def checked() -> int:
            if job.stop.is_set(): raise _Stopped()
            if self.permission_ticket(job.parent)!=job.ticket: raise _Stopped()
            if self.vault.session_generation!=job.generation: raise _Stopped()
            at=self._clock()
            deadline=(job.grant['deadline_ns'] if job.grant else job.watch['deadline_ns'] if job.watch else None)
            if deadline is None or at>deadline: raise _Expired()
            return at
        def progress(value: StrategyCloseProgress,terminal: bool=False) -> None:
            if value['status'] in _TERMINAL:
                self._persist(job,'TERMINAL',{'status':value['status'],'cause':job.stop_cause})
            else: self._persist(job,'STATUS',{'status':value['status']})
            with self._lock:
                job.last_result=value.copy(); job.updated_at_ns=self._clock()
                job.status='STOP_REQUESTED' if job.stop.is_set() and not terminal and value['status'] not in _TERMINAL else value['status']
        def fresh_submission() -> GrvtTestnetOrderClient:
            checked()
            return self._client_factory(api_key)
        def observed(observation: dict):
            checked()
            self._persist(job,'OBSERVATION',observation)
            with self._lock:
                job.watch['observations']+=1; job.watch['last_market_event_ns']=observation['event_time_ns']
                job.watch['last_reason']=observation['reason']; job.status='WATCHING'; job.updated_at_ns=self._clock()
        try:
            with self.vault.guard_session_generation(job.generation):
                checked(); client=self._client_factory(api_key)
            if job.watch is not None and job.grant is None:
                from app.live_execution.grvt_native_exit import NativeGroupExit,NativePartialExit,NativeRecoveryExit,settle_native_exit
                if job.recovery_only:
                    from app.live_execution.grvt_native_exit import inspect_native_exit
                    outcome=inspect_native_exit(self.vault,client,job.parent,clock_ns=checked)
                    if outcome is None: raise GrvtTestnetBracketError('El grupo conserva sus protecciones; no requiere recuperación')
                else:
                    outcome=watch_filled_exit(self.vault,client,job.parent,max_attempts=job.watch['max_attempts'],
                        deadline_ns=job.watch['deadline_ns'],stream_factory=self._stream_factory,clock_ns=checked,
                        public_post=self._public,on_observation=observed)
                if isinstance(outcome,NativePartialExit):
                    from app.live_execution.grvt_native_partial_close import request_native_partial_close
                    outcome=request_native_partial_close(self.vault,client,job.parent,max_attempts=job.watch['max_attempts'],
                        deadline_ns=min(job.watch['deadline_ns'],checked()+300_000_000_000),clock_ns=checked)
                elif isinstance(outcome,NativeRecoveryExit):
                    from app.live_execution.grvt_native_recovery_close import request_native_recovery_close
                    outcome=request_native_recovery_close(self.vault,client,job.parent,max_attempts=job.watch['max_attempts'],
                        deadline_ns=min(job.watch['deadline_ns'],checked()+300_000_000_000),clock_ns=checked)
                if isinstance(outcome,NativeGroupExit):
                    result=settle_native_exit(self.vault,client,job.parent,clock_ns=checked,
                        deadline_ns=job.watch['deadline_ns'],wait=lambda seconds:self._wait(job.stop,seconds),on_progress=progress)
                    progress(result,terminal=True)
                    return
                with self._lock: job.grant=outcome
            result=run_strategy_close(self.vault,client,job.parent,private_key=private_key,
                expected_signer=expected_signer,public_post=self._public,clock_ns=checked,stop_event=job.stop,
                wait=lambda seconds:self._wait(job.stop,seconds),on_progress=progress,
                submission_client_factory=fresh_submission)
            progress(result,terminal=True)
        except (_Stopped,VaultError):
            with self._lock: job.status='STOPPED'
        except _Expired:
            with self._lock: job.status='EXPIRED'
        except ExitWatchGap:
            with self._lock: job.status='GAP'
        except Exception:
            # Ninguna respuesta/traceback remota ni credencial llega al estado.
            revoked=job.stop.is_set() or self.vault.session_generation!=job.generation
            with self._lock: job.status='STOPPED' if revoked else 'NEEDS_REVIEW'
        finally:
            self._persist(job,'TERMINAL',{'status':job.status,'cause':job.stop_cause},opportunistic=True)
            with self._lock:
                job.active=False; job.thread=None; job.updated_at_ns=self._clock()
            del api_key,private_key,expected_signer,client
