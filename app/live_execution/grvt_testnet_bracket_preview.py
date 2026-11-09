"""Revisión de entrada protegida TESTNET: señal verificada, sin firma ni reserva.

La protección manual no despliega todas las salidas de la estrategia PAPER.
El coordinador vuelve a verificar señal, contratos, riesgo e historial al enviar.
"""

from decimal import Decimal,ROUND_FLOOR,ROUND_CEILING,localcontext
import hashlib
import hmac
import re
import secrets

from app.live_execution.grvt_testnet_bracket import (
    GrvtTestnetBracketError,GrvtTestnetBracketDraft,compile_testnet_bracket,sign_testnet_bracket,
)
from app.live_execution.grvt_testnet_journal import require_new_testnet_submission
from app.live_execution.grvt_testnet_oco import _encode,instrument_terms
from app.live_execution.grvt_testnet_order import GrvtTestnetLimits
from app.live_execution.grvt_testnet_protection import GrvtTestnetTrigger
from app.live_execution.grvt_testnet_quote import fetch_testnet_quote
from app.live_execution.grvt_testnet_risk import fetch_testnet_risk
from app.live_execution.grvt_testnet_signal import (
    reviewed_testnet_signal,list_testnet_signal_candidates,SIGNAL_MAX_AGE_NS,
)
from app.live_execution.grvt_testnet_signer import (
    GrvtTestnetLimitDraft,GrvtTestnetInstrument,fetch_testnet_instrument,
)


MAX_ENTRY_NOTIONAL=Decimal('1000')
MAX_EXIT_NOTIONAL=Decimal('10000')
NO_BUILDER='0x'+'0'*40
ENTRY_SCHEMA='orbis.grvt.testnet.bracket.preview.v1'
EMERGENCY_SCHEMA='orbis.grvt.testnet.bracket.emergency.preview.v1'


def preview_bracket_entry(paper,lab,vault,client,*,sub,session_id,action_request_id,size,limit_price,
        entry_cap,exit_cap,stop_loss,take_profit,ttl_minutes,max_spread_bps,max_slippage_bps,
        public_post,clock_ns,client_factory=None) -> dict:
    if (type(entry_cap) is not Decimal or type(exit_cap) is not Decimal
            or not entry_cap.is_finite() or not exit_cap.is_finite()
            or not 0<entry_cap<=MAX_ENTRY_NOTIONAL or not 0<exit_cap<=MAX_EXIT_NOTIONAL
            or type(ttl_minutes) is not int or not 2<=ttl_minutes<=1440):
        raise GrvtTestnetBracketError('Límites de revisión TESTNET inválidos')
    now=clock_ns()
    source=next((item for item in list_testnet_signal_candidates(paper,now_ns=now)
        if item['session_id']==session_id and item['action_request_id']==action_request_id),None)
    if source is None or source['action']!='OPEN' or not source['candidate']:
        raise GrvtTestnetBracketError('No hay una entrada PAPER reciente para revisar')
    entry_limits=GrvtTestnetLimits(sub,source['instrument'],entry_cap)
    exit_limits=GrvtTestnetLimits(sub,source['instrument'],exit_cap)
    require_new_testnet_submission(vault,sub,source['client_order_id'])
    with reviewed_testnet_signal(paper,lab,entry_limits,session_id=session_id,action_request_id=action_request_id,
            side=source['side'],reduce_only=False,now_ns=now,clock_ns=clock_ns) as (_,origin,definition,recorded,checked):
        instrument=fetch_testnet_instrument(entry_limits.instrument,http_post=public_post)
        if client is None:
            if client_factory is None: raise GrvtTestnetBracketError('Falta cliente TESTNET')
            client=client_factory()
        state,rules=fetch_testnet_risk(client,entry_limits.instrument,sub,public_post=public_post,clock_ns=checked)
        quote=fetch_testnet_quote(entry_limits.instrument,http_post=public_post,clock_ns=checked)
        stamp=checked(); nonces=[]
        while len(nonces)<3:
            candidate=secrets.randbits(32)
            if candidate not in nonces: nonces.append(candidate)
        entry=GrvtTestnetLimitDraft(sub,entry_limits.instrument,source['side'],size,limit_price,False,
            NO_BUILDER,source['client_order_id'],nonces[0],recorded+SIGNAL_MAX_AGE_NS)
        draft=compile_testnet_bracket(entry,state,rules,instrument,entry_limits,exit_limits,quote,
            stop_loss=stop_loss,take_profit=take_profit,max_spread_bps=max_spread_bps,
            max_slippage_bps=max_slippage_bps,exit_nonces=tuple(nonces[1:]),
            exit_expiration_ns=stamp+ttl_minutes*60_000_000_000,now_ns=stamp)
        legs=[]
        for role,order,trigger in (('ENTRY',draft.entry,None),('STOP_LOSS',draft.stop_loss,stop_loss),
                ('TAKE_PROFIT',draft.take_profit,take_profit)):
            legs.append({'role':role,'client_order_id':order.client_order_id,'side':order.side,
                'size':format(order.size,'f'),'limit_price':format(order.limit_price,'f'),
                'trigger_price':format(trigger,'f') if trigger is not None else None,'nonce':order.nonce,
                'expiration_ns':str(order.expiration_ns)})
        checked()
        return {'schema':ENTRY_SCHEMA,'network':'TESTNET','mode':'MANUAL_PROTECTED_ENTRY',
            'automatic_strategy':False,'source':origin,'sub_account_id':sub,'instrument':instrument.name,
            'instrument_terms':instrument_terms(instrument),'entry_side':entry.side,'size':format(size,'f'),
            'entry_cap':format(entry_cap,'f'),'exit_cap':format(exit_cap,'f'),'last_price':format(quote.last_price,'f'),
            'created_at_ns':str(stamp),'entry_expiration_ns':str(entry.expiration_ns),
            'expires_at_ms':entry.expiration_ns//1_000_000,'max_spread_bps':max_spread_bps,
            'max_slippage_bps':max_slippage_bps,'legs':legs,
            'strategy_exits_not_deployed':[name for name in ('exit_rules','trailing_stop','max_holding_bars') if name in definition]}


def preview_mac(preview: dict,key: bytes,token: str) -> str:
    encoded=_encode(preview)[0].encode('ascii')
    if len(encoded)>32768: raise GrvtTestnetBracketError('Revisión protegida demasiado grande')
    return hmac.new(key,b'orbis-grvt-testnet-bracket-review-v1\0'+token.encode('ascii')+b'\0'+encoded,
        hashlib.sha256).hexdigest()


def verify_preview(preview: dict,mac: str,key: bytes,token: str,*,schema: str,now_ns: int) -> None:
    try:
        if (type(now_ns) is not int or not secrets.compare_digest(preview_mac(preview,key,token),mac)
                or preview['schema']!=schema or preview['network']!='TESTNET'
                or not 0<=now_ns-int(preview['created_at_ns'])<=120_000_000_000
                or now_ns//1_000_000>=preview['expires_at_ms']):
            raise ValueError('invalid reviewed terms')
    except Exception:
        raise GrvtTestnetBracketError('Revisión alterada, caducada o de otra sesión') from None


def sign_reviewed_entry(vault,preview: dict,*,private_key: str,expected_signer: str,now_ns: int):
    if not re.fullmatch(r'(?:0x)?[0-9a-fA-F]{64}',private_key):
        raise GrvtTestnetBracketError('Clave dedicada TESTNET inválida')
    require_new_testnet_submission(vault,preview['sub_account_id'],preview['source']['client_order_id'])
    terms=preview['instrument_terms']
    instrument=GrvtTestnetInstrument(preview['instrument'],int(terms['asset_id']),terms['base_decimals'],
        Decimal(terms['tick_size']),Decimal(terms['min_size']),Decimal(terms['min_notional']))
    entry_limits=GrvtTestnetLimits(preview['sub_account_id'],preview['instrument'],Decimal(preview['entry_cap']))
    exit_limits=GrvtTestnetLimits(preview['sub_account_id'],preview['instrument'],Decimal(preview['exit_cap']))
    if entry_limits.max_order_notional>MAX_ENTRY_NOTIONAL or exit_limits.max_order_notional>MAX_EXIT_NOTIONAL:
        raise GrvtTestnetBracketError('Tope de revisión TESTNET excedido')
    drafts=[GrvtTestnetLimitDraft(preview['sub_account_id'],preview['instrument'],leg['side'],
        Decimal(leg['size']),Decimal(leg['limit_price']),leg['role']!='ENTRY',NO_BUILDER,
        leg['client_order_id'],leg['nonce'],int(leg['expiration_ns'])) for leg in preview['legs']]
    draft=GrvtTestnetBracketDraft(*drafts,GrvtTestnetTrigger('STOP_LOSS',Decimal(preview['legs'][1]['trigger_price'])),
        GrvtTestnetTrigger('TAKE_PROFIT',Decimal(preview['legs'][2]['trigger_price'])))
    orders=sign_testnet_bracket(draft,entry_limits,exit_limits,instrument,
        dedicated_private_key=bytes.fromhex(private_key.removeprefix('0x')),expected_signer=expected_signer,now_ns=now_ns)
    return orders,entry_limits,exit_limits,instrument


def preview_bracket_emergency(vault,client,parent: str,*,public_post,clock_ns,client_factory=None) -> dict:
    from app.live_execution.grvt_testnet_bracket_emergency import _source,_instrument,require_no_unreconciled_order
    from app.live_execution.grvt_testnet_continuity import check_testnet_history_continuity
    from app.live_execution.grvt_testnet_reconciliation import require_reconciled_continuity
    from app.live_execution.grvt_testnet_risk import evaluate_testnet_risk
    from app.portfolio.models import GrvtTestnetStop
    now=clock_ns(); cid=str(2**63|secrets.randbits(63))
    with vault.session() as db:
        group,plan,anchor,_,held=_source(db,parent,now_ns=now,client_order_id=cid,historical=False)
        require_no_unreconciled_order(db,group.sub_account_id)
        stop=db.get(GrvtTestnetStop,group.sub_account_id)
        if stop is None or not stop.stopped: raise GrvtTestnetBracketError('Activa STOP antes de preparar la emergencia')
        instrument=_instrument(plan)
        limits=GrvtTestnetLimits(group.sub_account_id,plan['instrument'],Decimal(plan['exit_cap']))
        source_id,source_digest=anchor.client_order_id,anchor.receipt_digest
    actual=fetch_testnet_instrument(instrument.name,http_post=public_post)
    if instrument_terms(actual)!=instrument_terms(instrument): raise GrvtTestnetBracketError('Instrumento cambiado')
    if client is None:
        if client_factory is None: raise GrvtTestnetBracketError('Falta cliente TESTNET')
        client=client_factory()
    state,rules=fetch_testnet_risk(client,instrument.name,limits.sub_account_id,public_post=public_post,clock_ns=clock_ns)
    check_testnet_history_continuity(vault,client,limits.sub_account_id,now_ns=clock_ns())
    quote=fetch_testnet_quote(instrument.name,http_post=public_post,clock_ns=clock_ns)
    with vault.session() as db:
        require_reconciled_continuity(db,limits.sub_account_id,{'settle_currency':state.settle_currency,
            'settle_balance':format(state.settle_balance,'f'),'positions':[[name,format(size,'f')] for name,size in state.positions]},
            now_ns=clock_ns())
    side='SELL' if held>0 else 'BUY'
    size=held.copy_abs()
    with localcontext() as ctx:
        ctx.prec=100
        price=((quote.bid if held>0 else quote.ask)/instrument.tick_size).to_integral_value(
            rounding=ROUND_FLOOR if held>0 else ROUND_CEILING)*instrument.tick_size
        if size<instrument.min_size or size*price<instrument.min_notional:
            raise GrvtTestnetBracketError('El residuo es inferior al mínimo del instrumento')
    now=clock_ns(); nonce=secrets.randbits(32)
    draft=GrvtTestnetLimitDraft(limits.sub_account_id,instrument.name,side,size,price,True,NO_BUILDER,
        cid,nonce,now+10_000_000_000)
    if not evaluate_testnet_risk(draft,limits,state,rules,now_ns=now).allowed:
        raise GrvtTestnetBracketError('Riesgo de cierre no verificable')
    return {'schema':EMERGENCY_SCHEMA,'network':'TESTNET','parent_client_order_id':parent,
        'client_order_id':cid,'sub_account_id':limits.sub_account_id,'instrument':instrument.name,
        'instrument_terms':instrument_terms(instrument),'side':side,'size':format(size,'f'),
        'limit_price':format(price,'f'),'max_order_notional':format(limits.max_order_notional,'f'),
        'source_client_order_id':source_id,'source_receipt_digest':source_digest,'nonce':nonce,
        'created_at_ns':str(now),'expires_at_ms':(now+120_000_000_000)//1_000_000}


def sign_reviewed_emergency(vault,preview: dict,*,private_key: str,expected_signer: str,now_ns: int):
    from app.live_execution.grvt_testnet_bracket_emergency import _source,_instrument,require_no_unreconciled_order
    from app.live_execution.grvt_testnet_signer import sign_testnet_limit
    from app.portfolio.models import GrvtTestnetOrderIntent
    if not re.fullmatch(r'(?:0x)?[0-9a-fA-F]{64}',private_key):
        raise GrvtTestnetBracketError('Clave dedicada TESTNET inválida')
    with vault.session() as db:
        if db.get(GrvtTestnetOrderIntent,preview['client_order_id']) is not None:
            raise GrvtTestnetBracketError('Cierre ya archivado; recupera su ID')
        group,plan,anchor,_,held=_source(db,preview['parent_client_order_id'],now_ns=now_ns,
            client_order_id=preview['client_order_id'],historical=False)
        require_no_unreconciled_order(db,group.sub_account_id)
        if anchor.client_order_id!=preview['source_client_order_id'] or anchor.receipt_digest!=preview['source_receipt_digest']:
            raise GrvtTestnetBracketError('El origen de la emergencia cambió desde su revisión')
        instrument=_instrument(plan)
        limits=GrvtTestnetLimits(group.sub_account_id,instrument.name,Decimal(plan['exit_cap']))
        if (preview['sub_account_id']!=limits.sub_account_id or Decimal(preview['size'])!=held.copy_abs()
                or preview['side']!=('SELL' if held>0 else 'BUY')
                or preview['instrument_terms']!=instrument_terms(instrument)):
            raise GrvtTestnetBracketError('Ámbito de revisión cambiado')
    draft=GrvtTestnetLimitDraft(limits.sub_account_id,limits.instrument,preview['side'],Decimal(preview['size']),
        Decimal(preview['limit_price']),True,NO_BUILDER,preview['client_order_id'],preview['nonce'],now_ns+10_000_000_000)
    return sign_testnet_limit(draft,limits,instrument,dedicated_private_key=bytes.fromhex(private_key.removeprefix('0x')),
        expected_signer=expected_signer,now_ns=now_ns)
