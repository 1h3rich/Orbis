"""Bounded one-use operations over the encrypted LIMIT attempt journal."""
from collections.abc import Callable
from hmac import compare_digest
import secrets
import time

from eth_account import Account

from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError, _message, client_ids
from app.live_execution.grvt_mainnet_journal import _control, encode, require_new_mainnet_bracket
from app.live_execution.grvt_mainnet_preflight import _state
from app.live_execution.grvt_mainnet_transport import GrvtMainnetClient, public_post as default_public_post
from app.live_execution.grvt_order_codec import order_signable
from app.live_execution.grvt_perp_limit_journal import (
    _plan, append_limit_event, claim_limit_attempt, contract_from_echo, read_limit_attempt,
    stamp, verified_limit_attempt,
)
from app.live_execution.grvt_perp_limit_origin import VerifiedLimitPaperOrigin, VerifiedPartialPaperOrigin
from app.live_execution.grvt_perp_limit_plan import FRESH_NS, PerpLimitEntryPlan, PerpPartialLimitEntryTerms, unsigned_limit_entry
from app.live_execution.grvt_perp_limit_preflight import collect_limit_preflight, verify_limit_preflight
from app.live_execution.grvt_perp_limit_receipt import (
    PerpLimitContract, collect_limit_observations, lookup_limit_order, reconcile_limit_entry,
)
from app.portfolio.models import GrvtMainnetLimitAttempt, GrvtMainnetLimitSlot


def _permission(vault, require_permission: Callable, clock_ns: Callable):
    generation = vault.session_generation
    if generation is None or not callable(require_permission):
        raise GrvtMainnetError('Falta una sesión REAL con permiso revocable')
    started = last = stamp(clock_ns())

    def checked():
        nonlocal last
        require_permission()
        now = stamp(clock_ns())
        if vault.session_generation != generation or not last <= now <= started + 120_000_000_000:
            raise GrvtMainnetError('Permiso LIMIT MAINNET revocado, vencido o con reloj regresado')
        last = now
        return now

    checked()
    return generation, checked


def _sign(plan: PerpLimitEntryPlan, cid: str, *, key: bytes, expected_signer: str, at: int) -> PerpLimitContract:
    try:
        if type(key) is not bytes or len(key) != 32:
            raise ValueError('Clave dedicada inválida')
        signer = Account.from_key(key)
        if signer.address.lower() != expected_signer.lower():
            raise ValueError('Firmante dedicado distinto')
        nonce = secrets.randbits(32)
        wire = unsigned_limit_entry(plan,client_order_id=cid,nonce=nonce,issued_at_ns=at)
        message = _message(wire,plan.source.instrument.instrument())
        signed = signer.sign_message(order_signable(message,chain_id=325))
        wire['signature'].update(signer=signer.address,r='0x'+signed.r.to_bytes(32,'big').hex(),
                                 s='0x'+signed.s.to_bytes(32,'big').hex(),v=signed.v)
        return PerpLimitContract(schema_name=f'orbis.grvt.perp.limit.entry.contract.v{plan.revision}',plan=plan,
            client_order_id=cid,nonce=nonce,issued_at_ns=str(at),expected_signer=signer.address,wire=wire)
    except Exception:
        raise GrvtMainnetError('No se pudo firmar el contrato LIMIT MAINNET') from None
    finally:
        del key


def _client(factory, sub: str, checked) -> GrvtMainnetClient:
    checked()
    client = factory()
    checked()
    if not isinstance(client,GrvtMainnetClient) or client.network != 'MAINNET' or client.sub_account_id != sub:
        raise GrvtMainnetError('Sesión ajena a la subcuenta LIMIT MAINNET')
    return client


def _matched(client: GrvtMainnetClient, saved: dict, control) -> None:
    if not compare_digest(client.identity_ref(control.identity_salt),saved['claim']['account_ref']):
        raise GrvtMainnetError('La identidad de la subcuenta LIMIT cambió')


def _send_gate(db, row, contract, client, checked) -> dict:
    saved = verified_limit_attempt(db,row)
    claim = saved['claim']
    control = _control(db,row.sub_account_id)
    _matched(client,saved,control)
    if (not saved['slot_reserved'] or control.stopped or control.revision != claim['control_revision']
            or claim['wire_digest'] != encode(contract.wire)[1]):
        raise GrvtMainnetError('Intento LIMIT revocado o alterado antes del envío')
    _plan(claim,checked())
    return saved


def submit_limit_entry_once(vault, origin: VerifiedLimitPaperOrigin, budget, seed, records, *,
                            client_order_id: str, max_pending_seconds: int, entry_deadline_ns: str,
                            client_factory: Callable, private_key: bytes, expected_signer: str,
                            require_permission: Callable, confirmation: str,
                            public_post: Callable = default_public_post, clock_ns: Callable = time.time_ns) -> dict:
    if (confirmation != 'ENVIAR_ENTRADA_LIMIT_GRVT_MAINNET'
            or not isinstance(origin, VerifiedLimitPaperOrigin)):
        raise GrvtMainnetError('Falta la confirmación LIMIT MAINNET y la prueba PAPER verificada')
    return _submit_limit_entry(vault,origin,lambda:origin.terms(budget,max_pending_seconds=max_pending_seconds),
        seed,records,client_order_id=client_order_id,entry_deadline_ns=entry_deadline_ns,
        client_factory=client_factory,private_key=private_key,expected_signer=expected_signer,
        require_permission=require_permission,public_post=public_post,clock_ns=clock_ns)


def submit_partial_limit_entry_once(vault, origin: VerifiedPartialPaperOrigin, terms: PerpPartialLimitEntryTerms,
                                    seed, records, *, client_order_id: str, entry_deadline_ns: str,
                                    client_factory: Callable, private_key: bytes, expected_signer: str,
                                    require_permission: Callable, confirmation: str,
                                    public_post: Callable = default_public_post, clock_ns: Callable = time.time_ns) -> dict:
    if (confirmation != 'ENVIAR_ENTRADA_LIMIT_PARCIALES_GRVT_MAINNET'
            or not isinstance(origin, VerifiedPartialPaperOrigin)):
        raise GrvtMainnetError('Falta la confirmación LIMIT con parciales y la prueba PAPER verificada')
    terms = PerpPartialLimitEntryTerms.model_validate(terms.payload())

    def reviewed_terms():
        reviewed = origin.limit_terms(terms.budget,max_attempts=terms.max_attempts,
            reserved_close_attempts=terms.reserved_close_attempts,max_pending_seconds=terms.max_pending_seconds)
        if reviewed != terms:
            raise GrvtMainnetError('La política LIMIT de parciales difiere del prefijo PAPER revisado')
        return reviewed

    return _submit_limit_entry(vault,origin,reviewed_terms,seed,records,client_order_id=client_order_id,
        entry_deadline_ns=entry_deadline_ns,client_factory=client_factory,private_key=private_key,
        expected_signer=expected_signer,require_permission=require_permission,public_post=public_post,clock_ns=clock_ns)


def _submit_limit_entry(vault, origin, reviewed_terms: Callable, seed, records, *,
                        client_order_id: str, entry_deadline_ns: str, client_factory: Callable,
                        private_key: bytes, expected_signer: str, require_permission: Callable,
                        public_post: Callable, clock_ns: Callable) -> dict:
    cid = client_ids(client_order_id)[0]
    generation, checked = _permission(vault,require_permission,clock_ns)
    terms = reviewed_terms()
    sub = terms.budget.sub_account_id
    with vault.session() as db:
        require_new_mainnet_bracket(db,sub,cid)
    client = _client(client_factory,sub,checked)
    proof = collect_limit_preflight(client,terms,seed,records,entry_deadline_ns=entry_deadline_ns,
                                    public_post=public_post,clock_ns=checked)
    try:
        with origin.hold_current(), vault.guard_session_generation(generation):
            at = checked()
            plan = verify_limit_preflight(proof,terms,seed,records,entry_deadline_ns=entry_deadline_ns,now_ns=at)
            contract = _sign(plan,cid,key=private_key,expected_signer=expected_signer,at=at)
            digest = claim_limit_attempt(vault,contract,proof,seed,records,client=client,checked=checked)
            with vault.session() as db:
                db.connection().exec_driver_sql('BEGIN IMMEDIATE')
                row = db.get(GrvtMainnetLimitAttempt,cid)
                saved = _send_gate(db,row,contract,client,checked)
                if saved['claim_digest'] != digest or saved['events']['sequence'] != 0:
                    raise GrvtMainnetError('El intento LIMIT ya fue consumido')
                append_limit_event(db,row,'SEND_STARTED',{},checked())
                db.commit()
            with vault.session() as db:
                db.connection().exec_driver_sql('BEGIN IMMEDIATE')
                row = db.get(GrvtMainnetLimitAttempt,cid)
                saved = _send_gate(db,row,contract,client,checked)
                if saved['claim_digest'] != digest or saved['events']['sequence'] != 1:
                    raise GrvtMainnetError('El intento LIMIT cambió antes de transmitir')
                order_id = client.submit_limit_once(contract,require_permission=checked,clock_ns=checked)
                append_limit_event(db,row,'ACK',{'order_id':order_id},checked())
                db.commit()
    except GrvtMainnetError:
        raise
    except Exception:
        raise GrvtMainnetError('Intento LIMIT MAINNET sin confirmar; consulta su identificador') from None
    finally:
        del private_key
    return read_limit_attempt(vault,cid)


def _observe(vault, cid: str, client_factory, *, generation, checked) -> dict:
    saved = read_limit_attempt(vault,cid)
    if (saved['events']['empty_settled'] or saved['events']['abandoned_unsent'] or saved['events']['exit_audits']
            or saved['events']['reductions']):
        return saved
    if not saved['events']['send_started']:
        raise GrvtMainnetError('El intento LIMIT aún no tiene registro de envío')
    sub = saved['sub_account_id']
    client = _client(client_factory,sub,checked)
    with vault.session() as db:
        control = _control(db,sub)
        _matched(client,saved,control)
        salt = control.identity_salt
    claim = saved['claim']
    plan = _plan(claim,claim['claimed_at_ns'])
    echo = lookup_limit_order(client,sub,cid,instrument=plan.source.terms.instrument,
        issued_at_ns=claim['issued_at_ns'],expires_at_ns=int(plan.result['expires_at_ns']),checked=checked)
    contract = contract_from_echo(claim,plan,echo,salt=salt)
    observed = collect_limit_observations(client,contract,identity_salt=salt,account_ref=claim['account_ref'],
        clock_ns=checked,require_permission=checked)
    receipt = reconcile_limit_entry(contract,observed)
    with vault.guard_session_generation(generation), vault.session() as db:
        db.connection().exec_driver_sql('BEGIN IMMEDIATE')
        row = db.get(GrvtMainnetLimitAttempt,cid)
        current = verified_limit_attempt(db,row)
        if (current['claim_digest'] != saved['claim_digest']
                or current['events']['head'] != saved['events']['head']):
            raise GrvtMainnetError('El intento LIMIT cambió durante la observación')
        _matched(client,current,_control(db,sub))
        append_limit_event(db,row,'OBSERVATION',{'source':observed.payload(),'result':receipt},checked())
        db.commit()
    return read_limit_attempt(vault,cid)


def observe_limit_attempt(vault, cid: str, *, client_factory: Callable,
                          require_permission: Callable, clock_ns: Callable = time.time_ns) -> dict:
    generation, checked = _permission(vault,require_permission,clock_ns)
    return _observe(vault,cid,client_factory,generation=generation,checked=checked)


def cancel_limit_attempt_once(vault, cid: str, *, client_factory: Callable,
                              require_permission: Callable, confirmation: str,
                              clock_ns: Callable = time.time_ns) -> dict:
    if confirmation != 'CANCELAR_ENTRADA_LIMIT_GRVT_MAINNET':
        raise GrvtMainnetError('Falta confirmación de cancelación LIMIT MAINNET')
    generation, checked = _permission(vault,require_permission,clock_ns)
    saved = read_limit_attempt(vault,cid)
    if saved['events']['cancel_claimed']:
        return saved
    observed = _observe(vault,cid,client_factory,generation=generation,checked=checked)
    events = observed['events']
    if events['receipt'] is None or events['receipt']['status'] != 'OPEN':
        return observed
    sub = observed['sub_account_id']
    client = _client(client_factory,sub,checked)
    with vault.guard_session_generation(generation):
        with vault.session() as db:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            row = db.get(GrvtMainnetLimitAttempt,cid)
            current = verified_limit_attempt(db,row)
            _matched(client,current,_control(db,sub))
            if current['events']['cancel_claimed'] or current['events']['head'] != events['head']:
                raise GrvtMainnetError('El intento LIMIT cambió antes de cancelar')
            order_id = events['order_id']
            append_limit_event(db,row,'CANCEL_CLAIM',{'order_id':order_id,'observation_head':events['head']},checked())
            cancel_head = row.event_head
            db.commit()
        with vault.session() as db:
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            row = db.get(GrvtMainnetLimitAttempt,cid)
            current = verified_limit_attempt(db,row)
            _matched(client,current,_control(db,sub))
            if row.event_head != cancel_head or not 0 <= checked()-events['observation_at_ns'] <= FRESH_NS:
                raise GrvtMainnetError('El intento LIMIT cambió antes de transmitir la cancelación')
            checked()
            client.cancel_limit_once(sub,order_id,require_permission=checked)
            append_limit_event(db,row,'CANCEL_ACK',{'order_id':order_id},checked())
            db.commit()
    return read_limit_attempt(vault,cid)


def settle_empty_limit_attempt(vault, cid: str, *, client_factory: Callable,
                               require_permission: Callable, clock_ns: Callable = time.time_ns) -> dict:
    """Release only after terminal zero fills and two stable flat account reads."""
    generation, checked = _permission(vault,require_permission,clock_ns)
    saved = _observe(vault,cid,client_factory,generation=generation,checked=checked)
    events = saved['events']
    if events['empty_settled'] or events['abandoned_unsent']:
        return saved
    if not events['receipt']['terminal'] or events['receipt']['filled_size'] != '0':
        raise GrvtMainnetError('El intento LIMIT mantiene una orden o exposición pendiente')
    sub = saved['sub_account_id']
    client = _client(client_factory,sub,checked)
    with vault.session() as db:
        _matched(client,saved,_control(db,sub))

    class CheckedAccount:
        def read_account(self, route, scope):
            checked()
            value = client.read_account(route,scope)
            checked()
            return value

    port = CheckedAccount()
    states = [_state(port,sub,checked), _state(port,sub,checked)]
    with vault.guard_session_generation(generation), vault.session() as db:
        db.connection().exec_driver_sql('BEGIN IMMEDIATE')
        row = db.get(GrvtMainnetLimitAttempt,cid)
        current = verified_limit_attempt(db,row)
        _matched(client,current,_control(db,sub))
        if current['claim_digest'] != saved['claim_digest'] or current['events']['head'] != events['head']:
            raise GrvtMainnetError('El intento LIMIT cambió antes de liberar la subcuenta')
        db.delete(db.get(GrvtMainnetLimitSlot,sub))
        append_limit_event(db,row,'EMPTY_SETTLED',{'states':states,'observation_head':events['head']},checked())
        db.commit()
    return read_limit_attempt(vault,cid)
