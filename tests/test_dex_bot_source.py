"""A DEX preparation reproduces LAB/PAPER without importing fictitious funds."""
from app.live_execution.dex_bot_source import DexBotSources
from tests.dex_bot_source_fixture import SourceCase


def test_bnb_long_preparation_pins_tested_version_and_separates_real_capital(tmp_path):
    case=SourceCase(tmp_path)
    try:
        sources=DexBotSources(case.paper)
        preview=sources.preview(kind='STRATEGY',session_id=case.session['id'],entry_usdt='12.000000000000000001',
            budget_usdt='60',exposure_usdt='25',loss_usdt='5',wbnb_quota='1',monthly_usdt=None,now=case.now)
        assert preview['source']['version_id']==case.version['version_id']
        assert preview['source']['source_run_id']==case.run['id']
        assert preview['source']['symbol']=='BNB/USDT'
        assert preview['signal_market']=='MEXC · BNB/USDT'
        assert preview['execution_market']=='Uniswap v3 · USDT/WBNB · BNB Chain'
        assert preview['amounts']['entry_units']=='12000000000000000001'
        assert preview['amounts']['budget_units']=='60000000000000000000'
        assert preview['preparation_only'] is True and preview['automatic_execution'] is False
        assert not any(key in preview for key in ('cash','units','paper_position','private_key','address'))
        assert len(preview['preparation_digest'])==64
    finally:case.close()


def test_preparation_rejects_another_asset_and_missing_backtest(tmp_path):
    import pytest
    from app.live_execution.dex_artifact import DexExecutorError
    for symbol,tested in [('BTC/USDT',True),('BNB/USDT',False)]:
        directory=tmp_path/('tested' if tested else 'untested');directory.mkdir()
        case=SourceCase(directory,symbol=symbol,tested=tested)
        try:
            sources=DexBotSources(case.paper)
            rows=sources.candidates(now=case.now)
            assert len(rows)==1 and rows[0]['ready'] is False
            with pytest.raises(ValueError):sources.observation('STRATEGY',case.session['id'],now=case.now)
        finally:case.close()


def test_preparation_rejects_inexact_or_overspent_capital_and_stale_paper(tmp_path):
    from datetime import timedelta
    from decimal import localcontext
    import pytest
    from app.live_execution.dex_artifact import DexExecutorError
    case=SourceCase(tmp_path)
    try:
        sources=DexBotSources(case.paper)
        body={'kind':'STRATEGY','session_id':case.session['id'],'entry_usdt':'12','budget_usdt':'60',
              'exposure_usdt':'25','loss_usdt':'5','wbnb_quota':'1','monthly_usdt':None,'now':case.now}
        for change in ({'entry_usdt':'0.0000000000000000001'},{'entry_usdt':12.0},{'entry_usdt':'NaN'},
                       {'entry_usdt':'Infinity'},{'entry_usdt':'1001'},{'budget_usdt':'11'},{'exposure_usdt':'10'},
                       {'wbnb_quota':'0'},{'loss_usdt':'-1'},{'monthly_usdt':'1'},{'now':case.now+timedelta(hours=1)}):
            with pytest.raises(DexExecutorError):sources.preview(**{**body,**change})
        with localcontext() as context:
            context.prec=6
            preview=sources.preview(**{**body,'entry_usdt':'12.000000000000000001'})
            assert preview['amounts']['entry_units']=='12000000000000000001'
    finally:case.close()


def test_preparation_requires_encrypted_unlocked_source_and_detects_changed_results(tmp_path):
    import json,pytest
    from app.automation.spot_paper_access import spot_paper_access
    from app.live_execution.dex_artifact import DexExecutorError
    from tests.dex_bot_source_fixture import PASSWORD
    case=SourceCase(tmp_path)
    sources=DexBotSources(case.paper)
    case.close()
    with pytest.raises(DexExecutorError):sources.candidates(now=case.now)
    spot_paper_access.unlock(case.paper.path,PASSWORD)
    with case.lab.connect() as db:
        saved=json.loads(db.execute('SELECT result_json FROM runs WHERE id=?',(case.run['id'],)).fetchone()[0])
        saved['metrics']['net_profit']='999999'
        db.execute('UPDATE runs SET result_json=? WHERE id=?',(json.dumps(saved),case.run['id']))
    try:
        assert sources.candidates(now=case.now)[0]['ready'] is False
        with pytest.raises(ValueError):sources.observation('STRATEGY',case.session['id'],now=case.now)
    finally:case.close()


def test_dca_preparation_keeps_native_rules_with_explicit_real_limits_and_no_sell_permission(tmp_path):
    import pytest
    from tests.dex_bot_source_fixture import DcaSourceCase
    from app.live_execution.dex_artifact import DexExecutorError
    case=DcaSourceCase(tmp_path,research=True)
    try:
        sources=DexBotSources(dca_paper=case.paper)
        rows=sources.candidates(now=case.now)
        assert len(rows)==1 and rows[0]['ready'] and rows[0]['source_kind']=='DCA'
        assert sources.candidates(now=case.now,selection=('DCA',case.session['id']))==rows
        body={'kind':'DCA','session_id':case.session['id'],'entry_usdt':'12','budget_usdt':'60','exposure_usdt':'25',
              'loss_usdt':'5','wbnb_quota':'1','monthly_usdt':'40','now':case.now}
        preview=sources.preview(**body)
        assert preview['allow_buy'] is True and preview['allow_sell'] is False
        assert preview['definition']==case.version['definition']
        assert preview['dca']['effective_monthly_units']==str(40*10**18)
        assert preview['dca']['rule_amount_units']==str(100*10**18)
        assert preview['source']['run_hash']==case.run['result_hash']
        assert preview['amounts']['budget_units']!=str(1000*10**18)
        with pytest.raises(DexExecutorError):sources.preview(**{**body,'monthly_usdt':None})
    finally:case.close()


def test_stale_observation_is_visible_as_unready_in_source_picker(tmp_path):
    from datetime import timedelta
    case=SourceCase(tmp_path)
    try:
        candidates=DexBotSources(case.paper).candidates(now=case.now+timedelta(hours=1))
        assert candidates[0]['ready'] is False
        assert 'última vela cerrada' in candidates[0]['reason']
    finally:case.close()
