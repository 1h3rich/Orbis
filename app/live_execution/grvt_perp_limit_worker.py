"""Session-bound worker for one future LIMIT signal and its verified exit."""
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Event, Thread
import time

from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError, client_ids
from app.live_execution.grvt_mainnet_journal import _control, require_new_mainnet_bracket, signer_ref
from app.live_execution.grvt_mainnet_transport import _result
from app.live_execution.grvt_perp_limit_execution import _matched, submit_limit_entry_once, submit_partial_limit_entry_once
from app.live_execution.grvt_perp_limit_journal import PARTIAL_SCHEMA, read_limit_attempt, stamp
from app.live_execution.grvt_perp_limit_origin import VerifiedLimitPaperOrigin, VerifiedPartialPaperOrigin
from app.live_execution.grvt_perp_limit_plan import NS, LimitTerms, PerpPartialLimitEntryTerms, read_limit_terms
from app.live_execution.grvt_perp_limit_receipt import LimitObservationPending
from app.live_execution.grvt_perp_limit_supervisor import LimitSupervisor
from app.live_execution.grvt_perp_signal_source import GrvtPerpSignalSource
from app.live_execution.grvt_perp_signals import PerpLimitSignalSeed, PerpPartialSignalSeed, record_signal_input


class _Revoked(RuntimeError):
    pass


@dataclass(repr=False)
class LimitJob:
    sub: str
    cid: str
    review_id: str
    generation: int
    ticket: tuple[int,int]
    entry_deadline: int
    deadline: int
    monotonic_deadline: float
    key: bytes | None
    stop: Event = field(default_factory=Event)
    controller: LimitSupervisor | None = None
    status: str = 'WARMING'
    active: bool = True
    decision: dict | None = None

    def revoke(self):
        self.stop.set()
        self.key = None
        if self.controller is not None:
            self.controller.stop()

    def view(self):
        return {'client_order_id':self.cid,'review_id':self.review_id,'active':self.active and not self.stop.is_set(),
                'status':self.status,'decision':self.decision,
                'deadline_ns':str(self.deadline),'authorization':False}


def validate_limit_timing(wait_seconds: int, pending_seconds: int, exit_seconds: int,
                          authorization_seconds: int) -> None:
    if (not 30 <= wait_seconds <= 600 or not 5 <= pending_seconds <= 120
            or not wait_seconds+pending_seconds+exit_seconds+30 <= authorization_seconds <= 840):
        raise ValueError('El plazo total debe cubrir señal, orden, permanencia y salida LIMIT')


LimitOrigin = VerifiedLimitPaperOrigin | VerifiedPartialPaperOrigin


def start_limit_worker(host, origin: LimitOrigin, terms: LimitTerms, *, cid: str, review_id: str,
                       private_key: bytes, expected_signer: str, client_factory: Callable,
                       session_checked: Callable, start_checked: Callable, generation: int,
                       ticket: tuple[int,int], wait_seconds: int, authorization_seconds: int,
                       max_reductions: int) -> dict:
    terms = read_limit_terms(terms.payload())
    sub = terms.budget.sub_account_id
    cid = client_ids(cid)[0]
    validate_limit_timing(wait_seconds,terms.max_pending_seconds,terms.budget.exit_seconds,authorization_seconds)
    partial = isinstance(terms,PerpPartialLimitEntryTerms)
    if (not isinstance(origin,VerifiedPartialPaperOrigin if partial else VerifiedLimitPaperOrigin)
            or type(max_reductions) is not int or not 1 <= max_reductions <= 20
            or (max_reductions != terms.max_attempts if partial else max_reductions > 5)
            or type(private_key) is not bytes or len(private_key) != 32):
        raise GrvtMainnetError('Falta la autorización LIMIT y su original verificado')

    def verify():
        if isinstance(terms,PerpPartialLimitEntryTerms) and isinstance(origin,VerifiedPartialPaperOrigin):
            reviewed = origin.limit_terms(terms.budget,max_pending_seconds=terms.max_pending_seconds,
                max_attempts=terms.max_attempts,reserved_close_attempts=terms.reserved_close_attempts)
        else:
            assert isinstance(origin,VerifiedLimitPaperOrigin)
            reviewed = origin.terms(terms.budget,max_pending_seconds=terms.max_pending_seconds)
        if reviewed != terms:
            raise GrvtMainnetError('La prueba PAPER LIMIT cambió después de revisar')
        with host.vault.guard_session_generation(generation),host.vault.session() as db:
            require_new_mainnet_bracket(db,sub,cid)

    return _schedule(host,sub,cid,review_id=review_id,origin=origin,terms=terms,private_key=private_key,
        expected_signer=expected_signer,client_factory=client_factory,session_checked=session_checked,
        start_checked=start_checked,generation=generation,ticket=ticket,wait_seconds=wait_seconds,
        authorization_seconds=authorization_seconds,max_reductions=max_reductions,verify=verify)


def verify_limit_resume(vault, sub: str, cid: str, *, client=None, signer: str | None = None) -> dict:
    saved = read_limit_attempt(vault,cid)
    if saved['sub_account_id'] != sub or not saved['slot_reserved'] or not saved['events']['send_started']:
        raise GrvtMainnetError('No hay un envío LIMIT pendiente en esta subcuenta')
    with vault.session() as db:
        control = _control(db,sub)
        if control.stopped:
            raise GrvtMainnetError('Rearma la subcuenta antes de retomar el intento LIMIT')
        if client is not None:
            _matched(client,saved,control)
        if signer is not None and signer_ref(control.identity_salt,signer) != saved['claim']['signer_ref']:
            raise GrvtMainnetError('Conecta la firma dedicada del intento original')
    return saved


def validate_resume_reductions(saved: dict, max_reductions: int) -> bool:
    claim = saved['claim']
    partial = claim['schema']==PARTIAL_SCHEMA
    terms = claim['preflight']['plan']['source']['terms']
    if type(max_reductions) is not int or (max_reductions != terms['max_attempts'] if partial
                                             else not 1 <= max_reductions <= 5):
        raise GrvtMainnetError('Los límites del permiso no corresponden al intento LIMIT')
    return partial


def resume_limit_worker(host, sub: str, cid: str, *, review_id: str, claim_digest: str, event_head: str,
                        private_key: bytes, expected_signer: str, client_factory: Callable,
                        session_checked: Callable, start_checked: Callable, generation: int,
                        ticket: tuple[int,int], authorization_seconds: int, max_reductions: int) -> dict:
    cid = client_ids(cid)[0]
    if (type(authorization_seconds) is not int or not 60 <= authorization_seconds <= 600
            or type(max_reductions) is not int or not 1 <= max_reductions <= 20
            or type(private_key) is not bytes or len(private_key) != 32):
        raise GrvtMainnetError('Falta la autorización LIMIT para retomar este intento')

    def verify():
        with host.vault.guard_session_generation(generation):
            saved = verify_limit_resume(host.vault,sub,cid,signer=expected_signer)
            validate_resume_reductions(saved,max_reductions)
            if saved['claim_digest'] != claim_digest or saved['events']['head'] != event_head:
                raise GrvtMainnetError('El intento LIMIT cambió después de revisar')

    return _schedule(host,sub,cid,review_id=review_id,origin=None,terms=None,private_key=private_key,
        expected_signer=expected_signer,client_factory=client_factory,session_checked=session_checked,
        start_checked=start_checked,generation=generation,ticket=ticket,wait_seconds=0,
        authorization_seconds=authorization_seconds,max_reductions=max_reductions,verify=verify,
        resume_prefix=(claim_digest,event_head))


def _schedule(host, sub: str, cid: str, *, review_id: str, origin: LimitOrigin | None,
              terms: LimitTerms | None, private_key: bytes, expected_signer: str,
              client_factory: Callable, session_checked: Callable, start_checked: Callable,
              generation: int, ticket: tuple[int,int], wait_seconds: int, authorization_seconds: int,
              max_reductions: int, verify: Callable, resume_prefix: tuple[str,str] | None = None) -> dict:
    with host._lock:
        host._require_ticket(sub,ticket)
        if host.active_sub(sub):
            raise GrvtMainnetError('Subcuenta MAINNET activa o preparando')
        host._starting.add(sub)
    job = None
    try:
        start_checked()
        verify()
        start_checked()
        at = stamp(host.clock())
        with host._lock:
            host._require_ticket(sub,ticket)
            host._revisions[sub] = host._revisions.get(sub,0)+1
            job = LimitJob(sub,cid,review_id,generation,host.ticket(sub),at+wait_seconds*NS,
                           at+authorization_seconds*NS,time.monotonic()+authorization_seconds,private_key)
            if resume_prefix is not None:
                job.status = 'VERIFYING_ORDER'
            Thread(target=_run,args=(host,job,origin,terms,max_reductions,expected_signer,
                                     session_checked,client_factory,resume_prefix),
                   name='orbis-grvt-limit-'+cid[-6:],daemon=True).start()
            # A failed launch must never become a receipt for this review.
            host._limit_jobs[sub] = job
            return job.view()
    except Exception:
        if job is not None:
            job.revoke()
            with host._lock:
                job.active = False
                job.status = 'REVIEW'
        raise
    finally:
        with host._lock:
            host._starting.discard(sub)
        del private_key


def _wait_for_limit_signal(host, job: LimitJob, origin: LimitOrigin, terms: LimitTerms,
                           source: GrvtPerpSignalSource, checked: Callable, poll: int | float):
    if isinstance(terms,PerpPartialLimitEntryTerms) and isinstance(origin,VerifiedPartialPaperOrigin):
        seed = source.prepare_partial(origin,terms.exit_terms())
        seed_type = PerpPartialSignalSeed
    else:
        seed = source.prepare(origin.origin)
        seed_type = PerpLimitSignalSeed
    if not isinstance(seed,seed_type) or int(seed.requested_at_ns) < int(terms.paper.last_received_at_ns):
        raise GrvtMainnetError('Calentamiento LIMIT ajeno al origen revisado')
    feed,records = seed.start(),[]
    with host._lock:
        if not job.stop.is_set():
            job.status = 'WAITING_SIGNAL'
    while checked() < job.entry_deadline:
        raw = source.poll(feed)
        checked()
        result,record = record_signal_input(feed,raw)
        feed = result.state
        records.append(record)
        if feed.status != 'RUNNING':
            return 'DATA_GAP',seed,records
        if record['decisions'] and record['decisions'][-1]['entry_matched'] and checked() < job.entry_deadline:
            return 'SENDING',seed,records
        host.waiter(job.stop,poll)
    return 'NO_SIGNAL',seed,records


def _run(host, job: LimitJob, origin: LimitOrigin | None, terms: LimitTerms | None,
         max_reductions: int, expected_signer: str, session_checked: Callable, client_factory: Callable,
         resume_prefix: tuple[str,str] | None):
    last = 0
    status = 'REVIEW'

    def checked():
        nonlocal last
        if job.stop.is_set() or host.vault.session_generation != job.generation:
            raise _Revoked()
        try:
            session_checked()
            host._require_ticket(job.sub,job.ticket)
        except Exception:
            raise _Revoked() from None
        at = stamp(host.clock())
        if at < last or at >= job.deadline or time.monotonic() >= job.monotonic_deadline or job.stop.is_set():
            raise _Revoked()
        last = at
        return at

    def progress(value, decision=None):
        with host._lock:
            if not job.stop.is_set():
                job.status,job.decision = value,decision

    poll = max(1,min(3,host.poll))
    try:
        checked()
        source = (host.perp_source_factory(checked) if host.perp_source_factory is not None else
                  GrvtPerpSignalSource(clock_ns=host.clock,require_permission=checked,
                                       public_post=lambda url,body:_result(host.public(url,body))))
        venue = {'client_factory':client_factory,'require_permission':checked,'clock_ns':host.clock}
        if resume_prefix is None:
            if origin is None or terms is None:
                raise GrvtMainnetError('Falta el origen para la entrada LIMIT')
            phase,seed,records = _wait_for_limit_signal(host,job,origin,terms,source,checked,poll)
            if phase != 'SENDING':
                status = phase
                return
            progress(phase)
            checked()
            key = job.key
            if key is None:
                raise _Revoked()
            try:
                options = dict(**venue,client_order_id=job.cid,
                    entry_deadline_ns=str(min(checked()+terms.max_pending_seconds*NS,job.deadline-30*NS)),
                    expected_signer=expected_signer,public_post=host.public)
                if isinstance(terms,PerpPartialLimitEntryTerms):
                    submit_partial_limit_entry_once(host.vault,origin,terms,seed,records,**options,private_key=key,
                        confirmation='ENVIAR_ENTRADA_LIMIT_PARCIALES_GRVT_MAINNET')
                else:
                    submit_limit_entry_once(host.vault,origin,terms.budget,seed,records,**options,private_key=key,
                        max_pending_seconds=terms.max_pending_seconds,confirmation='ENVIAR_ENTRADA_LIMIT_GRVT_MAINNET')
            except GrvtMainnetError:
                checked()
                saved = read_limit_attempt(host.vault,job.cid)
                if (not saved['events']['send_started'] or saved['claim']['seed'] != seed.payload()
                        or saved['claim']['records'] != records):
                    raise
            del key
            partial = isinstance(terms,PerpPartialLimitEntryTerms)
        else:
            saved = read_limit_attempt(host.vault,job.cid)
            if (saved['claim_digest'],saved['events']['head']) != resume_prefix:
                raise GrvtMainnetError('El intento LIMIT cambió antes de retomar')
            partial = validate_resume_reductions(saved,max_reductions)
        checked()
        key = job.key
        if key is None:
            raise _Revoked()
        controller = LimitSupervisor(host.vault,job.cid,**venue,private_key=key,expected_signer=expected_signer,
            deadline_ns=job.deadline,max_reductions=max_reductions,source=source,public_post=host.public,
            confirmation=('SUPERVISAR_ORDEN_LIMIT_PARCIALES_GRVT_MAINNET' if partial
                          else 'SUPERVISAR_ORDEN_LIMIT_GRVT_MAINNET'))
        with host._lock:
            if job.stop.is_set():
                controller.stop()
                raise _Revoked()
            job.controller,job.key = controller,None
        del key
        unavailable = 0
        while True:
            checked()
            try:
                result = controller.step()
            except LimitObservationPending:
                unavailable += 1
                progress('VERIFYING_ORDER',job.decision)
                if unavailable >= 4:
                    status = 'REVIEW'
                    return
                host.waiter(job.stop,poll)
                continue
            unavailable = 0
            status = result['status']
            progress(status,result['decision'])
            if status in ('COMPLETE','REVIEW'):
                return
            host.waiter(job.stop,poll)
    except _Revoked:
        status = 'STOPPED'
    except Exception:
        status = 'STOPPED' if job.stop.is_set() else 'REVIEW'
    finally:
        job.revoke()
        with host._lock:
            job.active = False
            job.status = status
        host.on_inactive()
