"""Streaming, replayed research export. Missing footer means incomplete output."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from uuid import UUID

from app.derivatives.perp_observed_archive_format import (
    MAX_SEED_BYTES,OriginSnapshot,_event_rows,event_hash,start_source,unpack,
)
from app.derivatives.perp_observed_types import ObservedError
from app.derivatives.perp_observed_wire import INPUT,apply_input,checkpoint,read_observed_seed,transition_evidence
from app.lab.store import content_hash,encoded


EXPORT_SCHEMA='orbis.paper.perp.observed.export.v1'
HEADER_FIELDS={'kind','schema','session_id','runtime_id','created_at','start_request_id',
    'seed','origin','sequence','head_hash','checkpoint','execution_authority','origin_verification','environment'}
EVENT_FIELDS={'kind','sequence','request_id','previous_hash','hash','input','evidence'}
MAX_LINE_BYTES=MAX_SEED_BYTES+1024*1024


def export_lines(archive,session_id: str):
    # Replay first; no bytes are offered as verified before this succeeds.
    report=archive.read(session_id);snapshot=report['session'];stamp=archive._stamp()
    with archive.paper.connect() as db:
        row=db.execute('SELECT * FROM paper_observed_sessions WHERE id=?',(session_id,)).fetchone()
        seed=read_observed_seed(json.loads(row['seed_json']))
        origin=OriginSnapshot.model_validate(json.loads(row['origin_json']))
        if content_hash(seed.payload())!=report['seed_hash'] or origin.payload()!=report['origin']:
            raise ObservedError('El origen cambió antes de exportar')
        header={'kind':'HEADER','schema':EXPORT_SCHEMA,'session_id':session_id,'environment':'PAPER',
            'runtime_id':row['runtime_id'],'created_at':row['created_at'],'start_request_id':row['start_request_id'],
            'seed':seed.payload(),'origin':origin.payload(),'sequence':snapshot['sequence'],
            'head_hash':snapshot['head_hash'],'checkpoint':snapshot['checkpoint'],
            'execution_authority':'NONE','origin_verification':'SAVED_REFERENCE_NOT_LAB_RECHECK'}
        digest=hashlib.sha256();line=(encoded(header)+'\n').encode('utf-8');digest.update(line);yield line
        previous=None;count=0
        for event in _event_rows(db,session_id,snapshot['sequence']):
            archive._stamp(stamp)
            source=unpack(event['input_blob']);evidence=unpack(event['evidence_blob'])
            if (event['sequence']!=count or event['previous_hash']!=previous
                    or content_hash(source)!=event['input_hash']
                    or event_hash(session_id,count,previous,source,evidence)!=event['hash']):
                raise ObservedError('La cadena cambió durante la exportación')
            value={'kind':'EVENT','sequence':count,'request_id':event['request_id'],'previous_hash':previous,
                   'hash':event['hash'],'input':source,'evidence':evidence}
            line=(encoded(value)+'\n').encode('utf-8');digest.update(line);yield line
            count+=1;previous=event['hash']
        if count!=snapshot['sequence']+1 or previous!=snapshot['head_hash']:
            raise ObservedError('La exportación no alcanza el head verificado')
        archive._stamp(stamp)
        yield (encoded({'kind':'COMPLETE','events':count,'head_hash':previous,'sha256':digest.hexdigest()})+'\n').encode('utf-8')


def _line(source) -> tuple[bytes,dict]:
    raw=source.readline(MAX_LINE_BYTES+1)
    if not raw or len(raw)>MAX_LINE_BYTES or not raw.endswith(b'\n'):
        raise ObservedError('Exportación truncada o línea excesiva')
    value=json.loads(raw)
    if type(value) is not dict:raise ObservedError('Objeto de exportación requerido')
    return raw,value


def verify_observed_export(source, *, on_verified_state=None) -> dict:
    """Read a binary stream and replay its source; never attach/import a session."""
    try:
        raw,header=_line(source)
        if (set(header)!=HEADER_FIELDS or header['kind']!='HEADER' or header['schema']!=EXPORT_SCHEMA
                or header['execution_authority']!='NONE' or header['environment']!='PAPER'
                or header['origin_verification']!='SAVED_REFERENCE_NOT_LAB_RECHECK'
                or type(header['sequence']) is not int or not 0<=header['sequence']<10**9):
            raise ObservedError('Cabecera de exportación incompatible')
        for key in ('session_id','runtime_id','start_request_id'):
            if str(UUID(header[key]))!=header[key]:raise ObservedError('Identificador de exportación inválido')
        if datetime.fromisoformat(header['created_at']).utcoffset() is None:
            raise ObservedError('Fecha de exportación sin zona horaria')
        seed=read_observed_seed(header['seed']);origin=OriginSnapshot.model_validate(header['origin'])
        if seed.payload()!=header['seed'] or origin.payload()!=header['origin']:
            raise ObservedError('Inputs de exportación no canónicos')
        origin.check_seed(seed)
        transition=seed.start();history_hash,evidence=transition_evidence(None,transition,None);state=transition.state
        initial=start_source(content_hash(seed.payload()),content_hash(origin.payload()))
        digest=hashlib.sha256(raw);previous=None;requests=set()
        for index in range(header['sequence']+1):
            raw,row=_line(source)
            if (set(row)!=EVENT_FIELDS or row['kind']!='EVENT' or type(row['sequence']) is not int
                    or row['sequence']!=index or row['previous_hash']!=previous
                    or str(UUID(row['request_id']))!=row['request_id'] or row['request_id'] in requests):
                raise ObservedError('Cadena de exportación discontinua o duplicada')
            requests.add(row['request_id'])
            if index==0:
                if row['input']!=initial or row['request_id']!=header['start_request_id']:
                    raise ObservedError('Inicio de exportación alterado')
            else:
                event=INPUT.validate_python(row['input'])
                if event.payload()!=row['input']:raise ObservedError('Input de exportación no canónico')
                transition=apply_input(state,event)
                history_hash,evidence=transition_evidence(state,transition,history_hash);state=transition.state
            computed=event_hash(header['session_id'],index,previous,row['input'],evidence)
            if row['evidence']!=evidence or row['hash']!=computed:
                raise ObservedError('La exportación no reproduce sus resultados')
            previous=computed;digest.update(raw)
        _,footer=_line(source)
        if (set(footer)!={'kind','events','head_hash','sha256'} or footer['kind']!='COMPLETE'
                or type(footer['events']) is not int or footer['events']!=header['sequence']+1
                or footer['head_hash']!=previous or previous!=header['head_hash']
                or footer['sha256']!=digest.hexdigest() or source.read(1)!=b''
                or header['checkpoint']!=checkpoint(state,history_hash)):
            raise ObservedError('Exportación incompleta o cierre/huella alterados')
        if on_verified_state is not None:
            # Only the computed state, after the mandatory footer/EOF check.
            # This is an internal replay hook, never a checkpoint decoder.
            on_verified_state(state)
        return {'verified':True,'verification':'OBSERVED_REPLAY_VERIFIED','environment':'PAPER','session_id':header['session_id'],
            'events':header['sequence']+1,'head_hash':previous,'checkpoint':header['checkpoint'],
            'origin_verification':'SAVED_REFERENCE_NOT_LAB_RECHECK','execution_authority':'NONE',
            'session_activated':False}
    except (ValueError,TypeError,KeyError,AttributeError,UnicodeError) as exc:
        if isinstance(exc,ObservedError):raise
        raise ObservedError('Exportación observada no verificable: '+str(exc)[:160]) from exc
