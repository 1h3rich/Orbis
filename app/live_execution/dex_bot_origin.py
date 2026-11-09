"""Reproducible LAB/PAPER certificate kept inside the encrypted REAL binding.

Only source definitions, candles and historical results are copied. None of
those theoretical cash/units/results become a REAL balance or execution.
"""
from dataclasses import dataclass,field
from datetime import datetime
from uuid import UUID
import hashlib
import json

from app.automation.dca import dca_engine_for_rule,dca_signal_window_key,seed_dca_ema,validate_dca,simulate_dca
from app.automation.paper_dca_gap import dca_paper_overlap_count
from app.lab.engine import CURRENT_ENGINE_VERSION,INTERVALS,IndicatorCache,evaluate,validate_candles,validate_strategy,run_backtest
from app.lab.store import content_hash
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_bot_source import DexBotSources
from app.portfolio.wallet_funding import _encoded,_decoded,_time
from app.research.dex_binding import CheckedDexResearch

_SEAL=object()
STRATEGY_METADATA={'id','dataset_id','version_id','created_at','engine_version','dataset_hash','dataset_source','strategy_hash','result_hash'}
DCA_METADATA={'id','version_id','dataset_id','dataset_hash','created_at','engine_version','definition_hash','result_hash'}


@dataclass(frozen=True,slots=True,repr=False)
class PreparedDexOrigin:
    _seal:object=field(repr=False)
    payload_json:str=field(repr=False)
    payload_digest:str
    _sources:DexBotSources=field(repr=False)
    research:CheckedDexResearch|None=field(default=None,repr=False)

    def material(self,*,now:datetime) -> dict:
        if self._seal is not _SEAL:raise DexExecutorError('Reproduce la preparación desde LAB/PAPER')
        material=_decoded(self.payload_json,self.payload_digest)
        created=_time(datetime.fromisoformat(material['review']['created_at']))
        expires=_time(datetime.fromisoformat(material['review']['expires_at']))
        if not created<=_time(now)<expires:raise DexExecutorError('La preparación LAB/PAPER ha caducado')
        validate_origin(material,replay=False)
        self._sources._repository(material['review']['source_kind'])
        if self.research is not None:self.research.assert_current()
        return material


def prepare_origin(sources:DexBotSources,*,now:datetime,**parameters) -> PreparedDexOrigin:
    validation_id=parameters.pop('research_validation_id',None)
    review=sources.preview(now=now,**parameters)
    source=sources.observation(parameters['kind'],parameters['session_id'],now=now,verify_backtest=False)
    if (source.binding()!=review['source'] or source.observation_digest!=review['observation_digest']
            or source.candle_t!=review['candle_t']):raise DexExecutorError('PAPER cambió durante la preparación')
    repository=sources._repository(parameters['kind'])
    meta,bars=repository.lab.dataset(source.dataset_id)
    store=repository.store if parameters['kind']=='DCA' else repository.lab
    saved=store.run(source.source_run_id)
    with repository.connect() as db:
        if parameters['kind']=='DCA':
            event=db.execute('SELECT event_json FROM paper_dca_events WHERE session_id=? AND sequence_no=0 AND execution_t=?',
                (source.session_id,source.candle_t+86400)).fetchone()
        else:
            event=db.execute("SELECT payload_json FROM events WHERE session_id=? AND candle_t=? AND kind='OBSERVATION'",
                (source.session_id,source.candle_t)).fetchone()
        if event is None:raise DexExecutorError('La observación PAPER ya no está disponible')
    material={'schema':'orbis.dex.bot.origin.v1','review':review,
        'dataset':{key:meta[key] for key in ('symbol','timeframe','source','verified_public','hash')}|{'candles':bars},
        'backtest':saved,'seed':[bar.json() for bar in source.candles],
        'observed_at':source.observed_at.isoformat(),'observation_json':event[0]}
    validate_origin(material,replay=True)
    checked=sources.check_research(parameters['kind'],review['source'],review['candle_t'],validation_id) if validation_id is not None else None
    sources._repository(parameters['kind'])
    raw,digest=_encoded(material)
    return PreparedDexOrigin(_SEAL,raw,digest,sources,checked)


def validate_origin(material:dict,*,replay:bool=False) -> None:
    """Validate self-contained source identity, hashes and optional full replay."""
    try:
        if set(material)!={'schema','review','dataset','backtest','seed','observed_at','observation_json'} or material['schema']!='orbis.dex.bot.origin.v1':raise ValueError('schema')
        review,meta,saved=material['review'],material['dataset'],material['backtest']
        expected={'protocol','source_kind','source','definition','observation_digest','candle_t','signal_market','execution_market',
            'chain_id','amounts','dca','allow_buy','allow_sell','created_at','expires_at','preparation_only','automatic_execution','pending','preparation_digest'}
        if (set(review)!=expected or review['protocol']!='orbis.dex.bot.preparation.v1'
                or review['source_kind'] not in ('STRATEGY','DCA') or review['chain_id']!='0x38'
                or review['preparation_only'] is not True or review['automatic_execution'] is not False
                or content_hash({k:v for k,v in review.items() if k!='preparation_digest'})!=review['preparation_digest']):raise ValueError('review')
        source=review['source'];dca=review['source_kind']=='DCA'
        definition=validate_dca(review['definition']) if dca else validate_strategy(review['definition'])
        source_keys={'session_id','version_id','dataset_id','dataset_hash','source_run_id','engine_version','symbol','timeframe','definition_hash'}
        if dca:source_keys|={'kind','run_hash'}
        if set(source)!=source_keys:raise ValueError('source fields')
        for key in ('session_id','version_id','dataset_id','source_run_id'):
            if str(UUID(source[key]))!=source[key]:raise ValueError('source identifier')
        if (source['symbol']!='BNB/USDT' or source['engine_version']!=(dca_engine_for_rule(definition) if dca else CURRENT_ENGINE_VERSION)
                or source['definition_hash']!=content_hash(definition) or definition!=review['definition']
                or not dca and definition['direction']!='LONG'
                or dca and (source['kind']!='DCA' or source['timeframe']!='1d')
                or review['allow_buy'] is not True or review['allow_sell'] is not (not dca)
                or review['signal_market']!='MEXC · BNB/USDT' or review['execution_market']!='Uniswap v3 · USDT/WBNB · BNB Chain'):
            raise ValueError('definition')
        if (set(meta)!={'symbol','timeframe','source','verified_public','hash','candles'}
                or meta['symbol']!=source['symbol'] or meta['timeframe']!=source['timeframe']
                or meta['source']!='mexc-spot-public' or meta['verified_public'] is not True
                or meta['hash']!=source['dataset_hash'] or content_hash({k:v for k,v in meta.items() if k!='hash'})!=meta['hash']):raise ValueError('dataset')
        created=_time(datetime.fromisoformat(review['created_at']))
        expires=_time(datetime.fromisoformat(review['expires_at']))
        observed=_time(datetime.fromisoformat(material['observed_at']))
        if (expires-created).total_seconds()!=60:raise ValueError('review lifetime')
        bars,gaps=validate_candles(meta['candles'],source['timeframe'],now=int(created.timestamp()))
        seed,missing=validate_candles(material['seed'],source['timeframe'],now=int(created.timestamp()))
        if (gaps or missing or not 3<=len(bars)<len(seed)<=5000 or material['seed'][:len(bars)]!=meta['candles']
                or seed[-1].t!=review['candle_t'] or seed[-1].t!=int(created.timestamp())//INTERVALS[source['timeframe']]*INTERVALS[source['timeframe']]-INTERVALS[source['timeframe']]
                or not seed[-1].t+INTERVALS[source['timeframe']]<=observed.timestamp()<=created.timestamp()):raise ValueError('seed')
        raw=material['observation_json']
        if type(raw) is not str or hashlib.sha256(raw.encode()).hexdigest()!=review['observation_digest']:raise ValueError('observation hash')
        observation=json.loads(raw)
        if observation['signal_candle' if dca else 'candle']!=material['seed'][-1]:raise ValueError('observation candle')
        if dca:
            if observation['kind']!='OBSERVATION' or observation['engine_version']!=source['engine_version']:
                raise ValueError('DCA observation')
            window_key=dca_signal_window_key(definition)
            if window_key:
                width=dca_paper_overlap_count(definition)+1
                if len(seed)<width or observation.get(window_key)!=material['seed'][-width:]:
                    raise ValueError('DCA signal window')
                ema=seed_dca_ema(definition,(bar.c for bar in bars))
                if ema is not None and len(seed)==len(bars)+1:
                    if observation.get('ema_origin')!=ema.snapshot():raise ValueError('DCA EMA origin')
                elif 'ema_origin' in observation:raise ValueError('DCA EMA scope')
        else:
            indicators=IndicatorCache(seed)
            match,entry=evaluate(definition['entry'],seed,len(seed)-1,indicators,base_timeframe=source['timeframe'])
            exit_match,exit_evidence=evaluate(definition['exit'],seed,len(seed)-1,indicators,base_timeframe=source['timeframe']) if 'exit' in definition else (None,None)
            if (observation['entry_match'] is not match or observation['entry']!=entry
                    or observation['exit_match'] is not exit_match or observation['exit']!=exit_evidence):raise ValueError('signal replay')
        metadata=DCA_METADATA if dca else STRATEGY_METADATA
        result={k:v for k,v in saved.items() if k not in metadata}
        if (saved['id']!=source['source_run_id'] or saved['version_id']!=source['version_id']
                or saved['dataset_id']!=source['dataset_id'] or saved['dataset_hash']!=source['dataset_hash']
                or saved['engine_version']!=source['engine_version'] or saved['result_hash']!=content_hash(result)
                or saved['definition_hash' if dca else 'strategy_hash']!=source['definition_hash']
                or not dca and saved['dataset_source']!=meta['source']
                or dca and source['run_hash']!=saved['result_hash']):raise ValueError('backtest identity')
        if replay:
            repeated=simulate_dca(bars,definition,timeframe=source['timeframe']) if dca else run_backtest(bars,definition,source['timeframe'])
            if content_hash(repeated)!=saved['result_hash']:raise ValueError('backtest replay')
    except (ValueError,KeyError,TypeError,AttributeError,IndexError,OverflowError):
        raise DexExecutorError('La procedencia guardada del bot DEX no es reproducible') from None
