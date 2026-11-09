"""Un cierre posterior pertenece al grupo del bot, sin reactivar su concesión."""
from tests.test_grvt_mainnet_bot_entry import grant_case,entry_args,close_remote_group


def stopped_then_settled(directory):
    from app.live_execution.grvt_mainnet_bot_entry import submit_bot_entry_once,finish_bot_grant
    from app.live_execution.grvt_mainnet_finance import audit_mainnet_bracket,reconcile_mainnet_bracket
    case,origin,source,emit,grant,policy=grant_case(directory);emit()
    group=submit_bot_entry_once(case.vault,origin.paper,origin.lab,grant['id'],**entry_args(case));parent=group['parent_client_order_id']
    finished=finish_bot_grant(case.vault,grant['id'],status='STOPPED',now_ns=case.clock[0])
    from app.portfolio.models import GrvtMainnetBotRun
    with case.vault.session() as db:
        row=db.get(GrvtMainnetBotRun,grant['id'])
        case.bot_journal_before=(row.payload_json,row.payload_digest,row.event_count,row.head_digest,row.active_slot)
    expected=close_remote_group(case,parent)
    for _ in range(2):
        audit_mainnet_bracket(case.vault,parent,client_factory=case.factory,require_permission=lambda:None,clock_ns=lambda:case.clock[0]);case.clock[0]+=6_000_000_000
    receipt=reconcile_mainnet_bracket(case.vault,parent,now_ns=case.clock[0])
    return case,grant,finished,parent,expected,receipt


def test_closed_group_reports_the_result_after_bot_stop_without_rewriting_its_journal(tmp_path):
    from app.live_execution.grvt_mainnet_bot_entry import read_bot_grant
    from app.live_execution.grvt_mainnet_bot_service import GrvtMainnetBotService
    from app.portfolio.models import GrvtMainnetBotRun
    from tests.test_grvt_mainnet_submit import PASSWORD
    case,grant,finished,parent,expected,receipt=stopped_then_settled(tmp_path)
    with case.vault.session() as db:
        row=db.get(GrvtMainnetBotRun,grant['id']);before=(row.payload_json,row.payload_digest,row.event_count,row.head_digest,row.active_slot)
    assert before==case.bot_journal_before
    saved=read_bot_grant(case.vault,grant['id'])
    assert saved['financial_outcomes'][0]['parent_client_order_id']==parent
    assert saved['financial_outcomes'][0]['net_realized_pnl_usdt']==expected
    assert saved['financial_outcomes'][0]['bot_recorded'] is False
    assert saved['terminal']==finished['terminal']=={'status':'STOPPED'} and saved['selections'][0]['settlement'] is None
    cold=GrvtMainnetBotService(case.vault).snapshot(grant['id'])
    assert not cold['active'] and cold['financial_complete'] and cold['net_realized_pnl_usdt']==expected
    with case.vault.session() as db:
        row=db.get(GrvtMainnetBotRun,grant['id']);assert (row.payload_json,row.payload_digest,row.event_count,row.head_digest,row.active_slot)==before
    case.vault.backup(tmp_path/'later-financial-outcome.db');case.vault.lock();case.vault.unlock(PASSWORD)
    assert read_bot_grant(case.vault,grant['id'])==saved;case.vault.lock()
