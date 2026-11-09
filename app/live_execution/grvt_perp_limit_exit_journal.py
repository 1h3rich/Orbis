"""Exit events in the encrypted LIMIT journal."""
import json

from app.lab.engine import number as strategy_number
from app.lab.perp_book_types import exact
from app.live_execution.grvt_mainnet_journal import _HASH, encode
from app.live_execution.grvt_mainnet_preflight import canonical, number
from app.live_execution.grvt_perp_limit_exit_evidence import (
    calculate_limit_exit_audit, limit_reduction_template, stable_exit_audits,
)
from app.live_execution.grvt_perp_limit_plan import FRESH_NS, NS, PerpLimitEntryPlan, PerpPartialLimitEntryTerms
from app.live_execution.grvt_perp_limit_receipt import ORDER_ID
from app.live_execution.grvt_perp_partial_targets import PartialExitObservation, ReconciledReduction, plan_partial_exit_order
from app.portfolio.models import GrvtMainnetLimitReduction


CLAIM_SCHEMA = 'orbis.grvt.perp.limit.reduction.claim.v1'
PARTIAL_CLAIM_SCHEMA = 'orbis.grvt.perp.limit.reduction.claim.v2'


def require_limit_close_decision(state: dict, head: str) -> None:
    if (type(head) is not str or state['supervision_head'] != head
            or state['supervision'] is None or state['supervision']['close_reason'] is None
            or state['receipt'] is None or not state['receipt']['terminal']):
        raise ValueError('La reducción automática LIMIT requiere una decisión de cierre verificada')


@exact
def partial_limit_template(plan: PerpLimitEntryPlan, state: dict, audit: dict, quote: dict, *, head: str,
                           cid: str, nonce: int, issued: int, expiry: int) -> tuple[dict|None,dict]:
    terms = plan.source.terms
    watch, receipt = state['supervision'], state['receipt']
    if (not isinstance(terms,PerpPartialLimitEntryTerms)
            or type(head) is not str or head != state['supervision_head']
            or watch is None or watch['action'] not in ('HOLD','CLOSE')
            or receipt is None or not receipt['terminal'] or not receipt['fills']):
        raise ValueError('La reducción LIMIT parcial requiere supervisión y entrada verificadas')
    result = audit['result']
    previous = state['reductions']
    orders = result['orders']
    if len(orders) != len(previous):
        raise ValueError('Falta la conciliación terminal de una reducción LIMIT parcial')
    attempts = []
    for row, order in zip(previous,orders):
        claim = row['claim']
        if claim['schema'] != PARTIAL_CLAIM_SCHEMA or claim['client_order_id'] != order['client_order_id']:
            raise ValueError('Identidad de reducción LIMIT parcial divergente')
        attempts.append(ReconciledReduction(order['client_order_id'],claim['decision']['target_index'],
            number(row['template']['legs'][0]['size']),number(order['filled_size'])))
    position = result['positions']
    if position and (len(position) != 1 or position[0][0] != terms.instrument):
        raise ValueError('Posición conciliada ajena al instrumento LIMIT')
    if (quote['event_time_ns'] < max(fill['event_time_ns'] for fill in receipt['fills'])
            or type(issued) is not int or not max(state['observation_at_ns'],state['supervision_at_ns']) <= issued):
        raise ValueError('Cotización parcial anterior a la entrada confirmada')
    reason = watch['close_reason']
    if reason is None and not 0 <= issued-int(watch['feed']['last_observed_ns']) <= FRESH_NS:
        raise ValueError('Supervisión LIMIT parcial vencida')
    latched_target = any(row['claim']['decision']['take_profit_latched'] for row in previous)
    if reason is None and latched_target:
        reason = 'TAKE_PROFIT'
    if result['policy_breaches'] and reason in (None,'TAKE_PROFIT'):
        reason = 'EXIT_LIMIT_BREACH'
    if reason in (None,'TAKE_PROFIT'):
        cursor = watch['exit']
        if cursor is None or int(cursor['last_quote_ns']) == 0:
            raise ValueError('Falta una cotización de supervisión posterior a la entrada LIMIT')
        observed = watch['quote']
        stamp = int(observed['event_time_ns'])
        if quote['event_time_ns'] < stamp or (quote['event_time_ns'] == stamp and
                any(number(quote[key]) != strategy_number(observed[key]) for key in ('bid','ask'))):
            raise ValueError('Cotización de reducción LIMIT anterior o divergente de la supervisión')
        definition = terms.definition
        long = definition['direction']=='LONG'
        sign = 1 if long else -1
        stop = number(receipt['entry_notional'])*(1-sign*strategy_number(definition['stop_loss']))
        target = number(receipt['entry_notional'])*(1+sign*strategy_number(definition['take_profit']))
        if 'trailing_stop' in definition:
            trail = strategy_number(cursor['trailing_notional'])*(1-sign*strategy_number(definition['trailing_stop']))
            stop = max(stop,trail) if long else min(stop,trail)
        current = number(quote['bid' if long else 'ask'])*number(receipt['filled_size'])
        if (current <= stop if long else current >= stop):
            reason = 'STOP_OR_TRAILING'
        elif (current >= target if long else current <= target):
            reason = 'TAKE_PROFIT'
    policy = terms.exit_terms()
    evidence = PartialExitObservation(number(receipt['filled_size']),number(receipt['entry_notional']),
        number(position[0][1]) if position else number('0'),audit['at_ns'],tuple(attempts),reason,quote,issued)
    decision, template = plan_partial_exit_order(policy,plan.source.instrument.instrument(),evidence,
        client_order_id=cid,nonce=nonce,expiration_ns=expiry)
    return template,{'action':decision.action,'reason':decision.reason,'target_index':decision.target_index,
        'remaining':canonical(decision.remaining),'quantity':canonical(decision.quantity) if decision.quantity is not None else None,
        'price_bound':canonical(decision.price_bound) if decision.price_bound is not None else None,
        'policy_digest':encode(policy.payload())[1],'take_profit_latched':latched_target or reason=='TAKE_PROFIT'}


def checked_reduction_claim(record, row, entry_claim, plan, state, *, at_ns: int) -> dict:
    if record is None:
        raise ValueError('Falta la reducción reservada LIMIT')
    claim = json.loads(record.payload_json)
    first,last = stable_exit_audits(state['exit_audits'], now_ns=at_ns)
    issued,expiry,deadline = (claim[key] for key in ('issued_at_ns','expiration_ns','deadline_ns'))
    partial = plan.revision==2
    if not partial and 'supervision_head' in claim:
        require_limit_close_decision(state,claim['supervision_head'])
    fields = {'schema','network','entry_claim_digest','client_order_id','issued_at_ns','expiration_ns',
                      'deadline_ns','nonce','audit_heads','quote','wire_digest','unsigned_digest','signer_ref'}
    if partial:
        fields |= {'supervision_head','decision'}
    present = set(claim) if partial else set(claim)-{'supervision_head'}
    if (present != fields
            or claim['schema'] != (PARTIAL_CLAIM_SCHEMA if partial else CLAIM_SCHEMA) or claim['network'] != 'MAINNET'
            or claim['entry_claim_digest'] != row.payload_digest or record.entry_client_order_id != row.client_order_id
            or claim['client_order_id'] != record.client_order_id or claim['client_order_id'] == row.client_order_id
            or claim['audit_heads'] != [first['head'],last['head']]
            or claim['signer_ref'] != entry_claim['signer_ref']
            or type(claim['nonce']) is not int or claim['nonce'] in
                {entry_claim['nonce'],*(item['claim']['nonce'] for item in state['reductions'])}
            or any(type(value) is not int for value in (issued,expiry,deadline))
            or not last['at_ns'] <= claim['quote']['observed_at_ns'] <= issued <= at_ns < expiry <= deadline < 2**63
            or not 0 < deadline-issued <= 120*NS
            or at_ns-issued > FRESH_NS
            or any(type(claim[key]) is not str or not _HASH.fullmatch(claim[key])
                   for key in ('wire_digest','unsigned_digest'))
            or encode(claim) != (record.payload_json,record.payload_digest)):
        raise ValueError('Contrato de reducción LIMIT fuera del prefijo autorizado')
    remaining = number(last['result']['remaining_size'])
    if remaining <= 0:
        raise ValueError('La subcuenta LIMIT no tiene exposición que reducir')
    if partial:
        template,decision = partial_limit_template(plan,state,last,claim['quote'],head=claim['supervision_head'],
            cid=claim['client_order_id'],nonce=claim['nonce'],issued=issued,expiry=expiry)
        if template is None or encode(decision) != encode(claim['decision']):
            raise ValueError('La reducción LIMIT no reproduce su objetivo y su cantidad')
    else:
        template = limit_reduction_template(plan,remaining,claim['quote'],cid=claim['client_order_id'],
            nonce=claim['nonce'],issued_at_ns=issued,expiration_ns=expiry)
    if encode(template)[1] != claim['unsigned_digest']:
        raise ValueError('La reducción LIMIT cambió tras calcularla')
    return {'claim':claim,'template':template,'ack':None}


def _require_observed_prefix(previous: dict, source: dict, result: dict) -> None:
    orders = previous['result']['orders']
    if (source['cutoff_ns'] < previous['source']['cutoff_ns']
            or result['orders'][:len(orders)] != orders):
        raise ValueError('Un estado terminal anterior de salida LIMIT cambió')
    ids = {order['client_order_id'] for order in orders}
    before = previous['source']['histories']
    after = source['histories']
    if ([row for row in before['fill_history']['rows'] if row['client_order_id'] in ids]
            != [row for row in after['fill_history']['rows'] if row['client_order_id'] in ids]):
        raise ValueError('Los fills anteriores de salida LIMIT cambiaron')
    funding = {(row['tx_id'],row['event_time_ns']):row for row in after['funding_payment_history']['rows']}
    if any(funding.get((row['tx_id'],row['event_time_ns'])) != row for row in before['funding_payment_history']['rows']):
        raise ValueError('La financiación confirmada de salida LIMIT cambió')


def replay_exit_event(db,row,entry_claim,plan,state,event,*,salt: str,at: int) -> None:
    kind,detail = event['kind'],event['detail']
    audits, reductions = state['exit_audits'], state['reductions']
    if kind == 'EXIT_AUDIT' and set(detail) == {'source','result'}:
        source = detail['source']
        if not 0 <= at-source['observed_at_ns'] <= FRESH_NS or source['cutoff_ns'] < state['observation_at_ns']:
            raise ValueError('Auditoría de salida LIMIT fuera del tiempo del diario')
        result = calculate_limit_exit_audit(plan,entry_claim,state['receipt'],reductions,source,salt=salt)
        prior = audits[-1] if audits else state['exit_prefix']
        if prior is not None:
            _require_observed_prefix(prior,source,result)
        if (result != detail['result']
                or audits and audits[-1]['result']['orders'] != result['orders']):
            raise ValueError('La auditoría de salida LIMIT diverge de sus originales')
        audits.append({'source':source,'result':result,'head':encode(event)[1],'at_ns':at})
        if len(audits) > 2:
            del audits[0]
    elif kind == 'EXIT_CLAIM' and set(detail) == {'client_order_id','claim_digest'}:
        record = db.get(GrvtMainnetLimitReduction,detail['client_order_id'])
        verified = checked_reduction_claim(record,row,entry_claim,plan,state,at_ns=at)
        if (record.payload_digest != detail['claim_digest']
                or any(item['claim']['client_order_id'] == record.client_order_id for item in reductions)):
            raise ValueError('Reducción LIMIT repetida o alterada')
        reductions.append(verified)
        state['exit_prefix'] = audits[-1]
        audits.clear()
    elif kind == 'EXIT_ACK' and set(detail) == {'client_order_id','order_id'} and reductions and not audits:
        last = reductions[-1]
        order_id = detail['order_id']
        if (last['ack'] is not None or detail['client_order_id'] != last['claim']['client_order_id']
                or type(order_id) is not str or order_id == '0' or not ORDER_ID.fullmatch(order_id)):
            raise ValueError('Acuse de salida LIMIT inválido')
        last['ack'] = order_id
    elif kind == 'EXIT_SETTLED' and set(detail) == {'audit_heads'}:
        first,last = stable_exit_audits(audits,now_ns=at)
        if (detail['audit_heads'] != [first['head'],last['head']]
                or last['result']['remaining_size'] != '0'):
            raise ValueError('El cierre LIMIT no acredita una cuenta plana')
        state['position_settled'] = True
    else:
        raise ValueError('Transición de salida LIMIT inválida')
