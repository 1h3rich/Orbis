"""Explicit RAM worker registry. Database rows do not restart trading."""
from datetime import datetime,timezone
from contextlib import contextmanager
from threading import Event,RLock
from uuid import UUID

from sqlalchemy import select

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_bot_worker import DexBotWorker,DexExecutionOptions
from app.live_execution.dex_dca_plan import ENGINE as DCA_ENGINE
from app.marketdata.adapters.mexc import MexcAdapter
from app.portfolio import dex_bot_journal as bots,dex_operator_journal as operator
from app.portfolio.models import RealDexBotRun,RealDexOperatorIntent,RealDexBotDecision

from app.live_execution.dex_versions import DCA_POLICY_OUTCOME,SUCCESSOR_BINDINGS,EXTERNAL_DCA_ENGINES,EXTERNAL_LONG_ENGINES
DCA_ENGINES=(DCA_ENGINE,'orbis.dex.bot.dca.v2',DCA_POLICY_OUTCOME,*EXTERNAL_DCA_ENGINES)


class DexBotSupervisor:
    def __init__(self,vault,connection,*,clock=None,worker_factory=DexBotWorker,worker_kwargs=None,
                 market_factory=MexcAdapter,interval_seconds=5):
        self.vault,self.connection=vault,connection
        self._clock=clock or (lambda:datetime.now(timezone.utc));self._factory=worker_factory
        self._kwargs=dict(worker_kwargs or {});self._lock=RLock()
        self._workers:dict[str,DexBotWorker]={};self._owners:dict[str,str]={};self._starting:dict[str,Event]={}
        self.market_factory=market_factory;self._interval=interval_seconds
        self._reviews:dict[str,tuple[str,Event]]={};self._cancelled:set[tuple[str,str]]=set()
        self.on_operational_idle=None

    def now(self)->datetime:return self._clock()

    def _notify_idle(self)->None:
        if self.on_operational_idle is not None:self.on_operational_idle()

    def operational_status(self)->dict:
        with self._lock:workers=list(self._workers.values())
        states=[worker.status() for worker in workers if worker.permission_mode=='OPERATIONAL']
        active=[state for state in states if state['execution_enabled']]
        return {'active':bool(active),'count':len(active),'until':min((state['expires_at'] for state in active),default=None)}

    def stop_session_workers(self)->None:
        with self._lock:
            for _,cancelled in self._reviews.values():cancelled.set()
            for cancelled in self._starting.values():cancelled.set()
            workers=list(self._workers.values())
        for worker in workers:
            if worker.permission_mode=='SESSION':worker.shutdown()

    @contextmanager
    def preparing(self,identifier:str,*,account_id:str):
        identifier,account_id=str(UUID(identifier)),str(UUID(account_id));cancelled=Event()
        with self._lock:
            if self._reviews or self._starting:raise DexExecutorError('Ya hay una activación en revisión')
            if (identifier,account_id) in self._cancelled:raise DexExecutorError('Esta activación fue cancelada; usa una solicitud nueva')
            self._reviews[identifier]=(account_id,cancelled)
        try:yield cancelled
        finally:
            with self._lock:self._reviews.pop(identifier,None)

    def _owned_worker(self,identifier,account_id):
        identifier,account_id=str(UUID(identifier)),str(UUID(account_id))
        with self._lock:
            if identifier in self._owners and self._owners[identifier]!=account_id:
                raise DexExecutorError('La sesión del bot pertenece a otra cuenta')
            return self._workers.get(identifier)

    def view(self,identifier:str,*,account_id:str)->dict:
        identifier,account_id=str(UUID(identifier)),str(UUID(account_id))
        worker=self._owned_worker(identifier,account_id)
        with self.vault.session() as db:
            row,terms,state,archive=bots.run_record(db,identifier)
            if row.account_id!=account_id:raise DexExecutorError('La sesión del bot pertenece a otra cuenta')
            result=bots._public(row,terms,state)
            from app.portfolio.dex_research import authorization_qualification
            result['research_qualification']=authorization_qualification(db,identifier)
            binding=bots.bindings.binding_record(db,row.binding_id)[1]
            result['root_binding_id']=binding.get('root_binding_id',row.binding_id)
            result['rearm_supported']=True
            result.update(source=archive['review']['source'],source_kind=archive['review']['source_kind'])
            if terms['schema'] in DCA_ENGINES:
                projection=archive['dca_projection']
                result['dca']={'limits':{key:value if key=='activation_window_seconds' else str(value) for key,value in terms['dca']['limits'].items()},
                    'reference':archive['cursor'].reference,'spent_units':str(projection.total_spent_units),
                    'monthly_spent_units':{key:str(value) for key,value in projection.monthly_spent_units.items()},
                    'consumed_keys':sorted(projection.consumed_keys),'gas_spent_wei':str(projection.gas_spent_wei)}
                if archive['last_average'] is not None:result['dca']['last_average']=archive['last_average']
                if archive['last_volume'] is not None:result['dca']['last_volume']=archive['last_volume']
                if binding['schema'] in SUCCESSOR_BINDINGS:result['policy_rollover_id']=binding['rollover_id']
            at=self._clock();options={};position_options={}
            external=db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()>=78
            if db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()>=71:
                from app.portfolio.dex_operator_outcomes import operator_outcome,outcome_witnesses
                options={'include_resolutions':True,'as_of':at}
                if external:options['include_external']=True
            settled=bots._settled(db,row.grant_id,pending_ok=True,run_id=row.id,**options)
            if options:position_options={'resolved_calls':outcome_witnesses(db,settled,as_of=at,include_external=external),'include_external':external}
            position,_=bots._position(db,row,terms,settled,at,**position_options)
            result['position']={key:str(position[key]) for key in ('owned_wbnb','cost_usdt','buy_spent','entries','realized_loss')}
            decisions=select(RealDexBotDecision.payload_digest).where(RealDexBotDecision.run_id==row.id)
            own=(RealDexOperatorIntent.grant_id==row.grant_id,RealDexOperatorIntent.decision_ref.in_(decisions))
            result['intents']=[operator._public(*operator.intent_record(db,item.id)) for item in db.scalars(
                select(RealDexOperatorIntent).where(*own).order_by(RealDexOperatorIntent.created_at.desc()).limit(100))]
            if options:
                for item in result['intents']:
                    fact=operator_outcome(db,item['id'],as_of=at,include_external=external)
                    if fact.external_witness_json is not None:
                        item.update(effective_outcome='EXTERNAL_REVIEWED',external_review=fact.witness(),
                            resolved_gas_fee_wei=str(fact.gas_fee_wei),strategy_executed=False)
                    if fact.resolution_id is not None:
                        from app.portfolio.dex_call_resolution import status as call_status
                        item.update(effective_outcome=fact.outcome,resolution_id=fact.resolution_id,
                            resolved_gas_fee_wei=str(fact.gas_fee_wei),resolution=call_status(db,'OPERATOR',item['id']))
            # This advertises a review path, never authority to configure or
            # restart. Its capture still requires every outcome and fresh state.
            result['policy_renewal_supported']=True
            result['has_execution_history']=db.scalar(select(RealDexOperatorIntent.id).where(
                *own,RealDexOperatorIntent.status!='ABANDONED_UNSENT').limit(1)) is not None or terms['schema'] in bots.CONTINUATION_ENGINES or bool(terms.get('resume'))
            runtime=worker.status() if worker is not None else {'running':False,'execution_enabled':False}
            # A receipt can be recovered with a new vault generation after
            # the original RAM worker stopped. Its old hash/flag cannot keep
            # a confirmed transaction pending in the current read model.
            pending=list(db.scalars(select(RealDexOperatorIntent.id).where(
                *own,
                RealDexOperatorIntent.status.in_(('BROADCAST_ATTEMPTED','NONCE_OBSERVED')))))
            runtime['pending_receipt']=any(not operator_outcome(db,item,as_of=at,include_external=external).settled for item in pending) if options else bool(pending)
            runtime['last_receipt_status']=next(('FINALIZED' if item.get('effective_outcome')=='EXECUTED' else
                item.get('effective_outcome',item['status']) for item in result['intents']
                if item.get('effective_outcome',item['status']) in {'FINALIZED','REVERTED','CANCELLED','EXECUTED','EXTERNAL_REVIEWED'}),None)
        enabled=result['stored_status']=='ACTIVE' and runtime['execution_enabled']
        result.update(worker=runtime,execution_enabled=enabled,automatic_execution=enabled,worker_required=not runtime['running'],
            runtime_status=('RUNNING' if enabled else 'SETTLING' if runtime['running'] and runtime['pending_receipt']
                else 'REARM_REQUIRED' if result['stored_status']=='ACTIVE' else result['stored_status']))
        return result

    def list_runs(self,*,account_id:str)->list[dict]:
        account_id=str(UUID(account_id))
        with self.vault.session() as db:
            identifiers=list(db.scalars(select(RealDexBotRun.id).where(RealDexBotRun.account_id==account_id).order_by(RealDexBotRun.created_at.desc())))
        return [self.view(identifier,account_id=account_id) for identifier in identifiers]

    def require_idle(self)->None:
        """Reject review before connecting a key or committing another epoch.

        A revoked worker may still be leaving a public read. Revocation does
        not mean its thread exited; never wait for that network under locks.
        """
        with self._lock:
            if self._starting or any(worker.alive for worker in self._workers.values()):
                raise DexExecutorError('Espera a que termine la parada del trabajador anterior')

    def launch(self,identifier:str,*,account_id:str,claim:str,wallet_address:str,options:DexExecutionOptions|None=None,cancelled:Event|None=None,
               research_digest:str|None=None)->dict:
        identifier,account_id=str(UUID(identifier)),str(UUID(account_id));cancelled=cancelled if cancelled is not None else Event();worker=None
        with self._lock:
            if identifier in self._workers or self._starting or any(item.alive for item in self._workers.values()):
                raise DexExecutorError('La operadora ya tiene un trabajador; no se repite el arranque')
            self._starting[identifier]=cancelled;self._owners[identifier]=account_id
        try:
            # No registry lock while checking SQL, permissions or starting a
            # thread. STOP first cancels the visible reservation.
            extra={'research_digest':research_digest} if research_digest is not None else {}
            worker=self._factory(self.vault,self.connection,run_id=identifier,account_id=account_id,claim=claim,
                wallet_address=wallet_address,options=options,clock=self._clock,on_exit=self._notify_idle,**self._kwargs,**extra)
            with self._lock:
                if not cancelled.is_set():self._workers[identifier]=worker
            if cancelled.is_set():
                worker.stop();raise DexExecutorError('Arranque cancelado antes de iniciar el trabajador')
            worker.start(interval_seconds=self._interval)
            return self.view(identifier,account_id=account_id)
        except Exception:
            if worker is not None:worker.stop('THREAD_START_FAILED')
            raise
        finally:
            with self._lock:
                self._starting.pop(identifier,None)
                if identifier not in self._workers:self._owners.pop(identifier,None)

    def stop(self,identifier:str,*,account_id:str)->dict:
        identifier,account_id=str(UUID(identifier)),str(UUID(account_id))
        with self._lock:
            review=self._reviews.get(identifier)
            if review is not None and review[0]!=account_id:raise DexExecutorError('La revisión pertenece a otra cuenta')
            worker=self._owned_worker(identifier,account_id)
            if review is not None:review[1].set()
            pending=self._starting.get(identifier)
            if pending is not None:pending.set()
        if worker is not None:worker.stop()
        else:
            with self.vault.session() as db:
                row=db.get(RealDexBotRun,identifier)
                if row is not None:bots.stop_run(db,identifier,account_id=account_id,now=self._clock())
                else:
                    with self._lock:
                        if len(self._cancelled)>=1000:raise DexExecutorError('Límite local de cancelaciones alcanzado')
                        self._cancelled.add((identifier,account_id))
                    return {'id':identifier,'runtime_status':'CANCELLED','execution_enabled':False,'automatic_execution':False}
        try:return self.view(identifier,account_id=account_id)
        except Exception:
            if worker is None:raise
            # A verified RAM worker can acknowledge revocation even when its
            # SQL archive is unreadable. Do not invent a persisted status.
            runtime=worker.status()
            return {'id':identifier,'account_id':account_id,'stored_status':None,'runtime_status':'REVIEW_REQUIRED',
                'execution_enabled':False,'automatic_execution':False,'review_required':True,'worker':runtime}

    def shutdown(self)->None:
        with self._lock:
            for cancelled in self._starting.values():cancelled.set()
            for _,cancelled in self._reviews.values():cancelled.set()
            workers=list(self._workers.values())
        for worker in workers:worker.shutdown()

    def join(self,*,timeout:float=5)->None:
        with self._lock:workers=list(self._workers.values())
        for worker in workers:worker.join(timeout=timeout)
