"""Research -> observed PAPER -> bounded MEXC authority, owned venues only."""
import json
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from app.lab.store import content_hash
from app.live_execution.spot_bot_source import verified_spot_bot_observation
from app.live_execution.spot_bot_store import bot_record, encoded, SpotBotStoreError
from app.portfolio.models import RealSpotBot
from app.research.mexc_binding import check_mexc_research, read_mexc_research_binding
from app.research.references import ResearchReferenceError
from tests.mexc_bot_fixture import make_environment, make_app
from tests.mexc_research_fixture import historical_reserve
from tests.test_mexc_bot_api import RecordedSupervisor, authorize, activate
from tests.test_mexc_recovery_api import new_anchor
from tests.test_mexc_execution_state import PASSWORD


@pytest.fixture
def case(tmp_path, monkeypatch):
    env = make_environment(tmp_path, monkeypatch)
    from app.portfolio import api
    original = api.RealAccess
    class CapturedAccess(original):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs)
            env['access'] = self
    monkeypatch.setattr(api,'RealAccess',CapturedAccess)
    return env


def research(case):
    source = verified_spot_bot_observation(case['paper'],case['session']['id'],now=case['clock'](),verify_backtest=True)
    return source,check_mexc_research(case['paper'],source,case['validation']['id'])


def test_binding_pins_distinct_historical_dataset_and_survives_source_closure(case):
    source,checked = research(case)
    binding = checked.binding
    assert str(binding.research.dataset_id) != source.dataset_id
    assert binding.research.dataset_to_t < binding.paper.seed_from_t
    assert binding.research.execution_authority == 'NONE' and binding.dca_anchor is None
    assert binding.paper.observation_digest == source.observation_digest
    checked.assert_current()
    case['paper'].passphrase = None
    assert read_mexc_research_binding(binding.exported(),source.binding()) == binding
    legacy = binding.payload()
    del legacy['paper']['observation_digest']
    legacy['sha256'] = content_hash(legacy)
    assert read_mexc_research_binding(legacy,source.binding()).exported() == legacy


def test_passive_review_pins_the_replayed_evidence_before_authorizing(case):
    account = case['account']
    base = f'/real/accounts/{account}/mexc-bots'
    query = {'paper_session_id':case['session']['id'], 'research_validation_id':case['validation']['id']}
    count = len(RecordedSupervisor.instances)
    with TestClient(make_app(case,supervisor_factory=RecordedSupervisor)) as client:
        token = client.post('/real/vault/unlock', json={'passphrase':PASSWORD}).json()['token']
        headers = {'X-Orbis-Vault-Token':token}
        assert client.get(base+'/research-review', params=query).status_code == 401
        assert client.get(base+'/research-review', params=query,
                          headers={**headers,'Origin':'https://outside.invalid'}).status_code == 403
        case['venue'].calls.clear()
        reply = client.get(base+'/research-review', params=query, headers=headers)
        assert reply.status_code == 200, reply.text
        assert reply.headers['Cache-Control'] == 'no-store'
        previous = reply.json()
        assert previous['research']['execution_authority'] == 'NONE'
        assert previous['paper']['session_id'] == query['paper_session_id']
        assert not case['venue'].calls and not case['trader'].posts
        assert not client.get(base,headers=headers).json()

        with case['lab'].connect() as db:
            db.execute("UPDATE validation_runs SET created_at='2025-01-01T00:00:00+00:00' WHERE id=?",
                       (case['validation']['id'],))
        headers = authorize(client)
        body = {'request_id':str(uuid4()), 'draft_id':case['draft'].policy_id,
                'anchor_id':new_anchor(client,headers,account), **query, 'entry_budget_quote':'20',
                'confirmation':'ACTIVAR_BOT_REAL', 'research_review_digest':previous['sha256']}
        rejected = client.post(base, json=body, headers=headers)
        assert rejected.status_code == 409 and 'Revisa el origen' in rejected.json()['detail']
        assert len(RecordedSupervisor.instances) == count and not case['trader'].posts
        assert not client.get(base,headers=headers).json()
        proof = client.get(base+'/research-review', params=query, headers=headers).json()
        assert proof['sha256'] != previous['sha256']
        accepted = client.post(base, json={**body,'research_review_digest':proof['sha256']}, headers=headers)
        assert accepted.status_code == 200 and accepted.json()['research_binding'] == proof, accepted.text
        with case['access'].vault.session() as db:
            _, config, _ = bot_record(db, body['request_id'])
            assert config['activation_request']['research_review_digest'] == proof['sha256']


@pytest.mark.parametrize('fault',['missing','other_version','altered_result','unverified','wrong_market','after_seed','engine'])
def test_new_activation_rejects_invalid_research_before_authorizing_or_launching(case,fault):
    lab,original = case['lab'],case['validation']
    identifier = original['id']
    if fault == 'missing':
        identifier = None
    elif fault == 'other_version':
        version = lab.version(original['version_id'])
        updated = lab.add_version(version['strategy_id'],{**version['definition'],'take_profit':'0.2'})
        identifier = historical_reserve(lab,updated['version_id'],before_t=original['test_start_t'])['id']
    elif fault in ('altered_result','engine'):
        with lab.connect() as db:
            row = json.loads(db.execute('SELECT result_json FROM validation_runs WHERE id=?',(identifier,)).fetchone()[0])
            if fault == 'engine':
                row['lab_engine_version'] = 'unavailable'
            else:
                row['out_of_sample']['metrics']['net_profit'] = '1234567'
                row['result_hash'] = content_hash({k:v for k,v in row.items() if k not in ('result_hash','strategy_hash','lab_engine_version')})
            db.execute('UPDATE validation_runs SET result_json=? WHERE id=?',(json.dumps(row),identifier))
    else:
        dataset_id = original['dataset_id']
        if fault == 'after_seed':
            # All candles are closed already, but follow the PAPER seed cutoff.
            identifier = historical_reserve(lab,original['version_id'],before_t=case['hour'])['id']
        else:
            with lab.connect() as db:
                if fault == 'unverified':
                    db.execute('UPDATE datasets SET verified_public=0 WHERE id=?',(dataset_id,))
                else:
                    db.execute("UPDATE datasets SET symbol='ETH/USDT' WHERE id=?",(dataset_id,))
    count = len(RecordedSupervisor.instances)
    with TestClient(make_app(case,supervisor_factory=RecordedSupervisor)) as client:
        headers = authorize(client)
        anchor = new_anchor(client,headers,case['account'])
        body = {'request_id':str(uuid4()),'draft_id':case['draft'].policy_id,'anchor_id':anchor,
                'paper_session_id':case['session']['id'],'entry_budget_quote':'20','confirmation':'ACTIVAR_BOT_REAL'}
        if identifier: body['research_validation_id'] = identifier
        response = client.post(f"/real/accounts/{case['account']}/mexc-bots",json=body,headers=headers)
        assert response.status_code == 409,response.text
        assert len(RecordedSupervisor.instances) == count and case['trader'].posts == []
        with case['access'].vault.session() as db:
            assert db.query(RealSpotBot).count() == 0


def test_common_public_candles_must_agree_even_if_each_dataset_is_self_consistent(case):
    source,_ = research(case)
    meta,_ = case['lab'].dataset(source.dataset_id)
    reserve = historical_reserve(case['lab'],source.version_id,before_t=meta['end_t']+3600)
    with pytest.raises(ResearchReferenceError,match='comunes'):
        check_mexc_research(case['paper'],source,reserve['id'])


def test_original_modified_during_exchange_preflight_cannot_gain_authority(case):
    count = len(RecordedSupervisor.instances)
    with TestClient(make_app(case,supervisor_factory=RecordedSupervisor)) as client:
        headers = authorize(client)
        def changed(_):
            with case['lab'].connect() as db:
                db.execute("UPDATE validation_runs SET created_at='2025-01-01T00:00:00+00:00' WHERE id=?",(case['validation']['id'],))
        case['trader'].pair_view_account = changed
        response,_,_ = activate(client,case,headers)
        assert response.status_code == 409,response.text
        assert len(RecordedSupervisor.instances) == count and not case['trader'].posts
        with case['access'].vault.session() as db:
            assert db.query(RealSpotBot).count() == 0


@pytest.mark.parametrize('change',['remove','self_rehash_other_market','different_validation_id'])
def test_archived_binding_cannot_be_removed_or_retargeted_with_only_a_config_rehash(case,change):
    with TestClient(make_app(case,supervisor_factory=RecordedSupervisor)) as client:
        headers = authorize(client)
        created,url,body = activate(client,case,headers)
        assert created.status_code == 200,created.text
        client.post(url+'/'+body['request_id']+'/stop',headers=headers,json={'confirmation':'DETENER_BOT'})
        vault = case['access'].vault
        with vault.session() as db:
            row,config,_ = bot_record(db,body['request_id'])
            if change == 'remove':
                config.pop('research_binding')
            elif change == 'different_validation_id':
                config['activation_request']['research_validation_id'] = str(uuid4())
            else:
                binding = config['research_binding']
                binding['research']['symbol'] = 'ETH/USDT'
                binding['research']['sha256'] = content_hash({k:v for k,v in binding['research'].items() if k != 'sha256'})
                binding['sha256'] = content_hash({k:v for k,v in binding.items() if k != 'sha256'})
            row.config_json,row.config_digest = encoded(config)
            db.commit()
        with vault.session() as db,pytest.raises(SpotBotStoreError):
            bot_record(db,body['request_id'])
        assert client.get(url,headers=headers).status_code == 409
        assert case['trader'].posts == []


def test_legacy_replay_remains_inert_and_new_rearm_requires_and_archives_research(case):
    with TestClient(make_app(case,supervisor_factory=RecordedSupervisor)) as client:
        headers = authorize(client)
        created,url,body = activate(client,case,headers)
        assert created.status_code == 200,created.text
        bot_url = url+'/'+body['request_id']
        client.post(bot_url+'/stop',headers=headers,json={'confirmation':'DETENER_BOT'})
        vault = case['access'].vault
        # Recreate the exact pre-v90 representation in this owned fixture.
        legacy_body = {k:v for k,v in body.items() if k != 'research_validation_id'}
        with vault.session() as db:
            row,config,_ = bot_record(db,body['request_id'])
            config.pop('research_binding')
            config['activation_request'].pop('research_validation_id')
            row.config_json,row.config_digest = encoded(config)
            db.commit()
        count = len(RecordedSupervisor.instances)
        replay = client.post(url,headers=headers,json=legacy_body)
        assert replay.status_code == 200 and not replay.json()['execution_enabled']
        assert replay.json()['research_binding'] is None and len(RecordedSupervisor.instances) == count
        rearm = {'request_id':str(uuid4()),'draft_id':case['draft'].policy_id,
                 'anchor_id':new_anchor(client,headers,case['account']),'paper_session_id':case['session']['id'],
                 'entry_budget_quote':'20','confirmation':'REANUDAR_BOT_REAL'}
        assert client.post(bot_url+'/rearm',headers=headers,json=rearm).status_code == 409
        rearm['research_validation_id'] = case['validation']['id']
        result = client.post(bot_url+'/rearm',headers=headers,json=rearm)
        assert result.status_code == 200,result.text
        assert result.json()['research_binding']['research']['artifact_id'] == case['validation']['id']
        with vault.session() as db:
            _,config,_ = bot_record(db,body['request_id'])
            assert 'research_binding' not in config['authorizations'][0]
            assert config['authorizations'][-1]['research_binding'] == result.json()['research_binding']
        assert client.post(bot_url+'/rearm',headers=headers,json=rearm).status_code == 200
        assert len(RecordedSupervisor.instances) == count+1 and case['trader'].posts == []


def test_candidate_separates_observed_paper_from_missing_research(case):
    with case['lab'].connect() as db:
        db.execute('DELETE FROM validation_runs WHERE id=?',(case['validation']['id'],))
    with TestClient(make_app(case,supervisor_factory=RecordedSupervisor)) as client:
        headers = authorize(client)
        response = client.get(f"/real/accounts/{case['account']}/mexc-bots/candidates",headers=headers)
        assert response.status_code == 200,response.text
        candidate = response.json()[0]
        assert candidate['paper_ready'] and not candidate['ready'] and candidate['research_options'] == []
        assert 'fuera de muestra' in candidate['reason']
