"""RAM-only perpetual coordinator over the shared native MAINNET journal.

The public rule feed has no account or simulated money. This coordinator owns
the separately revocable signer, actual fills, position checks and bounded
recovery. It has no HTTP activation route; saved grants never start workers.
"""
from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal,localcontext
import json
from threading import Event,Thread
import time

from eth_account import Account

from app.live_execution.grvt_mainnet_bot_entry import (
    MainnetBotPolicy,finish_bot_grant,settle_bot_selection,
)
from app.live_execution.grvt_mainnet_bot_service import GrvtMainnetBotService,_Job,_Stopped,_Expired
from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError
from app.live_execution.grvt_mainnet_finance import audit_mainnet_bracket,reconcile_mainnet_bracket
from app.live_execution.grvt_mainnet_journal import _control,encode,read_mainnet_bracket
from app.live_execution.grvt_mainnet_observation import observe_mainnet_bracket
from app.live_execution.grvt_mainnet_stop_origin import release_bot_stop
from app.live_execution.grvt_mainnet_transport import GrvtMainnetClient,_result
from app.live_execution.grvt_perp_bot_grant import (
    PerpBotSelectionRequest,_signal_selection,append_perp_market,append_supervision,begin_perp_bot_grant,
)
from app.live_execution.grvt_perp_bot_origin import VerifiedPerpBotOrigin
from app.live_execution.grvt_perp_entry_fills import collect_perp_entry_fills
from app.live_execution.grvt_perp_plan import PerpEntryTerms
from app.live_execution.grvt_perp_position import collect_position_review
from app.live_execution.grvt_perp_signal_source import GrvtPerpSignalSource
from app.live_execution.grvt_perp_signal_worker import PerpSignalWorker
from app.live_execution.grvt_perp_submit import submit_perp_entry_once
from app.live_execution.grvt_perp_supervision import SOURCE_FAULTS


class GrvtPerpCoordinator:
    def __init__(self,host: GrvtMainnetBotService):
        self.host=host

    def start(self,origin: VerifiedPerpBotOrigin,terms: PerpEntryTerms,policy: MainnetBotPolicy,*,
              api_key: str,private_key: bytes,expected_signer: str,expected_ticket: tuple[int,int],confirmation: str,
              keep_operating: bool=False,expected_generation: int|None=None,session_checked: Callable[[],None]=lambda:None,
              requested_run_id: str|None=None,source_factory: Callable[[Callable[[],int]],GrvtPerpSignalSource]|None=None,
              worker_waiter: Callable[[Event,float],None]|None=None,start_checked: Callable[[],None]=lambda:None) -> dict:
        host=self.host
        generation=host.vault.session_generation if expected_generation is None else expected_generation
        if (confirmation!='ACTIVAR_BOT_PERPETUO_GRVT_MAINNET' or generation is None
                or not isinstance(origin,VerifiedPerpBotOrigin) or type(keep_operating) is not bool
                or type(api_key) is not str or not api_key or type(private_key) is not bytes
                or len(private_key)!=32 or type(expected_signer) is not str
                or Account.from_key(private_key).address.lower()!=expected_signer.lower()):
            raise GrvtMainnetError('Falta autorización específica y revocable del bot perpetuo MAINNET')
        terms=PerpEntryTerms.model_validate(terms.payload())
        sub=policy.sub_account_id
        with host._lock:
            host._require_ticket(sub,expected_ticket)
            if host.active_sub(sub):raise GrvtMainnetError('Subcuenta MAINNET activa o preparando')
            host._starting.add(sub)
        job=None;run_id=None;worker=None;last=0
        def checked():
            nonlocal last
            if job is not None:return host._checked(job)
            host._require_ticket(sub,expected_ticket)
            if host.vault.session_generation!=generation:raise _Stopped()
            session_checked();start_checked()
            at=host.clock()
            if type(at) is not int or not 0<at<2**63 or at<last:
                raise GrvtMainnetError('Reloj de autorización perpetua inválido')
            last=at
            return at
        try:
            checked()
            origin.recheck(require_permission=checked)
            source=(source_factory(checked) if source_factory is not None else
                GrvtPerpSignalSource(clock_ns=host.clock,require_permission=checked,
                    public_post=lambda url,body:_result(host.public(url,body))))
            if not isinstance(source,GrvtPerpSignalSource):
                raise GrvtMainnetError('Fuente perpetua incompatible')
            # Seed again at activation. Historical signals in it are warm-up,
            # never candidates copied from an earlier preview or PAPER run.
            seed=source.prepare(origin.origin)
            checked();client=host.factory(api_key);checked()
            if (not isinstance(client,GrvtMainnetClient) or client.network!='MAINNET'
                    or client.sub_account_id!=sub):
                raise GrvtMainnetError('Identidad MAINNET ajena a la autorización')
            account_ref=client.identity_ref(host._salt)
            with host.vault.guard_session_generation(generation):
                saved=begin_perp_bot_grant(host.vault,origin,seed,terms,policy,now_ns=checked(),
                    keep_operating=keep_operating,run_id=requested_run_id,supervised=True)
            run_id=saved['id'];at=checked()
            with host._lock:
                host._require_ticket(sub,expected_ticket)
                host._revisions[sub]=host._revisions.get(sub,0)+1
                job=_Job(saved,generation,host.ticket(sub),api_key,private_key,expected_signer,
                    account_ref,keep_operating,session_checked,
                    time.monotonic()+(saved['deadline_ns']-at)/1e9,at)
                host._jobs[run_id]=job
            worker=PerpSignalWorker(seed,source=source,require_permission=checked,clock_ns=host.clock,
                poll_seconds=max(2,min(5,host.poll)),waiter=worker_waiter,
                append_record=lambda record:append_perp_market(host.vault,run_id,record,
                    require_permission=checked,clock_ns=host.clock))
            job.perp_worker=worker
            worker.start()
            Thread(target=self._run,args=(job,terms),name='orbis-grvt-perp-'+run_id[-6:],daemon=True).start()
            return host.snapshot(run_id)
        except Exception:
            # This also covers a failed Thread.start or a lost startup result.
            # Revocation is first; cleanup must never leave a live RAM signer.
            if job is not None:job.stop.set()
            if worker is not None:worker.stop()
            if job is not None:
                with host._lock:
                    job.active=False;job.api_key='';job.key=None;job.signer='';job.status='STOPPED'
            if run_id is not None:
                try:finish_bot_grant(host.vault,run_id,status='STOPPED',now_ns=max(last,host.clock()))
                except Exception:
                    if job is not None:job.archive_error=True
            raise GrvtMainnetError('No se pudo activar el bot perpetuo; consulta su registro antes de reintentar') from None
        finally:
            with host._lock:host._starting.discard(sub)
            del private_key

    def _supervision(self,job: _Job) -> dict:
        host=self.host
        at=host._checked(job)
        worker=job.perp_worker.snapshot()
        if worker['status'] in SOURCE_FAULTS and not job.source_fault_recorded:
            # Record only the safety fault, never retry an uncertain MARKET.
            job.source_fault_recorded=True
            append_supervision(host.vault,job.saved['id'],'SOURCE_FAULT',source_fault=worker['status'],
                require_permission=lambda:host._checked(job),clock_ns=host.clock)
        saved=host._refresh(job)
        checkpoint=saved['signal_checkpoint']
        if (at-int(checkpoint['last_observed_ns'])>5_000_000_000
                and not saved['supervision']['halt_new_entries']):
            append_supervision(host.vault,saved['id'],'SUPERVISION_TICK',
                require_permission=lambda:host._checked(job),clock_ns=host.clock)
            saved=host._refresh(job)
        # A permission failure is never interpreted as a venue close signal.
        if worker['status']=='PERMISSION_REVOKED':
            host._checked(job)
            raise GrvtMainnetError('La fuente perdió el permiso operativo')
        with host._lock:job.last_decision=saved['supervision']
        return saved['supervision']

    def _position(self,job: _Job,terms: PerpEntryTerms,parent: str) -> None:
        host=self.host
        group=read_mainnet_bracket(host.vault,parent)
        with host.vault.session() as db:
            salt=_control(db,terms.budget.sub_account_id).identity_salt
        raw=collect_position_review(host._client(job),terms,identity_salt=salt,
            expected_account_ref=group['claim']['account_ref'],require_permission=lambda:host._checked(job),
            clock_ns=host.clock)
        append_supervision(host.vault,job.saved['id'],'POSITION_REVIEW',parent=parent,position_input=raw,
            require_permission=lambda:host._checked(job),clock_ns=host.clock)
        job.position_checked_ns=host._checked(job)

    def _manage(self,job: _Job,terms: PerpEntryTerms,parent: str) -> dict:
        host=self.host
        while True:
            value=read_mainnet_bracket(host.vault,parent)
            host._cache_group(job,value)
            if value['reconciliation'] is not None:return value['reconciliation']
            host._checked(job)
            observed=observe_mainnet_bracket(host.vault,parent,client_factory=lambda:host._client(job),
                require_permission=lambda:host._checked(job),clock_ns=host.clock)
            state=observed['state']
            terminal=all(order['status'] in ('FILLED','CANCELLED','REJECTED') for order in state['orders'])
            if terminal and not state['needs_review'] and Decimal(state['residual_size_from_orders'])==0:
                host._status(job,'VERIFYING')
                audit_mainnet_bracket(host.vault,parent,client_factory=lambda:host._client(job),
                    require_permission=lambda:host._checked(job),clock_ns=host.clock)
                try:return reconcile_mainnet_bracket(host.vault,parent,now_ns=host._checked(job))
                except GrvtMainnetError:
                    host.waiter(job.stop,5.1)
                    continue
            supervision=self._supervision(job)
            if supervision['action']=='REVIEW':
                raise GrvtMainnetError('Posición ajena o supervisión pendiente de revisión')
            if state['needs_review'] or supervision['action']=='CLOSE':
                return host._close(job,parent)
            if state['entry_fully_filled']:
                if supervision['entry_state']!='CONFIRMED_OWN_FILLS':
                    try:
                        entry=collect_perp_entry_fills(host.vault,job.saved['id'],parent,
                            client_factory=lambda:host._client(job),require_permission=lambda:host._checked(job),
                            clock_ns=host.clock)
                    except GrvtMainnetError:
                        # Native protection can fill while history is read.
                        # Re-observe once; never fabricate an entry snapshot.
                        actual=observe_mainnet_bracket(host.vault,parent,client_factory=lambda:host._client(job),
                            require_permission=lambda:host._checked(job),clock_ns=host.clock)['state']
                        if actual['orders']!=state['orders']:continue
                        raise
                    with host._lock:job.entry_fills=entry
                    append_supervision(host.vault,job.saved['id'],'ENTRY_CONFIRMED',parent=parent,
                        entry_digest=encode(entry)[1],require_permission=lambda:host._checked(job),clock_ns=host.clock)
                if host._checked(job)-job.position_checked_ns>=5_000_000_000:
                    self._position(job,terms,parent)
                supervision=self._supervision(job)
                if supervision['action']=='REVIEW':
                    raise GrvtMainnetError('La cuenta necesita revisión manual')
                if supervision['action']=='CLOSE':return host._close(job,parent)
                host._status(job,'RECHECKING_POSITION' if supervision['action']=='RECHECK' else 'WATCHING')
            else:
                # FOK may be cancelled/expired/partial. Cancel its surviving
                # native orders and audit the actual residue before reduction.
                return host._close(job,parent)
            host.waiter(job.stop,max(2,min(5,host.poll)))

    def _fee_ceiling_exceeded(self,group: dict,terms: PerpEntryTerms) -> bool:
        with localcontext() as context:
            context.prec=100
            ceiling=Decimal(terms.budget.fee_rate_ceiling)
            return any(Decimal(fill['fee'])>Decimal(fill['size'])*Decimal(fill['price'])*ceiling
                for fill in group['audits'][-1]['histories']['fill_history']['rows'])

    def _run(self,job: _Job,terms: PerpEntryTerms) -> None:
        host=self.host;final='REVIEW'
        try:
            while True:
                at=host._checked(job)
                supervision=self._supervision(job)
                saved=host._refresh(job);policy=saved['policy']
                if supervision['halt_new_entries']:break
                if at>=saved['entry_deadline_ns']:final='EXPIRED';break
                host._status(job,'WAITING_SIGNAL')
                candidate=job.perp_worker.candidate()
                armed=max(saved['started_at_ns'],saved['selections'][-1]['settled_at_ns'] or 0) if saved['selections'] else saved['started_at_ns']
                decision=json.loads(candidate.decision_json) if candidate is not None else None
                if decision is None or int(decision['source_close_ns'])<=armed:
                    host.waiter(job.stop,max(2,min(5,host.poll)))
                    continue
                _,parent,_=_signal_selection(saved['source'],saved['id'],decision)
                host._status(job,'PREPARING_ENTRY')
                try:
                    submit_perp_entry_once(host.vault,terms,parent_client_order_id=parent,
                        bot_selection=PerpBotSelectionRequest(saved['id'],decision['sha256']),
                        client_factory=lambda:host._client(job),private_key=job.key,expected_signer=job.signer,
                        require_permission=lambda:host._checked(job),confirmation='ENVIAR_ENTRADA_PERPETUA_GRVT_MAINNET',
                        public_post=host.public,clock_ns=host.clock)
                except GrvtMainnetError:
                    current=host._refresh(job)
                    if (len(current['selections'])!=len(saved['selections'])+1
                            or current['selections'][-1]['parent_client_order_id']!=parent):raise
                    # A burned SELECT without a claim cannot be resent. A
                    # durable claim is recovered only by querying its own IDs.
                    read_mainnet_bracket(host.vault,parent)
                with host._lock:job.parent=parent
                host._refresh(job)
                self._manage(job,terms,parent);host._checked(job)
                group=read_mainnet_bracket(host.vault,parent);host._cache_group(job,group)
                settle_bot_selection(host.vault,saved['id'],now_ns=host._checked(job))
                saved=host._refresh(job)
                if self._fee_ceiling_exceeded(group,terms):break
                if saved['supervision']['halt_new_entries']:break
                with localcontext() as context:
                    context.prec=100
                    net=sum((Decimal(item['settlement']['net_realized_pnl_usdt']) for item in saved['selections']),Decimal(0))
                    if len(saved['selections'])>=policy['max_entries']:final='ENTRY_LIMIT';break
                    if Decimal(policy['entry_cap'])*(len(saved['selections'])+1)>Decimal(policy['total_entry_cap']):final='BUDGET_LIMIT';break
                    if -net>=Decimal(policy['loss_cap']):final='LOSS_LIMIT';break
                with host.vault.session() as db:
                    control=_control(db,policy['sub_account_id']);stopped=control.stopped;revision=control.revision
                if stopped:release_bot_stop(host.vault,saved['id'],parent,expected_revision=revision,
                    require_permission=lambda:host._checked(job),now_ns=host.clock())
                with host._lock:job.parent=None;job.position_checked_ns=0
        except _Stopped:final='STOPPED'
        except _Expired:final='EXPIRED'
        except Exception:final='STOPPED' if job.stop.is_set() else 'REVIEW'
        finally:
            # Revoke the producer before finishing the durable grant. A public
            # request still in flight cannot append or publish after this.
            job.stop.set();job.perp_worker.stop()
            host._status(job,final)
            try:
                with host.vault.guard_session_generation(job.generation):
                    finish_bot_grant(host.vault,job.saved['id'],status=final,now_ns=max(job.last_ns,host.clock()))
                    host._refresh(job)
            except Exception:
                with host._lock:job.archive_error=True
            with host._lock:job.active=False;job.api_key='';job.key=None;job.signer=''
            host.on_inactive()
