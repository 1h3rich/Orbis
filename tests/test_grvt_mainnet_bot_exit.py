"""La salida MAINNET utiliza fills propios y mercado de producción, no PAPER."""
from decimal import Decimal
import httpx
from tests.test_grvt_mainnet_bot_entry import grant_case,entry_args


def exit_case(directory):
    from app.live_execution.grvt_mainnet_bot_entry import submit_bot_entry_once
    case,origin,source,emit,grant,policy=grant_case(directory);emit()
    group=submit_bot_entry_once(case.vault,origin.paper,origin.lab,grant['id'],**entry_args(case));parent=group['parent_client_order_id']
    entry=case.settings['orders'][parent];stamp=case.clock[0]
    case.settings['histories']['fill_history']=[{'sub_account_id':'123456','instrument':'BTC_USDT_Perp','trade_id':'actual-mainnet-entry',
        'order_id':entry['order_id'],'client_order_id':parent,'event_time':str(stamp),'is_buyer':True,'is_taker':True,
        'size':entry['legs'][0]['size'],'price':'100','fee':'0.02','fee_currency':'USDT','realized_pnl':'0','builder_fee':'0','is_liquidation':False}]
    settings={'mark':'96.9'}
    def public(url,body):
        assert url.startswith('https://market-data.grvt.io/')
        if url.endswith('/kline'):
            start=int(body['start_time'])//1_000_000_000;end=(int(body['end_time'])+1)//1_000_000_000
            rows=[{'instrument':'BTC_USDT_Perp','open_time':str(t*1_000_000_000),'close_time':str((t+3600)*1_000_000_000),
                'open':'100','high':'101','low':'99','close':'100','volume_b':'10'} for t in range(start,end,3600)]
            return httpx.Response(200,json={'result':list(reversed(rows)),'next':''},request=httpx.Request('POST',url))
        reply=case.public(url,body)
        if url.endswith('/ticker'):
            payload=reply.json();payload['result']['mark_price']=settings['mark']
            return httpx.Response(200,json=payload,request=httpx.Request('POST',url))
        return reply
    return case,origin,grant,parent,public,settings


def test_mainnet_exit_uses_actual_fills_and_fixed_mainnet_market_without_sending(tmp_path):
    from app.live_execution.grvt_mainnet_bot_exit import review_mainnet_bot_exit
    case,origin,grant,parent,public,settings=exit_case(tmp_path)
    proof=review_mainnet_bot_exit(case.vault,grant['id'],parent,client_factory=case.factory,public_post=public,
        require_permission=lambda:None,clock_ns=lambda:case.clock[0])
    assert proof['decision']['action']=='CLOSE' and proof['decision']['reason']=='STOP'
    assert proof['decision']['entry_price']=='100' and Decimal(proof['decision']['stop_price'])==Decimal('97')
    assert proof['decision']['trigger_basis']=='MAINNET_MARK' and proof['decision']['execution_authorized'] is False
    assert len(case.sent)==1 and not case.settings['cancel_posts'] and not case.settings['reduction_posts']
    case.vault.lock()


def test_saved_close_decision_replays_offline_and_survives_backup(tmp_path):
    from app.live_execution.grvt_mainnet_bot_exit import review_mainnet_bot_exit,save_bot_exit
    case,origin,grant,parent,public,settings=exit_case(tmp_path)
    proof=review_mainnet_bot_exit(case.vault,grant['id'],parent,client_factory=case.factory,public_post=public,
        require_permission=lambda:None,clock_ns=lambda:case.clock[0])
    assert save_bot_exit(case.vault,proof,require_permission=lambda:None)==proof
    case.vault.backup(tmp_path/'mainnet-close-proof.db');case.vault.lock()


def test_missing_mainnet_mark_or_duplicate_fills_never_authorize_a_software_exit(tmp_path):
    import pytest
    from copy import deepcopy
    from app.live_execution.grvt_mainnet_bot_exit import review_mainnet_bot_exit
    from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError
    case,origin,grant,parent,public,settings=exit_case(tmp_path)
    def missing_mark(url,body):
        result=public(url,body)
        if url.endswith('/ticker'):
            data=result.json();data['result'].pop('mark_price',None)
            return httpx.Response(200,json=data,request=httpx.Request('POST',url))
        return result
    args={'client_factory':case.factory,'require_permission':lambda:None,'clock_ns':lambda:case.clock[0]}
    with pytest.raises(ValueError):review_mainnet_bot_exit(case.vault,grant['id'],parent,public_post=missing_mark,**args)
    case.settings['histories']['fill_history'].append(deepcopy(case.settings['histories']['fill_history'][0]))
    with pytest.raises(GrvtMainnetError,match='Fill ajeno o contradictorio'):review_mainnet_bot_exit(case.vault,grant['id'],parent,public_post=public,**args)
    assert not case.settings['cancel_posts'] and not case.settings['reduction_posts'];case.vault.lock()


def test_backup_rejects_a_changed_exit_decision_even_when_its_hash_is_updated(tmp_path):
    import pytest
    from copy import deepcopy
    from app.live_execution.grvt_mainnet_bot_exit import review_mainnet_bot_exit,save_bot_exit
    from app.live_execution.grvt_mainnet_journal import encode
    from app.portfolio.models import GrvtMainnetBotExit
    from app.portfolio.vault import VaultError
    case,origin,grant,parent,public,settings=exit_case(tmp_path)
    proof=review_mainnet_bot_exit(case.vault,grant['id'],parent,client_factory=case.factory,public_post=public,
        require_permission=lambda:None,clock_ns=lambda:case.clock[0])
    save_bot_exit(case.vault,proof,require_permission=lambda:None)
    changed=deepcopy(proof);changed['decision']['entry_price']='50'
    with case.vault.session() as db:
        row=db.get(GrvtMainnetBotExit,parent);row.payload_json,row.payload_digest=encode(changed);db.commit()
    with pytest.raises(VaultError):case.vault.backup(tmp_path/'changed-exit.db')
    case.vault.lock()
