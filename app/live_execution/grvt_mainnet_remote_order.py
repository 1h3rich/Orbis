"""Normaliza sólo representación al contrato revisado; nunca cambia su valor.

Permite ceros decimales y casing de direcciones/hex en el eco del proveedor.
El verificador de firmas y el digest original se comprueban después.
"""

from decimal import Decimal
import re

from eth_utils import to_checksum_address
from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError


_DECIMAL=re.compile(r'-?[0-9]{1,30}(?:\.[0-9]{1,18})?\Z')


def _pick(raw,expected,key=''):
    if isinstance(expected,dict):return {name:_pick(raw[name],value,name) for name,value in expected.items()}
    if isinstance(expected,list):
        if not isinstance(raw,list) or len(raw)!=len(expected):raise ValueError('leg count')
        return [_pick(item,value) for item,value in zip(raw,expected)]
    if key in ('size','limit_price','trigger_price','builder_fee'):
        if (not isinstance(raw,str) or not _DECIMAL.fullmatch(raw)
                or not isinstance(expected,str) or not _DECIMAL.fullmatch(expected)
                or Decimal(raw)!=Decimal(expected)):raise ValueError('decimal contract value changed')
        return expected
    return raw


def remote_contract(raw: dict,template: dict) -> dict:
    try:
        result={key:_pick(raw[key],value,key) for key,value in template.items() if key!='signature'}
        signature={key:raw['signature'][key] for key in ('nonce','expiration','chain_id','signer','r','s','v')}
        if (not isinstance(signature['signer'],str) or not re.fullmatch(r'0x[a-fA-F0-9]{40}',signature['signer'])
                or any(not isinstance(signature[key],str) or not re.fullmatch(r'0x[a-fA-F0-9]{64}',signature[key]) for key in ('r','s'))
                or not isinstance(signature['expiration'],str) or not re.fullmatch(r'[0-9]{1,20}',signature['expiration'])
                or int(signature['expiration'])!=int(template['signature']['expiration'])):
            raise ValueError('signature representation or expiration changed')
        signature['signer']=to_checksum_address(signature['signer'])
        signature['r']=signature['r'].lower();signature['s']=signature['s'].lower()
        signature['expiration']=template['signature']['expiration'];result['signature']=signature
        return result
    except Exception:
        raise GrvtMainnetError('El contrato remoto MAINNET no coincide con la revisión original') from None


def standalone_order_metadata(metadata: dict) -> bool:
    # These venue fields affect execution but are outside the EIP-712 signature.
    if (type(metadata) is not dict or metadata.get('is_ecn', False) is not False
            or metadata.get('is_private', False) is not False or metadata.get('rfq_id') not in (None, '')
            or type(metadata.get('slippage_bps', 0)) is not int or metadata.get('slippage_bps', 0) != 0):
        return False
    trigger = metadata.get('trigger')
    if trigger in (None, ''):
        return True
    if type(trigger) is not dict or trigger.get('trigger_type') != 'UNSPECIFIED':
        return False
    details = trigger.get('tpsl')
    return (details is None or type(details) is dict
            and details.get('trigger_by') == 'UNSPECIFIED'
            and isinstance(details.get('trigger_price'),str)
            and _DECIMAL.fullmatch(details['trigger_price'])
            and Decimal(details['trigger_price']) == 0
            and details.get('close_position') is False and details.get('is_split_position') is False)
