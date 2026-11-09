"""Decodifica margin_rules sin mezclarlo con los sobres de otros endpoints.

GRVT publica este DTO directamente. El sobre único `result` se admite por
compatibilidad; nunca se elige entre dos versiones contradictorias del DTO.
La validación financiera y del instrumento corresponde a MarginRules.
"""

import httpx


def margin_rules_response(response: httpx.Response) -> dict:
    if response.status_code != 200:
        raise ValueError('margin rules unavailable')
    body = response.json()
    if not isinstance(body, dict) or 'error' in body or 'code' in body:
        raise ValueError('invalid margin rules response')
    if 'result' in body:
        if set(body) != {'result'}:
            raise ValueError('ambiguous margin rules response')
        body = body['result']
    if (not isinstance(body, dict) or 'error' in body or 'code' in body or 'result' in body
            or not {'instrument', 'max_position_size', 'risk_brackets'} <= set(body)):
        raise ValueError('incomplete margin rules response')
    return body
