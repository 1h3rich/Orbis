"""Versioned research identity for NEW GRVT MAINNET bot grants.

The LAB replay evaluates price rules, not a historical margin/funding model.
PAPER quantities and balances never determine the REAL policy.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer, StrictInt, ValidationError, model_validator

from app.core.paper_archive import full_archive_matches
from app.core.paper_archive_chain import chained_archive_matches
from app.lab.engine import INTERVALS
from app.lab.paper_reference import paper_lab_reference
from app.lab.paper_signal_replay import verify_archived_paper_signals, verify_paper_signals
from app.lab.store import content_hash
from app.lab.verification import verify_backtest_run
from app.research.references import Digest, Timestamp, ResearchReference, ResearchReferenceError, strategy_reference

def _nanoseconds(value):
    if type(value) is str and re.fullmatch(r'[1-9][0-9]{0,18}',value):value=int(value)
    if type(value) is not int or not 0<value<2**63:raise ValueError('Nanosegundos no válidos')
    return value


# Transport as text before sealing/hashing. Browser JSON must not round uint64.
Nanoseconds = Annotated[StrictInt, BeforeValidator(_nanoseconds), PlainSerializer(str,return_type=str,when_used='json')]


class GrvtPaperIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    session_id: UUID
    source_digest: Digest
    source_run_id: UUID
    source_run_digest: Digest
    dataset_id: UUID
    dataset_hash: Digest
    seed_from_t: Timestamp
    seed_to_t: Timestamp
    observed_through_t: Timestamp
    observations: Annotated[StrictInt, Field(gt=0)]
    observations_digest: Digest
    archive_head: Digest
    last_observed_at_ns: Nanoseconds


class GrvtResearchPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid', populate_by_name=True)
    schema_name: Literal['orbis.grvt.bot.research.v1'] = Field(alias='schema')
    research: ResearchReference
    paper: GrvtPaperIdentity
    historical_execution_model: Literal['PRICE_RULES_WITHOUT_DERIVATIVES_MARGIN_OR_FUNDING']

    @model_validator(mode='after')
    def coherent(self):
        r,p = self.research,self.paper
        step = INTERVALS[r.timeframe]
        if (r.family != 'STRATEGY' or not r.public_data or r.source != 'grvt-perp-trade-public'
                or not r.symbol.endswith('/USDT:PERP') or r.dataset_to_t > p.seed_to_t
                or p.seed_from_t > p.seed_to_t or p.observed_through_t <= p.seed_to_t
                or (p.seed_to_t-p.seed_from_t)%step or (p.observed_through_t-p.seed_to_t)!=p.observations*step
                or p.last_observed_at_ns < (p.observed_through_t+step)*1_000_000_000
                or any(w.bars < 30 for w in r.windows)
                or r.dataset_id == p.dataset_id and r.dataset_hash != p.dataset_hash):
            raise ValueError('Reserva incompatible con el tramo GRVT PAPER')
        return self

    def payload(self):
        return self.model_dump(mode='json', by_alias=True, exclude={'sha256'})


class GrvtResearchBinding(GrvtResearchPayload):
    sha256: Digest

    @model_validator(mode='after')
    def digest(self):
        if content_hash(self.payload()) != self.sha256:
            raise ValueError('Huella de investigación GRVT alterada')
        return self

    def exported(self):
        return self.model_dump(mode='json', by_alias=True)


def read_grvt_research_binding(value: dict, source: dict) -> GrvtResearchBinding:
    """Check the archived identity. This is not a new replay of local originals."""
    try:
        binding = GrvtResearchBinding.model_validate(value)
        r,p = binding.research,binding.paper
        if (p.source_digest != content_hash(source) or str(p.session_id) != source['session_id']
                or str(p.dataset_id) != source['dataset_id'] or p.dataset_hash != source['dataset_hash']
                or str(r.version_id) != source['version_id'] or r.definition_hash != content_hash(source['definition'])
                or r.direction != source['definition']['direction'] or r.symbol != source['symbol']
                or r.timeframe != source['timeframe'] or r.simulation_engine != source['engine_version']
                or p.last_observed_at_ns < source['session_started_at_ns']):
            raise ValueError('source')
        return binding
    except (ValidationError,KeyError,TypeError,ValueError):
        raise ResearchReferenceError('La investigación GRVT no coincide con el origen autorizado') from None


def _observations(db, session_id, through_t):
    return [dict(row) for row in db.execute('''SELECT candle_t,observed_at_ns,entry_match,exit_match,evidence_json
        FROM paper_signal_observations WHERE session_id=? AND candle_t<=? ORDER BY candle_t''',(session_id,through_t))]


def _paper_snapshot(paper,lab,source,*,now_ns,through_t=None,replay=True):
    """A stable prefix may remain valid while PAPER appends newer observations."""
    with paper.connect() as db:
        db.execute('BEGIN')
        row=db.execute('SELECT * FROM paper_signal_sessions WHERE id=?',(source['session_id'],)).fetchone()
        if row is None:raise ResearchReferenceError('Sesión GRVT PAPER ausente')
        session=dict(row)
        account=db.execute('SELECT stopped_at FROM paper_perp_accounts WHERE id=?',(session['account_id'],)).fetchone()
        if (session['status']!='ACTIVE' or session['execution_mode']!='AUTO_PAPER' or account is None
                or account['stopped_at'] is not None or not session['source_run_id']
                or any(session[key]!=source[key] for key in ('version_id','dataset_id','dataset_hash','symbol','timeframe','engine_version'))
                or session['started_at_ns']!=source['session_started_at_ns']
                or json.loads(session['definition_json'])!=source['definition']):
            raise ResearchReferenceError('El origen GRVT PAPER cambió o no fija un backtest')
        if replay:
            result=verify_archived_paper_signals(lab,db,session,source['definition'],'grvt')
            if result['status']=='unsupported':
                rows=_observations(db,source['session_id'],session['last_t'])
                events=[]
                for item in rows:
                    evidence=json.loads(item['evidence_json'])
                    events.append({'candle_t':item['candle_t'],'candle':evidence['candle'],
                        'entry_match':bool(item['entry_match']),
                        'exit_match':None if item['exit_match'] is None else bool(item['exit_match']),
                        'entry':evidence['entry'],'exit':evidence['exit']})
                if (not full_archive_matches(db,source['session_id'],session['candles_json'],'grvt')
                        or not chained_archive_matches(db,source['session_id'],'grvt')):
                    raise ResearchReferenceError('Archivo GRVT PAPER alterado')
                result=verify_paper_signals(lab,session,source['definition'],json.loads(session['candles_json']),events)
            if not result['verified']:
                raise ResearchReferenceError('Las señales observadas GRVT PAPER no son reproducibles')
        end=session['last_t'] if through_t is None else through_t
        observations=_observations(db,source['session_id'],end)
        if (not observations or end>session['last_t'] or observations[-1]['candle_t']!=end
                or any(type(o['observed_at_ns']) is not int or type(o['candle_t']) is not int
                    or type(o['entry_match']) is not int or o['entry_match'] not in (0,1)
                    or o['exit_match'] is not None and (type(o['exit_match']) is not int or o['exit_match'] not in (0,1))
                    or not source['session_started_at_ns']<=o['observed_at_ns']<=now_ns
                    or not (o['candle_t']+INTERVALS[source['timeframe']])*1_000_000_000<=o['observed_at_ns']<
                           (o['candle_t']+2*INTERVALS[source['timeframe']])*1_000_000_000 for o in observations)):
            raise ResearchReferenceError('Se requiere un tramo GRVT PAPER observado antes de activar')
        head=db.execute('SELECT digest FROM paper_signal_archive_chain WHERE session_id=? AND candle_t=?',
                        (source['session_id'],end)).fetchone()
        if head is None:raise ResearchReferenceError('Falta la huella del archivo PAPER')
        return {'source_run_id':session['source_run_id'],'observed_through_t':end,'observations':len(observations),
            'observations_digest':content_hash(observations),'archive_head':head['digest'],
            'last_observed_at_ns':observations[-1]['observed_at_ns']}


@dataclass(frozen=True)
class CheckedGrvtResearch:
    binding: GrvtResearchBinding
    paper: object
    lab: object
    source: dict
    version_digest: str
    dataset_digest: str
    seed_digest: str

    def assert_current(self,now_ns):
        r,p=self.binding.research,self.binding.paper
        dataset,_=self.lab.dataset(str(r.dataset_id));seed,_=self.lab.dataset(str(p.dataset_id))
        evidence=_paper_snapshot(self.paper,self.lab,self.source,now_ns=now_ns,through_t=p.observed_through_t,replay=False)
        expected={key:getattr(p,key) for key in evidence}
        expected['source_run_id']=str(p.source_run_id)
        if (evidence!=expected or content_hash(self.lab.validation(str(r.artifact_id)))!=r.artifact_digest
                or content_hash(self.lab.version(str(r.version_id)))!=self.version_digest
                or content_hash(self.lab.run(str(p.source_run_id)))!=p.source_run_digest
                or content_hash(dataset)!=self.dataset_digest or content_hash(seed)!=self.seed_digest):
            raise ResearchReferenceError('La investigación o el prefijo PAPER cambiaron durante la autorización GRVT')


def check_grvt_research(paper,lab,source,validation_id,*,now_ns,expected_binding=None):
    """Replay research/backtest/PAPER; keep a sealed review prefix when supplied."""
    try:
        reference=strategy_reference(lab,str(UUID(validation_id)))
        dataset,history=lab.dataset(str(reference.dataset_id));seed,seed_rows=lab.dataset(source['dataset_id'])
        version=lab.version(source['version_id'])
        if (dataset['hash']!=reference.dataset_hash or content_hash(version['definition'])!=reference.definition_hash
                or not seed['verified_public'] or seed['source']!='grvt-perp-trade-public' or seed['gaps']
                or seed['hash']!=source['dataset_hash']):
            raise ResearchReferenceError('El histórico de investigación o PAPER cambió')
        past={row['t']:row for row in history}
        if any(row['t'] in past and row!=past[row['t']] for row in seed_rows):
            raise ResearchReferenceError('Las velas comunes de investigación y GRVT PAPER difieren')
        archived=read_grvt_research_binding(expected_binding,source) if expected_binding else None
        evidence=_paper_snapshot(paper,lab,source,now_ns=now_ns,
            through_t=archived.paper.observed_through_t if archived else None)
        origin,_,_=paper_lab_reference(lab,source['dataset_id'],source['dataset_hash'],source['version_id'],evidence['source_run_id'])
        run=lab.run(origin['id'])
        if not verify_backtest_run(lab,run)['verified']:
            raise ResearchReferenceError('El backtest fijado en GRVT PAPER no se reproduce')
        payload=GrvtResearchPayload.model_validate({'schema':'orbis.grvt.bot.research.v1','research':reference.exported(),
            'paper':{'session_id':source['session_id'],'source_digest':content_hash(source),**evidence,
                'source_run_digest':content_hash(run),'dataset_id':source['dataset_id'],'dataset_hash':source['dataset_hash'],
                'seed_from_t':seed['start_t'],'seed_to_t':seed['end_t']},
            'historical_execution_model':'PRICE_RULES_WITHOUT_DERIVATIVES_MARGIN_OR_FUNDING'}).payload()
        binding=GrvtResearchBinding.model_validate({**payload,'sha256':content_hash(payload)})
        read_grvt_research_binding(binding.exported(),source)
        if archived is not None and binding!=archived:
            raise ResearchReferenceError('La evidencia revisada de GRVT cambió')
        checked=CheckedGrvtResearch(binding,paper,lab,source,content_hash(version),content_hash(dataset),content_hash(seed))
        checked.assert_current(now_ns)
        return checked
    except ResearchReferenceError:
        raise
    except (KeyError,TypeError,ValueError):
        raise ResearchReferenceError('Selecciona una reserva de la misma versión/mercado/temporalidad y un origen GRVT PAPER observado con backtest fijado') from None


def grvt_research_options(paper,lab,source):
    """Metadata discovery only; preview AND start must replay the full proof."""
    with paper.connect() as db:
        session=db.execute('SELECT source_run_id FROM paper_signal_sessions WHERE id=?',(source['session_id'],)).fetchone()
        count=db.execute('SELECT COUNT(*) FROM paper_signal_observations WHERE session_id=?',(source['session_id'],)).fetchone()[0]
    if session is None or not session['source_run_id'] or not count:
        return [],False
    seed,_=lab.dataset(source['dataset_id']);result=[]
    for row in lab.validations():
        if row['version_id']!=source['version_id']:continue
        try:
            saved=lab.validation(row['id']);meta,_=lab.dataset(row['dataset_id'])
            if (saved.get('strategy_hash')!=content_hash(source['definition'])
                    or saved.get('lab_engine_version')!=source['engine_version'] or saved.get('result_hash') is None
                    or not meta['verified_public'] or meta['source']!='grvt-perp-trade-public' or meta['gaps']
                    or meta['symbol']!=source['symbol'] or meta['timeframe']!=source['timeframe']
                    or meta['hash']!=saved['dataset_hash'] or meta['end_t']>seed['end_t']
                    or saved.get('method') not in ('CHRONOLOGICAL_HOLDOUT','ROLLING_FIXED_VERSION')):continue
            result.append({'id':row['id'],'method':saved['method'],'created_at':row['created_at'],
                           'test_bars':saved['test_bars'],'verification':'ON_PREVIEW_AND_START'})
        except (KeyError,TypeError,ValueError):continue
    return result,True
