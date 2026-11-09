"""Supervised grant with owned native fills and explicit live position fields."""
from decimal import Decimal

from app.live_execution.grvt_mainnet_bot_entry import read_bot_grant
from app.live_execution.grvt_mainnet_journal import encode
from app.live_execution.grvt_perp_bot_grant import append_supervision
from app.live_execution.grvt_perp_entry_fills import collect_perp_entry_fills
from tests.grvt_perp_bot_fixture import perp_bot_case


def supervised_case(directory,monkeypatch,*,direction='LONG',mode='ISOLATED',fee='0.02'):
    owned=perp_bot_case(directory,monkeypatch,direction=direction,mode=mode,supervised=True)
    owned.emit()
    sent=owned.send(read_bot_grant(owned.case.vault,owned.grant['id']))
    case=owned.case
    parent=sent['parent_client_order_id']
    order=case.settings['orders'][parent]
    size=order['legs'][0]['size'];at=case.clock[0]
    case.settings['histories']['fill_history']=[{
        'sub_account_id':'123456','instrument':'BTC_USDT_Perp','trade_id':'supervised-owned-entry',
        'order_id':order['order_id'],'client_order_id':parent,'event_time':str(at),
        'is_buyer':direction=='LONG','is_taker':True,'size':size,'price':'100',
        'fee':fee,'fee_currency':'USDT','realized_pnl':'0','builder_fee':'0','is_liquidation':False}]
    case.settings['account_balance']=case.settings['account_equity']=str(Decimal(100)-Decimal(fee))
    margin=Decimal(size)*20;maintenance=Decimal(size)
    case.settings['initial_margin']=str(margin)
    case.settings['maintenance_margin']=str(maintenance)
    case.settings['positions']=[{'sub_account_id':'123456','instrument':'BTC_USDT_Perp',
        'size':size if direction=='LONG' else '-'+size,'event_time':str(at),
        'margin_type':mode,'leverage':'5','isolated_balance':str(margin) if mode=='ISOLATED' else None,
        'isolated_im':str(margin) if mode=='ISOLATED' else None,'isolated_mm':str(maintenance) if mode=='ISOLATED' else None}]
    entry=collect_perp_entry_fills(case.vault,owned.grant['id'],parent,client_factory=case.factory,
        require_permission=lambda:None,clock_ns=lambda:case.clock[0])
    view=append_supervision(case.vault,owned.grant['id'],'ENTRY_CONFIRMED',parent=parent,
        entry_digest=encode(entry)[1],require_permission=lambda:None,clock_ns=lambda:case.clock[0])
    owned.parent=parent;owned.entry=entry;owned.initial_supervision=view
    return owned


def position_review(owned):
    from app.live_execution.grvt_perp_position import collect_position_review
    from app.live_execution.grvt_mainnet_journal import _control,read_mainnet_bracket
    case=owned.case
    saved=read_mainnet_bracket(case.vault,owned.parent)
    with case.vault.session() as db:salt=_control(db,'123456').identity_salt
    return collect_position_review(case.factory(),owned.terms,identity_salt=salt,
        expected_account_ref=saved['claim']['account_ref'],require_permission=lambda:None,
        clock_ns=lambda:case.clock[0])
