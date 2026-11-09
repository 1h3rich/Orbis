"""Local encrypted observed sessions with replay and process-owned hot cursors.

No method starts a worker. A new archive instance never attaches stored active
states automatically. Source validation is rechecked at create; stored history
can be replayed with PAPER alone, without claiming LAB was rechecked then.
"""
from __future__ import annotations

from dataclasses import dataclass,field
from datetime import datetime,timezone
import json
import hmac
import secrets
import re
from threading import RLock
import time
from uuid import UUID,uuid4

from app.derivatives.paper_access import paper_perp_access,paper_perp_format
from app.derivatives.paper_store import PaperPerpStore
from app.derivatives.perp_observed_archive_format import (
    MAX_SEED_BYTES,OriginSnapshot,ReplayedObserved,event_hash,pack,replay_observed_session,start_source,unpack,
)
from app.derivatives.perp_observed_origin import VerifiedObservedOrigin
from app.derivatives.perp_observed_types import ObservedError
from app.derivatives.perp_observed_view import ObservedReplayView
from app.derivatives.perp_observed_lease import ObservedSessionLease
from app.derivatives.perp_observed_wire import (
    INPUT,CloseEvent,GapEvent,StopEvent,QuoteEvent,ObservedInput,ObservedSeed,apply_input,read_observed_seed,transition_evidence,
)
from app.lab.store import content_hash,encoded


def identifier(raw: str) -> str:
    try:
        if type(raw) is not str or str(UUID(raw))!=raw:raise ValueError()
    except (ValueError,TypeError,AttributeError):
        raise ObservedError('Identificador observado no canónico') from None
    return raw


@dataclass(frozen=True)
class _AccessStamp:
    device: int
    inode: int
    key_digest: bytes=field(repr=False)
    epoch: object|None=field(repr=False)


@dataclass(frozen=True)
class _Cursor:
    replayed: ReplayedObserved=field(repr=False)
    stamp: _AccessStamp=field(repr=False)
    lease: ObservedSessionLease=field(repr=False)


def _summary(header,replayed: ReplayedObserved|None,*,current_runtime: str,active: bool) -> dict:
    snapshot=(replayed.checkpoint if replayed is not None else json.loads(header['checkpoint_json']))
    stored=snapshot['status']
    effective=(stored if active or stored not in ('RUNNING','CLOSE_ONLY') else
               'RESTARTED' if header['runtime_id']!=current_runtime else 'PAUSED')
    return {'id':header['id'],'created_at':header['created_at'],'sequence':header['sequence'],
        'head_hash':header['head_hash'],'engine':snapshot['engine'],'environment':'PAPER','stored_status':stored,
        'status':effective,'active_in_this_process':active,
        'runtime_matches':header['runtime_id']==current_runtime,
        'verification':'OBSERVED_REPLAY_VERIFIED' if replayed is not None else 'CHECKPOINT_ONLY',
        'checkpoint':snapshot,'origin_verification':'SAVED_REFERENCE_NOT_LAB_RECHECK',
        'execution_authority':'NONE'}


class ObservedArchive:
    def __init__(self,paper: PaperPerpStore,*,runtime_id: str|None=None,clock_ns=time.time_ns):
        self.paper=paper;self.runtime_id=identifier(runtime_id or str(uuid4()));self.clock_ns=clock_ns
        self._cache: dict[str,_Cursor]={};self._locks: dict[str,RLock]={};self._lock=RLock()
        self._observers: dict[str,object]={}
        self._stamp_secret=secrets.token_bytes(32)

    def _session_lock(self,session_id: str):
        with self._lock:return self._locks.setdefault(session_id,RLock())

    def _stamp(self,expected: _AccessStamp|None=None) -> _AccessStamp:
        try:
            if paper_perp_format(self.paper.path)!='encrypted_or_invalid':
                raise ObservedError('Las sesiones observadas requieren GRVT PAPER cifrado')
            key=self.paper.current_passphrase()
            if key is None:raise ObservedError('GRVT PAPER bloqueado durante la operación')
            info=self.paper.path.stat()
            digest=hmac.digest(self._stamp_secret,key.encode('utf-8'),'sha256')
            current=_AccessStamp(info.st_dev,info.st_ino,digest,paper_perp_access.epoch(self.paper.path))
            if expected is not None and current!=expected:
                raise ObservedError('La sesión GRVT PAPER cambió durante la operación')
            return current
        except (ValueError,OSError):
            # Invalid cursors cannot survive lock/re-authentication and later
            # resume. No cached identity contains the unlock password itself.
            with self._lock:
                for session_id,cursor in tuple(self._cache.items()):
                    if expected is None or cursor.stamp==expected:
                        cursor.lease.close();self._cache.pop(session_id,None);self._observers.pop(session_id,None)
            raise

    def _fresh(self,received_at_ns: str) -> None:
        now=self.clock_ns()
        if type(now) is not int or not 0<=now-int(received_at_ns)<=15_000_000_000:
            raise ObservedError('Recepción ficticia futura o antigua respecto al reloj actual')

    def create(self,origin: VerifiedObservedOrigin,seed: ObservedSeed,*,request_id: str,
               expected_session: _AccessStamp|None=None) -> dict:
        request_id=identifier(request_id);stamp=self._stamp(expected_session)
        if not isinstance(origin,VerifiedObservedOrigin):raise ObservedError('Origen LAB verificado requerido')
        seed=read_observed_seed(seed.payload())
        reference=origin.reference();prefix=json.loads(origin.trade_prefix_json)
        source=OriginSnapshot(reference=reference,trade_prefix_hash=content_hash(prefix),trade_prefix_count=len(prefix))
        source.check_seed(seed)
        if seed.definition!=json.loads(origin.definition_json):raise ObservedError('Definición distinta del origen LAB')
        seed_json=encoded(seed.payload());origin_json=encoded(source.payload())
        if len(seed_json.encode('utf-8'))>MAX_SEED_BYTES:raise ObservedError('Seed observado excesivo')
        seed_hash=content_hash(seed.payload());origin_hash=content_hash(source.payload())
        # Request identity is the tested profile/origin, not a new market quote.
        with self.paper.connect() as db:
            existing=db.execute('SELECT * FROM paper_observed_sessions WHERE start_request_id=?',(request_id,)).fetchone()
        if existing is not None:
            if existing['request_hash']!=origin_hash:raise ObservedError('Petición de inicio reutilizada con otro origen')
            return self.read(existing['id'])
        self._fresh(seed.received_at_ns)
        started=seed.start();history_hash,evidence=transition_evidence(None,started,None)
        session_id=str(uuid4());initial=start_source(seed_hash,origin_hash)
        digest=event_hash(session_id,0,None,initial,evidence)
        lease=ObservedSessionLease(self.paper.path,session_id,exclusive=False)
        try:
            with origin.hold_current(),paper_perp_access.guard(self.paper.path,stamp.epoch):
                self._stamp(stamp)
                with self.paper.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    self._stamp(stamp);origin.check_session()
                    existing=db.execute('SELECT * FROM paper_observed_sessions WHERE start_request_id=?',(request_id,)).fetchone()
                    if existing is None:
                        db.execute('INSERT INTO paper_observed_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                            (session_id,datetime.now(timezone.utc).isoformat(),request_id,origin_hash,self.runtime_id,
                             seed_json,seed_hash,origin_json,origin_hash,0,digest,encoded(evidence['checkpoint']),started.state.status))
                        db.execute('INSERT INTO paper_observed_events VALUES (?,?,?,?,?,?,?,?)',
                            (session_id,0,request_id,content_hash(initial),None,digest,pack(initial),pack(evidence)))
                        db.executemany('INSERT INTO paper_observed_candles VALUES (?,?,?,?)',
                            ((session_id,bar.t,encoded(bar.json()),'SEED') for bar in started.state.history))
                        existing=db.execute('SELECT * FROM paper_observed_sessions WHERE id=?',(session_id,)).fetchone()
                    elif existing['request_hash']!=origin_hash:
                        raise ObservedError('Petición de inicio reutilizada con otro origen')
                    self._stamp(stamp);origin.check_session()
                if existing['id']==session_id:
                    replayed=ReplayedObserved(started.state,history_hash,0,digest,seed_hash,origin_hash,evidence['checkpoint'])
                    self._cache[session_id]=_Cursor(replayed,stamp,lease)
                    lease=None
                    return _summary(existing,replayed,current_runtime=self.runtime_id,active=True)
            # A concurrent identical request won. Replaying does not attach it.
            return self.read(existing['id'])
        finally:
            if lease is not None:lease.close()

    def _check_head(self,header,cursor: _Cursor) -> None:
        if header is None:raise ObservedError('Sesión observada desaparecida')
        expected=cursor.replayed
        if (header['sequence']!=expected.sequence or header['head_hash']!=expected.head_hash
                or header['seed_hash']!=expected.seed_hash or header['origin_hash']!=expected.origin_hash
                or header['runtime_id']!=self.runtime_id or header['status']!=expected.state.status
                or header['checkpoint_json']!=encoded(expected.checkpoint)):
            raise ObservedError('El head observado cambió fuera de este consumidor')

    def append(self,session_id: str,event: ObservedInput,*,request_id: str) -> dict:
        session_id=identifier(session_id);request_id=identifier(request_id)
        event=INPUT.validate_python(event.payload());source=event.payload();input_hash=content_hash(source)
        with self._session_lock(session_id):
            old=self._cache.get(session_id)
            if old is None:raise ObservedError('Sesión no activa en este proceso; no se reactiva al reabrir')
            stamp=self._stamp(old.stamp)
            with self.paper.connect() as db:
                header=db.execute('SELECT id,created_at,sequence,head_hash,seed_hash,origin_hash,runtime_id,status,checkpoint_json FROM paper_observed_sessions WHERE id=?',(session_id,)).fetchone()
                self._check_head(header,old)
                duplicate=db.execute('SELECT sequence,input_hash,hash FROM paper_observed_events WHERE session_id=? AND request_id=?',(session_id,request_id)).fetchone()
                if duplicate is not None:
                    if duplicate['input_hash']!=input_hash:raise ObservedError('Petición observada reutilizada con otro input')
                    return {'id':session_id,'sequence':duplicate['sequence'],'head_hash':duplicate['hash'],'duplicate':True}
            self._fresh(event.received_at_ns)
            transition=apply_input(old.replayed.state,event)
            history_hash,evidence=transition_evidence(old.replayed.state,transition,old.replayed.history_hash)
            sequence=old.replayed.sequence+1
            digest=event_hash(session_id,sequence,old.replayed.head_hash,source,evidence)
            source_blob=pack(source);evidence_blob=pack(evidence)
            with paper_perp_access.guard(self.paper.path,stamp.epoch):
                self._stamp(stamp)
                with self.paper.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    self._stamp(stamp)
                    header=db.execute('SELECT id,created_at,sequence,head_hash,seed_hash,origin_hash,runtime_id,status,checkpoint_json FROM paper_observed_sessions WHERE id=?',(session_id,)).fetchone()
                    self._check_head(header,old)
                    db.execute('INSERT INTO paper_observed_events VALUES (?,?,?,?,?,?,?,?)',
                        (session_id,sequence,request_id,input_hash,old.replayed.head_hash,digest,source_blob,evidence_blob))
                    for index,trade in enumerate(evidence['trades'],start=old.replayed.state.closed_trades):
                        db.execute('INSERT INTO paper_observed_trades VALUES (?,?,?,?)',(session_id,index,sequence,encoded(trade)))
                    for candle in transition.state.history[len(old.replayed.state.history):]:
                        db.execute('INSERT INTO paper_observed_candles VALUES (?,?,?,?)',
                            (session_id,candle.t,encoded(candle.json()),'OBSERVED'))
                    db.execute('UPDATE paper_observed_sessions SET sequence=?,head_hash=?,checkpoint_json=?,status=? WHERE id=?',
                        (sequence,digest,encoded(evidence['checkpoint']),transition.state.status,session_id))
                    self._stamp(stamp)
                replayed=ReplayedObserved(transition.state,history_hash,sequence,digest,
                    old.replayed.seed_hash,old.replayed.origin_hash,evidence['checkpoint'])
                self._cache[session_id]=_Cursor(replayed,stamp,old.lease)
            return {'id':session_id,'sequence':sequence,'head_hash':digest,'duplicate':False,
                    'status':transition.state.status,'checkpoint':evidence['checkpoint']}

    def active_ids(self) -> tuple[str,...]:
        # Access is checked, but reading this never reattaches a cold account.
        result=[]
        for session_id,cursor in tuple(self._cache.items()):
            try:self._stamp(cursor.stamp)
            except (ValueError,OSError):continue
            if cursor.replayed.state.status in ('RUNNING','CLOSE_ONLY'):result.append(session_id)
        return tuple(result)

    def accept_quote(self,session_id: str,event: QuoteEvent,*,request_id: str) -> dict:
        """Serialize consumer receipt against controls, preserving source times.

        A fresh point may have been collected just before a concurrent control.
        It is accepted now, not backdated through the control. Funding and MARK
        still run before exits. Nothing waits for HTTP while holding this lock.
        """
        session_id=identifier(session_id);request_id=identifier(request_id)
        event=QuoteEvent.model_validate(event.payload())
        if event.collected_at_ns is not None:raise ObservedError('La fuente no fija la aceptación del consumidor')
        with self._session_lock(session_id):
            state=self.current_state(session_id);collected=int(event.received_at_ns)
            if collected<state.last_received_ns:raise ObservedError('Recogida anterior al estado observado')
            accepted=self.clock_ns()
            value=event.model_copy(update={'received_at_ns':str(accepted),'collected_at_ns':event.received_at_ns})
            return self.append(session_id,value,request_id=request_id)

    def find_start(self,request_id: str) -> dict|None:
        request_id=identifier(request_id);stamp=self._stamp()
        with self.paper.connect() as db:
            row=db.execute('SELECT id FROM paper_observed_sessions WHERE start_request_id=?',(request_id,)).fetchone()
        self._stamp(stamp)
        return self.read(row['id']) if row is not None else None

    def control(self,session_id: str,kind: str,*,request_id: str) -> dict:
        """Receipt time belongs to the server; retries keep the first timestamp."""
        session_id=identifier(session_id);request_id=identifier(request_id)
        if kind not in ('STOP','CLOSE_REQUEST'):raise ObservedError('Control observado no válido')
        with self._session_lock(session_id):
            stamp=self._stamp()
            with self.paper.connect() as db:
                row=db.execute('SELECT sequence,hash,input_hash,previous_hash,input_blob,evidence_blob FROM paper_observed_events WHERE session_id=? AND request_id=?',
                    (session_id,request_id)).fetchone()
            self._stamp(stamp)
            if row is not None:
                source=unpack(row['input_blob']);evidence=unpack(row['evidence_blob'])
                if (content_hash(source)!=row['input_hash']
                        or event_hash(session_id,row['sequence'],row['previous_hash'],source,evidence)!=row['hash']):
                    raise ObservedError('Control observado alterado')
                if source.get('kind')!=kind:
                    raise ObservedError('Petición observada reutilizada con otro control')
                return {'id':session_id,'sequence':row['sequence'],'head_hash':row['hash'],'duplicate':True}
            model=StopEvent if kind=='STOP' else CloseEvent
            event=model(schema_name='orbis.paper.perp.observed.input.v1',kind=kind,received_at_ns=str(self.clock_ns()))
            return self.append(session_id,event,request_id=request_id)

    def interrupt(self,session_id: str,reason: str) -> None:
        """Suspend an owned cursor; inability to save is propagated, never hidden."""
        try:
            state=self.current_state(session_id)
            if state.status in ('RUNNING','CLOSE_ONLY'):
                self.append(session_id,GapEvent(schema_name='orbis.paper.perp.observed.input.v1',kind='GAP',
                    received_at_ns=str(self.clock_ns()),reason=reason),request_id=str(uuid4()))
        finally:self.detach(session_id)

    def current_state(self,session_id: str):
        """Immutable process-owned state for a collector; never loads a checkpoint."""
        session_id=identifier(session_id)
        with self._session_lock(session_id):
            cursor=self._cache.get(session_id)
            if cursor is None:raise ObservedError('Sesión no activa en este proceso')
            self._stamp(cursor.stamp)
            return cursor.replayed.state

    def verified_inactive(self,session_id: str,*,expected_access=None):
        """Recomputed source for a separate closure; never attaches a cursor.

        The caller can carry the access stamp through public HTTP and recheck it
        before publication. This method holds no transaction across that HTTP.
        """
        session_id=identifier(session_id)
        with self._session_lock(session_id):
            stamp=self._stamp(expected_access)
            self.active_ids()  # Discard invalid old-epoch cursors first.
            if session_id in self._cache or session_id in self._observers:
                raise ObservedError('Detén la observación antes de estimar un cierre tras hueco')
            with self.paper.connect() as db:
                header=db.execute('SELECT * FROM paper_observed_sessions WHERE id=?',(session_id,)).fetchone()
                replayed=replay_observed_session(db,header)
            self._stamp(stamp)
            if replayed.state.opened is None or not replayed.state.account.positions:
                raise ObservedError('No hay posición pendiente en el prefijo reproducido')
            return dict(header),replayed,stamp

    def detach(self,session_id: str) -> None:
        """Revoke this consumer. Does not close exposure or claim a durable write."""
        session_id=identifier(session_id)
        with self._session_lock(session_id):
            cursor=self._cache.pop(session_id,None)
            if cursor is not None:cursor.lease.close()
            self._observers.pop(session_id,None)

    def claim_observer(self,session_id: str,observer: object) -> None:
        """One explicit observer per owned cursor; does not start any thread."""
        session_id=identifier(session_id)
        with self._session_lock(session_id):
            self.current_state(session_id)
            if session_id in self._observers:
                raise ObservedError('Ya existe un observador para esta sesión')
            self._observers[session_id]=observer

    def export_lines(self,session_id: str):
        from app.derivatives.perp_observed_export import export_lines
        return export_lines(self,identifier(session_id))

    def read(self,session_id: str,*,view: ObservedReplayView|None=None,
             snapshot_sequence: int|None=None,snapshot_hash: str|None=None) -> dict:
        session_id=identifier(session_id);stamp=self._stamp()
        with self.paper.connect() as db:
            header=db.execute('SELECT * FROM paper_observed_sessions WHERE id=?',(session_id,)).fetchone()
            if snapshot_sequence is not None or snapshot_hash is not None:
                if (header is None or type(snapshot_sequence) is not int or not 0<=snapshot_sequence<=header['sequence']
                        or type(snapshot_hash) is not str or re.fullmatch('[0-9a-f]{64}',snapshot_hash) is None):
                    raise ObservedError('Snapshot de inspección no válido')
                anchor=db.execute('SELECT hash,evidence_blob FROM paper_observed_events WHERE session_id=? AND sequence=?',
                    (session_id,snapshot_sequence)).fetchone()
                if anchor is None or anchor['hash']!=snapshot_hash:raise ObservedError('El snapshot ya no pertenece a esta sesión')
                try:
                    claimed=unpack(anchor['evidence_blob'])['checkpoint']
                    header={**dict(header),'sequence':snapshot_sequence,'head_hash':snapshot_hash,
                        'checkpoint_json':encoded(claimed),'status':claimed['status']}
                except (KeyError,TypeError,ValueError) as exc:
                    raise ObservedError('Snapshot de inspección no verificable') from exc
                # This is only a claimed historical header. The original full
                # replay below must reproduce it; no account is loaded from it.
            if view is not None and header is not None:
                try:view.bind(header)
                except (ValueError,KeyError,TypeError) as exc:
                    raise ObservedError('Ventana de inspección no verificable') from exc
            replayed=replay_observed_session(db,header,allow_appended_prefix=True,
                on_verified_event=view.consume if view is not None else None)
        self._stamp(stamp)
        with paper_perp_access.guard(self.paper.path,stamp.epoch),self.paper.connect() as db:
            current=db.execute('SELECT * FROM paper_observed_sessions WHERE id=?',(session_id,)).fetchone()
            if current is None or any(current[key]!=header[key] for key in ('seed_json','seed_hash','origin_json','origin_hash','runtime_id','start_request_id','request_hash')):
                raise ObservedError('La sesión observada cambió durante la reproducción')
            anchor=db.execute('SELECT hash FROM paper_observed_events WHERE session_id=? AND sequence=?',(session_id,replayed.sequence)).fetchone()
            maximum=db.execute('SELECT max(sequence) FROM paper_observed_events WHERE session_id=?',(session_id,)).fetchone()[0]
            if (current['sequence']<replayed.sequence or anchor is None or anchor['hash']!=replayed.head_hash
                    or maximum!=current['sequence']):
                raise ObservedError('El prefijo observado ya no pertenece al archivo actual')
            cached=self._cache.get(session_id)
            active=bool(cached and cached.stamp==stamp and cached.replayed.head_hash==current['head_hash']
                        and cached.replayed.state.status in ('RUNNING','CLOSE_ONLY'))
        result={'schema':'orbis.paper.perp.observed.verified.v1',
            'session':_summary(header,replayed,current_runtime=self.runtime_id,active=active),
            'origin':json.loads(header['origin_json']),'seed_hash':header['seed_hash'],
            'snapshot_is_latest':current['head_hash']==replayed.head_hash,'latest_head_hash':current['head_hash']}
        if view is not None:result['view']=view.payload(replayed.state)
        return result

    def page(self,*,offset: int=0,limit: int=25) -> dict:
        if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=100:
            raise ObservedError('Página observada no válida')
        if paper_perp_format(self.paper.path) in ('missing','empty'):
            return {'items':[],'total':0,'offset':offset,'limit':limit}
        stamp=self._stamp()
        with paper_perp_access.guard(self.paper.path,stamp.epoch),self.paper.connect() as db:
            self._stamp(stamp)
            total=db.execute('SELECT count(*) FROM paper_observed_sessions').fetchone()[0]
            rows=db.execute('SELECT id,created_at,sequence,head_hash,seed_hash,origin_hash,runtime_id,status,checkpoint_json FROM paper_observed_sessions ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?',(limit,offset)).fetchall()
            result=[]
            for row in rows:
                cached=self._cache.get(row['id']);active=False
                if cached is not None and cached.stamp==stamp:
                    self._check_head(row,cached)
                    active=cached.replayed.state.status in ('RUNNING','CLOSE_ONLY')
                result.append(_summary(row,None,current_runtime=self.runtime_id,active=active))
        return {'items':result,
                'total':total,'offset':offset,'limit':limit}

    def events_page(self,session_id: str,*,offset: int=0,limit: int=100) -> dict:
        session_id=identifier(session_id)
        if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=200:
            raise ObservedError('Página de eventos no válida')
        stamp=self._stamp()
        with self.paper.connect() as db:
            total=db.execute('SELECT count(*) FROM paper_observed_events WHERE session_id=?',(session_id,)).fetchone()[0]
            rows=db.execute('SELECT * FROM paper_observed_events WHERE session_id=? ORDER BY sequence LIMIT ? OFFSET ?',(session_id,limit,offset)).fetchall()
        result=[]
        for row in rows:
            source=unpack(row['input_blob']);evidence=unpack(row['evidence_blob'])
            if (content_hash(source)!=row['input_hash']
                    or event_hash(session_id,row['sequence'],row['previous_hash'],source,evidence)!=row['hash']):
                raise ObservedError('Evento observado alterado')
            result.append({'sequence':row['sequence'],'hash':row['hash'],'input':source,'evidence':evidence})
        self._stamp(stamp)
        return {'items':result,'total':total,'offset':offset,'limit':limit,
                'verification':'INDIVIDUAL_HASH_ONLY_NOT_FULL_REPLAY'}
