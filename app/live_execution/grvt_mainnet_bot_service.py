"""Coordinador MAINNET revocable: señales futuras, fills, salidas y recibos.

Sólo los trabajos RAM tienen autoridad. Un registro cifrado nunca arranca una
hebra ni restaura credenciales. STOP revoca antes de esperar al almacenamiento.
"""
from copy import deepcopy
from dataclasses import dataclass,field,asdict
from datetime import datetime,timezone
from decimal import Decimal,localcontext
import secrets,time
from threading import Event,RLock,Thread
from eth_account import Account
from sqlalchemy import select,func

from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError,unsigned_mainnet_bracket
from app.live_execution.grvt_mainnet_bot_entry import (MainnetBotPolicy,begin_bot_grant,read_bot_grant,
    submit_bot_entry_once,settle_bot_selection,finish_bot_grant,_entry_draft)
from app.live_execution.grvt_bot_source import bot_source,next_bot_open
from app.live_execution.grvt_mainnet_journal import read_mainnet_bracket,_control
from app.live_execution.grvt_mainnet_transport import GrvtMainnetClient,fetch_mainnet_instrument,public_post as _public_post,TICKER_URL,_result
from app.live_execution.grvt_mainnet_observation import observe_mainnet_bracket
from app.live_execution.grvt_mainnet_finance import audit_mainnet_bracket,reconcile_mainnet_bracket
from app.live_execution.grvt_mainnet_bot_exit import review_mainnet_bot_exit,save_bot_exit
from app.live_execution.grvt_mainnet_recovery import begin_mainnet_recovery
from app.live_execution.grvt_mainnet_recovery_runner import run_mainnet_recovery
from app.live_execution.grvt_mainnet_stop_origin import release_bot_stop
from app.portfolio.models import GrvtMainnetBotRun

class _Stopped(RuntimeError):pass
class _Expired(RuntimeError):pass

@dataclass(repr=False)
class _Job:
    saved:dict
    generation:int
    ticket:tuple[int,int]
    api_key:str=field(repr=False)
    key:bytes|None=field(repr=False)
    signer:str=field(repr=False)
    account_ref:str
    keep_operating:bool
    session_checked:object=field(repr=False)
    monotonic_deadline:float
    last_ns:int
    status:str='WAITING_SIGNAL'
    active:bool=True
    parent:str|None=None
    archive_error:bool=False
    stop:Event=field(default_factory=Event,repr=False)
    market_cache:dict=field(default_factory=dict,repr=False)
    last_decision:dict|None=None
    group_cache:dict|None=field(default=None,repr=False)
    entry_fills:dict|None=field(default=None,repr=False)
    executions_cache:dict|None=field(default=None,repr=False)
    perp_worker:object|None=field(default=None,repr=False)
    position_checked_ns:int=0
    source_fault_recorded:bool=False


class GrvtMainnetBotService:
    def __init__(self,vault,*,client_factory=None,public_post=None,clock_ns=None,waiter=None,poll_seconds=2,on_inactive=None,
                 perp_source_factory=None,perp_worker_waiter=None):
        self.vault=vault;self.factory=client_factory or GrvtMainnetClient
        self.public=public_post or _public_post;self.clock=clock_ns or time.time_ns
        self.waiter=waiter or (lambda stop,seconds:stop.wait(seconds));self.poll=poll_seconds
        self.on_inactive=on_inactive or (lambda:None)
        self.perp_source_factory=perp_source_factory;self.perp_worker_waiter=perp_worker_waiter
        self._lock=RLock();self._epoch=0;self._revisions={};self._jobs={};self._limit_jobs={};self._starting=set();self._salt=secrets.token_hex(32)

    def ticket(self,sub):
        with self._lock:return self._epoch,self._revisions.get(sub,0)

    def _require_ticket(self,sub,ticket):
        if self.ticket(sub)!=ticket:raise _Stopped()

    def _checked(self,job):
        if job.stop.is_set():raise _Stopped()
        if self.vault.session_generation!=job.generation or job.key is None or not job.api_key:raise _Stopped()
        if not job.keep_operating:job.session_checked()
        # Signal collection and position management share one RAM permission.
        # Vault/session callbacks stay outside this lock: archive writers may
        # already hold the vault lock when they recheck permission.
        with self._lock:
            if job.stop.is_set() or job.key is None or not job.api_key:raise _Stopped()
            self._require_ticket(job.saved['policy']['sub_account_id'],job.ticket)
            at=self.clock()
            if type(at) is not int or at<job.last_ns:raise GrvtMainnetError('Reloj del bot MAINNET regresado')
            job.last_ns=at
            if at>=job.saved['deadline_ns'] or time.monotonic()>=job.monotonic_deadline:raise _Expired()
            return at

    def _client(self,job):
        self._checked(job);client=self.factory(job.api_key);self._checked(job)
        if (not isinstance(client,GrvtMainnetClient) or client.network!='MAINNET'
                or client.sub_account_id!=job.saved['policy']['sub_account_id'] or client.identity_ref(self._salt)!=job.account_ref):
            raise GrvtMainnetError('Identidad del bot MAINNET cambiada')
        return client

    def _metrics(self,saved):
        with localcontext() as context:
            context.prec=100
            settled=[item['settlement'] for item in saved['selections'] if item['settlement'] is not None]
            outcomes=saved.get('financial_outcomes',settled)
            return {'entry_count':len(saved['selections']),'completed_entries':len(outcomes),
                'entry_budget_reserved':format(Decimal(saved['policy']['entry_cap'])*len(saved['selections']),'f'),
                'financial_complete':bool(saved['selections']) and len(outcomes)==len(saved['selections']),
                'external_closures':sum(item.get('bot_recorded') is False for item in outcomes),
                'net_realized_pnl_usdt':format(sum((Decimal(item['net_realized_pnl_usdt']) for item in outcomes),Decimal(0)),'f')}

    def cached_snapshot(self,run_id):
        with self._lock:
            job=self._jobs.get(run_id)
            return {**deepcopy(job.saved),**self._view(job)} if job else None

    def cached_group(self,parent):
        with self._lock:
            job=next((job for job in self._jobs.values() if job.parent==parent and job.group_cache is not None),None)
            if job is None:return None
            value=deepcopy(job.group_cache);value['job']={'active':job.active,'status':job.status};return value

    def _cache_group(self,job,value):
        from app.live_execution.grvt_mainnet_view import mainnet_group_view
        from app.live_execution.grvt_mainnet_executions import executions_view
        presented=mainnet_group_view(value,{'active':job.active,'status':job.status})
        entry=job.entry_fills if job.entry_fills and job.entry_fills['parent_client_order_id']==value['parent_client_order_id'] else None
        executions=executions_view(value,timeframe=job.saved['source']['timeframe'],entry=entry)
        with self._lock:job.group_cache=presented;job.executions_cache=executions

    def cached_executions(self,parent):
        with self._lock:
            job=next((job for job in self._jobs.values() if job.parent==parent and job.executions_cache is not None),None)
            return deepcopy(job.executions_cache) if job is not None else None

    def _view(self,job,saved=None):
        return {**self._metrics(job.saved if saved is None else saved),'last_decision':deepcopy(job.last_decision),'active':job.active,'status':job.status,'parent_client_order_id':job.parent,'archive_error':job.archive_error,
            'permission_remaining_seconds':max(0,(job.saved['deadline_ns']-self.clock())//1_000_000_000) if job.active else 0,
            'control_revision':list(self.ticket(job.saved['policy']['sub_account_id']))}

    def snapshot(self,run_id):
        with self._lock:
            job=self._jobs.get(run_id)
            if job is not None and job.active:return {**deepcopy(job.saved),**self._view(job)}
        saved=read_bot_grant(self.vault,run_id)
        if job is not None:
            with self._lock:job.saved=saved;return {**deepcopy(saved),**self._view(job)}
        return {**saved,**self._metrics(saved),'active':False,'status':saved['terminal']['status'] if saved['terminal'] else 'INTERRUPTED',
            'parent_client_order_id':saved['selections'][-1]['parent_client_order_id'] if saved['selections'] else None,
            'permission_remaining_seconds':0,'archive_error':False,'control_revision':list(self.ticket(saved['policy']['sub_account_id']))}

    def operational_status(self):
        with self._lock:
            jobs=[job for job in self._jobs.values() if job.active and job.keep_operating and not job.stop.is_set()
                and self.clock()<job.saved['deadline_ns'] and time.monotonic()<job.monotonic_deadline]
            until=min((job.saved['deadline_ns'] for job in jobs),default=None)
        return {'active':bool(jobs),'count':len(jobs),'until':datetime.fromtimestamp(until/1e9,timezone.utc).isoformat() if until else None}

    def active_sub(self,sub):
        with self._lock:
            limit=self._limit_jobs.get(sub)
            return (sub in self._starting or limit is not None and limit.active
                    or any(job.active and job.saved['policy']['sub_account_id']==sub for job in self._jobs.values()))

    def limit_status(self,sub):
        with self._lock:
            job=self._limit_jobs.get(sub)
            return deepcopy(job.view()) if job is not None else None

    def history(self,sub,*,generation,offset=0):
        with self.vault.try_guard_session_generation(generation) as available:
            if not available:
                with self._lock:items=[{**deepcopy(job.saved),**self._view(job)} for job in self._jobs.values() if job.saved['policy']['sub_account_id']==sub]
                return {'network':'MAINNET','items':items,'history_pending':True,'has_more':False,'control_revision':list(self.ticket(sub))}
            with self.vault.session() as db:
                rows=list(db.scalars(select(GrvtMainnetBotRun.id).where(GrvtMainnetBotRun.sub_account_id==sub)
                    .order_by(func.json_extract(GrvtMainnetBotRun.payload_json,'$.started_at_ns').desc()).offset(offset).limit(21)))
            items=[]
            for identifier in rows[:20]:
                saved=read_bot_grant(self.vault,identifier)
                with self._lock:
                    job=self._jobs.get(identifier)
                    if job is not None and not job.active:job.saved=saved
                    live=self._view(job,saved) if job else None
                items.append({**saved,**live} if live else {**saved,**self._metrics(saved),'active':False,'status':saved['terminal']['status'] if saved['terminal'] else 'INTERRUPTED',
                    'archive_error':False,'permission_remaining_seconds':0,'parent_client_order_id':saved['selections'][-1]['parent_client_order_id'] if saved['selections'] else None})
            return {'network':'MAINNET','items':items,'history_pending':False,'has_more':len(rows)>20,'control_revision':list(self.ticket(sub))}

    def sub_status(self,sub):
        with self._lock:
            job=next((job for job in self._jobs.values() if job.active and job.saved['policy']['sub_account_id']==sub),None)
            limit=self.limit_status(sub)
            return self._view(job) if job else limit if limit and limit['active'] else None

    def start(self,paper,lab,session_id,policy:MainnetBotPolicy,*,api_key,private_key,expected_signer,expected_ticket,
            keep_operating=False,expected_generation=None,session_checked=lambda:None,requested_run_id=None,research_check=None):
        generation=self.vault.session_generation if expected_generation is None else expected_generation
        if (generation is None or type(keep_operating) is not bool or type(private_key) is not bytes or len(private_key)!=32
                or Account.from_key(private_key).address.lower()!=expected_signer.lower()):raise GrvtMainnetError('Firma o autorización del bot MAINNET inválida')
        sub=policy.sub_account_id;run_id=None
        with self._lock:
            self._require_ticket(sub,expected_ticket)
            if self.active_sub(sub):raise GrvtMainnetError('Bot MAINNET ya activo o preparando')
            self._starting.add(sub)
        def checked():
            self._require_ticket(sub,expected_ticket)
            if self.vault.session_generation!=generation:raise _Stopped()
            session_checked();return self.clock()
        def public(url,body):checked();reply=self.public(url,body);checked();return reply
        try:
            source=bot_source(paper,lab,session_id);checked()
            if research_check is not None:
                if research_check.source!=source:raise GrvtMainnetError('La investigación pertenece a otro origen')
                research_check.assert_current(checked())
            instrument=fetch_mainnet_instrument(source['instrument'],http_post=public)
            quote=_result(public(TICKER_URL,{'instrument':source['instrument']}))['result']
            draft=_entry_draft(policy,source,instrument,quote,checked())
            unsigned_mainnet_bracket(draft,instrument,parent_client_order_id=str(2**63+555),nonces=(1,2,3),issued_at_ns=checked())
            client=self.factory(api_key);checked()
            if not isinstance(client,GrvtMainnetClient) or client.network!='MAINNET' or client.sub_account_id!=sub:raise GrvtMainnetError('Subcuenta del bot MAINNET distinta')
            account_ref=client.identity_ref(self._salt)
            with self.vault.guard_session_generation(generation):
                checked()
                if research_check is not None:research_check.assert_current(checked())
                saved=begin_bot_grant(self.vault,paper,lab,session_id,policy,now_ns=checked(),keep_operating=keep_operating,run_id=requested_run_id,
                    research_binding=research_check.binding.exported() if research_check is not None else None)
            run_id=saved['id'];checked()
            if saved['source']!=source:raise GrvtMainnetError('Origen del bot cambió durante la autorización')
            with self._lock:
                self._require_ticket(sub,expected_ticket);self._revisions[sub]=self._revisions.get(sub,0)+1
                job=_Job(saved,generation,self.ticket(sub),api_key,private_key,expected_signer,account_ref,keep_operating,session_checked,
                    time.monotonic()+(saved['deadline_ns']-saved['started_at_ns'])/1e9,saved['started_at_ns'])
                self._jobs[run_id]=job
            Thread(target=self._run,args=(job,paper,lab),name='orbis-grvt-mainnet-'+run_id[-6:],daemon=True).start()
            return self.snapshot(run_id)
        except Exception:
            if run_id is not None:
                try:finish_bot_grant(self.vault,run_id,status='STOPPED',now_ns=max(saved['started_at_ns'],self.clock()))
                except Exception:pass
            raise GrvtMainnetError('No se pudo activar el bot MAINNET; revisa conexión, límites y origen') from None
        finally:
            with self._lock:self._starting.discard(sub)
            del private_key

    def stop_subaccount(self,sub):
        with self._lock:
            self._revisions[sub]=self._revisions.get(sub,0)+1
            if sub in self._limit_jobs:self._limit_jobs[sub].revoke()
            workers=[]
            for job in self._jobs.values():
                if job.saved['policy']['sub_account_id']==sub:
                    job.stop.set()
                    if job.perp_worker is not None:workers.append(job.perp_worker)
        for worker in workers:worker.stop()

    def stop_all(self):
        with self._lock:
            self._epoch+=1
            for job in self._limit_jobs.values():job.revoke()
            for job in self._jobs.values():job.stop.set()
            workers=[job.perp_worker for job in self._jobs.values() if job.perp_worker is not None]
        for worker in workers:worker.stop()

    def stop_session_jobs(self):
        with self._lock:
            for job in self._limit_jobs.values():job.revoke()
            workers=[]
            for job in self._jobs.values():
                if not job.keep_operating:
                    job.stop.set()
                    if job.perp_worker is not None:workers.append(job.perp_worker)
        for worker in workers:worker.stop()

    def start_perp(self,origin,terms,policy,**authorization):
        from app.live_execution.grvt_perp_coordinator import GrvtPerpCoordinator
        authorization.setdefault('source_factory',self.perp_source_factory)
        authorization.setdefault('worker_waiter',self.perp_worker_waiter)
        return GrvtPerpCoordinator(self).start(origin,terms,policy,**authorization)

    def stop(self,run_id):
        with self._lock:job=self._jobs.get(run_id)
        if job is not None:self.stop_subaccount(job.saved['policy']['sub_account_id'])
        else:finish_bot_grant(self.vault,run_id,status='STOPPED',now_ns=self.clock())
        return self.snapshot(run_id)

    def retire_interrupted(self,sub):
        """Parada expresa tras reapertura: libera sólo concesiones sin hebra viva."""
        with self.vault.session() as db:
            identifiers=list(db.scalars(select(GrvtMainnetBotRun.id).where(GrvtMainnetBotRun.active_slot==sub)))
        for identifier in identifiers:
            with self._lock:job=self._jobs.get(identifier);active=bool(job and job.active)
            if active:continue
            saved=finish_bot_grant(self.vault,identifier,status='STOPPED',now_ns=self.clock())
            if job is not None:
                with self._lock:job.saved=saved;job.archive_error=False;job.status='STOPPED'

    def _status(self,job,status):
        with self._lock:job.status=status

    def _refresh(self,job):
        saved=read_bot_grant(self.vault,job.saved['id'])
        with self._lock:job.saved=saved
        return saved

    def _close(self,job,parent):
        at=self._checked(job);saved=read_mainnet_bracket(self.vault,parent)
        if saved['recovery'] is None:
            seconds=min(job.saved['policy']['recovery_seconds'],(job.saved['deadline_ns']-at)//1_000_000_000)
            if seconds<30:raise _Expired()
            begin_mainnet_recovery(self.vault,parent,ttl_seconds=seconds,max_attempts=job.saved['policy']['max_attempts'],
                confirmation='RECUPERAR_POSICION_GRVT_MAINNET',now_ns=at)
        status=run_mainnet_recovery(self.vault,parent,checked=lambda:self._checked(job),factory=lambda:self._client(job),key=job.key,
            signer=job.signer,public=self.public,clock=self.clock,waiter=self.waiter,stop=job.stop,on_status=lambda status:self._status(job,status))
        self._checked(job)
        if status!='COMPLETE':raise GrvtMainnetError('Cierre MAINNET pendiente de revisión')
        return read_mainnet_bracket(self.vault,parent)['reconciliation']

    def _manage(self,job,parent):
        while True:
            try:
                value=read_mainnet_bracket(self.vault,parent);self._cache_group(job,value);receipt=value['reconciliation']
                if receipt is not None:return receipt
                self._checked(job);observed=observe_mainnet_bracket(self.vault,parent,client_factory=lambda:self._client(job),
                    require_permission=lambda:self._checked(job),clock_ns=self.clock);state=observed['state']
                terminal=all(order['status'] in ('FILLED','CANCELLED','REJECTED') for order in state['orders'])
                if terminal and not state['needs_review'] and Decimal(state['residual_size_from_orders'])==0:
                    self._status(job,'VERIFYING');audit_mainnet_bracket(self.vault,parent,client_factory=lambda:self._client(job),
                        require_permission=lambda:self._checked(job),clock_ns=self.clock)
                    try:return reconcile_mainnet_bracket(self.vault,parent,now_ns=self._checked(job))
                    except GrvtMainnetError:self.waiter(job.stop,5.1);continue
                if state['needs_review']:return self._close(job,parent)
                if state['entry_fully_filled']:
                    proof=review_mainnet_bot_exit(self.vault,job.saved['id'],parent,client_factory=lambda:self._client(job),
                        public_post=self.public,require_permission=lambda:self._checked(job),clock_ns=self.clock,market_cache=job.market_cache)
                    if job.entry_fills is None or job.entry_fills['parent_client_order_id']!=parent:
                        from app.live_execution.grvt_mainnet_entry_fills import save_entry_fills
                        snapshot=save_entry_fills(self.vault,proof,require_permission=lambda:self._checked(job))
                        with self._lock:job.entry_fills=snapshot
                        self._cache_group(job,read_mainnet_bracket(self.vault,parent))
                    with self._lock:job.last_decision=proof['decision']
                    if proof['decision']['action']=='CLOSE':
                        save_bot_exit(self.vault,proof,require_permission=lambda:self._checked(job));return self._close(job,parent)
                    self._status(job,'WATCHING')
                else:self._status(job,'RECOVERING_ENTRY')
            except Exception:
                receipt=read_mainnet_bracket(self.vault,parent)['reconciliation']
                if receipt is not None:return receipt
                raise
            self.waiter(job.stop,self.poll)

    def _run(self,job,paper,lab):
        final='REVIEW'
        try:
            while True:
                at=self._checked(job);saved=self._refresh(job);policy=saved['policy']
                if at>=saved['entry_deadline_ns']:final='EXPIRED';break
                self._status(job,'WAITING_SIGNAL')
                if bot_source(paper,lab,saved['source']['session_id'])!=saved['source']:raise GrvtMainnetError('La sesión PAPER se detuvo o cambió')
                armed=max(saved['started_at_ns'],saved['selections'][-1]['settled_at_ns'] or 0) if saved['selections'] else saved['started_at_ns']
                if next_bot_open(paper,saved['source'],armed_at_ns=armed,now_ns=at) is None:self.waiter(job.stop,self.poll);continue
                self._status(job,'PREPARING_ENTRY')
                try:
                    result=submit_bot_entry_once(self.vault,paper,lab,saved['id'],client_factory=lambda:self._client(job),private_key=job.key,
                        expected_signer=job.signer,require_permission=lambda:self._checked(job),public_post=self.public,clock_ns=self.clock)
                    parent=result['parent_client_order_id']
                except GrvtMainnetError:
                    current=self._refresh(job)
                    if len(current['selections'])!=len(saved['selections'])+1:raise
                    parent=current['selections'][-1]['parent_client_order_id'];read_mainnet_bracket(self.vault,parent)
                with self._lock:job.parent=parent
                self._refresh(job);self._manage(job,parent);self._checked(job)
                self._cache_group(job,read_mainnet_bracket(self.vault,parent))
                settle_bot_selection(self.vault,saved['id'],now_ns=self.clock());saved=self._refresh(job)
                with localcontext() as context:
                    context.prec=100
                    net=sum((Decimal(item['settlement']['net_realized_pnl_usdt']) for item in saved['selections']),Decimal(0))
                    if len(saved['selections'])>=policy['max_entries']:final='ENTRY_LIMIT';break
                    if Decimal(policy['entry_cap'])*(len(saved['selections'])+1)>Decimal(policy['total_entry_cap']):final='BUDGET_LIMIT';break
                    if -net>=Decimal(policy['loss_cap']):final='LOSS_LIMIT';break
                with self.vault.session() as db:control=_control(db,policy['sub_account_id']);stopped=control.stopped;revision=control.revision
                if stopped:release_bot_stop(self.vault,saved['id'],parent,expected_revision=revision,
                    require_permission=lambda:self._checked(job),now_ns=self.clock())
                with self._lock:job.parent=None
        except _Stopped:final='STOPPED'
        except _Expired:final='EXPIRED'
        except Exception:final='STOPPED' if job.stop.is_set() else 'REVIEW'
        finally:
            self._status(job,final)
            try:
                with self.vault.guard_session_generation(job.generation):
                    finish_bot_grant(self.vault,job.saved['id'],status=final,now_ns=max(job.last_ns,self.clock()));self._refresh(job)
            except Exception:
                with self._lock:job.archive_error=True
            with self._lock:job.active=False;job.api_key='';job.key=None;job.signer=''
            self.on_inactive()
