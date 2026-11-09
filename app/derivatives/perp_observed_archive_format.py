"""Encrypted observed-session tables and streaming replay. No execution authority."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime,timezone
import json
import zlib
from uuid import UUID
from typing import Callable

from pydantic import Field

from app.derivatives.perp_observed_types import ObservedError,ObservedState
from app.derivatives.perp_observed_wire import (
    ExactInput,INPUT,ObservedSeed,apply_input,checkpoint,read_observed_seed,transition_evidence,
)
from app.lab.perp_dataset import SYMBOLS
from app.lab.store import content_hash,encoded
from app.research.perp_reference import ObservedResearchReference


DDL=(
    '''CREATE TABLE paper_observed_sessions (
        id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
        start_request_id TEXT NOT NULL UNIQUE, request_hash TEXT NOT NULL,
        runtime_id TEXT NOT NULL, seed_json TEXT NOT NULL, seed_hash TEXT NOT NULL,
        origin_json TEXT NOT NULL, origin_hash TEXT NOT NULL,
        sequence INTEGER NOT NULL CHECK(sequence>=0),head_hash TEXT NOT NULL,
        checkpoint_json TEXT NOT NULL,status TEXT NOT NULL
    )''',
    '''CREATE TABLE paper_observed_events (
        session_id TEXT NOT NULL REFERENCES paper_observed_sessions(id),
        sequence INTEGER NOT NULL CHECK(sequence>=0), request_id TEXT NOT NULL,
        input_hash TEXT NOT NULL,previous_hash TEXT,hash TEXT NOT NULL,
        input_blob BLOB NOT NULL,evidence_blob BLOB NOT NULL,
        PRIMARY KEY(session_id,sequence),UNIQUE(session_id,request_id)
    )''',
    '''CREATE TABLE paper_observed_candles (
        session_id TEXT NOT NULL REFERENCES paper_observed_sessions(id),
        candle_t INTEGER NOT NULL,candle_json TEXT NOT NULL,
        origin TEXT NOT NULL CHECK(origin IN ('SEED','OBSERVED')),
        PRIMARY KEY(session_id,candle_t)
    )''',
    '''CREATE TABLE paper_observed_trades (
        session_id TEXT NOT NULL REFERENCES paper_observed_sessions(id),
        trade_index INTEGER NOT NULL,event_sequence INTEGER NOT NULL,
        payload_json TEXT NOT NULL,PRIMARY KEY(session_id,trade_index),
        FOREIGN KEY(session_id,event_sequence) REFERENCES paper_observed_events(session_id,sequence)
    )''',
    'CREATE INDEX ix_paper_observed_created ON paper_observed_sessions(created_at,id)',
)
TABLES=('paper_observed_sessions','paper_observed_events','paper_observed_candles','paper_observed_trades')
MAX_EVENT_BYTES=4*1024*1024
MAX_SEED_BYTES=64*1024*1024


class OriginSnapshot(ExactInput):
    reference: ObservedResearchReference
    trade_prefix_hash: str=Field(pattern=r'^[0-9a-f]{64}$')
    trade_prefix_count: int=Field(ge=1,le=5000)

    def check_seed(self,seed: ObservedSeed) -> None:
        reference=self.reference
        created=datetime.fromisoformat(reference.created_at).astimezone(timezone.utc)
        delta=created-datetime(1970,1,1,tzinfo=timezone.utc)
        created_ns=(delta.days*86400+delta.seconds)*10**9+delta.microseconds*1000
        if (seed.profile.payload()!=reference.profile.payload() or seed.timeframe!=reference.timeframe
                or reference.symbol not in SYMBOLS
                or seed.quote.instrument!=reference.symbol.replace('/','_').replace(':PERP','_Perp')
                or int(seed.received_at_ns)<created_ns
                or len(seed.history)<self.trade_prefix_count
                or content_hash([bar.model_dump() for bar in seed.history[:self.trade_prefix_count]])!=self.trade_prefix_hash):
            raise ObservedError('El seed PAPER no corresponde al origen validado')


def pack(payload: dict) -> bytes:
    raw=encoded(payload).encode('utf-8')
    if len(raw)>MAX_EVENT_BYTES:raise ObservedError('Evento observado demasiado grande')
    return zlib.compress(raw,6)


def unpack(raw: bytes) -> dict:
    if type(raw) is not bytes or len(raw)>MAX_EVENT_BYTES+1024:
        raise ObservedError('Bloque observado inválido o excesivo')
    try:
        decoder=zlib.decompressobj();decoded=decoder.decompress(raw,MAX_EVENT_BYTES+1)
        if (len(decoded)>MAX_EVENT_BYTES or not decoder.eof
                or decoder.unconsumed_tail or decoder.unused_data):
            raise ValueError('Bloque truncado, concatenado o excesivo')
        result=json.loads(decoded)
        if type(result) is not dict:raise ValueError('Objeto requerido')
        return result
    except (ValueError,zlib.error,UnicodeError) as exc:
        raise ObservedError('Bloque observado no verificable') from exc


def event_hash(session_id: str,sequence: int,previous: str|None,source: dict,evidence: dict) -> str:
    return content_hash({'session_id':session_id,'sequence':sequence,'previous':previous,
                         'input':source,'evidence':evidence})


def start_source(seed_hash: str,origin_hash: str) -> dict:
    return {'kind':'START','seed_hash':seed_hash,'origin_hash':origin_hash}


@dataclass(frozen=True)
class ReplayedObserved:
    state: ObservedState
    history_hash: str
    sequence: int
    head_hash: str
    seed_hash: str
    origin_hash: str
    checkpoint: dict


def _event_rows(db,session_id: str,last: int):
    """Release each short read cursor before running CPU work on its rows."""
    after=-1
    while after<last:
        rows=db.execute('SELECT * FROM paper_observed_events WHERE session_id=? AND sequence>? AND sequence<=? ORDER BY sequence LIMIT 16',
                        (session_id,after,last)).fetchall()
        if not rows:break
        for row in rows:yield row
        after=rows[-1]['sequence']


def replay_observed_session(db,header,*,allow_appended_prefix: bool=False,
        on_verified_event: Callable[[int,dict,ObservedState],None]|None=None) -> ReplayedObserved:
    """Stream evidence; never load a financial account from saved checkpoint JSON."""
    if header is None:raise ObservedError('Sesión PAPER observada no encontrada')
    try:
        for key in ('id','runtime_id','start_request_id'):
            if str(UUID(header[key]))!=header[key]:raise ObservedError('Identificador de archivo no canónico')
        if datetime.fromisoformat(header['created_at']).utcoffset() is None:
            raise ObservedError('Fecha de archivo sin zona horaria')
        if len(header['seed_json'].encode('utf-8'))>MAX_SEED_BYTES:
            raise ObservedError('Seed observado excesivo')
        seed=read_observed_seed(json.loads(header['seed_json']))
        origin=OriginSnapshot.model_validate(json.loads(header['origin_json']))
        seed_hash=content_hash(seed.payload());origin_hash=content_hash(origin.payload())
        if (seed_hash!=header['seed_hash'] or origin_hash!=header['origin_hash']
                or header['request_hash']!=origin_hash
                or seed.payload()!=json.loads(header['seed_json'])
                or origin.payload()!=json.loads(header['origin_json'])):
            raise ObservedError('Seed u origen PAPER alterados')
        origin.check_seed(seed)
        transition=seed.start();history_hash,evidence=transition_evidence(None,transition,None)
        state=transition.state;previous=None;count=0;trade_count=0
        for row in _event_rows(db,header['id'],header['sequence']):
            if str(UUID(row['request_id']))!=row['request_id']:raise ObservedError('Petición de evento no canónica')
            if type(row['sequence']) is not int or row['sequence']!=count or row['previous_hash']!=previous:
                raise ObservedError('Cadena observada discontinua')
            source=unpack(row['input_blob']);saved=unpack(row['evidence_blob'])
            if count==0:
                if source!=start_source(seed_hash,origin_hash):raise ObservedError('Inicio observado alterado')
                if row['request_id']!=header['start_request_id']:raise ObservedError('Petición de inicio alterada')
            else:
                event=INPUT.validate_python(source)
                if event.payload()!=source:raise ObservedError('Input observado no canónico')
                transition=apply_input(state,event)
                history_hash,evidence=transition_evidence(state,transition,history_hash)
                state=transition.state
            digest=event_hash(header['id'],count,previous,source,evidence)
            if (content_hash(source)!=row['input_hash'] or saved!=evidence or digest!=row['hash']):
                raise ObservedError('El evento observado no reproduce sus resultados')
            for trade in evidence['trades']:
                indexed=db.execute('SELECT event_sequence,payload_json FROM paper_observed_trades WHERE session_id=? AND trade_index=?',
                    (header['id'],trade_count)).fetchone()
                if indexed is None or indexed['event_sequence']!=count or indexed['payload_json']!=encoded(trade):
                    raise ObservedError('Índice de operaciones observadas divergente')
                trade_count+=1
            if on_verified_event is not None:on_verified_event(count,evidence,state)
            previous=digest;count+=1
        current=checkpoint(state,history_hash)
        if (count==0 or header['sequence']!=count-1 or header['head_hash']!=previous
                or header['checkpoint_json']!=encoded(current) or header['status']!=state.status
                or db.execute('SELECT count(*) FROM paper_observed_trades WHERE session_id=? AND event_sequence<=?',(header['id'],header['sequence'])).fetchone()[0]!=trade_count):
            raise ObservedError('Checkpoint observado no coincide con la repetición')
        rows=db.execute('SELECT candle_t,candle_json,origin FROM paper_observed_candles WHERE session_id=? AND candle_t<=? ORDER BY candle_t',
                        (header['id'],state.history[-1].t)).fetchall()
        candles=iter(state.history)
        for index,row in enumerate(rows):
            candle=next(candles,None)
            if (candle is None or row['candle_t']!=candle.t or row['candle_json']!=encoded(candle.json())
                    or row['origin']!=('SEED' if index<len(seed.history) else 'OBSERVED')):
                raise ObservedError('Archivo de velas observadas divergente')
        if next(candles,None) is not None:raise ObservedError('Faltan velas observadas')
        if not allow_appended_prefix:
            if (db.execute('SELECT count(*) FROM paper_observed_events WHERE session_id=?',(header['id'],)).fetchone()[0]!=count
                    or db.execute('SELECT count(*) FROM paper_observed_trades WHERE session_id=?',(header['id'],)).fetchone()[0]!=trade_count
                    or db.execute('SELECT count(*) FROM paper_observed_candles WHERE session_id=?',(header['id'],)).fetchone()[0]!=len(state.history)):
                raise ObservedError('Hay datos fuera del head observado')
        return ReplayedObserved(state,history_hash,count-1,previous,seed_hash,origin_hash,current)
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError) as exc:
        if isinstance(exc,ObservedError):raise
        raise ObservedError('Sesión PAPER observada no verificable: '+str(exc)[:180]) from exc
