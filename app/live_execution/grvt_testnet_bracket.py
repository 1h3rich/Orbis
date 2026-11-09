"""Contrato de entrada FOK con TP/SL vinculados de GRVT TESTNET.

Los hijos se dimensionan desde la entrada solicitada, no desde una posición
observada inventada. Su activación depende del llenado completo del padre.
Compilar/firmar no reserva ni autoriza un envío ni garantiza protección.
"""

from dataclasses import dataclass
from decimal import Decimal,ROUND_CEILING,ROUND_FLOOR,localcontext
import hashlib
import re
from app.live_execution.grvt_bracket_state import native_recovery_reason,native_partial_role,summarise_bracket_state

from app.live_execution.grvt_testnet_order import GrvtTestnetLimits
from app.live_execution.grvt_testnet_signer import (
    GrvtTestnetInstrument,GrvtTestnetLimitDraft,_positive_decimal,_exact_uint,_UINT64_MAX,
)
from app.live_execution.grvt_testnet_protection import GrvtTestnetTrigger,PROTECTION_LIFETIME_NS
from app.live_execution.grvt_testnet_quote import GrvtTestnetQuote,guard_testnet_limit_price
from app.live_execution.grvt_testnet_risk import GrvtTestnetRiskState,evaluate_testnet_risk
from app.derivatives.paper_math import MarginRules


class GrvtTestnetBracketError(RuntimeError):
    """Error de contrato saneado: sin claves, firmas ni payloads remotos."""


BRACKET_FIELDS=frozenset({'schema','parent_client_order_id','sub_account_id','instrument',
    'entry_cap','exit_cap','created_at_ns','instrument_terms','signal_source','price_policy','legs'})


@dataclass(frozen=True,slots=True)
class GrvtTestnetBracketDraft:
    entry: GrvtTestnetLimitDraft
    stop_loss: GrvtTestnetLimitDraft
    take_profit: GrvtTestnetLimitDraft
    stop_trigger: GrvtTestnetTrigger
    target_trigger: GrvtTestnetTrigger


def bracket_client_ids(parent_id: str) -> tuple[str,str,str]:
    if (not isinstance(parent_id,str) or not re.fullmatch(r'[0-9]{19,20}',parent_id)
            or not 2**63 <= int(parent_id) <= 2**64-1):
        raise GrvtTestnetBracketError('Identidad de entrada protegida inválida')
    children = tuple(str(2**63 | (int.from_bytes(hashlib.sha256(
        f'orbis-grvt-testnet-bracket-v1:{parent_id}:{role}'.encode('ascii')).digest()[:8],'big') & (2**63-1)))
        for role in ('STOP_LOSS','TAKE_PROFIT'))
    if len({parent_id,*children}) != 3:
        raise GrvtTestnetBracketError('Colisión de identidades vinculadas')
    return parent_id,*children


def compile_testnet_bracket(entry: GrvtTestnetLimitDraft,state: GrvtTestnetRiskState,
                            rules: MarginRules,instrument: GrvtTestnetInstrument,
                            entry_limits: GrvtTestnetLimits,exit_limits: GrvtTestnetLimits,
                            quote: GrvtTestnetQuote,*,stop_loss: Decimal,take_profit: Decimal,
                            max_spread_bps: int,max_slippage_bps: int,exit_nonces: tuple[int,int],
                            exit_expiration_ns: int,now_ns: int) -> GrvtTestnetBracketDraft:
    """Planifica sin modificar el estado real ni afirmar que la entrada ocurrió."""
    try:
        if (not isinstance(entry,GrvtTestnetLimitDraft) or not isinstance(instrument,GrvtTestnetInstrument)
                or not isinstance(quote,GrvtTestnetQuote) or quote.instrument != instrument.name
                or entry.reduce_only is not False or entry.side not in ('BUY','SELL')
                or entry.builder != '0x'+'0'*40
                or (entry_limits.sub_account_id,entry_limits.instrument) != (exit_limits.sub_account_id,exit_limits.instrument)
                or (entry.sub_account_id,entry.instrument) != (entry_limits.sub_account_id,instrument.name)
                or entry.instrument != entry_limits.instrument
                or type(now_ns) is not int or type(entry.expiration_ns) is not int
                or not now_ns < entry.expiration_ns <= now_ns+300_000_000_000
                or type(exit_expiration_ns) is not int or exit_expiration_ns <= entry.expiration_ns
                or not now_ns+60_000_000_000 <= exit_expiration_ns <= now_ns+PROTECTION_LIFETIME_NS
                or type(max_slippage_bps) is not int or not 0 <= max_slippage_bps <= 100
                or not isinstance(exit_nonces,tuple) or len(exit_nonces) != 2
                or any(type(n) is not int or not 0 <= n < 2**32 for n in (entry.nonce,*exit_nonces))
                or len({entry.nonce,*exit_nonces}) != 3):
            raise ValueError('invalid bracket scope')
        ids = bracket_client_ids(entry.client_order_id)
        size,price,last = _positive_decimal(entry.size),_positive_decimal(entry.limit_price),_positive_decimal(quote.last_price)
        _exact_uint(size,instrument.base_decimals,_UINT64_MAX)
        _exact_uint(price,9,_UINT64_MAX)
        with localcontext() as ctx:
            ctx.prec = 100
            if price % instrument.tick_size or size < instrument.min_size or size*price < instrument.min_notional:
                raise ValueError('entry instrument rules')
        guard_testnet_limit_price(entry.side,price,quote,max_spread_bps=max_spread_bps,
            max_slippage_bps=max_slippage_bps,now_ns=now_ns)
        decision = evaluate_testnet_risk(entry,entry_limits,state,rules,now_ns=now_ns)
        if not decision.allowed or decision.mode != 'OPEN':
            raise ValueError('actual account risk blocked')
        stop,target = GrvtTestnetTrigger('STOP_LOSS',stop_loss),GrvtTestnetTrigger('TAKE_PROFIT',take_profit)
        if (not stop.price < min(last,price) <= max(last,price) < target.price if entry.side == 'BUY'
                else not target.price < min(last,price) <= max(last,price) < stop.price):
            raise ValueError('triggers outside entry and LAST required')
        side = 'SELL' if entry.side == 'BUY' else 'BUY'
        children = []
        for trigger,cid,nonce in zip((stop,target),ids[1:],exit_nonces):
            with localcontext() as ctx:
                ctx.prec = 100
                if trigger.price % instrument.tick_size:
                    raise ValueError('trigger tick')
                multiplier = Decimal(1)+Decimal(max_slippage_bps)/10000*(1 if side == 'BUY' else -1)
                ticks = (trigger.price*multiplier/instrument.tick_size).to_integral_value(
                    rounding=ROUND_FLOOR if side == 'BUY' else ROUND_CEILING)
                limit = ticks*instrument.tick_size
                _exact_uint(limit,9,_UINT64_MAX)
                if size*limit < instrument.min_notional or size*max(last,trigger.price,limit) > exit_limits.max_order_notional:
                    raise ValueError('exit minimum or cap')
            children.append(GrvtTestnetLimitDraft(entry.sub_account_id,entry.instrument,side,size,limit,True,
                entry.builder,cid,nonce,exit_expiration_ns))
        return GrvtTestnetBracketDraft(entry,*children,stop,target)
    except Exception:
        raise GrvtTestnetBracketError('Plan de entrada FOK con SL/TP no verificable') from None


def validate_testnet_bracket(orders: object,entry_limits: GrvtTestnetLimits,
                             exit_limits: GrvtTestnetLimits,*,now_ns: int) -> tuple[str,str,str]:
    """Valida el grupo nativo, no autoriza red ni demuestra riesgo o fills."""
    from app.live_execution.grvt_testnet_order import _validate_limit_contract
    from app.live_execution.grvt_testnet_oco import validate_oco_orders
    try:
        if (not isinstance(orders,(list,tuple)) or len(orders) != 3
                or (entry_limits.sub_account_id,entry_limits.instrument) != (exit_limits.sub_account_id,exit_limits.instrument)):
            raise ValueError('three linked orders required')
        parent,sl,tp = orders
        cid = _validate_limit_contract(parent,entry_limits,now_ns,time_in_force='FILL_OR_KILL',
            metadata_fields={'client_order_id'},lifetime_ns=300_000_000_000)
        ids = bracket_client_ids(cid)
        if validate_oco_orders([sl,tp],exit_limits,now_ns=now_ns) != ids[1:]:
            raise ValueError('unlinked trigger identity')
        entry_leg = parent['legs'][0]
        if (parent['reduce_only'] is not False or parent['builder'] != '0x'+'0'*40
                or any(order['builder'].lower() != parent['builder'] for order in (sl,tp))
                or any(order['signature']['signer'].lower() != parent['signature']['signer'].lower() for order in (sl,tp))
                or len({order['signature']['nonce'] for order in orders}) != 3
                or int(sl['signature']['expiration']) <= int(parent['signature']['expiration'])
                or int(sl['signature']['expiration']) < now_ns+60_000_000_000
                or any(Decimal(order['legs'][0]['size']) != Decimal(entry_leg['size'])
                    or order['legs'][0]['is_buying_asset'] is entry_leg['is_buying_asset'] for order in (sl,tp))):
            raise ValueError('linked order terms differ')
        stop = Decimal(sl['metadata']['trigger']['tpsl']['trigger_price'])
        target = Decimal(tp['metadata']['trigger']['tpsl']['trigger_price'])
        price = Decimal(entry_leg['limit_price'])
        if not (stop < price < target if entry_leg['is_buying_asset'] else target < price < stop):
            raise ValueError('entry outside trigger band')
        return ids
    except Exception:
        raise GrvtTestnetBracketError('Contrato OTOCO testnet no verificable') from None


def sign_testnet_bracket(plan: GrvtTestnetBracketDraft,entry_limits: GrvtTestnetLimits,
                         exit_limits: GrvtTestnetLimits,instrument: GrvtTestnetInstrument,*,
                         dedicated_private_key: bytes,expected_signer: str,now_ns: int) -> list[dict]:
    from app.live_execution.grvt_testnet_signer import _sign_limit
    from app.live_execution.grvt_testnet_protection import sign_testnet_protection
    try:
        if not isinstance(plan,GrvtTestnetBracketDraft): raise ValueError('bracket draft required')
        parent = _sign_limit(plan.entry,entry_limits,instrument,dedicated_private_key=dedicated_private_key,
            expected_signer=expected_signer,now_ns=now_ns,time_in_force='FILL_OR_KILL',time_in_force_code=4)
        children = [sign_testnet_protection(draft,exit_limits,instrument,trigger,
            dedicated_private_key=dedicated_private_key,expected_signer=expected_signer,now_ns=now_ns)
            for draft,trigger in ((plan.stop_loss,plan.stop_trigger),(plan.take_profit,plan.target_trigger))]
        result = [parent,*children]
        verify_testnet_bracket(result,entry_limits,exit_limits,instrument,
            expected_triggers=(plan.stop_trigger,plan.target_trigger),now_ns=now_ns)
        return result
    except Exception:
        raise GrvtTestnetBracketError('No se pudo firmar el grupo OTOCO testnet') from None


def verify_testnet_bracket(orders: object,entry_limits: GrvtTestnetLimits,exit_limits: GrvtTestnetLimits,
                           instrument: GrvtTestnetInstrument,*,expected_triggers: tuple[GrvtTestnetTrigger,GrvtTestnetTrigger],
                           now_ns: int) -> str:
    from app.live_execution.grvt_testnet_signer import _verify_limit
    from app.live_execution.grvt_testnet_protection import verify_testnet_protection
    try:
        validate_testnet_bracket(orders,entry_limits,exit_limits,now_ns=now_ns)
        if (not isinstance(expected_triggers,tuple) or len(expected_triggers) != 2
                or tuple(trigger.kind for trigger in expected_triggers) != ('STOP_LOSS','TAKE_PROFIT')):
            raise ValueError('pinned triggers required')
        signer = _verify_limit(orders[0],entry_limits,instrument,now_ns=now_ns,
            time_in_force='FILL_OR_KILL',time_in_force_code=4)
        children = [verify_testnet_protection(order,exit_limits,instrument,expected_trigger=trigger,now_ns=now_ns)
                    for order,trigger in zip(orders[1:],expected_triggers)]
        if any(value.lower() != signer.lower() for value in children):
            raise ValueError('signers differ')
        return signer
    except Exception:
        raise GrvtTestnetBracketError('Firma o disparadores OTOCO testnet no verificables') from None


def canonical_bracket_order(order: dict) -> dict:
    """Representación exacta para cotejo; requiere contrato ya validado."""
    from app.live_execution.grvt_testnet_oco import _encode,canonical_amount,canonical_oco_order
    import json
    if 'trigger' in order['metadata']:
        return canonical_oco_order(order)
    result = json.loads(_encode(order)[0])
    for leg in result['legs']:
        for name in ('size','limit_price'): leg[name] = canonical_amount(leg[name])
    result['builder'] = result['builder'].lower()
    for name in ('signer','r','s'): result['signature'][name] = result['signature'][name].lower()
    result['signature']['expiration'] = str(int(result['signature']['expiration']))
    return result


def bracket_ack_from_result(result: object,order: dict,entry_limits: GrvtTestnetLimits,
                            exit_limits: GrvtTestnetLimits,*,now_ns: int):
    from app.live_execution.grvt_testnet_oco import _encode
    from app.live_execution.grvt_testnet_order import _ORDER_FIELDS,_validate_limit_contract,GrvtTestnetOrderAck
    from app.live_execution.grvt_testnet_protection import validate_testnet_protection
    try:
        if not isinstance(result,dict): raise ValueError('missing order')
        fields = ('client_order_id','trigger') if 'trigger' in order['metadata'] else ('client_order_id',)
        candidate = {name:result[name] for name in _ORDER_FIELDS}
        if 'trigger' not in order['metadata'] and result['metadata'].get('trigger') is not None:
            raise ValueError('parent converted to a trigger')
        candidate['metadata'] = {name:result['metadata'][name] for name in fields}
        if 'trigger' in candidate['metadata']:
            validate_testnet_protection(candidate,exit_limits,now_ns=now_ns)
        else:
            _validate_limit_contract(candidate,entry_limits,now_ns,time_in_force='FILL_OR_KILL',
                metadata_fields={'client_order_id'},lifetime_ns=300_000_000_000)
        if (_encode(canonical_bracket_order(candidate))[1] != _encode(canonical_bracket_order(order))[1]
                or not isinstance(result.get('order_id'),str)
                or not re.fullmatch(r'[A-Za-z0-9x]{1,80}',result['order_id'])):
            raise ValueError('different linked contract')
        return GrvtTestnetOrderAck(entry_limits.sub_account_id,order['metadata']['client_order_id'],result['order_id'],True)
    except Exception:
        raise GrvtTestnetBracketError('Acuse OTOCO no verificable') from None


def observe_testnet_bracket(client,orders: list[dict],entry_limits: GrvtTestnetLimits,
                             exit_limits: GrvtTestnetLimits,instrument: GrvtTestnetInstrument,*,
                             created_at_ns: int,clock_ns) -> dict:
    """Lee los tres contratos. Ni estado OPEN ni FOK sustituyen evidencia real.

    No cancela, cierra, reenvía ni concede otra entrada. La posición inferida
    de estados de órdenes NO se presenta como posición de cuenta conciliada.
    """
    from app.live_execution.grvt_testnet_oco import _encode,_stamp
    from app.live_execution.grvt_testnet_order import _ORDER_FIELDS
    from app.integrations.grvt_readonly import _order
    import json
    try:
        _stamp(created_at_ns)
        orders = json.loads(_encode(orders)[0])
        triggers = tuple(GrvtTestnetTrigger(order['metadata']['trigger']['trigger_type'],
            Decimal(order['metadata']['trigger']['tpsl']['trigger_price'])) for order in orders[1:])
        verify_testnet_bracket(orders,entry_limits,exit_limits,instrument,expected_triggers=triggers,now_ns=created_at_ns)
        start = clock_ns(); _stamp(start)
        if start < created_at_ns: raise ValueError('clock before preparation')
        previous = start
        result = []
        for role,expected in zip(('ENTRY','STOP_LOSS','TAKE_PROFIT'),orders):
            cid = expected['metadata']['client_order_id']
            raw = client._lookup_order_payload(entry_limits.sub_account_id,cid)
            # Validar tipos antes de normalizar: int(float(ns)) no es una
            # evidencia equivalente a una expiración decimal en cadena.
            bracket_ack_from_result(raw,expected,entry_limits,exit_limits,now_ns=created_at_ns)
            candidate = {name:raw[name] for name in _ORDER_FIELDS}
            fields = ('client_order_id',) if role == 'ENTRY' else ('client_order_id','trigger')
            if role == 'ENTRY' and raw['metadata'].get('trigger') is not None:
                raise ValueError('entry now conditional')
            candidate['metadata'] = {name:raw['metadata'][name] for name in fields}
            if _encode(canonical_bracket_order(candidate))[1] != _encode(canonical_bracket_order(expected))[1]:
                raise ValueError('observed signed contract changed')
            at = clock_ns(); _stamp(at)
            if at < previous: raise ValueError('clock regressed')
            previous = at
            normalised = {**raw,**canonical_bracket_order(candidate),
                'metadata':{**raw['metadata'],**candidate['metadata']}}
            observed = _order(normalised,entry_limits.sub_account_id,open_only=False,now_ns=at)
            leg = observed['legs'][0]
            if (observed['client_order_id'] != cid or len(observed['legs']) != 1
                    or not isinstance(observed.get('create_time_ns'),str)
                    or int(observed['create_time_ns']) < created_at_ns-1_000_000_000
                    or not Decimal(0) <= Decimal(leg['traded_size']) <= Decimal(leg['size'])):
                raise ValueError('observation identity or quantities')
            result.append({'role':role,'client_order_id':cid,'order_id':observed['order_id'],
                'status':observed['status'],'size':leg['size'],'traded_size':leg['traded_size'],
                'create_time_ns':observed['create_time_ns'],'update_time_ns':observed['update_time_ns']})
        if len({row['order_id'] for row in result}) != 3: raise ValueError('overlapping exchange identity')
        at = clock_ns(); _stamp(at)
        if at < previous: raise ValueError('clock regressed')
        return summarise_bracket_state(result,observed_at_ns=at,exit_expiration_ns=int(orders[1]['signature']['expiration']))
    except Exception:
        raise GrvtTestnetBracketError('Observación de los tres contratos OTOCO no verificable; conserva el bloqueo') from None


