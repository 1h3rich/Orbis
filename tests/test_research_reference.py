"""References bind local research, not money or an execution permission."""

from copy import deepcopy
from decimal import Inexact, ROUND_DOWN, localcontext
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.automation.api import create_automation_router
from app.automation.store import AutomationStore
from app.automation.validation import VALIDATION_ENGINE_VERSION as DCA_VALIDATION_VERSION, chronological_dca_holdout
from app.lab.api import create_lab_router
from app.lab.engine import CURRENT_ENGINE_VERSION, validate_strategy
from app.lab.experiment_verification import verify_lab_experiment
from app.lab.store import LabStore, content_hash
from app.lab.validation import VALIDATION_ENGINE_VERSION, ROLLING_ENGINE_VERSION, chronological_holdout, rolling_fixed_validation
from app.research import references
from app.research.references import ResearchReferenceError, dca_reference, revalidate_reference, strategy_reference
from tests.test_dca_validation import bars, rule


@pytest.fixture
def prepared(tmp_path):
    lab = LabStore(tmp_path / 'lab.db')
    automation = AutomationStore(tmp_path / 'automation.db')
    dataset = lab.save_dataset('BTC/USDT', '1d', 'mexc-spot-public', bars(120), [], verified_public=True)
    definition = validate_strategy({'direction': 'LONG', 'initial_capital': '1000', 'allocation': '1',
        'fee_rate': '0.001', 'slippage_rate': '0', 'stop_loss': '0.1', 'take_profit': '0.2',
        'entry': {'kind': 'CLOSE_ABOVE_EMA', 'period': 2}})
    strategy = lab.create_strategy('Reference source', definition)
    reserve = chronological_holdout(bars(120), definition, '1d')
    saved = lab.save_validation(dataset['id'], strategy['version_id'], dataset['hash'], reserve,
        VALIDATION_ENGINE_VERSION, lab_engine_version=CURRENT_ENGINE_VERSION,
        expected_strategy_hash=content_hash(definition))
    plan = automation.create_rule('DCA reference', rule())
    dca = automation.save_validation(plan['version_id'], dataset['id'], dataset['hash'],
        chronological_dca_holdout(bars(120), plan['definition'], '1d'), DCA_VALIDATION_VERSION,
        lab_path=lab.path, expected_definition_hash=content_hash(plan['definition']))
    return lab, automation, dataset, strategy, saved, dca


@pytest.mark.parametrize('family', ['STRATEGY', 'DCA'])
def test_reference_roundtrip_unchanged_old_version_and_hostile_context(prepared, family):
    lab, automation, dataset, strategy, saved, dca = prepared
    artifact = saved if family == 'STRATEGY' else dca
    def build():
        return strategy_reference(lab, artifact['id']) if family == 'STRATEGY' else dca_reference(automation, lab, artifact['id'])
    first = build()
    assert first.execution_authority == 'NONE' and first.family == family
    assert first.artifact_digest == content_hash(artifact)
    assert str(first.version_id) == artifact['version_id']
    assert str(first.dataset_id) == dataset['id'] and first.dataset_hash == dataset['hash']
    assert first.sample_bars == 36
    lab.add_version(strategy['id'], {**strategy['definition'], 'take_profit': '0.3'})
    assert build() == first
    exported = json.loads(json.dumps(first.exported()))
    assert exported['schema'] == 'orbis.research.reference.v1'
    assert revalidate_reference(exported, lab, automation) == first
    with localcontext() as context:
        context.prec = 1
        context.rounding = ROUND_DOWN
        context.traps[Inexact] = True
        assert build() == first
    assert lab.validation(saved['id']) == saved
    assert automation.validation(dca['id']) == dca


def test_rolling_reference_contains_disjoint_fixed_version_windows(prepared):
    lab, _, dataset, strategy, _, _ = prepared
    result = rolling_fixed_validation(bars(120), strategy['definition'], '1d', folds=3)
    saved = lab.save_validation(dataset['id'], strategy['version_id'], dataset['hash'], result,
        ROLLING_ENGINE_VERSION, lab_engine_version=CURRENT_ENGINE_VERSION,
        expected_strategy_hash=content_hash(strategy['definition']))
    ref = strategy_reference(lab, saved['id'])
    assert len(ref.windows) == 3 and ref.sample_bars == 90
    assert all(a.to_t < b.from_t for a, b in zip(ref.windows, ref.windows[1:]))


@pytest.mark.parametrize('change', ['hash', 'symbol', 'authority', 'family', 'bool_window'])
def test_transport_tamper_cannot_become_authority_or_another_artifact(prepared, change):
    lab, automation, _, _, saved, _ = prepared
    value = strategy_reference(lab, saved['id']).exported()
    if change == 'hash':
        value['sha256'] = '0' * 64
    else:
        if change == 'symbol': value['symbol'] = 'ETH/USDT'
        if change == 'authority': value['execution_authority'] = 'TRADE'
        if change == 'family': value['family'] = 'DCA'
        if change == 'bool_window': value['windows'][0]['bars'] = True
        value['sha256'] = content_hash({key: data for key, data in value.items() if key != 'sha256'})
    with pytest.raises(ResearchReferenceError):
        revalidate_reference(value, lab, automation)


def test_self_consistent_export_does_not_hide_modified_local_record(prepared):
    lab, automation, _, _, saved, _ = prepared
    value = strategy_reference(lab, saved['id']).exported()
    with lab.connect() as db:
        db.execute("UPDATE validation_runs SET created_at='2025-01-01T00:00:00+00:00' WHERE id=?", (saved['id'],))
    assert verify_lab_experiment(lab, lab.validation(saved['id']), 'validation')['verified']
    with pytest.raises(ResearchReferenceError):
        revalidate_reference(value, lab, automation)


def test_legacy_unpinned_and_missing_results_do_not_get_references(prepared):
    lab, _, _, _, saved, _ = prepared
    with lab.connect() as db:
        data = deepcopy(saved)
        for key in ('id', 'dataset_id', 'version_id', 'dataset_hash', 'created_at', 'engine_version', 'lab_engine_version'):
            data.pop(key)
        data.pop('strategy_hash')
        db.execute('UPDATE validation_runs SET result_json=? WHERE id=?', (json.dumps(data), saved['id']))
    with pytest.raises(ResearchReferenceError):
        strategy_reference(lab, saved['id'])
    with pytest.raises(ResearchReferenceError):
        strategy_reference(lab, 'missing')


@pytest.mark.parametrize('family', ['STRATEGY', 'DCA'])
def test_reference_factory_rejects_inputs_changed_after_replay(prepared, monkeypatch, family):
    lab, automation, dataset, _, saved, dca = prepared
    name = 'verify_lab_experiment' if family == 'STRATEGY' else 'verify_dca_validation'
    original = getattr(references, name)
    def race(*args, **kwargs):
        checked = original(*args, **kwargs)
        assert checked['verified']
        with lab.connect() as db:
            db.execute("UPDATE datasets SET source='changed' WHERE id=?", (dataset['id'],))
        return checked
    monkeypatch.setattr(references, name, race)
    with pytest.raises(ResearchReferenceError):
        if family == 'STRATEGY': strategy_reference(lab, saved['id'])
        else: dca_reference(automation, lab, dca['id'])


def test_reference_endpoints_are_read_only_and_protected_by_origin(prepared):
    lab, automation, _, _, saved, dca = prepared
    app = FastAPI()
    app.include_router(create_lab_router(lab.path))
    app.include_router(create_automation_router(automation.path, lab.path))
    client = TestClient(app)
    for endpoint, artifact in [('/lab/validations/', saved), ('/automation/dca/validations/', dca)]:
        response = client.get(endpoint + artifact['id'] + '/reference')
        assert response.status_code == 200, response.text
        assert response.headers['cache-control'] == 'no-store'
        assert response.json()['execution_authority'] == 'NONE'
        assert client.get(endpoint + artifact['id'] + '/reference',
                          headers={'Origin': 'https://outside.invalid'}).status_code == 403
    with automation.connect() as db:
        assert db.execute('SELECT count(*) FROM paper_dca_sessions').fetchone()[0] == 0
