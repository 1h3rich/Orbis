"""GRVT research identities and one-use MAINNET review; isolated transports."""
import json
from decimal import Inexact,ROUND_UP,localcontext
from uuid import uuid4

import pytest

from app.live_execution.grvt_bot_source import bot_source
from app.portfolio.grvt_bracket_api import _wire
from app.research.grvt_binding import check_grvt_research,read_grvt_research_binding
from app.research.references import ResearchReferenceError
from app.lab.store import content_hash
from tests.grvt_research_fixture import historical_grvt_reserve
from tests.test_grvt_mainnet_bot_service import service_case
from tests.test_grvt_mainnet_bot_api import api_case,preview
from tests.test_grvt_mainnet_api import ROOT


def prepared(path,direction='LONG'):
    case,origin,fixture,emit,settings,service=service_case(path,direction,research=True)
    source=bot_source(origin.paper,origin.lab,fixture['session_id'])
    checked=check_grvt_research(origin.paper,origin.lab,source,fixture['research_validation_id'],now_ns=service.clock())
    return case,origin,fixture,source,checked,emit,service


@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_exact_ns_roundtrip_and_later_paper_observation_preserve_review_prefix(tmp_path,direction):
    case,origin,fixture,source,checked,emit,service=prepared(tmp_path,direction)
    try:
        exported=checked.binding.exported()
        assert exported==_wire(exported)
        assert type(exported['paper']['last_observed_at_ns']) is str
        assert int(exported['paper']['last_observed_at_ns'])>2**53
        assert exported['research']['execution_authority']=='NONE'
        assert exported['research']['direction']==direction and exported['paper']['observations']==1
        assert exported['historical_execution_model']=='PRICE_RULES_WITHOUT_DERIVATIVES_MARGIN_OR_FUNDING'
        assert read_grvt_research_binding(json.loads(json.dumps(exported)),source)==checked.binding
        emit()
        updated=check_grvt_research(origin.paper,origin.lab,source,fixture['research_validation_id'],
            now_ns=service.clock(),expected_binding=exported)
        assert updated.binding==checked.binding
        checked.assert_current(service.clock())
        assert not case.sent
    finally:service.stop_all();case.vault.lock()


def test_callers_decimal_context_cannot_change_research_identity(tmp_path):
    case,origin,fixture,source,checked,_,service=prepared(tmp_path)
    try:
        with localcontext() as context:
            context.prec=1;context.rounding=ROUND_UP;context.traps[Inexact]=True
            actual=check_grvt_research(origin.paper,origin.lab,source,fixture['research_validation_id'],now_ns=service.clock())
        assert actual.binding==checked.binding
    finally:service.stop_all();case.vault.lock()


@pytest.mark.parametrize('fault',['unknown','version','after_seed','overlap','missing_backtest','observation'])
def test_incompatible_research_or_paper_cannot_form_an_authorization(tmp_path,fault):
    case,origin,fixture,source,checked,_,service=prepared(tmp_path)
    try:
        identifier=fixture['research_validation_id']
        if fault=='unknown':identifier=str(uuid4())
        elif fault=='version':
            version=origin.lab.version(source['version_id'])
            changed=origin.lab.add_version(version['strategy_id'],{**version['definition'],'take_profit':'0.2'})
            identifier=historical_grvt_reserve(origin.lab,changed['version_id'],checked.binding.paper.seed_from_t)['id']
        elif fault in ('after_seed','overlap'):
            identifier=historical_grvt_reserve(origin.lab,source['version_id'],
                checked.binding.paper.seed_to_t+(7200 if fault=='after_seed' else 3600))['id']
        elif fault=='missing_backtest':
            with origin.paper.connect() as db:db.execute('UPDATE paper_signal_sessions SET source_run_id=NULL WHERE id=?',(source['session_id'],))
        else:
            with origin.paper.connect() as db:db.execute('UPDATE paper_signal_observations SET entry_match=2 WHERE session_id=?',(source['session_id'],))
        with pytest.raises(ResearchReferenceError):
            check_grvt_research(origin.paper,origin.lab,source,identifier,now_ns=service.clock())
        assert not case.sent
    finally:service.stop_all();case.vault.lock()


@pytest.mark.parametrize('fault',['artifact','observations','archive_head'])
def test_lightweight_commit_guard_rejects_changes_to_sealed_inputs(tmp_path,fault):
    case,origin,fixture,source,checked,_,service=prepared(tmp_path)
    try:
        if fault=='artifact':
            with origin.lab.connect() as db:
                db.execute("UPDATE validation_runs SET created_at='2025-01-01T00:00:00+00:00' WHERE id=?",(fixture['research_validation_id'],))
        else:
            with origin.paper.connect() as db:
                if fault=='observations':
                    db.execute('UPDATE paper_signal_observations SET observed_at_ns=observed_at_ns+1 WHERE session_id=?',(source['session_id'],))
                else:db.execute("UPDATE paper_signal_archive_chain SET digest=? WHERE session_id=? AND candle_t=?",('0'*64,source['session_id'],checked.binding.paper.observed_through_t))
        with pytest.raises(ResearchReferenceError):checked.assert_current(service.clock())
        assert not case.sent
    finally:service.stop_all();case.vault.lock()


def test_review_v2_has_exact_wire_identity_and_rejects_changed_original_before_start(tmp_path):
    with api_case(tmp_path) as (case,origin,source,emit,settings,api,headers):
        body=preview(api,headers,source)
        review=body['review']
        assert review['schema']=='orbis.grvt.mainnet.bot.review.v2'
        binding=review['research_binding']
        assert binding['sha256']==content_hash({k:v for k,v in binding.items() if k!='sha256'})
        assert isinstance(binding['paper']['last_observed_at_ns'],str)
        with origin.lab.connect() as db:
            db.execute("UPDATE validation_runs SET created_at='2025-01-01T00:00:00+00:00' WHERE id=?",(source['research_validation_id'],))
        assert api.post(ROOT+'/bots/start',headers=headers,json=body).status_code==409
        assert api.post(ROOT+'/bots/start',headers=headers,json=body).status_code==409
        assert api.get(ROOT+'/bots',headers=headers,params={'sub_account_id':'123456'}).json()['items']==[]
        assert not case.sent


def test_new_grant_recovers_its_same_research_after_backup_and_source_lock(tmp_path):
    from decimal import Decimal
    from app.live_execution.grvt_mainnet_bot_entry import MainnetBotPolicy,read_bot_grant
    from app.portfolio.vault import RealVault
    from app.lab.access import lab_access
    from app.derivatives.paper_access import paper_perp_access
    from tests.test_grvt_mainnet_bot_service import KEY,SIGNER,PASSWORD,wait_for
    case,origin,fixture,source,checked,_,service=prepared(tmp_path)
    try:
        policy=MainnetBotPolicy('123456',Decimal('50'),Decimal('100'),Decimal('50'),Decimal('10'))
        started=service.start(origin.paper,origin.lab,source['session_id'],policy,
            api_key='synthetic-mainnet-api-key',private_key=KEY,expected_signer=SIGNER,
            expected_ticket=service.ticket('123456'),keep_operating=True,research_check=checked)
        assert started['schema']=='orbis.grvt.mainnet.bot.grant.v3'
        service.stop(started['id'])
        wait_for(lambda:not service.snapshot(started['id'])['active'])
        before=read_bot_grant(case.vault,started['id'])
        assert before['research_binding']==checked.binding.exported()
        lab_access.lock(origin.lab.path);paper_perp_access.lock(origin.paper.path)
        backup=case.vault.backup(tmp_path/'research-grant-copy.db')
        copied=RealVault(backup);copied.unlock(PASSWORD)
        try:assert read_bot_grant(copied,started['id'])==before
        finally:copied.lock()
        assert not case.sent
    finally:service.stop_all();case.vault.lock()


def test_preview_requires_a_reserve_and_candidates_require_pinned_observed_paper(tmp_path):
    with api_case(tmp_path) as (case,origin,source,emit,settings,api,headers):
        body=preview(api,headers,source)
        payload={**body['review']['policy'],'permission_revision':body['permission_revision'],
                 'session_id':source['session_id'],'keep_operating':True}
        assert api.post(ROOT+'/bots/preview',headers=headers,json=payload).status_code==422
        payload['research_validation_id']=str(uuid4())
        assert api.post(ROOT+'/bots/preview',headers=headers,json=payload).status_code==409
        with origin.paper.connect() as db:
            db.execute('UPDATE paper_signal_sessions SET source_run_id=NULL WHERE id=?',(source['session_id'],))
        candidates=api.get(ROOT+'/bots/sources',headers=headers).json()['items']
        assert len(candidates)==1 and not candidates[0]['paper_ready'] and candidates[0]['research_options']==[]
        assert not case.sent
