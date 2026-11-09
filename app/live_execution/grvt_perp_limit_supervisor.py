"""Drive a reserved LIMIT attempt using a new, revocable in-memory authorization."""
from collections.abc import Callable
from threading import Event, Lock
import secrets
import time

from eth_account import Account

from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError, client_ids
from app.live_execution.grvt_mainnet_journal import _control, signer_ref
from app.live_execution.grvt_mainnet_transport import public_post as default_public_post
from app.live_execution.grvt_perp_limit_execution import (
    cancel_limit_attempt_once, observe_limit_attempt, settle_empty_limit_attempt,
)
from app.live_execution.grvt_perp_limit_exit import (
    audit_limit_exit, settle_limit_exit, submit_limit_reduction_once,
)
from app.live_execution.grvt_perp_limit_exit_evidence import stable_exit_audits
from app.live_execution.grvt_perp_limit_journal import (
    PARTIAL_SCHEMA, _plan, append_limit_event, read_limit_attempt, stamp, verified_limit_watch,
)
from app.live_execution.grvt_perp_limit_plan import FRESH_NS, NS
from app.live_execution.grvt_perp_limit_watch import advance_limit_watch, limit_watch_checkpoint, start_limit_watch
from app.live_execution.grvt_perp_signal_source import GrvtPerpSignalSource
from app.live_execution.grvt_perp_signals import record_signal_input
from app.portfolio.models import GrvtMainnetLimitAttempt


class LimitSupervisor:
    def __init__(self, vault, cid: str, *, client_factory: Callable, require_permission: Callable,
                 private_key: bytes, expected_signer: str, deadline_ns: int, max_reductions: int,
                 confirmation: str, source: GrvtPerpSignalSource | None = None,
                 public_post: Callable = default_public_post, clock_ns: Callable = time.time_ns):
        partial = confirmation=='SUPERVISAR_ORDEN_LIMIT_PARCIALES_GRVT_MAINNET'
        if ((not partial and confirmation != 'SUPERVISAR_ORDEN_LIMIT_GRVT_MAINNET') or not callable(require_permission)
                or type(max_reductions) is not int or not 1 <= max_reductions <= (20 if partial else 5)):
            raise GrvtMainnetError('Falta la autorización LIMIT de supervisión y su límite de reducciones')
        self._vault = vault
        self._cid = client_ids(cid)[0]
        self._generation = vault.session_generation
        self._clock = clock_ns
        self._last = stamp(clock_ns())
        self._deadline = stamp(deadline_ns)
        if self._generation is None or not 30*NS <= self._deadline-self._last <= 86400*NS:
            raise GrvtMainnetError('Sesión REAL o plazo de supervisión LIMIT inválido')
        self._monotonic_deadline = time.monotonic()+(self._deadline-self._last)/NS
        require_permission()
        saved = read_limit_attempt(vault,self._cid)
        claim = saved['claim']
        if (partial != (claim['schema']==PARTIAL_SCHEMA)
                or partial and max_reductions != claim['preflight']['plan']['source']['terms']['max_attempts']):
            raise GrvtMainnetError('La autorización de supervisión no corresponde a la política LIMIT')
        self._partial = partial
        self._sub = saved['sub_account_id']
        self._digest = saved['claim_digest']
        with vault.session() as db:
            control = _control(db,self._sub)
            if not saved['slot_reserved'] or not saved['events']['send_started'] or control.stopped:
                raise GrvtMainnetError('El intento LIMIT no admite supervisión')
            try:
                if (type(private_key) is not bytes or len(private_key) != 32
                        or Account.from_key(private_key).address.lower() != expected_signer.lower()
                        or signer_ref(control.identity_salt,expected_signer) != saved['claim']['signer_ref']):
                    raise ValueError('firmante distinto')
            except Exception:
                raise GrvtMainnetError('El firmante de supervisión LIMIT no corresponde al intento') from None
            self._revision = control.revision
        self._stop = Event()
        self._lock = Lock()
        self._permission = require_permission
        self._key = private_key
        self._signer = expected_signer
        self._client = client_factory
        self._public = public_post
        self._max_reductions = max_reductions
        self._source = source if source is not None else GrvtPerpSignalSource(
            clock_ns=clock_ns,require_permission=self._checked)

    def stop(self) -> None:
        self._stop.set()
        self._key = None

    def _checked(self) -> int:
        try:
            if self._stop.is_set() or self._vault.session_generation != self._generation:
                raise ValueError('sesión revocada')
            self._permission()
            with self._vault.session() as db:
                control = _control(db,self._sub)
                if control is None or control.stopped or control.revision != self._revision:
                    raise ValueError('control revocado')
            at = stamp(self._clock())
            if (self._stop.is_set() or not self._last <= at < self._deadline
                    or time.monotonic() >= self._monotonic_deadline):
                raise ValueError('vigencia revocada')
            self._last = at
            return at
        except Exception:
            self.stop()
            raise GrvtMainnetError('Supervisión LIMIT detenida, vencida o revocada') from None

    def _append_market(self, saved: dict) -> tuple[dict, dict]:
        cid,vault = self._cid,self._vault
        with vault.session() as db:
            row = db.get(GrvtMainnetLimitAttempt,cid)
            current,watch = verified_limit_watch(db,row)
        if current['claim_digest'] != self._digest or current['events']['head'] != saved['events']['head']:
            raise GrvtMainnetError('El intento LIMIT cambió durante la supervisión')
        claim = saved['claim']
        plan = _plan(claim,claim['claimed_at_ns'])
        watch = watch if watch is not None else start_limit_watch(claim)
        if watch.close_reason is not None:
            return saved,limit_watch_checkpoint(watch,plan,saved['events'],at_ns=self._checked())
        self._checked()
        raw = None
        receipt = saved['events']['receipt']
        if receipt is None or not receipt['terminal'] or receipt['fills']:
            try:
                raw = self._source.poll(watch.feed)
            except Exception:
                # A failed public read contributes no market data. The watchdog
                # evaluates elapsed time against the last accepted observation.
                pass
        self._checked()
        record = record_signal_input(watch.feed,raw)[1] if raw is not None else None
        with vault.guard_session_generation(self._generation), vault.session() as db:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            row = db.get(GrvtMainnetLimitAttempt,cid)
            if row.payload_digest != self._digest or row.event_head != saved['events']['head']:
                raise GrvtMainnetError('El intento LIMIT cambió antes de guardar la decisión')
            at = self._checked()
            _,checkpoint = advance_limit_watch(watch,plan,saved['events'],record,at_ns=at)
            append_limit_event(db,row,'WATCH',{'record':record,'checkpoint':checkpoint},at)
            db.commit()
        return read_limit_attempt(vault,cid),checkpoint

    def step(self) -> dict:
        """Advance one step. A missing exchange receipt never triggers a resend."""
        with self._lock:
            self._checked()
            vault,cid = self._vault,self._cid
            venue = {'client_factory':self._client,'require_permission':self._checked,'clock_ns':self._clock}
            saved = read_limit_attempt(vault,cid)
            if saved['claim_digest'] != self._digest:
                raise GrvtMainnetError('El intento LIMIT autorizado cambió')
            if not saved['slot_reserved']:
                self.stop()
                return {'status':'COMPLETE','decision':saved['events']['supervision']}
            receipt = saved['events']['receipt']
            if receipt is None or not receipt['terminal']:
                saved = observe_limit_attempt(vault,cid,**venue)
            saved,decision = self._append_market(saved)
            action = decision['action']
            status = action
            if action == 'CANCEL_REMAINDER':
                try:
                    cancel_limit_attempt_once(vault,cid,**venue,confirmation='CANCELAR_ENTRADA_LIMIT_GRVT_MAINNET')
                except GrvtMainnetError:
                    self._checked()
                    if not read_limit_attempt(vault,cid)['events']['cancel_claimed']:
                        raise
                status = 'WAIT_TERMINAL'
            elif action == 'SETTLE_EMPTY':
                settle_empty_limit_attempt(vault,cid,**venue)
                status = 'COMPLETE'
            elif action in ('HOLD','CLOSE'):
                status = self._position_step(saved,venue,action)
            if status in ('COMPLETE','REVIEW'):
                self.stop()
            return {'status':status,'decision':decision}

    def _position_step(self, saved: dict, venue: dict, action: str) -> str:
        events = saved['events']
        audits = events['exit_audits']
        if not audits or self._checked()-audits[-1]['at_ns'] >= FRESH_NS:
            events = audit_limit_exit(self._vault,self._cid,**venue)['events']
            audits = events['exit_audits']
        if action == 'HOLD' and not self._partial:
            return 'HOLD'
        try:
            _,last = stable_exit_audits(audits,now_ns=self._checked())
        except ValueError:
            return 'VERIFYING_EXIT'
        if last['result']['remaining_size'] == '0':
            settle_limit_exit(self._vault,self._cid,require_permission=self._checked,clock_ns=self._clock)
            return 'COMPLETE'
        if len(events['reductions']) >= self._max_reductions or self._deadline-self._checked() < 5*NS:
            return 'REVIEW'
        new_id = str(2**63|secrets.randbits(63))
        key = self._key
        if key is None:
            raise GrvtMainnetError('Supervisión LIMIT revocada antes de firmar')
        try:
            updated = submit_limit_reduction_once(self._vault,self._cid,**venue,
                reduction_client_order_id=new_id,deadline_ns=min(self._deadline,self._last+20*NS),
                private_key=key,expected_signer=self._signer,public_post=self._public,
                confirmation=('REDUCIR_POSICION_LIMIT_PARCIALES_GRVT_MAINNET' if self._partial
                              else 'REDUCIR_POSICION_LIMIT_GRVT_MAINNET'),
                supervision_head=events['supervision_head'])
            if self._partial and len(updated['events']['reductions']) == len(events['reductions']):
                return 'HOLD'
        except GrvtMainnetError:
            self._checked()
            recorded = read_limit_attempt(self._vault,self._cid)['events']['reductions']
            if len(recorded) != len(events['reductions'])+1 or recorded[-1]['claim']['client_order_id'] != new_id:
                raise
        return 'VERIFYING_EXIT'
