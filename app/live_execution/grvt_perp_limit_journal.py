"""Encrypted GTT claim and event chain. Persisted state carries no authority."""
from collections.abc import Callable
import json

from sqlalchemy import func, select

from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError, client_ids
from app.live_execution.grvt_mainnet_journal import _HASH, _control, encode, require_new_mainnet_bracket, signer_ref
from app.live_execution.grvt_mainnet_remote_order import remote_contract
from app.live_execution.grvt_mainnet_transport import GrvtMainnetClient
from app.live_execution.grvt_perp_limit_plan import FRESH_NS, LimitSignalSeed, PerpLimitEntryPlan, unsigned_limit_entry
from app.live_execution.grvt_perp_limit_exit_journal import replay_exit_event
from app.live_execution.grvt_perp_limit_preflight import verify_limit_preflight
from app.live_execution.grvt_perp_limit_receipt import ORDER_ID, PerpLimitContract, PerpLimitReceiptInput, reconcile_limit_entry
from app.live_execution.grvt_perp_limit_watch import LimitWatch, advance_limit_watch, start_limit_watch
from app.live_execution.grvt_perp_signals import PerpLimitSignalSeed, PerpPartialSignalSeed
from app.portfolio.models import GrvtMainnetLimitAttempt, GrvtMainnetLimitEvent, GrvtMainnetLimitReduction, GrvtMainnetLimitSlot


SCHEMA = 'orbis.grvt.mainnet.perp.limit.claim.v1'
PARTIAL_SCHEMA = 'orbis.grvt.mainnet.perp.limit.claim.v2'
TABLES = {'grvt_mainnet_limit_attempts', 'grvt_mainnet_limit_slots', 'grvt_mainnet_limit_events',
          'grvt_mainnet_limit_reductions'}
MAX_CLAIM_BYTES = 64*1024*1024


def limit_schema(db, *, required=False) -> bool:
    connection = db.connection()
    if connection.exec_driver_sql('PRAGMA user_version').scalar_one() < 80:
        if required:
            raise GrvtMainnetError('Actualiza la bóveda para registrar intentos LIMIT REAL')
        return False
    names = set(connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").scalars())
    if not TABLES <= names:
        raise GrvtMainnetError('Diario LIMIT REAL incompleto')
    return True


def require_no_limit_conflict(db, sub: str, client_order_ids: tuple[str, ...]) -> bool:
    if not limit_schema(db):
        return False
    if (db.get(GrvtMainnetLimitSlot, sub) is not None
            or any(db.get(GrvtMainnetLimitAttempt,cid) is not None or db.get(GrvtMainnetLimitReduction,cid) is not None
                   for cid in client_order_ids)):
        raise GrvtMainnetError('Subcuenta o identificador reservado por un intento LIMIT REAL')
    history = False
    for row in db.scalars(select(GrvtMainnetLimitAttempt).where(GrvtMainnetLimitAttempt.sub_account_id == sub)):
        if verified_limit_attempt(db,row)['slot_reserved']:
            raise GrvtMainnetError('Subcuenta reservada por un intento LIMIT REAL')
        history = True
    return history


def limit_client_id_reserved(db, cid: str) -> bool:
    return limit_schema(db) and (
        db.get(GrvtMainnetLimitAttempt,cid) is not None or db.get(GrvtMainnetLimitReduction,cid) is not None)


def stamp(value: object) -> int:
    if type(value) is not int or not 0 < value < 2**63:
        raise ValueError('Instante LIMIT REAL inválido')
    return value


def signal_key(plan: PerpLimitEntryPlan) -> str:
    terms = plan.source.terms
    return encode({'sub':terms.budget.sub_account_id, 'instrument':terms.instrument,
                   'version':terms.research.profile.strategy_version_id, 'candle':plan.source.signal_candle.t})[1]


def _plan(claim: dict, at: int) -> PerpLimitEntryPlan:
    plan = PerpLimitEntryPlan.model_validate(claim['preflight']['plan'])
    if claim['schema'] != (PARTIAL_SCHEMA if plan.revision==2 else SCHEMA):
        raise ValueError('Versión del claim LIMIT incompatible con el plan')
    seed_type = PerpPartialSignalSeed if plan.revision==2 else PerpLimitSignalSeed
    return verify_limit_preflight(claim['preflight'], plan.source.terms,
        seed_type.model_validate(claim['seed']), claim['records'],
        entry_deadline_ns=plan.source.entry_deadline_ns, now_ns=at)


def contract_from_echo(claim: dict, plan: PerpLimitEntryPlan, echo: dict, *, salt: str) -> PerpLimitContract:
    template = unsigned_limit_entry(plan, client_order_id=claim['client_order_id'],
                                     nonce=claim['nonce'], issued_at_ns=claim['issued_at_ns'])
    wire = remote_contract(echo, template)
    signer = wire['signature']['signer']
    if (encode(wire)[1] != claim['wire_digest'] or signer_ref(salt, signer) != claim['signer_ref']):
        raise GrvtMainnetError('El eco LIMIT no corresponde a la orden registrada')
    return PerpLimitContract(schema_name=f'orbis.grvt.perp.limit.entry.contract.v{plan.revision}', plan=plan,
        client_order_id=claim['client_order_id'], nonce=claim['nonce'],
        issued_at_ns=str(claim['issued_at_ns']), expected_signer=signer, wire=wire)


def verify_empty_account(claim: dict, events: dict, states: list[dict], at: int) -> None:
    receipt = events['receipt']
    if (receipt is None or not receipt['terminal'] or receipt['filled_size'] != '0'
            or type(states) is not list or len(states) != 2):
        raise ValueError('El intento LIMIT no está vacío y terminal')
    baseline = claim['preflight']['market']['states'][-1]
    after = events['observation_at_ns']
    last_market_time = int(receipt['order_updated_at_ns'])
    for state in states:
        event_at = stamp(state['event_time_ns'])
        observed_at = stamp(state['observed_at_ns'])
        expected = {**baseline,'event_time_ns':event_at,'observed_at_ns':observed_at}
        if (encode(state) != encode(expected) or not after <= observed_at <= at or at-observed_at > FRESH_NS
                or not last_market_time <= event_at <= observed_at or observed_at-event_at > FRESH_NS):
            raise ValueError('La cuenta no acredita la misma caja plana tras la orden LIMIT')
        after, last_market_time = observed_at, event_at


def _replay_events(db, row, claim: dict, plan: PerpLimitEntryPlan, *, salt: str) -> tuple[dict, LimitWatch | None]:
    result = {'sequence':0,'head':None,'send_started':False,'ack':None,
              'receipt':None,'order_id':None,'observation_at_ns':None,
              'cancel_claimed':False,'cancel_acknowledged':False,'empty_settled':False,'abandoned_unsent':False,
              'exit_audits':[],'exit_prefix':None,'reductions':[],'position_settled':False,
              'supervision':None,'supervision_head':None}
    watch = None
    previous_time = claim['claimed_at_ns']
    events = db.scalars(select(GrvtMainnetLimitEvent).where(
        GrvtMainnetLimitEvent.client_order_id == row.client_order_id).order_by(GrvtMainnetLimitEvent.sequence))
    for event in events:
        value = json.loads(event.payload_json)
        at = stamp(value['at_ns'])
        if (set(value) != {'claim_digest','previous','sequence','kind','at_ns','detail'}
                or value['claim_digest'] != row.payload_digest or value['previous'] != result['head']
                or type(value['sequence']) is not int or value['sequence'] != result['sequence']+1
                or event.sequence != value['sequence'] or at < previous_time
                or result['empty_settled'] or result['position_settled'] or result['abandoned_unsent']
                or encode(value) != (event.payload_json,event.payload_digest)):
            raise ValueError('Cadena LIMIT REAL alterada')
        kind, detail = value['kind'], value['detail']
        if kind == 'SEND_STARTED' and not result['send_started'] and not detail:
            _plan(claim, at)
            result['send_started'] = True
        elif kind == 'ABANDON_UNSENT' and result['sequence'] == 0 and detail == {}:
            result['abandoned_unsent'] = True
        elif not result['send_started']:
            raise ValueError('Evento LIMIT sin intento de envío')
        elif kind == 'ACK' and result['sequence'] == 1:
            order_id = detail['order_id']
            if (set(detail) != {'order_id'} or type(order_id) is not str
                    or not ORDER_ID.fullmatch(order_id) or order_id == '0'):
                raise ValueError('Acuse LIMIT inválido')
            result.update(ack=detail,order_id=order_id)
        elif (kind == 'OBSERVATION' and set(detail) == {'source','result'}
              and not result['exit_audits'] and not result['reductions']):
            observations = PerpLimitReceiptInput.model_validate(detail['source'])
            first_at = int(observations.first.received_at_ns)
            last_at = int(observations.last.received_at_ns)
            if first_at < previous_time or last_at > at:
                raise ValueError('Observación LIMIT anterior al diario o futura')
            contract = contract_from_echo(claim, plan, observations.first.order, salt=salt)
            receipt = reconcile_limit_entry(contract, observations)
            earlier = result['receipt']
            by_trade = {fill['trade_id']:fill for fill in receipt['fills']}
            if (receipt != detail['result'] or result['order_id'] not in (None, receipt['order_id'])
                    or earlier is not None and (
                        int(earlier['order_updated_at_ns']) > int(receipt['order_updated_at_ns'])
                        or earlier['status'] == 'OPEN' and receipt['status'] in ('PENDING','REJECTED'))
                    or earlier is not None and earlier['terminal'] and (
                        earlier['status'] != receipt['status'] or earlier['reason'] != receipt['reason']
                        or earlier['fills'] != receipt['fills'])
                    or earlier is not None and any(
                        by_trade.get(fill['trade_id']) != fill for fill in earlier['fills'])):
                raise ValueError('Observación LIMIT incompatible con el prefijo')
            result.update(receipt=receipt,order_id=receipt['order_id'],observation_at_ns=last_at)
        elif kind == 'CANCEL_CLAIM' and not result['cancel_claimed']:
            if (detail != {'order_id':result['order_id'],'observation_head':result['head']}
                    or result['receipt'] is None or result['receipt']['status'] != 'OPEN'
                    or not 0 <= at-result['observation_at_ns'] <= FRESH_NS):
                raise ValueError('Cancelación LIMIT sin estado abierto reciente')
            result['cancel_claimed'] = True
        elif (kind == 'CANCEL_ACK' and result['cancel_claimed'] and not result['cancel_acknowledged']
              and detail == {'order_id':result['order_id']}):
            result['cancel_acknowledged'] = True
        elif kind == 'EMPTY_SETTLED' and set(detail) == {'states','observation_head'}:
            if detail['observation_head'] != result['head']:
                raise ValueError('Cierre vacío LIMIT sin el prefijo verificado')
            verify_empty_account(claim,result,detail['states'],at)
            result['empty_settled'] = True
        elif kind == 'WATCH' and set(detail) == {'record','checkpoint'}:
            watch = watch if watch is not None else start_limit_watch(claim)
            watch, checkpoint = advance_limit_watch(watch,plan,result,detail['record'],at_ns=at)
            if encode(checkpoint) != encode(detail['checkpoint']):
                raise ValueError('Decisión de supervisión LIMIT distinta de sus originales')
            result.update(supervision=checkpoint,supervision_head=event.payload_digest)
            if plan.revision==2:
                result['supervision_at_ns'] = at
        elif kind.startswith('EXIT_'):
            replay_exit_event(db,row,claim,plan,result,value,salt=salt,at=at)
        else:
            raise ValueError('Transición LIMIT REAL incompatible')
        result.update(sequence=event.sequence,head=event.payload_digest)
        previous_time = at
    if (row.event_count,row.event_head) != (result['sequence'],result['head']):
        raise ValueError('Cabecera de eventos LIMIT REAL divergente')
    if db.scalar(select(func.count()).select_from(GrvtMainnetLimitReduction).where(
            GrvtMainnetLimitReduction.entry_client_order_id == row.client_order_id)) != len(result['reductions']):
        raise ValueError('Reservas de reducción LIMIT fuera de la cadena')
    return result,watch


def verified_limit_watch(db, row: GrvtMainnetLimitAttempt) -> tuple[dict, LimitWatch | None]:
    try:
        if row is None or len(row.payload_json) > MAX_CLAIM_BYTES:
            raise ValueError('Intento LIMIT REAL no encontrado o excesivo')
        claim = json.loads(row.payload_json)
        slot = db.get(GrvtMainnetLimitSlot, row.sub_account_id)
        control = _control(db, row.sub_account_id)
        issued, claimed = stamp(claim['issued_at_ns']), stamp(claim['claimed_at_ns'])
        plan = _plan(claim, claimed)
        if (set(claim) != {'schema','network','client_order_id','issued_at_ns','claimed_at_ns','nonce',
                           'control_revision','account_ref','signer_ref','wire_digest','preflight','seed','records'}
                or claim['network'] != 'MAINNET'
                or claim['client_order_id'] != row.client_order_id
                or plan.source.terms.budget.sub_account_id != row.sub_account_id
                or row.signal_key != signal_key(plan) or not issued <= claimed
                or type(claim['control_revision']) is not int or not 0 <= claim['control_revision'] <= control.revision
                or control.account_ref != claim['account_ref']
                or any(type(claim[k]) is not str or not _HASH.fullmatch(claim[k]) for k in ('account_ref','signer_ref','wire_digest'))
                or encode(claim) != (row.payload_json,row.payload_digest)):
            raise ValueError('Claim, cuenta o ancla LIMIT REAL divergente')
        unsigned_limit_entry(plan,client_order_id=row.client_order_id,nonce=claim['nonce'],issued_at_ns=issued)
        events,watch = _replay_events(db,row,claim,plan,salt=control.identity_salt)
        slot_matches = slot is not None and slot.client_order_id == row.client_order_id
        if (slot_matches != (not (events['empty_settled'] or events['position_settled'] or events['abandoned_unsent']))
                or slot_matches and slot.claim_digest != row.payload_digest):
            raise ValueError('Reserva de subcuenta LIMIT REAL divergente')
        return {'claim':claim,'claim_digest':row.payload_digest,'sub_account_id':row.sub_account_id,
                'events':events,'authorization':False,'slot_reserved':slot_matches},watch
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError) as exc:
        raise GrvtMainnetError('Intento LIMIT REAL no verificable') from exc


def verified_limit_attempt(db, row: GrvtMainnetLimitAttempt) -> dict:
    return verified_limit_watch(db,row)[0]


def read_limit_attempt(vault, cid: str) -> dict:
    client_ids(cid)
    with vault.session() as db:
        limit_schema(db,required=True)
        return verified_limit_attempt(db, db.get(GrvtMainnetLimitAttempt,cid))


def abandon_unsent_limit(vault, sub: str, cid: str, *, generation: int, clock_ns: Callable[[], int]) -> dict:
    """Release a local reservation only before SEND_STARTED can exist."""
    client_ids(cid)
    with vault.guard_session_generation(generation), vault.session() as db:
        db.connection().exec_driver_sql('BEGIN IMMEDIATE')
        limit_schema(db,required=True)
        row = db.get(GrvtMainnetLimitAttempt,cid)
        saved = verified_limit_attempt(db,row)
        if saved['sub_account_id'] != sub or saved['events']['send_started']:
            raise GrvtMainnetError('El intento LIMIT requiere comprobación en GRVT')
        if saved['events']['abandoned_unsent']:
            return saved
        db.delete(db.get(GrvtMainnetLimitSlot,sub))
        append_limit_event(db,row,'ABANDON_UNSENT',{},stamp(clock_ns()))
        db.commit()
        return verified_limit_attempt(db,row)


def claim_limit_attempt(vault, contract: PerpLimitContract, preflight: dict, seed: LimitSignalSeed,
                        records: list[dict], *, client, checked: Callable[[], int]) -> str:
    contract = PerpLimitContract.model_validate(contract.payload())
    cid = contract.client_order_id
    sub = contract.plan.source.terms.budget.sub_account_id
    with vault.session() as db:
        db.connection().exec_driver_sql('BEGIN IMMEDIATE')
        limit_schema(db,required=True)
        require_new_mainnet_bracket(db,sub,cid)
        key = signal_key(contract.plan)
        if db.scalar(select(GrvtMainnetLimitAttempt.client_order_id).where(
                GrvtMainnetLimitAttempt.signal_key == key)) is not None:
            raise GrvtMainnetError('La señal LIMIT ya tiene un intento registrado')
        control = _control(db,sub,create=True)
        if not isinstance(client, GrvtMainnetClient) or client.network != 'MAINNET' or client.sub_account_id != sub:
            raise GrvtMainnetError('Identidad LIMIT REAL distinta de la subcuenta reservada')
        ref = client.identity_ref(control.identity_salt)
        if control.account_ref not in (None,ref):
            raise GrvtMainnetError('Identidad LIMIT REAL distinta de la subcuenta reservada')
        control.account_ref = ref
        payload = {'schema':PARTIAL_SCHEMA if contract.plan.revision==2 else SCHEMA,'network':'MAINNET','client_order_id':cid,
            'issued_at_ns':int(contract.issued_at_ns),'claimed_at_ns':checked(),'nonce':contract.nonce,
            'control_revision':control.revision,'account_ref':ref,
            'signer_ref':signer_ref(control.identity_salt,contract.expected_signer),
            'wire_digest':encode(contract.wire)[1],'preflight':preflight,'seed':seed.payload(),'records':records}
        if _plan(payload,payload['claimed_at_ns']).payload() != contract.plan.payload():
            raise GrvtMainnetError('Contrato LIMIT REAL distinto del preflight')
        text,digest = encode(payload)
        if len(text) > MAX_CLAIM_BYTES:
            raise GrvtMainnetError('Originales LIMIT REAL fuera de capacidad')
        row = GrvtMainnetLimitAttempt(client_order_id=cid,sub_account_id=sub,signal_key=key,
                                      payload_json=text,payload_digest=digest)
        db.add(row)
        db.flush()
        db.add(GrvtMainnetLimitSlot(sub_account_id=sub,client_order_id=cid,claim_digest=digest))
        db.flush()
        checked()
        db.commit()
        return digest


def append_limit_event(db, row: GrvtMainnetLimitAttempt, kind: str, detail: dict, at: int) -> None:
    value = {'claim_digest':row.payload_digest,'previous':row.event_head,'sequence':row.event_count+1,
             'kind':kind,'at_ns':at,'detail':detail}
    text,digest = encode(value)
    row.event_count,row.event_head = value['sequence'],digest
    db.add(GrvtMainnetLimitEvent(client_order_id=row.client_order_id,sequence=row.event_count,
                                payload_json=text,payload_digest=digest))
    db.flush()
    verified_limit_attempt(db,row)


def verify_all_limit_attempts(db) -> None:
    limit_schema(db,required=True)
    count = db.scalar(select(func.count()).select_from(GrvtMainnetLimitSlot))
    verified = 0
    for row in db.scalars(select(GrvtMainnetLimitAttempt)):
        verified += int(verified_limit_attempt(db,row)['slot_reserved'])
    if count != verified:
        raise GrvtMainnetError('Hay anclas LIMIT REAL fuera del diario')
