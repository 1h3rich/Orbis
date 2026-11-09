"""Self-contained origin replay, not merely a recomputed JSON checksum."""
from datetime import datetime,timezone
from copy import deepcopy
import hashlib,json
import pytest
from app.lab.store import content_hash
from app.live_execution.dex_bot_origin import prepare_origin,validate_origin
from app.live_execution.dex_bot_source import DexBotSources
from app.live_execution.dex_artifact import DexExecutorError
from tests.dex_bot_source_fixture import SourceCase,DcaSourceCase,DEX_EMA_RULE


def prepared(case,*,dca=False):
    return prepare_origin(DexBotSources(dca_paper=case.paper) if dca else DexBotSources(case.paper),
        kind='DCA' if dca else 'STRATEGY',session_id=case.session['id'],entry_usdt='12',budget_usdt='60',
        exposure_usdt='25',loss_usdt='5',wbnb_quota='1',monthly_usdt='40' if dca else None,now=datetime.now(timezone.utc))


def test_stored_observation_replays_rules_even_after_all_checksums_are_recomputed(tmp_path):
    case=SourceCase(tmp_path)
    try:
        material=prepared(case).material(now=datetime.now(timezone.utc))
        observation=json.loads(material['observation_json'])
        assert observation['entry_match'] is True and observation['entry']['kind']=='BEARISH_STREAK'
        observation['entry_match']=False
        material['observation_json']=json.dumps(observation,sort_keys=True,separators=(',',':'))
        review=material['review'];review['observation_digest']=hashlib.sha256(material['observation_json'].encode()).hexdigest()
        review['preparation_digest']=content_hash({k:v for k,v in review.items() if k!='preparation_digest'})
        with pytest.raises(DexExecutorError):validate_origin(material,replay=True)
    finally:case.close()


@pytest.mark.parametrize('dca',[False,True])
def test_copied_backtest_repeats_offline_and_rejects_fabricated_results(tmp_path,dca):
    case=(DcaSourceCase if dca else SourceCase)(tmp_path)
    try:
        material=prepared(case,dca=dca).material(now=datetime.now(timezone.utc))
        case.close();case.lab.path.unlink()
        validate_origin(material,replay=True)
        corrupted=deepcopy(material)
        run=corrupted['backtest']
        if dca:
            # This is the public DCA comparison report, not a REAL balance.
            run['results']['conditional']['metrics']['net_profit']='999999'
        else:run['metrics']['net_profit']='999999'
        metadata={'id','dataset_id','version_id','created_at','engine_version','dataset_hash','dataset_source','strategy_hash','result_hash','definition_hash'}
        run['result_hash']=content_hash({k:v for k,v in run.items() if k not in metadata})
        if dca:
            review=corrupted['review'];review['source']['run_hash']=run['result_hash']
            review['preparation_digest']=content_hash({k:v for k,v in review.items() if k!='preparation_digest'})
        with pytest.raises(DexExecutorError):validate_origin(corrupted,replay=True)
    finally:case.close()


@pytest.mark.parametrize('rule,key,engine',[
    ({'sma_gate':{'period':2,'condition':'below'}},'sma_window','dca-1.7'),
    (DEX_EMA_RULE,'average_window','dca-1.8'),
    (None,'volume_window','dca-1.9'),
])
def test_dca_filtered_origin_replays_window_and_ema_seed_even_after_rehash(tmp_path,rule,key,engine):
    case=DcaSourceCase(tmp_path,average_rule=rule,volume=key=='volume_window',research=True)
    try:
        material=prepared(case,dca=True).material(now=datetime.now(timezone.utc))
        assert material['review']['source']['engine_version']==engine
        observation=json.loads(material['observation_json'])
        assert observation[key]==material['seed'][-(4 if key=='volume_window' else 3):]
        if engine=='dca-1.8':assert observation['ema_origin']['count']==3
        case.close();case.lab.path.unlink()
        validate_origin(material,replay=True)

        def changed_observation(change):
            changed=deepcopy(material);payload=json.loads(changed['observation_json'])
            change(payload)
            changed['observation_json']=json.dumps(payload,sort_keys=True,separators=(',',':'))
            review=changed['review'];review['observation_digest']=hashlib.sha256(changed['observation_json'].encode()).hexdigest()
            review['preparation_digest']=content_hash({k:v for k,v in review.items() if k!='preparation_digest'})
            return changed

        wrong_window=changed_observation(lambda payload:payload[key][0].update(v='10000'))
        with pytest.raises(DexExecutorError):validate_origin(wrong_window,replay=True)
        if engine=='dca-1.8':
            wrong_ema=changed_observation(lambda payload:payload['ema_origin'].update(current='10000'))
            with pytest.raises(DexExecutorError):validate_origin(wrong_ema,replay=True)
    finally:case.close()
