"""Un único contrato IOC reduce-only MAINNET. No concede autoridad de red."""

from dataclasses import dataclass
from decimal import Decimal,ROUND_FLOOR,ROUND_CEILING,localcontext
import json
import re
from typing import Protocol

from eth_account import Account
from app.lab.perp_book_types import exact
from app.live_execution.grvt_mainnet_bracket import (GrvtMainnetError,MainnetInstrument,
    ZERO_ADDRESS,client_ids,decimal_amount,exact_uint,_message)
from app.live_execution.grvt_order_codec import order_signable
from app.live_execution.grvt_mainnet_preflight import number,canonical
from app.live_execution.grvt_mainnet_transport import TICKER_URL


class ReductionScope(Protocol):
    @property
    def sub_account_id(self) -> str: ...
    @property
    def instrument(self) -> str: ...
    @property
    def direction(self) -> str: ...
    @property
    def size(self) -> Decimal: ...
    @property
    def exit_cap(self) -> Decimal: ...
    @property
    def max_spread_bps(self) -> int: ...
    @property
    def max_slippage_bps(self) -> int: ...


@dataclass(frozen=True)
class BoundedReductionScope:
    sub_account_id: str
    instrument: str
    direction: str
    size: Decimal
    exit_cap: Decimal
    max_spread_bps: int
    max_slippage_bps: int


def reduction_price(quote: dict,draft: ReductionScope,instrument: MainnetInstrument,*,at_ns: int) -> Decimal:
    if (set(quote)!={'source','instrument','event_time_ns','observed_at_ns','bid','ask','bid_size','ask_size'}
            or quote['source']!=TICKER_URL or quote['instrument']!=draft.instrument
            or any(type(quote[key]) is not int or not 0<quote[key]<=at_ns+2_000_000_000
                or at_ns-quote[key]>5_000_000_000 for key in ('event_time_ns','observed_at_ns'))
            or quote['observed_at_ns']>at_ns):raise ValueError('reduction quote age or source')
    with localcontext() as context:
        context.prec=100
        bid,ask=number(quote['bid']),number(quote['ask']);buying=draft.direction=='SHORT'
        if (not 0<bid<=ask or min(number(quote['bid_size']),number(quote['ask_size']))<=0
                or (ask-bid)*10000/((bid+ask)/2)>draft.max_spread_bps):raise ValueError('reduction spread or liquidity')
        reference=ask if buying else bid
        bound=reference*(10000+(draft.max_slippage_bps if buying else -draft.max_slippage_bps))/10000
        price=(bound/instrument.tick_size).to_integral_value(rounding=ROUND_FLOOR if buying else ROUND_CEILING)*instrument.tick_size
        if price<=0 or (price<ask if buying else price>bid):raise ValueError('unmarketable reduction price')
        return price


@exact
def bounded_reduction(quote: dict, scope: ReductionScope, instrument: MainnetInstrument,
                      requested: Decimal, *, at_ns: int) -> tuple[Decimal, Decimal]:
    price = reduction_price(quote, scope, instrument, at_ns=at_ns)
    reference = number(quote['ask' if scope.direction == 'SHORT' else 'bid'])
    step = Decimal(1).scaleb(-instrument.base_decimals)
    cap = scope.exit_cap / max(price, reference)
    size = min(requested, (cap / step).to_integral_value(rounding=ROUND_FLOOR) * step)
    return size, price


def unsigned_reduction(draft: ReductionScope,instrument: MainnetInstrument,*,cid: str,size: Decimal,
        price: Decimal,nonce: int,issued_at_ns: int,expiration_ns: int) -> dict:
    client_ids(cid);decimal_amount(size);decimal_amount(price)
    if (type(nonce) is not int or not 0<=nonce<2**32 or type(issued_at_ns) is not int
            or type(expiration_ns) is not int or not 0<issued_at_ns<expiration_ns<2**63
            or not 1_000_000_000<=expiration_ns-issued_at_ns<=20_000_000_000
            or instrument.name!=draft.instrument or instrument.network!='MAINNET'):
        raise GrvtMainnetError('Contrato de reducción MAINNET fuera de alcance')
    exact_uint(size,instrument.base_decimals,2**64-1);exact_uint(price,9,2**64-1)
    with localcontext() as context:
        context.prec=100
        if (size>draft.size or size<instrument.min_size or size*price<instrument.min_notional or size*price>draft.exit_cap
                or price%instrument.tick_size):raise GrvtMainnetError('Residuo o precio incompatible con los mínimos y topes MAINNET')
    return {'sub_account_id':draft.sub_account_id,'is_market':False,'time_in_force':'IMMEDIATE_OR_CANCEL',
        'post_only':False,'reduce_only':True,'legs':[{'instrument':draft.instrument,'size':canonical(size),
            'limit_price':canonical(price),'is_buying_asset':draft.direction=='SHORT'}],
        'metadata':{'client_order_id':cid},'builder':ZERO_ADDRESS,'builder_fee':'0',
        'signature':{'nonce':nonce,'expiration':str(expiration_ns),'chain_id':'325'}}


def sign_reduction(template: dict,instrument: MainnetInstrument,*,private_key: bytes,expected_signer: str) -> dict:
    try:
        if type(private_key) is not bytes or len(private_key)!=32:raise ValueError('dedicated key')
        signer=Account.from_key(private_key)
        if signer.address.lower()!=expected_signer.lower():raise ValueError('signer mismatch')
        signed=signer.sign_message(order_signable(_message(template,instrument),chain_id=325))
        wire=json.loads(json.dumps(template))
        wire['signature'].update(signer=signer.address,r='0x'+signed.r.to_bytes(32,'big').hex(),
            s='0x'+signed.s.to_bytes(32,'big').hex(),v=signed.v)
        verify_reduction(wire,template,instrument);return wire
    except Exception:
        raise GrvtMainnetError('Firma dedicada de reducción MAINNET no verificable') from None
    finally:del private_key


def verify_reduction(wire: dict,template: dict,instrument: MainnetInstrument) -> str:
    try:
        signature=wire['signature'];base={**wire,'signature':{key:signature[key] for key in ('nonce','expiration','chain_id')}}
        if (set(signature)!={'nonce','expiration','chain_id','signer','r','s','v'}
                or json.dumps(base,sort_keys=True,allow_nan=False)!=json.dumps(template,sort_keys=True,allow_nan=False)
                or type(signature['v']) is not int or signature['v'] not in (27,28)
                or not isinstance(signature['signer'],str) or not re.fullmatch(r'0x[a-fA-F0-9]{40}',signature['signer'])
                or any(not isinstance(signature[key],str) or not re.fullmatch(r'0x[a-fA-F0-9]{64}',signature[key]) for key in ('r','s'))):
            raise ValueError('changed reduction contract')
        recovered=Account.recover_message(order_signable(_message(wire,instrument),chain_id=325),
            vrs=(signature['v'],int(signature['r'],16),int(signature['s'],16)))
        if recovered.lower()!=signature['signer'].lower():raise ValueError('wrong reduction signer')
        return recovered
    except Exception:
        raise GrvtMainnetError('Contrato firmado de reducción MAINNET alterado') from None
