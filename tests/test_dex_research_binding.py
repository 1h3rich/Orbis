"""DEX qualification uses original research, not historical DEX fill claims."""
import json
from decimal import localcontext
from uuid import uuid4

import pytest

from app.lab.store import content_hash
from app.live_execution.dex_bot_source import DexBotSources
from app.research.dex_binding import check_dex_research, read_dex_signal_research
from app.research.references import ResearchReferenceError
from tests.dex_bot_source_fixture import SourceCase, DcaSourceCase
from tests.mexc_research_fixture import historical_reserve


@pytest.fixture(params=['STRATEGY', 'DCA'])
def case(tmp_path, request):
    value = DcaSourceCase(tmp_path) if request.param == 'DCA' else SourceCase(tmp_path)
    value.kind = request.param
    value.sources = DexBotSources(dca_paper=value.paper) if value.kind == 'DCA' else DexBotSources(value.paper)
    value.repository = value.sources._repository(value.kind)
    value.observation = value.sources.observation(value.kind, value.session['id'], now=value.now)
    value.validation = historical_reserve(value.lab, value.version['version_id'],
        before_t=value.dataset['start_t'], symbol='BNB/USDT', timeframe=value.observation.timeframe,
        automation=value.store if value.kind == 'DCA' else None)
    yield value
    value.close()


def checked(case, identifier=None):
    return check_dex_research(case.repository, case.observation.binding(), case.observation.candle_t,
                              identifier or case.validation['id'])


def test_separate_markets_and_replay_originals(case):
    receipt = checked(case)
    receipt.assert_current()
    proof = receipt.evidence
    assert proof.research.dataset_to_t < proof.paper.seed_from_t
    assert proof.research.execution_authority == 'NONE'
    assert proof.signal_market == 'MEXC_BNB_USDT'
    assert proof.execution_market == 'UNISWAP_V3_BNB_USDT_WBNB'
    assert proof.historical_model == 'SIGNAL_MARKET_WITHOUT_DEX_EXECUTION_COSTS'
    assert (proof.dca_reference_policy is not None) == (case.kind == 'DCA')
    case.close()
    assert read_dex_signal_research(proof.exported(), case.observation.binding()) == proof


def test_decimal_context_does_not_change_qualification(case):
    expected = checked(case).evidence
    with localcontext() as context:
        context.prec = 6
        assert checked(case).evidence == expected


@pytest.mark.parametrize('fault', ['missing', 'other_version', 'later', 'wrong_market', 'overlap', 'altered_result'])
def test_reject_incompatible_or_forged_original(case, fault):
    identifier = case.validation['id']
    store = case.store if case.kind == 'DCA' else case.lab
    if fault == 'missing':
        identifier = str(uuid4())
    elif fault == 'other_version':
        if case.kind == 'DCA':
            other = case.store.create_rule('Otra regla', {**case.version['definition'], 'amount': '12'})
        else:
            other = case.lab.create_strategy('Otra estrategia', {**case.version['definition'], 'take_profit': '0.2'})
        identifier = historical_reserve(case.lab, other['version_id'], before_t=case.dataset['start_t'],
            symbol='BNB/USDT', timeframe=case.observation.timeframe,
            automation=store if case.kind == 'DCA' else None)['id']
    elif fault in ('later', 'overlap'):
        from app.lab.engine import INTERVALS
        step = INTERVALS[case.observation.timeframe]
        before = case.dataset['end_t'] + (2 if fault == 'later' else 1)*step
        identifier = historical_reserve(case.lab, case.version['version_id'], before_t=before,
            symbol='BNB/USDT', timeframe=case.observation.timeframe,
            automation=store if case.kind == 'DCA' else None)['id']
    elif fault == 'wrong_market':
        with case.lab.connect() as db:
            db.execute("UPDATE datasets SET symbol='ETH/USDT' WHERE id=?", (case.validation['dataset_id'],))
    else:
        # Even a recomputed outer hash is not a replay of the actual engine.
        with store.connect() as db:
            table = 'dca_validation_runs' if case.kind == 'DCA' else 'validation_runs'
            raw = json.loads(db.execute(f'SELECT result_json FROM {table} WHERE id=?', (identifier,)).fetchone()[0])
            if case.kind == 'DCA':
                raw['out_of_sample']['results']['conditional']['metrics']['buys'] = 1234567
            else:
                raw['out_of_sample']['metrics']['net_profit'] = '1234567'
            db.execute(f'UPDATE {table} SET result_json=? WHERE id=?', (json.dumps(raw), identifier))
    with pytest.raises((ResearchReferenceError, ValueError)):
        checked(case, identifier)


def test_commit_guard_rejects_original_changed_after_preflight(case):
    receipt = checked(case)
    store = case.store if case.kind == 'DCA' else case.lab
    with store.connect() as db:
        table = 'dca_validation_runs' if case.kind == 'DCA' else 'validation_runs'
        db.execute(f"UPDATE {table} SET created_at='2025-01-01T00:00:00+00:00' WHERE id=?", (case.validation['id'],))
    with pytest.raises(ResearchReferenceError):
        receipt.assert_current()


def test_archived_proof_cannot_change_source_with_only_outer_rehash(case):
    proof = checked(case).evidence.exported()
    proof['paper']['symbol'] = 'ETH/USDT'
    proof['sha256'] = content_hash({k: v for k, v in proof.items() if k != 'sha256'})
    with pytest.raises(ResearchReferenceError):
        read_dex_signal_research(proof, case.observation.binding())
