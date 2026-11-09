"""MEXC signal research for DEX execution; never a claim of historical DEX fills.

The financial execution journal keeps its own frozen versions. Qualification is
an independent immutable record tied to the exact binding/run header digest.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import BaseModel,ConfigDict,Field,ValidationError,model_validator

from app.lab.engine import INTERVALS
from app.lab.store import content_hash
from app.research.mexc_binding import PaperIdentity
from app.research.references import (Digest,ResearchReference,ResearchReferenceError,
    strategy_reference,dca_reference)


class DexSignalPayload(BaseModel):
    model_config=ConfigDict(frozen=True,extra='forbid',populate_by_name=True)
    schema_name:Literal['orbis.dex.signal.research.v1']=Field(alias='schema')
    research:ResearchReference
    paper:PaperIdentity
    signal_market:Literal['MEXC_BNB_USDT']
    execution_market:Literal['UNISWAP_V3_BNB_USDT_WBNB']
    historical_model:Literal['SIGNAL_MARKET_WITHOUT_DEX_EXECUTION_COSTS']
    dca_reference_policy:Literal['RESET_ON_INITIAL_ACTIVATION_KEEP_ON_REARM']|None

    @model_validator(mode='after')
    def coherent(self):
        r,p=self.research,self.paper;step=INTERVALS.get(p.timeframe)
        if (step is None or not r.public_data or r.source!='mexc-spot-public' or r.source!=p.source
                or r.version_id!=p.version_id or r.definition_hash!=p.definition_hash
                or r.symbol!='BNB/USDT' or r.symbol!=p.symbol or r.timeframe!=p.timeframe
                or r.simulation_engine!=p.simulation_engine or r.direction not in (None,'LONG')
                or r.dataset_to_t>p.seed_to_t or p.seed_from_t>p.seed_to_t or p.seed_to_t>=p.observation_t
                or (p.seed_to_t-p.seed_from_t)%step or (p.observation_t-p.seed_to_t)%step
                or any(w.bars<30 for w in r.windows)
                or (r.family=='DCA')!=(self.dca_reference_policy is not None)
                or r.dataset_id==p.dataset_id and r.dataset_hash!=p.dataset_hash):
            raise ValueError('Investigación incompatible con el origen de señales DEX')
        return self

    def payload(self):return self.model_dump(mode='json',by_alias=True,exclude={'sha256'})


class DexSignalResearch(DexSignalPayload):
    sha256:Digest

    @model_validator(mode='after')
    def digest(self):
        if content_hash(self.payload())!=self.sha256:raise ValueError('Huella de investigación DEX alterada')
        return self

    def exported(self):return self.model_dump(mode='json',by_alias=True)


def read_dex_signal_research(value:dict,source:dict)->DexSignalResearch:
    """Validate archived identity, separate from a replay of original stores."""
    try:
        result=DexSignalResearch.model_validate(value);r,p=result.research,result.paper
        if (p.source_digest!=content_hash(source) or str(p.session_id)!=source['session_id']
                or str(p.version_id)!=source['version_id'] or p.definition_hash!=source['definition_hash']
                or str(p.dataset_id)!=source['dataset_id'] or p.dataset_hash!=source['dataset_hash']
                or p.symbol!=source['symbol'] or p.timeframe!=source['timeframe']
                or p.simulation_engine!=source['engine_version']
                or (r.family=='DCA')!=(source.get('kind')=='DCA')):
            raise ValueError('source')
        return result
    except (ValidationError,KeyError,TypeError,ValueError):
        raise ResearchReferenceError('La reserva DEX no coincide con su origen de señales') from None


@dataclass(frozen=True)
class CheckedDexResearch:
    evidence:DexSignalResearch
    repository:object
    version_digest:str
    dataset_digest:str
    seed_digest:str

    def assert_current(self):
        r,p=self.evidence.research,self.evidence.paper
        lab=self.repository.lab;store=self.repository.store if r.family=='DCA' else lab
        dataset,_=lab.dataset(str(r.dataset_id));seed,_=lab.dataset(str(p.dataset_id))
        if (content_hash(store.validation(str(r.artifact_id)))!=r.artifact_digest
                or content_hash(store.version(str(r.version_id)))!=self.version_digest
                or content_hash(dataset)!=self.dataset_digest or content_hash(seed)!=self.seed_digest):
            raise ResearchReferenceError('La investigación cambió durante la autorización DEX')


def check_dex_research(repository,source:dict,observation_t:int,validation_id:str)->CheckedDexResearch:
    """Replay original research; the caller verifies live/archived PAPER origin."""
    try:
        validation_id=str(UUID(validation_id));dca=source.get('kind')=='DCA'
        lab=repository.lab;store=repository.store if dca else lab
        reference=dca_reference(store,lab,validation_id) if dca else strategy_reference(lab,validation_id)
        meta,history=lab.dataset(str(reference.dataset_id));seed,seed_rows=lab.dataset(source['dataset_id'])
        version=store.version(source['version_id'])
        if (meta['hash']!=reference.dataset_hash or meta['gaps'] or not meta['verified_public']
                or meta['source']!=reference.source or meta['symbol']!=reference.symbol or meta['timeframe']!=reference.timeframe
                or meta['start_t']!=reference.dataset_from_t or meta['end_t']!=reference.dataset_to_t
                or content_hash(version['definition'])!=reference.definition_hash
                or seed['hash']!=source['dataset_hash'] or seed['gaps'] or not seed['verified_public']
                or seed['source']!='mexc-spot-public' or seed['symbol']!=source['symbol'] or seed['timeframe']!=source['timeframe']):
            raise ResearchReferenceError('La investigación o la semilla PAPER DEX cambió')
        past={bar['t']:bar for bar in history}
        if any(bar['t'] in past and past[bar['t']]!=bar for bar in seed_rows):
            raise ResearchReferenceError('Las velas comunes de investigación y señales DEX difieren')
        payload=DexSignalPayload.model_validate({'schema':'orbis.dex.signal.research.v1','research':reference.exported(),
            'paper':{'source_digest':content_hash(source),'session_id':source['session_id'],'version_id':source['version_id'],
                'definition_hash':source['definition_hash'],'dataset_id':source['dataset_id'],'dataset_hash':source['dataset_hash'],
                'symbol':source['symbol'],'timeframe':source['timeframe'],'source':seed['source'],
                'seed_from_t':seed['start_t'],'seed_to_t':seed['end_t'],'observation_t':observation_t,
                'simulation_engine':source['engine_version']},
            'signal_market':'MEXC_BNB_USDT','execution_market':'UNISWAP_V3_BNB_USDT_WBNB',
            'historical_model':'SIGNAL_MARKET_WITHOUT_DEX_EXECUTION_COSTS',
            'dca_reference_policy':'RESET_ON_INITIAL_ACTIVATION_KEEP_ON_REARM' if dca else None}).payload()
        evidence=DexSignalResearch.model_validate({**payload,'sha256':content_hash(payload)})
        read_dex_signal_research(evidence.exported(),source)
        checked=CheckedDexResearch(evidence,repository,content_hash(version),content_hash(meta),content_hash(seed))
        checked.assert_current();return checked
    except ResearchReferenceError:raise
    except (KeyError,TypeError,ValueError):
        raise ResearchReferenceError('Selecciona una reserva reproducible BNB/USDT de la misma versión y temporalidad, anterior a PAPER') from None
