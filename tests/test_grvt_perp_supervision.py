"""Durable supervision, actual fills, quote decisions and original-row replay."""
from decimal import Decimal
import json

import pytest

from app.live_execution.grvt_mainnet_bot_entry import read_bot_grant
from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError
from app.live_execution.grvt_perp_bot_grant import append_supervision
from app.live_execution.grvt_perp_supervision import SUPERVISED_SCHEMA
from tests.grvt_perp_supervision_fixture import supervised_case,position_review
from tests.test_grvt_mainnet_submit import PASSWORD


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('mode',['CROSS','ISOLATED'])
def test_real_entry_snapshot_and_explicit_position_are_supervised_without_legacy_mark_exit(tmp_path,monkeypatch,side,mode):
    owned=supervised_case(tmp_path,monkeypatch,direction=side,mode=mode)
    case=owned.case
    try:
        saved=read_bot_grant(case.vault,owned.grant['id'])
        assert saved['schema']==SUPERVISED_SCHEMA
        assert saved['supervision']['entry_state']=='CONFIRMED_OWN_FILLS'
        assert not saved['supervision']['halt_new_entries']
        assert saved['supervision']['exit']['entry_notional']==owned.entry['notional']
        source=position_review(owned)
        result=append_supervision(case.vault,saved['id'],'POSITION_REVIEW',parent=owned.parent,
            position_input=source,require_permission=lambda:None,clock_ns=lambda:case.clock[0])
        assert result['position']['action']=='HOLD' and result['action']=='HOLD'
        assert result['execution_authority']=='NONE' and len(case.sent)==1
        case.vault.lock();case.vault.unlock(PASSWORD)
        assert read_bot_grant(case.vault,saved['id'])['supervision']==result
    finally:case.vault.lock()


@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_market_exit_is_durable_and_sticky_without_assuming_a_fill_at_the_trigger(tmp_path,monkeypatch,side):
    owned=supervised_case(tmp_path,monkeypatch,direction=side)
    case=owned.case
    try:
        owned.adapter.quote_override={'bid':'151' if side=='LONG' else '48',
            'ask':'152' if side=='LONG' else '49','mark_price':'100'}
        owned.record_quote()
        result=read_bot_grant(case.vault,owned.grant['id'])['supervision']
        assert result['action']=='CLOSE' and result['exit']['reason']=='TAKE_PROFIT'
        assert result['exit']['trigger_price']==('151' if side=='LONG' else '49')
        assert not result['halt_new_entries'] and len(case.sent)==1
        owned.adapter.quote_override.clear()
        owned.record_quote()
        assert read_bot_grant(case.vault,owned.grant['id'])['supervision']['exit']==result['exit']
        assert not case.settings['reduction_posts']  # Evidence has no execution authority.
    finally:case.vault.lock()


@pytest.mark.parametrize('fault',['configuration','fee','equity_loss','stale'])
def test_risk_conditions_halt_new_entries_and_preserve_a_pending_close(tmp_path,monkeypatch,fault):
    owned=supervised_case(tmp_path,monkeypatch,fee='1' if fault=='fee' else '0.02')
    case=owned.case
    try:
        if fault=='configuration':case.config['leverage']='4'
        elif fault=='equity_loss':case.settings['account_equity']='-1'
        if fault in ('configuration','equity_loss'):
            source=position_review(owned)
            result=append_supervision(case.vault,owned.grant['id'],'POSITION_REVIEW',parent=owned.parent,
                position_input=source,require_permission=lambda:None,clock_ns=lambda:case.clock[0])
        elif fault=='stale':
            case.clock[0]+=6*10**9
            result=append_supervision(case.vault,owned.grant['id'],'SUPERVISION_TICK',
                require_permission=lambda:None,clock_ns=lambda:case.clock[0])
        else:result=read_bot_grant(case.vault,owned.grant['id'])['supervision']
        assert result['action']=='CLOSE' and result['halt_new_entries']
        assert result['halt_reason'] in ('LEVERAGE_MISMATCH','FEE_CEILING_EXCEEDED','LOSS_LIMIT','DATA_STALE')
        assert len(case.sent)==1 and not case.settings['reduction_posts']
    finally:case.vault.lock()


def test_flat_position_race_requests_recheck_without_pretending_it_is_closed_or_reopening(tmp_path,monkeypatch):
    owned=supervised_case(tmp_path,monkeypatch)
    case=owned.case
    try:
        case.settings['positions']=[]
        source=position_review(owned)
        result=append_supervision(case.vault,owned.grant['id'],'POSITION_REVIEW',parent=owned.parent,
            position_input=source,require_permission=lambda:None,clock_ns=lambda:case.clock[0])
        assert result['action']=='RECHECK' and not result['halt_new_entries']
        assert read_bot_grant(case.vault,owned.grant['id'])['selections'][0]['settlement'] is None
        assert len(case.sent)==1
    finally:case.vault.lock()


def test_changed_original_entry_bytes_invalidate_a_warm_supervision_prefix(tmp_path,monkeypatch):
    from app.portfolio.models import GrvtMainnetEntryFills
    from app.live_execution.grvt_mainnet_journal import encode
    owned=supervised_case(tmp_path,monkeypatch)
    case=owned.case
    try:
        read_bot_grant(case.vault,owned.grant['id'])
        with case.vault.session() as db:
            row=db.get(GrvtMainnetEntryFills,owned.parent)
            value=json.loads(row.payload_json)
            value['fills']['rows'][0]['fee']='0.03'
            row.payload_json,row.payload_digest=encode(value)
            db.commit()
        with pytest.raises(GrvtMainnetError):read_bot_grant(case.vault,owned.grant['id'])
        assert len(case.sent)==1
    finally:case.vault.lock()


@pytest.mark.parametrize('reason',['SOURCE_ERROR','ARCHIVE_ERROR','ARCHIVE_DELAY'])
def test_source_fault_is_durable_single_use_and_never_retries_the_market_write(tmp_path,monkeypatch,reason):
    owned=supervised_case(tmp_path,monkeypatch)
    case=owned.case
    try:
        result=append_supervision(case.vault,owned.grant['id'],'SOURCE_FAULT',source_fault=reason,
            require_permission=lambda:None,clock_ns=lambda:case.clock[0])
        assert result['action']=='CLOSE' and result['halt_reason']==reason and result['halt_new_entries']
        with pytest.raises(GrvtMainnetError):
            append_supervision(case.vault,owned.grant['id'],'SOURCE_FAULT',source_fault=reason,
                require_permission=lambda:None,clock_ns=lambda:case.clock[0])
        case.vault.lock();case.vault.unlock(PASSWORD)
        assert read_bot_grant(case.vault,owned.grant['id'])['supervision']==result
        assert len(case.sent)==1 and not case.settings['reduction_posts']
    finally:case.vault.lock()
