"""Transporte interno MAINNET de grupo protegido: una transmisión por instancia.

El cliente no constituye autorización. El servicio debe reservar el diario
y comprobar permiso/cuenta antes de invocarlo. No acepta URLs configurables.
"""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import hmac
import json
import re
from threading import Lock
import time

import httpx

from app.live_execution.grvt_mainnet_bracket import (GrvtMainnetError,MainnetInstrument,
    MainnetBracketDraft,ROLES,verify_mainnet_bracket)


AUTH_URL='https://edge.grvt.io/auth/api_key/login'
BULK_URL='https://trades.grvt.io/full/v2/bulk_orders'
INSTRUMENT_URL='https://market-data.grvt.io/full/v1/instrument'
TICKER_URL='https://market-data.grvt.io/full/v1/ticker'
MARGIN_URL='https://market-data.grvt.io/full/v1/margin_rules'
LOOKUP_URL='https://trades.grvt.io/full/v1/order'
ORDER_HISTORY_URL='https://trades.grvt.io/full/v1/order_history'
_READ={'account_summary':'https://trades.grvt.io/full/v1/account_summary',
    'open_orders':'https://trades.grvt.io/full/v1/open_orders',
    'get_all_initial_leverage':'https://trades.grvt.io/full/v1/get_all_initial_leverage'}
HISTORY_URLS={name:'https://trades.grvt.io/full/v1/'+name for name in (
    'fill_history','funding_payment_history','transfer_history','withdrawal_history',
    'deposit_history','interest_payment_history')}


class MainnetOrderNotFound(GrvtMainnetError):
    """The direct lookup returned GRVT's Data Not Found response."""


def public_post(url: str,body: dict) -> httpx.Response:
    return httpx.post(url,json=body,timeout=5,follow_redirects=False,trust_env=False)


def _private_post(url: str,body: dict,headers: dict) -> httpx.Response:
    return httpx.post(url,json=body,headers=headers,timeout=5,follow_redirects=False,trust_env=False)


def _result(reply: httpx.Response) -> dict:
    value=reply.json() if reply.status_code==200 else None
    if not isinstance(value,dict) or 'error' in value or 'code' in value:
        raise ValueError('unconfirmed response')
    return value


def _text_decimal(raw: object):
    from decimal import Decimal
    if not isinstance(raw,str) or not re.fullmatch(r'[0-9]{1,30}(?:\.[0-9]{1,18})?',raw):
        raise ValueError('invalid instrument decimal')
    return Decimal(raw)


def fetch_mainnet_instrument(name: str,*,http_post: Callable=public_post) -> MainnetInstrument:
    try:
        if not isinstance(name,str) or not re.fullmatch(r'[A-Z0-9]+_USDT_Perp',name): raise ValueError('instrument')
        row=_result(http_post(INSTRUMENT_URL,{'instrument':name}))['result']
        if (not isinstance(row,dict) or row.get('instrument')!=name or row.get('kind')!='PERPETUAL'
                or not isinstance(row.get('venues'),list) or 'ORDERBOOK' not in row['venues']):
            raise ValueError('instrument source')
        raw=row['instrument_hash']
        if not isinstance(raw,str) or not re.fullmatch(r'(?:0x[a-fA-F0-9]{1,64}|[0-9]{1,78})',raw):
            raise ValueError('instrument identity')
        return MainnetInstrument(name,int(raw,16 if raw.startswith('0x') else 10),row['base_decimals'],
            _text_decimal(row['tick_size']),_text_decimal(row['min_size']),_text_decimal(row['min_notional']))
    except Exception:
        raise GrvtMainnetError('Metadatos públicos GRVT MAINNET no verificables') from None


class GrvtMainnetClient:
    def __init__(self,api_key: str,*,http_post: Callable=_private_post,clock_ns: Callable=time.time_ns):
        if (not isinstance(api_key,str) or not 8<=len(api_key)<=256 or not api_key.isascii()
                or any(char.isspace() for char in api_key)):
            raise GrvtMainnetError('Clave GRVT MAINNET inválida')
        self._post=http_post; self._clock=clock_ns; self._used=False; self._cancel_used=False; self._lock=Lock()
        try:
            reply=http_post(AUTH_URL,{'api_key':api_key},{'Cookie':'rm=true;','Content-Type':'application/json'})
            data=_result(reply); cookie=reply.cookies.get('gravity'); account=reply.headers.get('x-grvt-account-id')
            sub=data.get('sub_account_id')
            if (data.get('status')!='success' or not isinstance(cookie,str) or not 8<=len(cookie)<=4096
                    or not cookie.isascii() or any(char.isspace() or char in ';\r\n' for char in cookie)
                    or not isinstance(account,str) or not re.fullmatch(r'0x[a-fA-F0-9]{40}',account)
                    or not isinstance(sub,str) or not re.fullmatch(r'[0-9]{1,20}',sub) or not 0<int(sub)<2**64
                    or str(int(sub))!=sub):
                raise ValueError('session scope')
            self._cookie=cookie; self._account=account; self._sub=sub
        except Exception:
            raise GrvtMainnetError('GRVT MAINNET no confirmó una sesión de subcuenta') from None
        finally: del api_key

    def __repr__(self): return 'GrvtMainnetClient(<redacted>)'

    @property
    def network(self): return 'MAINNET'

    @property
    def sub_account_id(self): return self._sub

    def identity_ref(self,salt: str) -> str:
        return hmac.new(bytes.fromhex(salt),('GRVT_MAINNET_ACCOUNT:'+self._account.lower()).encode('ascii'),hashlib.sha256).hexdigest()

    def _headers(self):
        return {'Cookie':'gravity='+self._cookie,'X-Grvt-Account-Id':self._account,'Content-Type':'application/json'}

    def read_account(self,path: str,sub: str) -> dict:
        try:
            if path not in _READ or sub!=self._sub: raise ValueError('scope')
            return _result(self._post(_READ[path],{'sub_account_id':sub},self._headers()))
        except Exception:
            raise GrvtMainnetError('Datos de cuenta MAINNET no verificables') from None

    def position_configuration(self,sub: str,instrument: str):
        from app.integrations.grvt_position_config import configuration_response,scope
        try:
            scope(sub,instrument)
            started=self._clock()
            raw=self.read_account('get_all_initial_leverage',sub)
            return configuration_response(raw,sub,instrument,requested_at_ns=started,received_at_ns=self._clock())
        except Exception:
            raise GrvtMainnetError('Configuración por instrumento MAINNET no verificable') from None

    def lookup_order_payload(self,sub: str,cid: str) -> dict:
        try:
            if (sub!=self._sub or not isinstance(cid,str) or not re.fullmatch(r'[0-9]{19,20}',cid)
                    or not 2**63<=int(cid)<2**64 or str(int(cid))!=cid): raise ValueError('order scope')
            reply = self._post(LOOKUP_URL,{'sub_account_id':sub,'client_order_id':cid},self._headers())
            if reply.status_code == 404:
                data = reply.json()
                if isinstance(data,dict) and data.get('code') in (1004,'1004'):
                    raise MainnetOrderNotFound('GRVT no encuentra la orden MAINNET')
            return _result(reply)['result']
        except MainnetOrderNotFound:
            raise
        except Exception:
            raise GrvtMainnetError('Orden MAINNET no verificable; no se reenvía') from None

    def order_history_page(self, sub: str, instrument: str, *,
                           start_ns: int, end_ns: int, cursor: str) -> dict:
        market = re.fullmatch(r'([A-Z0-9]+)_USDT_Perp', instrument) if isinstance(instrument,str) else None
        if market is None:
            raise GrvtMainnetError('Historial de órdenes MAINNET fuera de alcance')
        return self._history_page(ORDER_HISTORY_URL, sub, start_ns=start_ns, end_ns=end_ns,
                                  cursor=cursor, filters={'kind':['PERPETUAL'],'base':[market[1]],'quote':['USDT']})

    def history_page(self,name: str,sub: str,*,start_ns: int,end_ns: int,cursor: str='') -> dict:
        if not isinstance(name,str) or name not in HISTORY_URLS:
            raise GrvtMainnetError('Historial MAINNET fuera de alcance o no verificable')
        return self._history_page(HISTORY_URLS[name], sub, start_ns=start_ns, end_ns=end_ns, cursor=cursor)

    def _history_page(self, url: str, sub: str, *, start_ns: int, end_ns: int, cursor: str,
                      filters: dict | None = None) -> dict:
        try:
            if (sub!=self._sub or type(start_ns) is not int
                    or type(end_ns) is not int or not 1<start_ns<=end_ns<2**63
                    or not isinstance(cursor,str) or len(cursor)>1024
                    or not cursor.isascii() or any(ord(char)<32 for char in cursor)):
                raise ValueError('history scope')
            return _result(self._post(url,{'sub_account_id':sub,'start_time':str(start_ns-1),
                'end_time':str(end_ns),'limit':500,'cursor':cursor,**(filters or {})},self._headers()))
        except Exception:
            raise GrvtMainnetError('Historial MAINNET fuera de alcance o no verificable') from None

    def submit_reduction_once(self,wire: dict,template: dict,instrument: MainnetInstrument,*,
            require_permission: Callable,clock_ns: Callable=time.time_ns) -> dict:
        from app.live_execution.grvt_mainnet_reduction_order import verify_reduction
        wire=json.loads(json.dumps(wire,allow_nan=False));verify_reduction(wire,template,instrument)
        if wire['sub_account_id']!=self._sub:raise GrvtMainnetError('Subcuenta de reducción MAINNET distinta')
        with self._lock:
            if self._used:raise GrvtMainnetError('Transporte de reducción MAINNET consumido')
            require_permission();now=clock_ns()
            if type(now) is not int or not 0<int(template['signature']['expiration'])-now<=20_000_000_000:
                raise GrvtMainnetError('Reducción MAINNET caducada')
            self._used=True
            try:
                result=_result(self._post(BULK_URL,{'sub_account_id':self._sub,'orders':[wire],
                    'order_i_ds':[],'client_order_i_ds':[]},self._headers()))['result']
                if (result.get('cancel_acks')!=[] or not isinstance(result.get('orders'),list) or len(result['orders'])!=1):
                    raise ValueError('reduction acknowledgement shape')
                raw=result['orders'][0]
                from app.live_execution.grvt_mainnet_remote_order import remote_contract
                core=remote_contract(raw,wire)
                if (json.dumps(core,sort_keys=True,allow_nan=False)!=json.dumps(wire,sort_keys=True,allow_nan=False)
                        or not isinstance(raw.get('order_id'),str) or not re.fullmatch(r'[A-Za-z0-9x]{1,80}',raw['order_id'])):
                    raise ValueError('reduction acknowledgement changed')
                return {'client_order_id':wire['metadata']['client_order_id'],'order_id':raw['order_id']}
            except Exception:
                raise GrvtMainnetError('Reducción MAINNET no confirmada; consulta el ID sin reenviar') from None

    def cancel_bracket_once(self,sub: str,parent: str,*,require_permission: Callable) -> None:
        from app.live_execution.grvt_mainnet_bracket import client_ids
        if sub!=self._sub:raise GrvtMainnetError('Subcuenta de cancelación MAINNET distinta')
        client_ids(parent)
        with self._lock:
            if self._cancel_used:raise GrvtMainnetError('Cancelación MAINNET consumida; consulta los IDs')
            require_permission();self._cancel_used=True
            try:
                value=_result(self._post(BULK_URL,{'sub_account_id':sub,'orders':[],'order_i_ds':[],
                    'client_order_i_ds':[parent],'time_to_live_ms':'5000'},self._headers()))['result']
                if (not isinstance(value,dict) or value.get('orders')!=[] or not isinstance(value.get('cancel_acks'),list)
                        or len(value['cancel_acks'])!=1 or not (value['cancel_acks'][0].get('ack') is True
                            or value['cancel_acks'][0].get('ack')=='true')):
                    raise ValueError('cancel acknowledgement')
            except Exception:
                raise GrvtMainnetError('Cancelación MAINNET no confirmada; consulta los IDs y no reenvíes') from None

    def submit_limit_once(self, contract, *, require_permission: Callable, clock_ns: Callable) -> str:
        from app.derivatives.perp_observed_types import MAX_SIGNAL_DELAY_NS
        from app.live_execution.grvt_mainnet_remote_order import remote_contract
        from app.live_execution.grvt_perp_limit_plan import FRESH_NS, NS
        from app.live_execution.grvt_perp_limit_receipt import ORDER_ID, PerpLimitContract, _standalone_order
        from app.lab.store import content_hash

        contract = PerpLimitContract.model_validate(contract.payload())
        wire = contract.wire
        if wire['sub_account_id'] != self._sub:
            raise GrvtMainnetError('Subcuenta LIMIT MAINNET distinta')
        with self._lock:
            if self._used:
                raise GrvtMainnetError('Transporte LIMIT MAINNET consumido')
            require_permission()
            now = clock_ns()
            if (type(now) is not int or not 0 <= now-int(contract.issued_at_ns) <= FRESH_NS
                    or not 0 <= now-int(contract.plan.source.signal_known_at_ns) <= MAX_SIGNAL_DELAY_NS
                    or now + NS > int(wire['signature']['expiration'])):
                raise GrvtMainnetError('Orden LIMIT MAINNET vencida antes del envío')
            self._used = True
            try:
                result = _result(self._post(BULK_URL, {'sub_account_id':self._sub,'orders':[wire],
                    'order_i_ds':[],'client_order_i_ds':[]}, self._headers()))['result']
                if result.get('cancel_acks') != [] or type(result.get('orders')) is not list or len(result['orders']) != 1:
                    raise ValueError('Acuse LIMIT incompleto')
                echo = result['orders'][0]
                order_id = echo.get('order_id')
                if (type(order_id) is not str or order_id == '0' or not ORDER_ID.fullmatch(order_id)
                        or not _standalone_order(echo.get('metadata'))
                        or content_hash(remote_contract(echo,wire)) != content_hash(wire)):
                    raise ValueError('Acuse LIMIT distinto del contrato')
                return order_id
            except Exception:
                raise GrvtMainnetError('Envío LIMIT MAINNET incierto; consulta su identificador') from None

    def cancel_limit_once(self, sub: str, order_id: str, *, require_permission: Callable) -> None:
        from app.live_execution.grvt_perp_limit_receipt import ORDER_ID

        if sub != self._sub or type(order_id) is not str or order_id == '0' or not ORDER_ID.fullmatch(order_id):
            raise GrvtMainnetError('Cancelación LIMIT MAINNET fuera de ámbito')
        with self._lock:
            if self._cancel_used:
                raise GrvtMainnetError('Cancelación LIMIT MAINNET ya consumida')
            require_permission()
            self._cancel_used = True
            try:
                result = _result(self._post(BULK_URL, {'sub_account_id':sub,'orders':[],
                    'order_i_ds':[order_id],'client_order_i_ds':[]}, self._headers()))['result']
                acks = result.get('cancel_acks')
                if (result.get('orders') != [] or type(acks) is not list or len(acks) != 1
                        or not (acks[0].get('ack') is True or acks[0].get('ack') == 'true')):
                    raise ValueError('Acuse de cancelación LIMIT inválido')
            except Exception:
                raise GrvtMainnetError('Cancelación LIMIT MAINNET incierta; consulta el estado y los fills') from None

    def submit_bracket_once(self,orders: list[dict],draft: MainnetBracketDraft,instrument: MainnetInstrument,*,
            issued_at_ns: int,clock_ns: Callable,require_permission: Callable) -> dict:
        wire=json.loads(json.dumps(orders,allow_nan=False))
        if draft.sub_account_id!=self._sub: raise GrvtMainnetError('Subcuenta MAINNET distinta de la autenticada')
        verify_mainnet_bracket(wire,draft,instrument,issued_at_ns=issued_at_ns,now_ns=clock_ns())
        with self._lock:
            if self._used: raise GrvtMainnetError('Transporte MAINNET consumido; consulta el diario')
            require_permission()
            self._used=True
            try:
                reply=self._post(BULK_URL,{'sub_account_id':self._sub,'orders':wire,'order_i_ds':[],
                    'client_order_i_ds':[]},self._headers())
                result=_result(reply)['result']
                if (not isinstance(result,dict) or result.get('cancel_acks')!=[]
                        or not isinstance(result.get('orders'),list) or len(result['orders'])!=3):
                    raise ValueError('incomplete acknowledgement')
                matched=[]
                for role,original,row in zip(ROLES,wire,result['orders']):
                    if not isinstance(row,dict) or not isinstance(row.get('metadata'),dict): raise ValueError('ack shape')
                    from app.live_execution.grvt_mainnet_remote_order import remote_contract
                    core=remote_contract(row,original)
                    if (json.dumps(core,sort_keys=True,allow_nan=False)!=json.dumps(original,sort_keys=True,allow_nan=False)
                            or not isinstance(row.get('order_id'),str) or not re.fullmatch(r'[A-Za-z0-9x]{1,80}',row['order_id'])):
                        raise ValueError('changed acknowledgement')
                    matched.append({'role':role,'client_order_id':original['metadata']['client_order_id'],'order_id':row['order_id']})
                if len({row['order_id'] for row in matched})!=3: raise ValueError('duplicate exchange identity')
                return {'orders':matched,'execution_unverified':True,'position_verified':False}
            except Exception:
                raise GrvtMainnetError('Envío MAINNET no confirmado; consulta el intento y no lo repitas') from None
