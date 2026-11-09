"""CLOSE PAPER → retirada de protecciones → recibo → reducción del residuo TESTNET.

Cada llamada avanza un paso; las esperas entre auditorías no bloquean el hilo.
La solicitud sobrevive a un reinicio, las credenciales no. No abre posiciones,
levanta STOP ni interpreta una señal PAPER como prueba de una ejecución real.
"""

from contextlib import contextmanager
from collections.abc import Callable,Iterator
from datetime import datetime,timezone,timedelta
from decimal import Decimal
import json
import os
import re
import stat
import time
from threading import Event
from typing import Any,Literal,TypedDict,cast

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.derivatives.paper_store import PaperPerpStore
from app.lab.store import LabStore
from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
from app.live_execution.grvt_testnet_bracket_journal import _verified,read_bracket
from app.live_execution.grvt_testnet_oco import _encode,_stamp
from app.live_execution.grvt_testnet_order import GrvtTestnetLimits,GrvtTestnetOrderClient
from app.live_execution.grvt_testnet_preflight import _signal_source
from app.live_execution.grvt_testnet_signal import reviewed_testnet_signal
from app.live_execution.grvt_filled_close import FilledCloseRequest,verified_filled_close
from app.live_execution.grvt_native_partial_close import NativePartialCloseRequest,SCHEMA as NATIVE_PARTIAL_SCHEMA,verified_native_partial_close
from app.live_execution.grvt_native_recovery_close import NativeRecoveryCloseRequest,SCHEMA as NATIVE_RECOVERY_SCHEMA,verified_native_recovery_close
from app.portfolio.models import GrvtTestnetBracketGroup,GrvtTestnetStrategyClose,GrvtTestnetStop
from app.portfolio.vault import RealVault


CloseStatus=Literal['COMPLETE','EXPIRED','ATTEMPT_LIMIT','REVIEW_UNSENT','CANCELLING',
    'WAITING_AUDIT','GROUP_RECONCILED','AUDITING_GROUP','AUDITING_CLOSE',
    'CLOSE_RECONCILED','CLOSE_SENT','STOPPED','RECOVERING']
PublicPost=Callable[[str,dict[str,Any]],httpx.Response]


class StrategyCloseRequest(TypedDict):
    schema: Literal['orbis.grvt.testnet.strategy.close.v1']
    parent_client_order_id: str
    plan_digest: str
    close_source: dict[str,str]
    paper_position_id: str
    created_at_ns: int
    deadline_ns: int
    max_attempts: int


class StrategyCloseProgress(TypedDict):
    network: Literal['TESTNET']
    parent_client_order_id: str
    status: CloseStatus
    remaining_position: str|None
    position_basis: Literal['LAST_RECONCILED','UNVERIFIED']
    mainnet_order_sent: Literal[False]


def _progress(parent: str,status: CloseStatus,remaining: str|None=None) -> StrategyCloseProgress:
    return {'network':'TESTNET','parent_client_order_id':parent,'status':status,
        'remaining_position':remaining,'position_basis':'LAST_RECONCILED' if remaining is not None else 'UNVERIFIED',
        'mainnet_order_sent':False}


@contextmanager
def _exclusive(vault: RealVault) -> Iterator[None]:
    # Se serializa la secuencia completa entre conexiones/procesos, sin
    # mantener una transacción SQLite durante otros servicios del diario.
    try:
        import fcntl
    except ImportError:
        raise GrvtTestnetBracketError('El coordinador requiere bloqueo de archivos del sistema') from None
    path=vault.path.with_name(vault.path.name+'.grvt-close.lock')
    fd=os.open(path,os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode): raise GrvtTestnetBracketError('Bloqueo de cierre inválido')
        os.fchmod(fd,0o600)
        try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise GrvtTestnetBracketError('Otro paso de cierre sigue en curso') from None
        yield
    finally: os.close(fd)


def _request(db: Session,row: GrvtTestnetStrategyClose) -> StrategyCloseRequest|FilledCloseRequest|NativePartialCloseRequest|NativeRecoveryCloseRequest:
    try:
        p=json.loads(row.payload_json)
        if p.get('schema')=='orbis.grvt.testnet.strategy.close.v2': return verified_filled_close(db,row,p)
        if p.get('schema')==NATIVE_PARTIAL_SCHEMA: return verified_native_partial_close(db,row,p)
        if p.get('schema')==NATIVE_RECOVERY_SCHEMA: return verified_native_recovery_close(db,row,p)
        if (_encode(p)!=(row.payload_json,row.payload_digest)
                or set(p)!={'schema','parent_client_order_id','plan_digest','close_source','paper_position_id',
                    'created_at_ns','deadline_ns','max_attempts'}
                or p['schema']!='orbis.grvt.testnet.strategy.close.v1'
                or row.parent_client_order_id!=p['parent_client_order_id']): raise ValueError('request')
        group=db.get(GrvtTestnetBracketGroup,row.parent_client_order_id); plan=_verified(db,group)
        source=_signal_source(p['close_source'],row.close_client_order_id)
        if (source['action']!='CLOSE' or any(source[key]!=plan['signal_source'][key] for key in
                ('kind','session_id','direction','version_id','dataset_id','dataset_hash'))
                or p['plan_digest']!=group.payload_digest
                or type(p['max_attempts']) is not int or not 1<=p['max_attempts']<=3
                or not isinstance(p['paper_position_id'],str) or len(p['paper_position_id'])!=36
                or type(p['deadline_ns']) is not int
                or not 0<p['deadline_ns']-p['created_at_ns']<=300_000_000_000):
            raise ValueError('scope')
        _stamp(p['created_at_ns']); _stamp(p['deadline_ns'])
        return cast(StrategyCloseRequest,p)
    except Exception:
        raise GrvtTestnetBracketError('Solicitud de cierre de estrategia no verificable') from None


def verify_all_strategy_closes(db: Session) -> None:
    for row in db.scalars(select(GrvtTestnetStrategyClose)): _request(db,row)


def read_strategy_close(vault: RealVault,parent: str) -> StrategyCloseRequest|FilledCloseRequest|NativePartialCloseRequest|NativeRecoveryCloseRequest|None:
    with vault.session() as db:
        row=db.get(GrvtTestnetStrategyClose,parent)
        return _request(db,row) if row is not None else None


def request_strategy_close(paper: PaperPerpStore,lab: LabStore,vault: RealVault,parent: str,*,
        action_request_id: str,max_attempts: int,deadline_ns: int,clock_ns: Callable[[],int]=time.time_ns) -> StrategyCloseRequest:
    now=clock_ns(); _stamp(now)
    if (type(max_attempts) is not int or not 1<=max_attempts<=3 or type(deadline_ns) is not int
            or not 0<deadline_ns-now<=300_000_000_000):
        raise GrvtTestnetBracketError('Límites de cierre inválidos')
    with _exclusive(vault):
        existing=read_strategy_close(vault,parent)
        if existing is not None:
            if existing['schema']!='orbis.grvt.testnet.strategy.close.v1':
                raise GrvtTestnetBracketError('El grupo ya está vinculado a un cierre sobre fills')
            if existing['close_source']['action_request_id']!=action_request_id:
                raise GrvtTestnetBracketError('El grupo ya está vinculado a otro cierre')
            return existing  # No prolonga plazo ni aumenta intentos al reintentar.
        plan=read_bracket(vault,parent); opening=plan['signal_source']
        if plan['emergency_attempts']: raise GrvtTestnetBracketError('Hay reducciones anteriores; revisa el grupo')
        limits=GrvtTestnetLimits(plan['sub_account_id'],plan['instrument'],Decimal(plan['exit_cap']))
        side='SELL' if opening['direction']=='LONG' else 'BUY'
        with reviewed_testnet_signal(paper,lab,limits,session_id=opening['session_id'],
                action_request_id=action_request_id,side=side,reduce_only=True,now_ns=now,clock_ns=clock_ns
                ) as (_,source,_,_,fresh):
            # Dos operaciones de una misma versión/sesión no son la misma posición.
            with paper.connect() as db:
                actions=list(db.execute('SELECT * FROM paper_signal_actions WHERE session_id=? AND request_id IN (?,?)',
                    (opening['session_id'],opening['action_request_id'],action_request_id)))
                if len(actions)!=2 or actions[0]['position_id']!=actions[1]['position_id']:
                    raise GrvtTestnetBracketError('El cierre no pertenece a la posición de entrada')
                original=next(row for row in actions if row['request_id']==opening['action_request_id'])
                digest=paper.digest({'session_id':opening['session_id'],'candle_t':original['candle_t'],
                    'action':'OPEN','position_id':None,'request_id':original['request_id']})
                booked=db.execute('SELECT digest FROM paper_perp_requests WHERE request_id=?',
                    (original['request_id'],)).fetchone()
                event=db.execute('''SELECT e.kind FROM paper_perp_events e JOIN paper_perp_positions p ON p.id=e.position_id
                    WHERE e.request_id=? AND e.position_id=? AND e.account_id=p.account_id AND p.origin_session_id=?''',
                    (original['request_id'],original['position_id'],opening['session_id'])).fetchone()
                if (original['action']!='OPEN' or original['status']!='EXECUTED' or original['digest']!=digest
                        or booked is None or booked['digest']!=digest or event is None or event['kind']!='OPEN'):
                    raise GrvtTestnetBracketError('Asiento de entrada PAPER no verificable')
            with vault.session() as db:
                try:
                    db.connection().exec_driver_sql('BEGIN IMMEDIATE')
                    group=db.get(GrvtTestnetBracketGroup,parent); current=_verified(db,group)
                    if current['signal_source']!=opening: raise GrvtTestnetBracketError('Entrada cambiada')
                    at=fresh()
                    payload: StrategyCloseRequest={'schema':'orbis.grvt.testnet.strategy.close.v1','parent_client_order_id':parent,
                        'plan_digest':group.payload_digest,'close_source':source,'paper_position_id':actions[0]['position_id'],
                        'created_at_ns':at,'deadline_ns':deadline_ns,'max_attempts':max_attempts}
                    encoded,digest=_encode(payload)
                    row=GrvtTestnetStrategyClose(parent_client_order_id=parent,close_client_order_id=source['client_order_id'],
                        payload_json=encoded,payload_digest=digest)
                    from app.live_execution.grvt_stop_control import record_testnet_stop
                    record_testnet_stop(db,group.sub_account_id,now=_stamp(at),reason='AUTO_EXIT',parent_client_order_id=parent)
                    db.add(row); db.flush(); _request(db,row); db.commit(); return payload
                except Exception: db.rollback(); raise


def advance_strategy_close(vault: RealVault,client: GrvtTestnetOrderClient,parent: str,*,private_key: str,
        expected_signer: str,public_post: PublicPost,clock_ns: Callable[[],int]=time.time_ns,
        submission_client_factory: Callable[[],GrvtTestnetOrderClient]|None=None) -> StrategyCloseProgress:
    """Credenciales explícitas por paso; ningún reenvío de un claim consumido."""
    generation=vault.session_generation
    if generation is None: raise GrvtTestnetBracketError('Bóveda bloqueada')
    with _exclusive(vault),vault.guard_session_generation(generation):
        request=read_strategy_close(vault,parent)
        if request is None: raise GrvtTestnetBracketError('Falta una solicitud de cierre verificada')
        now=clock_ns(); _stamp(now)
        def result(status: CloseStatus,remaining: str|None=None) -> StrategyCloseProgress:
            return _progress(parent,status,remaining)
        if now<request['created_at_ns']: raise GrvtTestnetBracketError('Reloj anterior a la solicitud')
        group=read_bracket(vault,parent)
        attempts=group['emergency_attempts']
        from app.live_execution.grvt_testnet_recovery import read_testnet_recovery
        recovery={item.client_order_id:item for item in read_testnet_recovery(vault,group['sub_account_id']).items}
        receipt=(recovery[attempts[-1]['client_order_id']].reconciliation if attempts else group['reconciliation'])
        if receipt is not None and receipt['result']['positions']==[]: return result('COMPLETE','0')
        remaining=receipt['result']['positions'][0][1] if receipt is not None else None
        if now>request['deadline_ns']: return result('EXPIRED',remaining)
        _validate_signer(private_key,expected_signer)
        last=now
        def checked():
            nonlocal last
            at=clock_ns(); _stamp(at)
            if not last<=at<=request['deadline_ns']:
                raise GrvtTestnetBracketError('Plazo de cierre vencido o reloj regresado')
            last=at
            return at
        from app.live_execution.grvt_testnet_journal import stop_testnet_subaccount
        stop_testnet_subaccount(vault,group['sub_account_id'],now=_datetime(now),
            reason='AUTO_EXIT',parent_client_order_id=parent)
        if group['reconciliation'] is None:
            from app.live_execution.grvt_testnet_bracket_observation import observe_bracket_orders
            from app.live_execution.grvt_testnet_bracket_cancel import cancel_bracket_once
            from app.live_execution.grvt_testnet_bracket_finance import read_bracket_audits,audit_bracket,reconcile_bracket
            observed=observe_bracket_orders(vault,client,parent,clock_ns=checked)
            if any(row['status'] not in ('FILLED','CANCELLED','REJECTED') for row in observed['orders']):
                if group['cancel_attempt'] is None:
                    cancel_bracket_once(vault,client,parent,acknowledge_unprotected_position=True,clock_ns=checked)
                return result('CANCELLING')
            audits=read_bracket_audits(vault,parent)
            if audits and now-audits[-1]['observed_at_ns']<5_000_000_000: return result('WAITING_AUDIT')
            audit_bracket(vault,client,parent,clock_ns=checked)
            if len(audits)>=1:
                reconcile_bracket(vault,parent,now_ns=checked())
                return result('GROUP_RECONCILED')
            return result('AUDITING_GROUP')
        if attempts and receipt is None:
            from app.live_execution.grvt_testnet_submit import audit_testnet_intent
            from app.live_execution.grvt_testnet_reconciliation import reconcile_testnet_order,GrvtTestnetReconciliationError,_ns
            from app.portfolio.models import GrvtTestnetAuditEvidence
            current=recovery[attempts[-1]['client_order_id']]
            if current.journal_status=='PREPARED': return result('REVIEW_UNSENT')
            with vault.session() as db:
                latest=db.scalar(select(GrvtTestnetAuditEvidence.observed_at).where(
                    GrvtTestnetAuditEvidence.client_order_id==current.client_order_id).order_by(
                        GrvtTestnetAuditEvidence.observed_at.desc()).limit(1))
            if latest is not None and now-_ns(latest)<5_000_000_000: return result('WAITING_AUDIT')
            # El servicio de conciliación valida separación/frescura de las
            # auditorías. Consultar/auditar nunca repite el POST perdido.
            audit_testnet_intent(vault,client,group['sub_account_id'],current.client_order_id,clock_ns=checked)
            try:
                reconcile_testnet_order(vault,group['sub_account_id'],current.client_order_id,now=_datetime(checked()))
            except GrvtTestnetReconciliationError:
                return result('AUDITING_CLOSE')
            return result('CLOSE_RECONCILED')
        if len(attempts)>=request['max_attempts']: return result('ATTEMPT_LIMIT',remaining)
        submission_client=client
        if not client.creation_available:
            if submission_client_factory is None: return result('REVIEW_UNSENT',remaining)
            checked(); submission_client=submission_client_factory()
            if not submission_client.creation_available:
                raise GrvtTestnetBracketError('El transporte de reducción ya está consumido')
        from app.live_execution.grvt_testnet_bracket_preview import preview_bracket_emergency,sign_reviewed_emergency
        from app.live_execution.grvt_testnet_bracket_emergency import submit_bracket_emergency_once
        preview=preview_bracket_emergency(vault,client,parent,public_post=public_post,clock_ns=checked)
        signed=sign_reviewed_emergency(vault,preview,private_key=private_key,expected_signer=expected_signer,now_ns=checked())
        submit_bracket_emergency_once(vault,submission_client,parent,signed,public_post=public_post,clock_ns=checked)
        # La siguiente iteración observa por ID con el lector original; cada
        # transporte de creación adicional muere sin resetear su guardia.
        return result('CLOSE_SENT')


def _datetime(ns: int) -> datetime:
    return datetime.fromtimestamp(ns//1_000_000_000,timezone.utc)+timedelta(microseconds=ns%1_000_000_000//1000)


def _validate_signer(private_key: str,expected_signer: str) -> None:
    from eth_account import Account
    try:
        if (not isinstance(private_key,str) or not re.fullmatch(r'(?:0x)?[a-fA-F0-9]{64}',private_key)
                or not isinstance(expected_signer,str) or not re.fullmatch(r'0x[a-fA-F0-9]{40}',expected_signer)
                or Account.from_key(bytes.fromhex(private_key.removeprefix('0x'))).address.lower()!=expected_signer.lower()):
            raise ValueError('signer')
    except Exception:
        raise GrvtTestnetBracketError('Clave dedicada TESTNET y firmante no coinciden') from None


def run_strategy_close(vault: RealVault,client: GrvtTestnetOrderClient,parent: str,*,private_key: str,
        expected_signer: str,public_post: PublicPost,clock_ns: Callable[[],int]=time.time_ns,
        stop_event: Event|None=None,wait: Callable[[float],bool]|None=None,
        on_progress: Callable[[StrategyCloseProgress],None]|None=None,
        submission_client_factory: Callable[[],GrvtTestnetOrderClient]|None=None) -> StrategyCloseProgress:
    """Ejecuta sólo el cierre ya solicitado, con plazo y cancelación.

    No observa nuevas señales ni activa entradas. El llamador suministra
    credenciales efímeras y debe mostrar que la operación sigue en curso.
    Un bloqueo/reapertura de la bóveda revoca esta sesión del ejecutor.
    """
    from app.live_execution.grvt_testnet_order import GrvtTestnetOrderError
    stop_event=stop_event or Event(); wait=wait or stop_event.wait
    generation=vault.session_generation
    if generation is None: raise GrvtTestnetBracketError('Bóveda bloqueada')
    request=read_strategy_close(vault,parent)
    if request is None: raise GrvtTestnetBracketError('Falta una solicitud de cierre verificada')
    duration=max(0,(request['deadline_ns']-clock_ns())/1_000_000_000)
    monotonic_deadline=time.monotonic()+duration
    terminal={'COMPLETE','EXPIRED','ATTEMPT_LIMIT','REVIEW_UNSENT'}
    def claims():
        group=read_bracket(vault,parent)
        return (bool(group['cancel_attempt']),frozenset(item['client_order_id']
            for item in group['emergency_attempts'] if item['status']!='PREPARED'))
    try:
        for _ in range(302):
            if stop_event.is_set(): return _progress(parent,'STOPPED')
            with vault.guard_session_generation(generation):
                before=claims()
                try:
                    result=advance_strategy_close(vault,client,parent,private_key=private_key,
                        expected_signer=expected_signer,public_post=public_post,clock_ns=clock_ns,
                        submission_client_factory=submission_client_factory)
                except GrvtTestnetOrderError:
                    # Sólo un claim nuevo justifica pasar a recuperación tras
                    # perder una respuesta. Un fallo de consulta no reenvía.
                    if claims()==before: raise
                    result=_progress(parent,'RECOVERING')
            if on_progress is not None: on_progress(result)
            if result['status'] in terminal: return result
            if time.monotonic()>=monotonic_deadline:
                return _progress(parent,'EXPIRED')
            if wait(1): return _progress(parent,'STOPPED')
        raise GrvtTestnetBracketError('Límite de observaciones del cierre alcanzado')
    finally:
        # No se conservan en estado global; no se promete borrado físico de
        # strings de Python. La siguiente ejecución requiere otras entradas.
        del private_key,expected_signer,client
