"""Supervised reductions and financial closure for a verified LIMIT position."""
from collections.abc import Callable
import re
import secrets
import time

from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError, client_ids
from app.live_execution.grvt_mainnet_finance import _histories
from app.live_execution.grvt_mainnet_journal import _control, encode, signer_ref
from app.live_execution.grvt_mainnet_preflight import _state, canonical, number
from app.live_execution.grvt_mainnet_reduction_order import reduction_price, sign_reduction
from app.live_execution.grvt_mainnet_transport import TICKER_URL, _result, fetch_mainnet_instrument, public_post as default_public_post
from app.live_execution.grvt_perp_limit_execution import _client, _matched, _observe, _permission
from app.live_execution.grvt_perp_limit_exit_evidence import (
    calculate_limit_exit_audit, limit_reduction_template, reduction_scope, stable_exit_audits,
)
from app.live_execution.grvt_perp_limit_exit_journal import (
    CLAIM_SCHEMA, PARTIAL_CLAIM_SCHEMA, partial_limit_template, require_limit_close_decision,
)
from app.live_execution.grvt_perp_limit_journal import (
    _plan, append_limit_event, limit_client_id_reserved, read_limit_attempt, stamp, verified_limit_attempt,
)
from app.live_execution.grvt_perp_limit_plan import FRESH_NS, NS
from app.live_execution.grvt_perp_limit_receipt import lookup_limit_order
from app.portfolio.models import (
    GrvtMainnetBotSelection, GrvtMainnetBracket, GrvtMainnetLeg, GrvtMainnetLimitAttempt,
    GrvtMainnetLimitReduction, GrvtMainnetLimitSlot, GrvtMainnetReduction,
)


def _current(db, cid: str, saved: dict):
    row = db.get(GrvtMainnetLimitAttempt,cid)
    current = verified_limit_attempt(db,row)
    if (current['claim_digest'] != saved['claim_digest']
            or current['events']['head'] != saved['events']['head'] or not current['slot_reserved']):
        raise GrvtMainnetError('El intento LIMIT cambió durante la salida')
    return row,current


def audit_limit_exit(vault, cid: str, *, client_factory: Callable, require_permission: Callable,
                     clock_ns: Callable = time.time_ns) -> dict:
    generation,checked = _permission(vault,require_permission,clock_ns)
    saved = read_limit_attempt(vault,cid)
    if not saved['slot_reserved']:
        return saved
    entry = saved['events']['receipt']
    if entry is None or not entry['terminal']:
        saved = _observe(vault,cid,client_factory,generation=generation,checked=checked)
        entry = saved['events']['receipt']
    if not entry['terminal'] or number(entry['filled_size']) <= 0:
        raise GrvtMainnetError('La salida LIMIT necesita una entrada terminal con fills')
    sub = saved['sub_account_id']
    client = _client(client_factory,sub,checked)
    with vault.session() as db:
        control = _control(db,sub)
        _matched(client,saved,control)
        salt = control.identity_salt
    claim = saved['claim']
    plan = _plan(claim,claim['claimed_at_ns'])
    reductions = saved['events']['reductions']
    try:
        orders = []
        for item in reductions:
            reduction = item['claim']
            order = lookup_limit_order(client,sub,reduction['client_order_id'],
                instrument=plan.source.terms.instrument,issued_at_ns=reduction['issued_at_ns'],
                expires_at_ns=reduction['expiration_ns'],checked=checked)
            orders.append({'received_at_ns':str(checked()),'order':order})
        cutoff = checked()
        baseline = claim['preflight']['market']['states'][-1]['observed_at_ns']
        histories = _histories(client,sub,baseline,cutoff,checked)

        class CheckedAccount:
            def read_account(self, route, scope):
                checked()
                reply = client.read_account(route,scope)
                checked()
                return reply

        account = _state(CheckedAccount(),sub,checked,allow_position=plan.source.terms.instrument)
        source = {'cutoff_ns':cutoff,'observed_at_ns':checked(),'orders':orders,'histories':histories,'account':account}
        result = calculate_limit_exit_audit(plan,claim,entry,reductions,source,salt=salt)
        with vault.guard_session_generation(generation), vault.session() as db:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            row,current = _current(db,cid,saved)
            _matched(client,current,_control(db,sub))
            append_limit_event(db,row,'EXIT_AUDIT',{'source':source,'result':result},checked())
            db.commit()
    except GrvtMainnetError:
        raise
    except Exception:
        raise GrvtMainnetError('La posición LIMIT o sus fills requieren revisión') from None
    return read_limit_attempt(vault,cid)


def _quote(plan, public_post, checked) -> dict:
    name = plan.source.terms.instrument
    checked()
    raw = _result(public_post(TICKER_URL,{'instrument':name}))['result']
    at = checked()
    if not isinstance(raw.get('event_time'),str) or not re.fullmatch(r'[0-9]{1,20}',raw['event_time']):
        raise ValueError('Hora de cotización de salida LIMIT inválida')
    fields = {'bid':'best_bid_price','ask':'best_ask_price','bid_size':'best_bid_size','ask_size':'best_ask_size'}
    return {'source':TICKER_URL,'instrument':raw['instrument'],'event_time_ns':int(raw['event_time']),
            'observed_at_ns':at,**{name:canonical(number(raw[field])) for name,field in fields.items()}}


def _reserved(db, cid: str) -> bool:
    return (limit_client_id_reserved(db,cid) or any(db.get(model,cid) is not None for model in
        (GrvtMainnetLeg,GrvtMainnetBracket,GrvtMainnetReduction,GrvtMainnetBotSelection)))


def _recent_audits(events: dict, at_ns: int) -> tuple[dict, dict]:
    try:
        return stable_exit_audits(events['exit_audits'],now_ns=at_ns)
    except ValueError as exc:
        raise GrvtMainnetError(str(exc)) from None


def submit_limit_reduction_once(vault, cid: str, *, reduction_client_order_id: str, deadline_ns: int,
                               client_factory: Callable, require_permission: Callable, confirmation: str,
                               private_key: bytes, expected_signer: str,
                               public_post: Callable = default_public_post, clock_ns: Callable = time.time_ns,
                               supervision_head: str | None = None) -> dict:
    if confirmation not in ('REDUCIR_POSICION_LIMIT_GRVT_MAINNET','REDUCIR_POSICION_LIMIT_PARCIALES_GRVT_MAINNET'):
        raise GrvtMainnetError('Falta la autorización explícita de reducción LIMIT')
    new_id = client_ids(reduction_client_order_id)[0]
    generation,checked = _permission(vault,require_permission,clock_ns)
    saved = read_limit_attempt(vault,cid)
    events = saved['events']
    entry_claim = saved['claim']
    plan = _plan(entry_claim,entry_claim['claimed_at_ns'])
    partial = plan.revision==2
    if partial != (confirmation=='REDUCIR_POSICION_LIMIT_PARCIALES_GRVT_MAINNET'):
        raise GrvtMainnetError('La autorización de reducción no corresponde al perfil LIMIT')
    if partial and (type(supervision_head) is not str or events['supervision_head'] != supervision_head):
        raise GrvtMainnetError('Falta la supervisión vigente para reducir el perfil LIMIT con parciales')
    if not partial and supervision_head is not None:
        try:
            require_limit_close_decision(events,supervision_head)
        except ValueError as exc:
            raise GrvtMainnetError(str(exc)) from None
    first,last = _recent_audits(events,checked())
    remaining = number(last['result']['remaining_size'])
    if remaining <= 0 or not saved['slot_reserved']:
        raise GrvtMainnetError('La posición LIMIT ya está plana')
    sub = saved['sub_account_id']
    instrument = plan.source.instrument.instrument()
    client = _client(client_factory,sub,checked)
    with vault.session() as db:
        control = _control(db,sub)
        _matched(client,saved,control)
        if partial and control.stopped:
            raise GrvtMainnetError('La subcuenta LIMIT con parciales está detenida')
        control_revision = control.revision
        if (signer_ref(control.identity_salt,expected_signer) != entry_claim['signer_ref'] or _reserved(db,new_id)):
            raise GrvtMainnetError('El firmante o el identificador de reducción LIMIT no corresponden')
    try:
        checked()
        live = fetch_mainnet_instrument(instrument.name,http_post=public_post)
        checked()
        if live != instrument:
            raise ValueError('Instrumento LIMIT cambiado antes de reducir')
        quote = _quote(plan,public_post,checked)
        issued = checked()
        expiry = min(issued+20*NS,stamp(deadline_ns))
        if not 5*NS <= expiry-issued <= 20*NS or not 0 < deadline_ns-issued <= 120*NS:
            raise ValueError('La autorización de reducción LIMIT vence demasiado pronto')
        used = {entry_claim['nonce'],*(item['claim']['nonce'] for item in events['reductions'])}
        nonce = secrets.randbits(32)
        while nonce in used:
            nonce = secrets.randbits(32)
        decision = None
        if partial:
            template,decision = partial_limit_template(plan,events,last,quote,head=supervision_head,
                cid=new_id,nonce=nonce,issued=issued,expiry=expiry)
            if template is None:
                if decision['action'] == 'HOLD':
                    return saved
                raise ValueError('La posición LIMIT con parciales requiere revisión')
        else:
            template = limit_reduction_template(plan,remaining,quote,cid=new_id,nonce=nonce,
                                                issued_at_ns=issued,expiration_ns=expiry)
        wire = sign_reduction(template,instrument,private_key=private_key,expected_signer=expected_signer)
        payload = {'schema':PARTIAL_CLAIM_SCHEMA if partial else CLAIM_SCHEMA,'network':'MAINNET',
            'entry_claim_digest':saved['claim_digest'],
            'client_order_id':new_id,'issued_at_ns':issued,'expiration_ns':expiry,'deadline_ns':deadline_ns,
            'nonce':nonce,'audit_heads':[first['head'],last['head']],'quote':quote,
            'wire_digest':encode(wire)[1],'unsigned_digest':encode(template)[1],'signer_ref':entry_claim['signer_ref']}
        if supervision_head is not None:
            payload['supervision_head'] = supervision_head
        if partial:
            payload['decision'] = decision
        text,digest = encode(payload)
        with vault.guard_session_generation(generation):
            with vault.session() as db:
                db.connection().exec_driver_sql('BEGIN IMMEDIATE')
                row,current = _current(db,cid,saved)
                control = _control(db,sub)
                _matched(client,current,control)
                if partial and (control.stopped or control.revision != control_revision):
                    raise ValueError('La subcuenta cambió antes de reservar la reducción')
                if _reserved(db,new_id):
                    raise ValueError('Identificador de reducción LIMIT consumido')
                db.add(GrvtMainnetLimitReduction(client_order_id=new_id,entry_client_order_id=cid,
                                                 payload_json=text,payload_digest=digest))
                append_limit_event(db,row,'EXIT_CLAIM',{'client_order_id':new_id,'claim_digest':digest},checked())
                claimed_head = row.event_head
                db.commit()
            with vault.session() as db:
                db.connection().exec_driver_sql('BEGIN IMMEDIATE')
                row = db.get(GrvtMainnetLimitAttempt,cid)
                current = verified_limit_attempt(db,row)
                control = _control(db,sub)
                _matched(client,current,control)
                if partial and (control.stopped or control.revision != control_revision):
                    raise ValueError('La subcuenta cambió antes de transmitir la reducción')
                if row.event_head != claimed_head:
                    raise ValueError('La reducción LIMIT cambió antes de transmitir')

                def dispatch_permission():
                    now = checked()
                    if not 0 <= now-last['at_ns'] <= FRESH_NS or now >= expiry:
                        raise ValueError('La auditoría LIMIT venció antes de transmitir')
                    reduction_price(quote,reduction_scope(plan,remaining),instrument,at_ns=now)

                ack = client.submit_reduction_once(wire,template,instrument,require_permission=dispatch_permission,
                                                   clock_ns=checked)
                append_limit_event(db,row,'EXIT_ACK',{'client_order_id':new_id,'order_id':ack['order_id']},checked())
                db.commit()
    except Exception:
        raise GrvtMainnetError('Salida LIMIT sin confirmar; consulta su identificador antes de otro intento') from None
    finally:
        del private_key
    return read_limit_attempt(vault,cid)


def settle_limit_exit(vault, cid: str, *, require_permission: Callable,
                      clock_ns: Callable = time.time_ns) -> dict:
    generation,checked = _permission(vault,require_permission,clock_ns)
    saved = read_limit_attempt(vault,cid)
    if saved['events']['position_settled']:
        return saved
    first,last = _recent_audits(saved['events'],checked())
    if last['result']['remaining_size'] != '0':
        raise GrvtMainnetError('La posición LIMIT aún mantiene exposición')
    with vault.guard_session_generation(generation), vault.session() as db:
        db.connection().exec_driver_sql('BEGIN IMMEDIATE')
        row,current = _current(db,cid,saved)
        db.delete(db.get(GrvtMainnetLimitSlot,current['sub_account_id']))
        append_limit_event(db,row,'EXIT_SETTLED',{'audit_heads':[first['head'],last['head']]},checked())
        db.commit()
    return read_limit_attempt(vault,cid)
