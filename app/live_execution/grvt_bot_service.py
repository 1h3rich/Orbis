"""Concesión GRVT TESTNET de uno o varios ciclos con límites y parada.

Esperar no autentica una cuenta privada. Sólo una nueva OPEN causal puede
consumir la selección persistente. Después se reutiliza el coordinador OTOCO
y el observador de fills. Los ciclos sucesivos requieren un cierre plano
conciliado y el mismo STOP automático. Nunca se reanuda después de reiniciar.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal
from threading import Event, RLock, Thread
import time
from typing import Callable

from app.live_execution.grvt_bot_archive import begin_bot_run, read_bot_run, select_bot_open, finish_bot_run, STATUSES
from app.live_execution.grvt_bot_entry_plan import automatic_entry_terms, GrvtBotConfigurationError
from app.live_execution.grvt_bot_source import bot_source, next_bot_open
from app.live_execution.grvt_strategy_close_service import GrvtStrategyCloseService
from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
from app.live_execution.grvt_testnet_bracket_preview import preview_bracket_entry, sign_reviewed_entry
from app.live_execution.grvt_testnet_bracket_submit import submit_bracket_signal_once
from app.live_execution.grvt_testnet_order import GrvtTestnetOrderClient
from app.live_execution.grvt_testnet_quote import fetch_testnet_quote
from app.live_execution.grvt_testnet_signer import fetch_testnet_instrument
from app.live_execution.grvt_testnet_strategy_close import _validate_signer
from app.portfolio.models import GrvtTestnetStop
from app.portfolio.vault import RealVault, VaultError


class _Revoked(RuntimeError): pass
class _Expired(RuntimeError): pass


@dataclass
class _BotJob:
    saved: dict
    generation: int
    ticket: tuple[int, int]
    status: str = 'WAITING_SIGNAL'
    active: bool = True
    parent: str | None = None
    selection: dict | None = None
    exit_ticket: tuple[int, int] | None = None
    stop: Event = field(default_factory=Event, repr=False)
    cause: str = 'NONE'
    archive_error: bool = False
    thread: Thread | None = field(default=None, repr=False)
    deadline_ns: int = 0
    last_ns: int = 0


class GrvtBotService:
    def __init__(self, vault: RealVault, closes: GrvtStrategyCloseService, *,
            client_factory: Callable[[str], GrvtTestnetOrderClient], public_post: Callable,
            clock_ns: Callable[[], int] = time.time_ns, poll_seconds: float = .5):
        self.vault = vault; self.closes = closes; self._factory = client_factory
        self._public = public_post; self._clock = clock_ns; self._poll = poll_seconds
        self._lock = RLock(); self._jobs: dict[str, _BotJob] = {}
        self._epoch = 0; self._subepochs: dict[str, int] = {}

    def permission_ticket(self, sub: str) -> tuple[int, int]:
        with self._lock: return self._epoch, self._subepochs.get(sub, 0)

    def require_permission(self, sub: str, ticket: tuple[int, int]) -> None:
        if ticket != self.permission_ticket(sub): raise GrvtTestnetBracketError('Permiso del ciclo retirado')

    def snapshot(self, run_id: str) -> dict:
        with self._lock:
            job = self._jobs.get(run_id)
            if job:
                return {**deepcopy(job.saved), 'selection':deepcopy(job.selection),
                    'active':job.active, 'status':job.status, 'stop_requested':job.stop.is_set(),
                    'archive_error':job.archive_error, 'permission_revision':list(job.ticket),
                    'permission_remaining_seconds':max(0,(job.deadline_ns-self._clock())//1_000_000_000) if job.active else 0}
        return {**read_bot_run(self.vault,run_id), 'active':False, 'permission_remaining_seconds':0,
            'permission_revision':None, 'archive_error':False}

    def history(self, sub: str, *, offset: int = 0, expected_generation: int | None = None) -> dict:
        from app.live_execution.grvt_bot_archive import bot_run_history
        generation = expected_generation if expected_generation is not None else self.vault.session_generation
        if generation is None: raise VaultError('Bóveda bloqueada')
        with self.vault.try_guard_session_generation(generation) as available:
            if available:
                page = bot_run_history(self.vault,sub,offset=offset)
                page['items'] = [self.snapshot(item['id']) for item in page['items']]
                from app.live_execution.grvt_stop_control import read_testnet_stop
                page['subaccount_stop'] = read_testnet_stop(self.vault,sub)
            else:
                # Autenticación/RPC puede retener la bóveda. Estado y STOP de
                # los trabajos RAM siguen disponibles, sin fingir un historial vacío.
                with self._lock:
                    ids = [run for run,job in self._jobs.items() if job.saved['sub_account_id'] == sub]
                page = {'items':[self.snapshot(run) for run in ids[-20:]],'offset':offset,'has_more':False,'subaccount_stop':None}
            return {'network':'TESTNET','authorization':False,'control_revision':list(self.permission_ticket(sub)),
                'history_pending':not available,**page}

    def start(self, paper, lab, session_id: str, *, sub_account_id: str, entry_cap: Decimal,
            exit_cap: Decimal, wait_seconds: int, exit_seconds: int, max_attempts: int,
            max_spread_bps: int, max_slippage_bps: int, api_key: str, private_key: str,
            expected_signer: str, expected_ticket: tuple[int,int], expected_generation: int | None = None,
            max_entries: int = 1, total_entry_cap: Decimal | None = None, loss_cap: Decimal | None = None) -> dict:
        _validate_signer(private_key,expected_signer)
        generation = expected_generation if expected_generation is not None else self.vault.session_generation
        if generation is None: raise GrvtTestnetBracketError('Bóveda bloqueada')
        self.require_permission(sub_account_id,expected_ticket)
        source = bot_source(paper,lab,session_id)
        def checked_configuration():
            self.require_permission(sub_account_id,expected_ticket)
            if self.vault.session_generation != generation:
                raise GrvtTestnetBracketError('Sesión del ciclo retirada')
            return self._clock()
        def configuration_public(url, body):
            checked_configuration()
            reply = self._public(url,body)
            checked_configuration()
            return reply
        # Sólo información pública: no concede permiso ni autentica mientras
        # espera. En la señal se vuelve a obtener y validar toda la evidencia.
        instrument = fetch_testnet_instrument(source['instrument'],http_post=configuration_public)
        quote = fetch_testnet_quote(source['instrument'],http_post=configuration_public,clock_ns=checked_configuration)
        try:
            automatic_entry_terms(source['definition'],instrument,quote,entry_cap=entry_cap,exit_cap=exit_cap,
                max_spread_bps=max_spread_bps,max_slippage_bps=max_slippage_bps,now_ns=checked_configuration())
        except GrvtTestnetBracketError:
            raise GrvtBotConfigurationError('Mínimos de GRVT o protección incompatibles; revisa los topes de entrada y salida') from None
        checked_configuration()
        if bot_source(paper,lab,session_id) != source:
            raise GrvtTestnetBracketError('El origen del ciclo cambió')
        with self.vault.guard_session_generation(generation), self._lock:
            self.require_permission(sub_account_id,expected_ticket)
            run = begin_bot_run(self.vault,source,sub_account_id=sub_account_id,entry_cap=entry_cap,exit_cap=exit_cap,
                wait_seconds=wait_seconds,exit_seconds=exit_seconds,max_attempts=max_attempts,
                max_spread_bps=max_spread_bps,max_slippage_bps=max_slippage_bps,now_ns=self._clock(),
                max_entries=max_entries,total_entry_cap=total_entry_cap,loss_cap=loss_cap)
            self._subepochs[sub_account_id] = self._subepochs.get(sub_account_id,0)+1
            ticket = self.permission_ticket(sub_account_id)
            saved = read_bot_run(self.vault,run)
            job = _BotJob(saved,generation,ticket,deadline_ns=saved['entry_deadline_ns'],last_ns=saved['started_at_ns'])
            self._jobs[run] = job
        try:
            thread = Thread(target=self._run,args=(job,paper,lab,api_key,private_key,expected_signer),
                name='orbis-grvt-bot-'+run[-6:],daemon=True)
            with self._lock: job.thread = thread
            thread.start()
        except Exception:
            with self._lock: job.active = False; job.status = 'NEEDS_REVIEW'
            self._finish(job)
            raise GrvtTestnetBracketError('No se pudo iniciar el ciclo') from None
        return self.snapshot(run)

    def stop(self, run_id: str, *, cause: str = 'USER_STOP') -> dict:
        with self._lock:
            job = self._jobs.get(run_id)
            if job:
                sub = job.saved['sub_account_id']
                if job.active:
                    self._subepochs[sub] = self._subepochs.get(sub,0)+1
                    job.stop.set()
                    if job.status not in STATUSES: job.status = 'STOP_REQUESTED'
                    if job.cause == 'NONE': job.cause = cause
                parent = job.parent
            else:
                parent = None
        if job:
            if parent: self.closes.stop(parent,cause=cause)
        else:
            # Recuperación explícita después de reiniciar: sólo retira la
            # reserva local. No cancela protección ni declara cerrada la cuenta.
            saved = read_bot_run(self.vault,run_id)
            if saved['terminal'] is None:
                finish_bot_run(self.vault,run_id,status='STOPPED',cause=cause,now_ns=self._clock())
        return self.snapshot(run_id)

    def stop_all(self, cause: str = 'REVOKED') -> None:
        with self._lock:
            self._epoch += 1; self._subepochs.clear()
            parents = []
            for job in self._jobs.values():
                if job.active:
                    job.stop.set()
                    if job.status not in STATUSES: job.status = 'STOP_REQUESTED'
                    if job.cause == 'NONE': job.cause = cause
                    if job.parent: parents.append(job.parent)
        for parent in parents: self.closes.stop(parent,cause=cause)

    def stop_subaccount(self, sub: str) -> None:
        """Revoca incluso un /start retrasado que aún no tiene ID visible."""
        with self._lock:
            self._subepochs[sub] = self._subepochs.get(sub,0)+1
            ids = [run for run,job in self._jobs.items() if job.saved['sub_account_id'] == sub and job.active]
        for run in ids: self.stop(run)

    def rearm_finished(self, run_id: str, *, expected_ticket: tuple[int,int], expected_stop_revision: int,
            expected_generation: int | None = None) -> dict:
        """Nueva acción expresa; sólo libera STOP automático después de un final plano.

        No arranca un trabajo, no restaura claves ni reutiliza el permiso anterior.
        La próxima activación necesitará la revisión que resulte de esta acción.
        """
        from sqlalchemy import select
        from app.portfolio.models import GrvtTestnetBotRun
        from app.live_execution.grvt_bot_archive import _read
        from app.live_execution.grvt_stop_control import release_automatic_stop
        generation = expected_generation if expected_generation is not None else self.vault.session_generation
        if generation is None: raise VaultError('Bóveda bloqueada')
        with self.vault.guard_session_generation(generation), self.vault.session() as db:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            row = db.get(GrvtTestnetBotRun,run_id); saved = _read(db,row); sub = saved['sub_account_id']
            self.require_permission(sub,expected_ticket)
            if (saved['status'] not in ('COMPLETE','ENTRY_LIMIT','BUDGET_LIMIT','LOSS_LIMIT')
                    or saved['selection'] is None or db.scalar(select(GrvtTestnetBotRun.id).where(
                        GrvtTestnetBotRun.active_slot == sub).limit(1)) is not None
                    or type(expected_stop_revision) is not int or expected_stop_revision < 1):
                raise GrvtTestnetBracketError('El ciclo no permite rearmar la subcuenta')
            result = release_automatic_stop(db,sub,saved['selection']['parent_client_order_id'],
                expected_revision=expected_stop_revision,now_ns=self._clock())
            with self._lock:
                self.require_permission(sub,expected_ticket)
                self._subepochs[sub] = self._subepochs.get(sub,0)+1
                db.commit()
            return {'network':'TESTNET','authorization':False,**result,'control_revision':list(self.permission_ticket(sub))}

    def _checked(self, job: _BotJob) -> int:
        if (job.stop.is_set() or self.vault.session_generation != job.generation
                or self.permission_ticket(job.saved['sub_account_id']) != job.ticket): raise _Revoked()
        at = self._clock()
        if type(at) is not int or at < job.last_ns:
            raise GrvtTestnetBracketError('Reloj del ciclo no verificable')
        job.last_ns = at
        if at > job.deadline_ns: raise _Expired()
        return at

    def _finish(self, job: _BotJob) -> None:
        try:
            # El trabajador puede esperar una lectura local. Una colisión de
            # cerrojo no es una revocación y no debe perder el resultado final.
            # STOP ya fue aplicado a RAM; la ruta de parada no espera este tramo.
            with self.vault.guard_session_generation(job.generation):
                finish_bot_run(self.vault,job.saved['id'],status=job.status,cause=job.cause,
                    now_ns=max(job.last_ns,self._clock()))
                saved = read_bot_run(self.vault,job.saved['id'])
                with self._lock: job.saved = saved
        except Exception:
            with self._lock: job.archive_error = True

    def _execute_episode(self, job: _BotJob, paper, lab, api_key: str, private_key: str,
            expected_signer: str, *, armed_at_ns: int) -> None:
        saved = job.saved; source = saved['source']; client = None
        def checked(): return self._checked(job)
        def public(url, body):
            checked(); response = self._public(url,body); checked(); return response
        while True:
            at = checked()
            with self.vault.guard_session_generation(job.generation):
                with self.vault.session() as db:
                    stop = db.get(GrvtTestnetStop,saved['sub_account_id'])
                    if stop and stop.stopped: raise _Revoked()
                candidate = next_bot_open(paper,source,armed_at_ns=armed_at_ns,now_ns=at)
                if candidate is not None:
                    if bot_source(paper,lab,source['session_id']) != source:
                        raise GrvtTestnetBracketError('El origen del ciclo cambió')
                    checked()
                    select_bot_open(self.vault,saved['id'],candidate,now_ns=checked())
                    with self._lock:
                        job.parent = candidate['parent_client_order_id']
                        selected = read_bot_run(self.vault,saved['id'])
                        job.saved = selected; job.selection = selected['selection']
                        job.exit_ticket = self.closes.permission_ticket(job.parent)
                        job.status = 'PREPARING_ENTRY'
                    break
            job.stop.wait(self._poll)
        with self.vault.guard_session_generation(job.generation):
            checked()
            instrument = fetch_testnet_instrument(source['instrument'],http_post=public)
            quote = fetch_testnet_quote(source['instrument'],http_post=public,clock_ns=checked)
            terms = automatic_entry_terms(source['definition'],instrument,quote,
                entry_cap=Decimal(saved['entry_cap']),exit_cap=Decimal(saved['exit_cap']),max_spread_bps=saved['max_spread_bps'],
                max_slippage_bps=saved['max_slippage_bps'],now_ns=checked())
            checked(); client = self._factory(api_key); checked()
            preview = preview_bracket_entry(paper,lab,self.vault,client,sub=saved['sub_account_id'],
                session_id=source['session_id'],action_request_id=candidate['action_request_id'],
                size=terms.size,limit_price=terms.limit_price,entry_cap=Decimal(saved['entry_cap']),
                exit_cap=Decimal(saved['exit_cap']),stop_loss=terms.stop_loss,take_profit=terms.take_profit,
                ttl_minutes=(saved['exit_seconds']+179)//60,max_spread_bps=saved['max_spread_bps'],
                max_slippage_bps=saved['max_slippage_bps'],public_post=public,clock_ns=checked)
            orders, entry_limits, exit_limits, instrument = sign_reviewed_entry(self.vault,preview,
                private_key=private_key,expected_signer=expected_signer,now_ns=checked())
            with self._lock: job.status = 'SENDING_ENTRY'
            submit_bracket_signal_once(paper,lab,self.vault,client,orders,entry_limits,exit_limits,instrument,
                session_id=source['session_id'],action_request_id=candidate['action_request_id'],
                max_spread_bps=saved['max_spread_bps'],max_slippage_bps=saved['max_slippage_bps'],
                public_post=public,clock_ns=checked)
            checked()
            # El permiso de salida procede de la misma activación, sin
            # extender la espera ni tomar una revisión posterior a STOP.
            job.deadline_ns = checked()+saved['exit_seconds']*1_000_000_000
            self.closes.start_watch(job.parent,max_attempts=saved['max_attempts'],ttl_seconds=saved['exit_seconds'],
                api_key=api_key,private_key=private_key,expected_signer=expected_signer,
                expected_generation=job.generation,expected_ticket=job.exit_ticket,deadline_ns=job.deadline_ns)
        while True:
            child = self.closes.state(job.parent)
            if child is None: raise GrvtTestnetBracketError('Seguimiento de salida ausente')
            if not child['active']:
                with self._lock: job.status = child['status'] if child['status'] in STATUSES else 'NEEDS_REVIEW'
                break
            checked()
            with self._lock: job.status = child['status']
            job.stop.wait(self._poll)

    def _run(self, job: _BotJob, paper, lab, api_key: str, private_key: str, expected_signer: str) -> None:
        try:
            header = job.saved; bounded = header['schema'] == 'orbis.grvt.testnet.bot.run.v2'
            armed = header['started_at_ns']
            while True:
                with self._lock:
                    job.deadline_ns = header['entry_deadline_ns']; job.status = 'WAITING_SIGNAL'
                self._execute_episode(job,paper,lab,api_key,private_key,expected_signer,armed_at_ns=armed)
                if job.status != 'COMPLETE' or not bounded: break
                self._checked(job)
                from app.live_execution.grvt_bot_cycles import complete_bot_cycle,rearm_bot_cycle,limit_status
                with self.vault.guard_session_generation(job.generation):
                    saved = complete_bot_cycle(self.vault,header['id'],now_ns=self._checked(job))
                    with self._lock: job.saved = saved
                    limit = limit_status(saved)
                    if limit:
                        with self._lock: job.status = limit
                        break
                    with self._lock: job.deadline_ns = header['entry_deadline_ns']; job.status = 'REARMING'
                    self._checked(job)
                    saved = rearm_bot_cycle(self.vault,header['id'],clock_ns=self._clock,
                        require_permission=lambda:self._checked(job))
                    armed = saved['cycles'][-1]['rearm']['rearmed_at_ns']
                    with self._lock: job.saved = saved; job.parent = None; job.exit_ticket = None
        except (_Revoked, VaultError):
            with self._lock: job.status = 'STOPPED'
        except _Expired:
            with self._lock: job.status = 'EXPIRED'
        except Exception:
            revoked = job.stop.is_set() or self.vault.session_generation != job.generation
            with self._lock: job.status = 'STOPPED' if revoked else 'NEEDS_REVIEW'
        finally:
            if job.parent and job.status != 'COMPLETE': self.closes.stop(job.parent,cause=job.cause if job.cause != 'NONE' else 'REVOKED')
            self._finish(job)
            with self._lock: job.active = False; job.thread = None
            del api_key, private_key, expected_signer
