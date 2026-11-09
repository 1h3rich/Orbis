"""Contratos protegidos MAINNET, independientes de permisos y datos TESTNET.

Sólo construye/verifica contratos. El envío requiere el diario y preflight
MAINNET. Una firma o acuse nunca acredita una ejecución o rentabilidad.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal,localcontext,ROUND_FLOOR,ROUND_CEILING
import json
import re
from typing import Literal

from eth_account import Account
from app.live_execution.grvt_order_codec import order_signable


ROLES=('ENTRY','STOP_LOSS','TAKE_PROFIT')
ZERO_ADDRESS='0x'+'0'*40
ENTRY_LIFETIME_NS=20_000_000_000
MAX_ENTRY_CAP=Decimal('1000')
MAX_EXIT_CAP=Decimal('10000')
_ADDRESS=re.compile(r'0x[a-fA-F0-9]{40}\Z')
_HEX32=re.compile(r'0x[a-fA-F0-9]{64}\Z')


class GrvtMainnetError(RuntimeError):
    """Error seguro, sin respuestas remotas, claves, cookies ni firmas."""


def decimal_amount(value: object) -> Decimal:
    if (type(value) is not Decimal or not value.is_finite() or value<=0
            or len(value.as_tuple().digits)>30 or abs(value.as_tuple().exponent)>18):
        raise GrvtMainnetError('Cantidad MAINNET fuera de rango')
    return value


def exact_uint(value: Decimal,scale: int,maximum: int) -> int:
    with localcontext() as context:
        context.prec=100
        scaled=value*(Decimal(10)**scale)
    if scaled!=scaled.to_integral_value() or not 0<scaled<=maximum:
        raise GrvtMainnetError('Precisión incompatible con GRVT MAINNET')
    return int(scaled)


@dataclass(frozen=True,slots=True)
class MainnetInstrument:
    name: str
    asset_id: int
    base_decimals: int
    tick_size: Decimal
    min_size: Decimal
    min_notional: Decimal
    network: Literal['MAINNET']='MAINNET'

    def __post_init__(self):
        if (self.network!='MAINNET' or not isinstance(self.name,str)
                or not re.fullmatch(r'[A-Z0-9]+_USDT_Perp',self.name)
                or type(self.asset_id) is not int or not 0<self.asset_id<2**256
                or type(self.base_decimals) is not int or not -6<=self.base_decimals<=18):
            raise GrvtMainnetError('Metadatos MAINNET inválidos')
        for name in ('tick_size','min_size','min_notional'): decimal_amount(getattr(self,name))


@dataclass(frozen=True,slots=True)
class MainnetBracketDraft:
    sub_account_id: str
    instrument: str
    direction: Literal['LONG','SHORT']
    size: Decimal
    entry_price: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    entry_cap: Decimal
    exit_cap: Decimal
    exit_seconds: int=3600
    max_spread_bps: int=25
    max_slippage_bps: int=25

    def __post_init__(self):
        if (not isinstance(self.sub_account_id,str) or not re.fullmatch(r'[0-9]{1,20}',self.sub_account_id)
                or not 0<int(self.sub_account_id)<2**64 or str(int(self.sub_account_id))!=self.sub_account_id
                or not isinstance(self.instrument,str) or not re.fullmatch(r'[A-Z0-9]+_USDT_Perp',self.instrument)
                or self.direction not in ('LONG','SHORT') or type(self.exit_seconds) is not int
                or not 120<=self.exit_seconds<=86400
                or any(type(value) is not int or not 0<=value<=100 for value in (self.max_spread_bps,self.max_slippage_bps))):
            raise GrvtMainnetError('Ámbito MAINNET inválido')
        for name in ('size','entry_price','stop_loss','take_profit','entry_cap','exit_cap'):
            decimal_amount(getattr(self,name))
        if (self.entry_cap>MAX_ENTRY_CAP or self.exit_cap>MAX_EXIT_CAP
                or not (self.stop_loss<self.entry_price<self.take_profit if self.direction=='LONG'
                    else self.take_profit<self.entry_price<self.stop_loss)):
            raise GrvtMainnetError('Límites o protección MAINNET inválidos')


def client_ids(parent: str) -> tuple[str,str,str]:
    if (not isinstance(parent,str) or not re.fullmatch(r'[0-9]{19,20}',parent)
            or not 2**63<=int(parent)<2**64 or str(int(parent))!=parent):
        raise GrvtMainnetError('Identificador MAINNET inválido')
    return parent,str(int(parent)^1),str(int(parent)^2)


def unsigned_mainnet_bracket(draft: MainnetBracketDraft,instrument: MainnetInstrument,*,
        parent_client_order_id: str,nonces: tuple[int,int,int],issued_at_ns: int) -> list[dict]:
    if (not isinstance(draft,MainnetBracketDraft) or not isinstance(instrument,MainnetInstrument)
            or instrument.network!='MAINNET' or draft.instrument!=instrument.name
            or type(issued_at_ns) is not int or not 0<issued_at_ns<2**63-draft.exit_seconds*1_000_000_000
            or not isinstance(nonces,tuple) or len(nonces)!=3 or len(set(nonces))!=3
            or any(type(value) is not int or not 0<=value<2**32 for value in nonces)):
        raise GrvtMainnetError('Contrato MAINNET inválido')
    ids=client_ids(parent_client_order_id); buying=draft.direction=='LONG'
    orders=[]
    with localcontext() as context:
        context.prec=100
        if draft.size<instrument.min_size: raise GrvtMainnetError('Cantidad inferior al mínimo MAINNET')
        exact_uint(draft.size,instrument.base_decimals,2**64-1)
        for index,role in enumerate(ROLES):
            trigger=None if index==0 else draft.stop_loss if index==1 else draft.take_profit
            if trigger is None: price=draft.entry_price
            else:
                if trigger%instrument.tick_size: raise GrvtMainnetError('Disparador fuera del tick MAINNET')
                bound=trigger*(Decimal(10000)+(-draft.max_slippage_bps if buying else draft.max_slippage_bps))/10000
                price=(bound/instrument.tick_size).to_integral_value(rounding=ROUND_CEILING if buying else ROUND_FLOOR)*instrument.tick_size
                if draft.size*trigger>draft.exit_cap: raise GrvtMainnetError('Disparador superior al tope MAINNET')
            decimal_amount(price); exact_uint(price,9,2**64-1)
            if (price%instrument.tick_size or draft.size*price<instrument.min_notional
                    or draft.size*price>(draft.entry_cap if index==0 else draft.exit_cap)):
                raise GrvtMainnetError('Precio, mínimo o tope MAINNET incompatible')
            metadata={'client_order_id':ids[index]}
            if trigger is not None:
                metadata['trigger']={'trigger_type':role,'tpsl':{'trigger_by':'LAST','trigger_price':format(trigger,'f'),
                    'close_position':False,'is_split_position':False}}
            orders.append({'sub_account_id':draft.sub_account_id,'is_market':False,
                'time_in_force':'FILL_OR_KILL' if index==0 else 'GOOD_TILL_TIME','post_only':False,'reduce_only':index!=0,
                'legs':[{'instrument':draft.instrument,'size':format(draft.size,'f'),'limit_price':format(price,'f'),
                    'is_buying_asset':buying if index==0 else not buying}],
                'metadata':metadata,'builder':ZERO_ADDRESS,'builder_fee':'0',
                'signature':{'nonce':nonces[index],'expiration':str(issued_at_ns+(ENTRY_LIFETIME_NS if index==0 else draft.exit_seconds*1_000_000_000)),
                    'chain_id':'325'}})
    return orders


def _message(order: dict,instrument: MainnetInstrument) -> dict:
    leg=order['legs'][0]; signature=order['signature']
    return {'subAccountID':int(order['sub_account_id']),'isMarket':False,
        'timeInForce':{'FILL_OR_KILL':4,'GOOD_TILL_TIME':1,'IMMEDIATE_OR_CANCEL':3}[order['time_in_force']],
        'postOnly':False,'reduceOnly':order['reduce_only'],
        'legs':[{'assetID':instrument.asset_id,'contractSize':exact_uint(Decimal(leg['size']),instrument.base_decimals,2**64-1),
            'limitPrice':exact_uint(Decimal(leg['limit_price']),9,2**64-1),'isBuyingContract':leg['is_buying_asset']}],
        'builder':ZERO_ADDRESS,'builderFee':0,'nonce':signature['nonce'],'expiration':int(signature['expiration'])}


def sign_mainnet_bracket(draft: MainnetBracketDraft,instrument: MainnetInstrument,*,parent_client_order_id: str,
        nonces: tuple[int,int,int],private_key: bytes,expected_signer: str,now_ns: int) -> list[dict]:
    orders=unsigned_mainnet_bracket(draft,instrument,parent_client_order_id=parent_client_order_id,
        nonces=nonces,issued_at_ns=now_ns)
    try:
        if type(private_key) is not bytes or len(private_key)!=32 or not isinstance(expected_signer,str) or not _ADDRESS.fullmatch(expected_signer):
            raise ValueError('dedicated signer')
        signer=Account.from_key(private_key)
        if signer.address.lower()!=expected_signer.lower(): raise ValueError('wrong signer')
        for order in orders:
            signed=signer.sign_message(order_signable(_message(order,instrument),chain_id=325))
            order['signature'].update(signer=signer.address,r='0x'+signed.r.to_bytes(32,'big').hex(),
                s='0x'+signed.s.to_bytes(32,'big').hex(),v=signed.v)
        verify_mainnet_bracket(orders,draft,instrument,issued_at_ns=now_ns,now_ns=now_ns)
        return orders
    except Exception:
        raise GrvtMainnetError('No se pudo firmar la entrada protegida MAINNET') from None
    finally: del private_key


def verify_mainnet_bracket(orders: list[dict],draft: MainnetBracketDraft,instrument: MainnetInstrument,*,
        issued_at_ns: int,now_ns: int) -> str:
    try:
        if (not isinstance(orders,list) or len(orders)!=3 or type(now_ns) is not int
                or not issued_at_ns<=now_ns<issued_at_ns+ENTRY_LIFETIME_NS): raise ValueError('expired contract')
        nonces=tuple(order['signature']['nonce'] for order in orders)
        expected=unsigned_mainnet_bracket(draft,instrument,parent_client_order_id=orders[0]['metadata']['client_order_id'],
            nonces=nonces,issued_at_ns=issued_at_ns)
        signer=None
        for order,template in zip(orders,expected):
            signature=order['signature']
            if (set(signature)!={'nonce','expiration','chain_id','signer','r','s','v'}
                    or any(signature[key]!=template['signature'][key] for key in ('nonce','expiration','chain_id'))
                    or type(signature['v']) is not int or signature['v'] not in (27,28)
                    or not isinstance(signature['signer'],str) or not _ADDRESS.fullmatch(signature['signer'])
                    or any(not isinstance(signature[key],str) or not _HEX32.fullmatch(signature[key]) for key in ('r','s'))):
                raise ValueError('signature fields')
            actual={**order,'signature':{key:signature[key] for key in ('nonce','expiration','chain_id')}}
            if json.dumps(actual,sort_keys=True,allow_nan=False)!=json.dumps(template,sort_keys=True,allow_nan=False):
                raise ValueError('changed contract')
            recovered=Account.recover_message(order_signable(_message(order,instrument),chain_id=325),
                vrs=(signature['v'],int(signature['r'],16),int(signature['s'],16)))
            if recovered.lower()!=signature['signer'].lower() or signer is not None and signer.lower()!=recovered.lower():
                raise ValueError('wrong signer')
            signer=recovered
        return signer
    except Exception:
        raise GrvtMainnetError('Contrato o firma MAINNET no verificable') from None
