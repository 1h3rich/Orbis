"""Per-instrument GRVT configuration, observed locally, without write authority.

The documented read returns ``results``, not the account-summary ``result``.
It has no exchange event timestamp. Local request times must never be presented
as proof that the configuration remained unchanged outside those observations.
"""
from __future__ import annotations

from decimal import Decimal
import re
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


POSITION_CONFIG_URL = 'https://trades.grvt.io/full/v1/get_all_initial_leverage'
MAX_CONFIG_AGE_NS = 5_000_000_000


def scope(sub_account_id: str, instrument: str) -> None:
    if (type(sub_account_id) is not str or not re.fullmatch(r'[1-9][0-9]{0,19}', sub_account_id)
            or int(sub_account_id) >= 2**64 or type(instrument) is not str
            or not re.fullmatch(r'[A-Z0-9]{1,30}_USDT_Perp', instrument)):
        raise ValueError('Ámbito de configuración GRVT inválido')


def leverage(raw: object) -> Decimal:
    if type(raw) is not str or not re.fullmatch(r'[0-9]{1,5}(?:\.[0-9]{1,18})?', raw):
        raise ValueError('Apalancamiento GRVT no verificable')
    value = Decimal(raw)
    if not 1 <= value <= 10000:
        raise ValueError('Apalancamiento GRVT fuera de rango')
    return value


class PositionConfiguration(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True, populate_by_name=True)
    schema_name: Literal['orbis.grvt.position.configuration.v1'] = Field(alias='schema')
    network: Literal['MAINNET']
    source: Literal['https://trades.grvt.io/full/v1/get_all_initial_leverage']
    sub_account_id: str
    instrument: str
    margin_type: Literal['CROSS', 'ISOLATED']
    leverage: str
    min_leverage: str
    max_leverage: str
    requested_at_ns: str
    received_at_ns: str

    @model_validator(mode='after')
    def valid(self) -> Self:
        scope(self.sub_account_id, self.instrument)
        if not leverage(self.min_leverage) <= leverage(self.leverage) <= leverage(self.max_leverage):
            raise ValueError('Límites de apalancamiento incoherentes')
        for at in (self.requested_at_ns, self.received_at_ns):
            if not re.fullmatch(r'[1-9][0-9]{0,18}', at) or int(at) >= 2**63:
                raise ValueError('Hora de observación GRVT inválida')
        if not 0 <= int(self.received_at_ns) - int(self.requested_at_ns) <= MAX_CONFIG_AGE_NS:
            raise ValueError('Consulta de configuración GRVT antigua o reloj regresado')
        return self

    def payload(self) -> dict:
        return self.model_dump(mode='json', by_alias=True)

    def terms(self) -> tuple:
        return (self.sub_account_id, self.instrument, self.margin_type,
                leverage(self.leverage), leverage(self.min_leverage), leverage(self.max_leverage))


def configuration_response(raw: dict, sub_account_id: str, instrument: str, *,
                           requested_at_ns: int, received_at_ns: int) -> PositionConfiguration:
    scope(sub_account_id, instrument)
    if (type(raw) is not dict or 'error' in raw or 'code' in raw
            or type(raw.get('results')) is not list or not 1 <= len(raw['results']) <= 10000
            or type(requested_at_ns) is not int or type(received_at_ns) is not int):
        raise ValueError('Configuración GRVT incompleta')
    found = None
    seen = set()
    for row in raw['results']:
        if type(row) is not dict:
            raise ValueError('Configuración GRVT malformada')
        name = row.get('instrument')
        # Other instrument kinds may coexist; they cannot impersonate the target.
        if (type(name) is not str or not re.fullmatch(r'[A-Za-z0-9_:\-]{3,100}', name)
                or name in seen):
            raise ValueError('Instrumento GRVT inválido o duplicado')
        seen.add(name)
        if name == instrument:
            found = row
    if found is None:
        raise ValueError('GRVT no devolvió configuración explícita para este instrumento')
    return PositionConfiguration.model_validate({
        'schema': 'orbis.grvt.position.configuration.v1', 'network': 'MAINNET',
        'source': POSITION_CONFIG_URL, 'sub_account_id': sub_account_id, 'instrument': instrument,
        **{key: found.get(key) for key in ('margin_type', 'leverage', 'min_leverage', 'max_leverage')},
        'requested_at_ns': str(requested_at_ns), 'received_at_ns': str(received_at_ns),
    })


def verify_configuration_pair(first: PositionConfiguration, last: PositionConfiguration, *, now_ns: int) -> None:
    # Reparse even instances: model_copy / model_construct do not confer trust.
    first = PositionConfiguration.model_validate(first.payload())
    last = PositionConfiguration.model_validate(last.payload())
    if (type(now_ns) is not int or not 0 < now_ns < 2**63
            or first.terms() != last.terms()
            or not int(first.requested_at_ns) <= int(first.received_at_ns) <= int(last.requested_at_ns)
                    <= int(last.received_at_ns) <= now_ns
            or now_ns - int(first.requested_at_ns) > MAX_CONFIG_AGE_NS):
        raise ValueError('Configuración GRVT cambiante, antigua o fuera de ámbito')
