"""Explicit LAB → observed PAPER orchestration, cancellation and lifecycle.

The app starts this coordinator only while holding its PAPER runtime lease.
Preparation replays LAB; activation collects public inputs outside locks. The
coordinator never reconnects stored sessions, submits REAL orders or holds keys.
"""
from __future__ import annotations

from dataclasses import dataclass,field
from decimal import Decimal, localcontext
import json
from threading import Condition,Event,RLock
import time
from typing import Callable

from app.derivatives.perp_observed_archive import ObservedArchive,_AccessStamp,identifier
from app.derivatives.perp_observed_origin import VerifiedObservedOrigin,verified_observed_origin
from app.derivatives.perp_observed_source import GrvtObservedSource
from app.derivatives.perp_observed_types import ObservedError
from app.derivatives.perp_observed_worker import ObservedSessionWorker
from app.lab.store import LabStore


@dataclass
class _Preparation:
    validation_id: str
    expires: float
    phase: str='PREPARING'
    origin: VerifiedObservedOrigin|None=field(default=None,repr=False)
    paper_stamp: _AccessStamp|None=field(default=None,repr=False)
    error: str|None=None


@dataclass
class _Activation:
    preparation_id: str|None
    validation_id: str|None
    cancelled: Event=field(default_factory=Event,repr=False)
    phase: str='ACTIVATING'
    session_id: str|None=None
    error: str|None=None
    in_progress: bool=True


class ObservedPaperService:
    def __init__(self,lab: LabStore,archive: ObservedArchive,*,source_factory: Callable|None=None,
                 worker_factory: Callable=ObservedSessionWorker,origin_factory: Callable=verified_observed_origin,
                 clock=time.monotonic,max_preparations: int=32,max_requests: int=256,max_workers: int=8,
                 preparation_seconds: float=300):
        if (any(type(n) is not int or n<1 for n in (max_preparations,max_requests,max_workers))
                or type(preparation_seconds) not in (int,float) or not 0<preparation_seconds<=900):
            raise ObservedError('Límites del coordinador PAPER no válidos')
        self.lab=lab;self.archive=archive;self.clock=clock
        self.source_factory=source_factory or (lambda:GrvtObservedSource(clock_ns=archive.clock_ns))
        self.worker_factory=worker_factory;self.origin_factory=origin_factory
        self.max_preparations=max_preparations;self.max_requests=max_requests;self.max_workers=max_workers
        self.preparation_seconds=preparation_seconds
        self._condition=Condition(RLock());self._accepting=False;self._draining=False;self._inflight=0
        self._preparations: dict[str,_Preparation]={};self._requests: dict[str,_Activation]={}
        self._workers: dict[str,ObservedSessionWorker]={}

    def start(self) -> None:
        with self._condition:
            if self._draining or self._inflight or any(worker.snapshot()['running'] for worker in self._workers.values()):
                raise ObservedError('El coordinador anterior todavía está drenando')
            self._accepting=True

    def request_stop(self) -> None:
        with self._condition:
            self._accepting=False
            self._draining=True
            for request in self._requests.values():request.cancelled.set()
            for worker in self._workers.values():worker.request_stop()
            self._condition.notify_all()

    def access_changed(self) -> None:
        """Unlock/lock never carries old preparation or observer authority over."""
        with self._condition:
            self._preparations.clear()
            for request in self._requests.values():
                if request.in_progress:request.cancelled.set()
            workers=tuple(self._workers.items())
            for _,worker in workers:worker.request_stop()
        for id,_ in workers:self.archive.detach(id)

    def snapshot(self) -> dict:
        with self._condition:
            return {'accepting':self._accepting,'draining':self._draining,'inflight':self._inflight,
                'running_workers':sum(bool(w.snapshot()['running']) for w in self._workers.values()),
                'max_workers':self.max_workers,'environment':'PAPER','execution_authority':'NONE'}

    def stop(self) -> None:
        self.request_stop()
        with self._condition:
            while self._inflight:self._condition.wait()
            workers=tuple(self._workers.values())
        # The lease owner must wait for actual completion, not a join timeout.
        for worker in workers:worker.stop()
        with self._condition:
            self._preparations.clear();self._workers.clear();self._draining=False

    def _available(self) -> None:
        if not self._accepting:raise ObservedError('El observador PAPER no acepta nuevos arranques')

    def _prune(self) -> None:
        now=self.clock()
        self._preparations={id:p for id,p in self._preparations.items() if p.phase=='PREPARING' or now<p.expires}
        self._workers={id:w for id,w in self._workers.items() if w.snapshot()['running']}
        # Published requests are permanently idempotent in SQL. Cancellation
        # tombstones remain until this process ends, including cancel-before-POST.
        self._requests={id:r for id,r in self._requests.items()
            if r.phase!='ACTIVATED' or r.in_progress or r.session_id in self._workers}

    def _preparation_view(self,id: str,p: _Preparation) -> dict:
        reference=p.origin.reference() if p.origin is not None else None
        definition=json.loads(p.origin.definition_json) if p.origin is not None else None
        view = {'preparation_id':id,'validation_id':p.validation_id,'phase':p.phase,
            'remaining_seconds':max(0,int(p.expires-self.clock())),'error':p.error,
            'reference':reference.model_dump(mode='json',by_alias=True) if reference else None,
            'initial_capital':definition['initial_capital'] if definition else None,
            'direction':definition['direction'] if definition else None,
            'environment':'PAPER','execution_authority':'NONE','session_created':False}
        if p.origin is not None and p.origin.optimization_origin_json is not None:
            view['optimization_origin'] = json.loads(p.origin.optimization_origin_json)
            with localcontext() as context:
                context.prec = 80
                view['exit_parameters_pct'] = {key:format(Decimal(definition[key]) * 100, 'f')
                                               for key in ('stop_loss', 'take_profit')}
        return view

    def prepare(self,validation_id: str,*,preparation_id: str) -> dict:
        validation_id=identifier(validation_id);preparation_id=identifier(preparation_id)
        with self._condition:
            self._available();self._prune()
            existing=self._preparations.get(preparation_id)
            if existing is not None:
                if existing.validation_id!=validation_id:raise ObservedError('Preparación reutilizada con otro origen')
                return self._preparation_view(preparation_id,existing)
            if len(self._preparations)>=self.max_preparations:raise ObservedError('Demasiadas preparaciones pendientes')
            p=_Preparation(validation_id,self.clock()+self.preparation_seconds)
            self._preparations[preparation_id]=p;self._inflight+=1
        try:
            stamp=self.archive._stamp()
            origin=self.origin_factory(self.lab,validation_id)
            self.archive._stamp(stamp);origin.check_session()
            with self._condition:
                self._available()
                if self.clock()>=p.expires or self._preparations.get(preparation_id) is not p:
                    raise ObservedError('La preparación PAPER ha caducado o fue cancelada')
                p.origin=origin;p.paper_stamp=stamp;p.phase='READY'
                return self._preparation_view(preparation_id,p)
        except Exception as exc:
            with self._condition:p.phase='FAILED';p.error=type(exc).__name__;p.origin=None;p.paper_stamp=None
            raise
        finally:
            with self._condition:self._inflight-=1;self._condition.notify_all()

    def _request_view(self,request_id: str,r: _Activation) -> dict:
        worker=self._workers.get(r.session_id) if r.session_id else None
        return {'request_id':request_id,'phase':r.phase,'session_id':r.session_id,
            'error':r.error,'in_progress':r.in_progress,'cancel_requested':r.cancelled.is_set(),
            'worker':worker.snapshot() if worker else None,'environment':'PAPER','execution_authority':'NONE'}

    def request_status(self,request_id: str) -> dict:
        request_id=identifier(request_id)
        with self._condition:
            existing=self._requests.get(request_id)
            if existing is not None:return self._request_view(request_id,existing)
        saved=self.archive.find_start(request_id)
        if saved is None:raise ObservedError('Petición PAPER no encontrada')
        id=saved['session']['id']
        with self._condition:
            worker=self._workers.get(id)
            return {'request_id':request_id,'phase':'ACTIVATED','session_id':id,'error':None,'in_progress':False,
                'cancel_requested':False,'worker':worker.snapshot() if worker else None,
                'environment':'PAPER','execution_authority':'NONE','session':saved['session']}

    def activate(self,validation_id: str,*,preparation_id: str,request_id: str) -> dict:
        validation_id=identifier(validation_id);preparation_id=identifier(preparation_id);request_id=identifier(request_id)
        with self._condition:
            self._available()
            prior=self._requests.get(request_id)
            if prior is not None:
                if prior.validation_id is not None and (prior.validation_id,prior.preparation_id)!=(validation_id,preparation_id):
                    raise ObservedError('Petición de activación reutilizada con otro origen')
                return self._request_view(request_id,prior)
        # Lost responses and cold reopens return original evidence, without
        # requiring a live preparation or creating another worker.
        saved=self.archive.find_start(request_id)
        if saved is not None:
            if saved['origin']['reference']['artifact_id']!=validation_id:
                raise ObservedError('Petición de activación reutilizada con otro origen')
            return self.request_status(request_id)
        with self._condition:
            self._available();self._prune()
            prior=self._requests.get(request_id)
            if prior is not None:
                if prior.validation_id is not None and (prior.validation_id,prior.preparation_id)!=(validation_id,preparation_id):
                    raise ObservedError('Petición de activación reutilizada con otro origen')
                return self._request_view(request_id,prior)
            p=self._preparations.get(preparation_id)
            if p is None or p.validation_id!=validation_id or p.phase!='READY' or self.clock()>=p.expires:
                raise ObservedError('Prepara de nuevo la validación antes de activar PAPER')
            if len(self._requests)>=self.max_requests:raise ObservedError('Demasiadas peticiones PAPER en esta sesión')
            reserved=sum(r.in_progress for r in self._requests.values())+len(self._workers)
            if reserved>=self.max_workers:raise ObservedError('Límite de observadores PAPER alcanzado')
            job=_Activation(preparation_id,validation_id);self._requests[request_id]=job;self._inflight+=1
        try:
            assert p.origin is not None and p.paper_stamp is not None
            source=self.source_factory()
            seed=source.prepare(p.origin)
            with self._condition:
                if job.cancelled.is_set() or not self._accepting or self.clock()>=p.expires:
                    job.phase='CANCELLED';return self._request_view(request_id,job)
            created=self.archive.create(p.origin,seed,request_id=request_id,expected_session=p.paper_stamp)
            id=created.get('id') or created['session']['id'];job.session_id=id
            if 'session' in created:
                # Another owner published this request; do not attach its book.
                job.phase='ACTIVATED';return self._request_view(request_id,job)
            worker=self.worker_factory(self.archive,id,source,clock_ns=self.archive.clock_ns)
            with self._condition:
                cancelled=job.cancelled.is_set() or not self._accepting or self.clock()>=p.expires
                if not cancelled:
                    self._workers[id]=worker;worker.start();job.phase='ACTIVATED'
            if cancelled:
                self.archive.interrupt(id,'ACTIVATION_CANCELLED');job.phase='CANCELLED'
            return self._request_view(request_id,job)
        except Exception as exc:
            job.phase='FAILED';job.error=type(exc).__name__
            if job.session_id is not None:
                with self._condition:
                    worker=self._workers.get(job.session_id)
                    if worker is not None:worker.request_stop()
                try:self.archive.interrupt(job.session_id,'ACTIVATION_FAILED')
                except Exception:job.error+=':INTERRUPTION_NOT_SAVED'
            raise
        finally:
            with self._condition:
                job.in_progress=False;self._inflight-=1;self._condition.notify_all()

    def cancel(self,request_id: str) -> dict:
        request_id=identifier(request_id)
        with self._condition:
            job=self._requests.get(request_id)
            if job is None:
                self._prune()
                if len(self._requests)>=self.max_requests:raise ObservedError('Demasiadas peticiones PAPER en esta sesión')
                job=_Activation(None,None,phase='CANCELLED',in_progress=False)
                self._requests[request_id]=job
            job.cancelled.set()
            if job.session_id in self._workers:
                self._workers[job.session_id].request_stop();job.phase='INTERRUPTING'
            return self._request_view(request_id,job)

    def sessions(self,*,offset: int=0,limit: int=25) -> dict:
        page=self.archive.page(offset=offset,limit=limit)
        with self._condition:
            return {**page,'items':[{**row,'worker':self._workers[row['id']].snapshot()
                if row['id'] in self._workers else None} for row in page['items']]}
