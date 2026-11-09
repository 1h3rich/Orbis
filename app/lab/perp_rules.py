"""Dated observations of public GRVT rules. They do not establish effective periods."""
from __future__ import annotations

from decimal import Decimal
from itertools import groupby
import json
import time
from typing import Literal, Self

from pydantic import Field, model_validator

from app.lab.access import lab_format
from app.lab.engine import LabError
from app.lab.perp_dataset import ExactModel, InstrumentSnapshot, PerpDataset, RiskSnapshot, validate_rule_pair
from app.lab.store import LabStore, content_hash, encoded


Symbol = Literal['BTC/USDT:PERP','ETH/USDT:PERP','SOL/USDT:PERP']
SCHEMA = 'orbis.lab.perp.rules.observation.v1'
SCOPE = 'OBSERVED_AT_CAPTURE_ONLY'
PAGE_SIZE = 50
WINDOW_CAP = 2000
SECOND_NS = 10**9

PERP_RULE_OBSERVATIONS_DDL = (
    '''CREATE TABLE IF NOT EXISTS perp_rule_observations (
        hash TEXT PRIMARY KEY NOT NULL,
        symbol TEXT NOT NULL,
        observed_at_ns INTEGER NOT NULL CHECK(observed_at_ns > 0),
        payload_json TEXT NOT NULL
    )''',
    '''CREATE INDEX IF NOT EXISTS ix_perp_rule_observations_symbol_time
       ON perp_rule_observations(symbol,observed_at_ns DESC,hash DESC)''',
)


class RuleObservation(ExactModel):
    schema_name: Literal['orbis.lab.perp.rules.observation.v1'] = Field(alias='schema')
    scope: Literal['OBSERVED_AT_CAPTURE_ONLY']
    provider: Literal['grvt']
    symbol: Symbol
    rules: RiskSnapshot
    specification: InstrumentSnapshot

    @model_validator(mode='after')
    def consistent(self) -> Self:
        validate_rule_pair(self.rules,self.specification,self.symbol.replace('/','_').replace(':PERP','_Perp'))
        earlier,later=sorted((int(self.rules.fetched_at_ns),int(self.specification.fetched_at_ns)))
        if not 0 < earlier <= later < 2**63 or later-earlier > 3600*SECOND_NS:
            raise ValueError('Observación de reglas fuera de su ventana de captura')
        return self

    @property
    def observed_at_ns(self) -> int:
        return max(int(self.rules.fetched_at_ns),int(self.specification.fetched_at_ns))

    def payload(self) -> dict:
        return self.model_dump(mode='json',by_alias=True)

    @classmethod
    def from_dataset(cls, dataset: PerpDataset) -> Self:
        return cls(schema=SCHEMA,scope=SCOPE,provider=dataset.provider,symbol=dataset.symbol,
                   rules=dataset.rules,specification=dataset.specification)


def capture_rule_observation(symbol: Symbol, *, adapter=None, clock=None) -> RuleObservation:
    from app.marketdata.adapters.grvt import GrvtAdapter

    clock = clock or time.time_ns
    source = adapter or GrvtAdapter()
    started = clock()
    rules = source.get_margin_rules(symbol)
    spec = source.get_instrument_spec(symbol)
    finished = clock()
    try:
        if (rules is None or spec is None or type(started) is not int or type(finished) is not int
                or not 0 <= finished-started <= 30*SECOND_NS):
            raise ValueError('captura incompleta o caducada')
        for raw in (rules,spec):
            stamp = raw['fetched_at_ns']
            if type(stamp) is not int or not started <= stamp <= finished:
                raise ValueError('marca temporal de captura inválida')
        return RuleObservation(schema=SCHEMA,scope=SCOPE,provider='grvt',symbol=symbol,
            rules={**rules,'fetched_at_ns':str(rules['fetched_at_ns'])},
            specification={**spec,'fetched_at_ns':str(spec['fetched_at_ns'])})
    except (ValueError, TypeError, KeyError) as exc:
        raise LabError('GRVT no devolvió reglas actuales verificables') from exc


def read_observation(row) -> tuple[str, RuleObservation]:
    try:
        raw = json.loads(row['payload_json'])
        observation = RuleObservation.model_validate(raw)
        digest = content_hash(raw)
        if (observation.payload()!=raw or digest!=row['hash'] or observation.symbol!=row['symbol']
                or observation.observed_at_ns!=row['observed_at_ns']):
            raise ValueError('observación de reglas alterada')
        return digest,observation
    except (ValueError, TypeError, KeyError) as exc:
        raise LabError('Observación de reglas perpetuas no verificable') from exc


def insert_observation(db, observation: RuleObservation) -> str:
    payload=observation.payload()
    observation=RuleObservation.model_validate(payload)
    digest=content_hash(payload)
    prior=db.execute('SELECT * FROM perp_rule_observations WHERE hash=?',(digest,)).fetchone()
    if prior is not None:
        return read_observation(prior)[0]
    db.execute('INSERT INTO perp_rule_observations (hash,symbol,observed_at_ns,payload_json) VALUES (?,?,?,?)',
               (digest,observation.symbol,observation.observed_at_ns,encoded(payload)))
    return digest


def _meaning(snapshot) -> dict:
    """Compare normalized quantities and sets, preserving exact archived wire values."""
    decimal_fields={'max_position_size','min_size','min_notional','tick_size','notional_floor','notional_cap',
                    'initial_margin_rate','maintenance_margin_rate','cumulative_maintenance_amount'}

    def normalize(value,key=''):
        if isinstance(value,dict):
            return {name:normalize(item,name) for name,item in value.items() if name!='fetched_at_ns'}
        if isinstance(value,list):
            return [normalize(item) for item in value]
        if key in decimal_fields and value is not None:
            return Decimal(value)
        return value

    payload=snapshot.model_dump()
    if 'venues' in payload:
        payload['venues']=sorted(payload['venues'])
    return normalize(payload)


def _changes(current: RuleObservation, earlier: RuleObservation) -> list[str]:
    return [key for key in ('rules','specification')
            if _meaning(getattr(current,key))!=_meaning(getattr(earlier,key))]


def _observed_instants(rows, assumption: RuleObservation) -> list[dict]:
    if len(rows)>WINDOW_CAP:
        raise LabError('Hay demasiadas capturas para este periodo de reglas')
    result = []
    for at, peers in groupby(rows, key=lambda row: row['observed_at_ns']):
        captures = [read_observation(row) for row in peers]
        digest,first = captures[0]
        conflict = any(_changes(observation,first) for _,observation in captures[1:])
        result.append({'observed_at_ns':str(at),'capture_count':len(captures),
                       'hash':None if conflict else digest,
                       'changes':None if conflict else _changes(first,assumption)})
    return result


def rules_for_window(db, assumption: RuleObservation, start_ns: int, end_ns: int) -> dict:
    """Point evidence for an exact requested window, without inferring rule validity."""
    if not 0 < start_ns < end_ns < 2**63:
        raise LabError('Periodo de reglas no válido')
    symbol = assumption.symbol
    rows = db.execute('''SELECT * FROM perp_rule_observations WHERE symbol=?
                         AND observed_at_ns>=? AND observed_at_ns<?
                         ORDER BY observed_at_ns,hash LIMIT ?''',
                      (symbol,start_ns,end_ns,WINDOW_CAP+1)).fetchall()
    within = _observed_instants(rows,assumption)
    before = db.execute('SELECT max(observed_at_ns) FROM perp_rule_observations WHERE symbol=? AND observed_at_ns<?',
                        (symbol,start_ns)).fetchone()[0]
    after = db.execute('SELECT min(observed_at_ns) FROM perp_rule_observations WHERE symbol=? AND observed_at_ns>=?',
                       (symbol,end_ns)).fetchone()[0]

    def boundary(at: int | None) -> dict | None:
        if at is None:
            return None
        peers = db.execute('''SELECT * FROM perp_rule_observations WHERE symbol=? AND observed_at_ns=?
                              ORDER BY hash LIMIT ?''',(symbol,at,WINDOW_CAP+1)).fetchall()
        return _observed_instants(peers,assumption)[0]

    at = assumption.observed_at_ns
    return {
        'scope':SCOPE,'effective_history':'UNVERIFIED',
        'symbol':symbol,'window':{'from_ns':str(start_ns),'to_ns':str(end_ns)},
        'assumed':{'hash':content_hash(assumption.payload()),'observed_at_ns':str(at),
                   'location':'BEFORE' if at<start_ns else 'AFTER' if at>=end_ns else 'WITHIN'},
        'before':boundary(before),'after':boundary(after),
        'within':{
            'capture_count':len(rows),
            'matching_instants':sum(item['changes']==[] for item in within),
            'different_instants':sum(bool(item['changes']) for item in within),
            'ambiguous_instants':sum(item['changes'] is None for item in within),
            'first':within[0] if within else None,'last':within[-1] if within else None,
            'latest_difference':next((item for item in reversed(within) if item['changes']),None),
        },
    }


class RuleObservationStore:
    def __init__(self, lab: LabStore):
        self.lab=lab

    def _require_encrypted(self) -> None:
        if not self.lab.encrypted or lab_format(self.lab.path)!='encrypted_or_invalid':
            raise LabError('El historial de reglas requiere LAB cifrado y desbloqueado')

    def save(self, observation: RuleObservation) -> dict:
        self._require_encrypted()
        with self.lab.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            digest=insert_observation(db,observation)
        return {'hash':digest,'observed_at_ns':str(observation.observed_at_ns),'observation':observation.payload()}

    def page(self, symbol: Symbol, *, before: tuple[int,str] | None = None) -> dict:
        self._require_encrypted()
        with self.lab.connect() as db:
            rows=db.execute('''SELECT o.*, EXISTS(
                                  SELECT 1 FROM perp_rule_observations peer
                                  WHERE peer.symbol=o.symbol AND peer.observed_at_ns=o.observed_at_ns AND peer.hash!=o.hash
                              ) AS tied FROM perp_rule_observations o
                              WHERE o.symbol=? AND (o.observed_at_ns,o.hash)<(?,?)
                              ORDER BY o.observed_at_ns DESC,o.hash DESC LIMIT ?''',
                            (symbol,*(before or (2**63-1,'g')),PAGE_SIZE+1)).fetchall()
        observations=[read_observation(row) for row in rows]
        items=[]
        for index,(digest,current) in enumerate(observations[:PAGE_SIZE]):
            earlier=observations[index+1][1] if index+1<len(observations) else None
            tied=bool(rows[index]['tied'] or earlier is not None and rows[index+1]['tied'])
            items.append({'hash':digest,'observed_at_ns':str(current.observed_at_ns),'observation':current.payload(),
                'changes':_changes(current,earlier) if earlier is not None and not tied else None,
                'same_instant':tied})
        last=items[-1] if len(observations)>PAGE_SIZE else None
        return {'symbol':symbol,'scope':SCOPE,'items':items,
                'next_before':{'ns':last['observed_at_ns'],'hash':last['hash']} if last else None}
