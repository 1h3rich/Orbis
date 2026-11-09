"""The diagnostic reads through the real Testnet transport with owned fixtures."""
from copy import deepcopy
import json
from types import SimpleNamespace

import httpx
import pytest

from app.live_execution.grvt_testnet_bracket import bracket_client_ids
from app.live_execution.grvt_testnet_market_probe import MarketProbeError, capture_market_probe
from app.live_execution.grvt_testnet_order import (
    AUTH_URL, FILL_HISTORY_URL, LOOKUP_URL, GrvtTestnetOrderClient,
)
from scripts import capture_grvt_market_probe


NOW = 1_790_000_005_000_000_000
SUB = '123456'
PARENT = str(2**63+8001)
REDUCTION = str(2**63+8002)


def _venue(direction='LONG'):
    roles = ('ENTRY','STOP_LOSS','TAKE_PROFIT','REDUCTION')
    ids = (*bracket_client_ids(PARENT),REDUCTION)
    side = direction == 'LONG'
    calls = []
    credentials = 'synthetic-view-secret'
    account_address = '0x'+'2'*40
    session = 'owned-testnet-cookie'

    def order(role, cid):
        metadata = {'client_order_id':cid,'create_time':str(NOW-1_000_000)}
        if role in roles[1:3]:
            metadata['trigger'] = {'trigger_type':role,'tpsl':{
                'trigger_by':'LAST','trigger_price':'95' if role=='STOP_LOSS' else '110',
                'close_position':False,'is_split_position':False}}
        return {'sub_account_id':SUB,'order_id':f'0xOwned{role.replace("_","")}',
                'metadata':metadata,'signature':{'r':'unused-signature','signer':account_address},
                'time_in_force':'FILL_OR_KILL' if role=='ENTRY' else 'IMMEDIATE_OR_CANCEL' if role=='REDUCTION' else 'GOOD_TILL_TIME',
                'is_market':False,'post_only':False,'reduce_only':role!='ENTRY',
                'legs':[{'instrument':'BTC_USDT_Perp','size':'0.2' if role=='REDUCTION' else '0.5',
                         'limit_price':'100','is_buying_asset':side if role=='ENTRY' else not side}],
                'state':{'status':'FILLED' if role=='ENTRY' else 'CANCELLED' if role=='REDUCTION' else 'PENDING',
                         'book_size':['0' if role in ('ENTRY','REDUCTION') else '0.5'],
                         'traded_size':['0.5' if role=='ENTRY' else '0'],'update_time':str(NOW-10),
                         'reject_reason':'IOC_CANCEL' if role=='REDUCTION' else 'UNSPECIFIED'}}

    orders = {role:order(role,cid) for role,cid in zip(roles,ids)}
    def fill(role, size):
        original=orders[role]
        return {'sub_account_id':SUB,'instrument':'BTC_USDT_Perp','trade_id':role,
                'order_id':original['order_id'],'client_order_id':original['metadata']['client_order_id'],
                'event_time':str(NOW-(30 if role=='ENTRY' else 20)),
                'is_buyer':original['legs'][0]['is_buying_asset'],'is_taker':True,
                'size':size,'price':'100','fee':'0.01','fee_currency':'USDT','realized_pnl':'0',
                'builder_fee':'0','is_liquidation':False}

    def position(size):
        return [{'sub_account_id':SUB,'instrument':'BTC_USDT_Perp',
                 'size':size if side else '-'+size}] if size != '0' else []

    account = {'sub_account_id':SUB,'event_time':str(NOW),'settle_currency':'USDT',
               'margin_type':'SIMPLE_CROSS_MARGIN','sub_account_mode':'SINGLE_ASSET_MODE',
               'positions':position('0.5'),'spot_balances':[{'currency':'USDT','balance':'9999.99'}],
               'total_equity':'9999.99','initial_margin':'0','maintenance_margin':'0','unrealized_pnl':'0'}
    venue = SimpleNamespace(orders=orders,account=account,position=position,fill=fill,open=[],
        fills=[fill('ENTRY','0.5')],calls=calls,hook=lambda *_:None,creds=(credentials,account_address,session))

    def post(url, body, headers):
        calls.append((url,deepcopy(body)))
        assert url in (AUTH_URL,LOOKUP_URL,FILL_HISTORY_URL,
            'https://trades.testnet.grvt.io/full/v1/account_summary','https://trades.testnet.grvt.io/full/v1/open_orders')
        venue.hook(url,body)
        if url == AUTH_URL:
            assert body == {'api_key':credentials}
            return httpx.Response(200,json={'status':'success','sub_account_id':SUB},
                headers={'set-cookie':f'gravity={session}; Path=/','x-grvt-account-id':account_address},
                request=httpx.Request('POST',url))
        assert headers['Cookie'] == f'gravity={session}'
        assert body['sub_account_id'] == SUB
        if url == LOOKUP_URL:
            selector = 'order_id' if 'order_id' in body else 'client_order_id'
            assert set(body) == {'sub_account_id',selector}
            result = next(row for row in orders.values()
                if (row['order_id'] if selector=='order_id' else row['metadata']['client_order_id']) == body[selector])
            return httpx.Response(200,json={'result':result})
        if url == FILL_HISTORY_URL:
            assert body['end_time'] == account['event_time']
            return httpx.Response(200,json={'result':venue.fills,'next':''})
        return httpx.Response(200,json={'result':venue.open if url.endswith('/open_orders') else account})

    venue.client = GrvtTestnetOrderClient(credentials,http_post=post,clock_ns=lambda:NOW)
    return venue


def _capture(venue, **kwargs):
    return capture_market_probe(venue.client,SUB,PARENT,clock_ns=lambda:NOW,**kwargs)


@pytest.mark.parametrize('direction',['LONG','SHORT'])
@pytest.mark.parametrize('selector',['client','exchange'])
def test_capture_fok_and_terminal_ioc_separates_order_size_book_size_and_open_membership(direction,selector):
    venue = _venue(direction)
    before = _capture(venue)
    assert before['stop_order_size'] == before['stop_book_size'] == '0.5'
    assert before['remaining_size'] == '0.5' and before['open_order_roles'] == []
    assert before['ioc_terminal'] is None
    venue.orders['REDUCTION']['state']['traded_size'] = ['0.1']
    stop = venue.orders['STOP_LOSS']
    stop['state'].update(status='OPEN',book_size=['0.4'])
    venue.open.append(stop)
    venue.fills.append(venue.fill('REDUCTION','0.1'))
    venue.account['positions'] = venue.position('0.4')
    venue.account['spot_balances'][0]['balance'] = venue.account['total_equity'] = '9999.98'
    selector_arg = {'ioc_client_id':REDUCTION} if selector=='client' else {'ioc_order_id':venue.orders['REDUCTION']['order_id']}
    after = _capture(venue,**selector_arg)
    assert after['network']=='TESTNET' and after['authorization'] is False
    assert after['ioc_terminal'] is True and after['orders'][3]['reject_reason']=='IOC_CANCEL'
    assert after['remaining_size'] == after['stop_book_size'] == '0.4'
    assert after['stop_order_size'] == '0.5' and after['open_order_roles'] == ['STOP_LOSS']
    assert [row['role'] for row in after['fills']]==['ENTRY','REDUCTION']
    assert all(secret not in json.dumps(after) for secret in (*venue.creds,'unused-signature'))


@pytest.mark.parametrize('fault',['race','unrelated_fill','wrong_open','stale_account','position','foreign_open','absent_ioc','equity','order_after_account'])
def test_capture_refuses_uncorrelated_evidence(fault):
    venue = _venue()
    selector = {}
    if fault == 'race':
        def hook(url, body):
            if url == FILL_HISTORY_URL:
                venue.orders['STOP_LOSS']['state'].update(status='CANCELLED',book_size=['0'])
        venue.hook = hook
    if fault == 'unrelated_fill':
        venue.fills.append(venue.fill('REDUCTION','0.1'))
    if fault == 'wrong_open':
        wrong = deepcopy(venue.orders['STOP_LOSS'])
        wrong['state']['book_size'] = ['0.4']
        venue.open.append(wrong)
    if fault == 'stale_account':
        venue.account['event_time'] = str(NOW-20_000_000_000)
    if fault == 'order_after_account':
        venue.account['event_time'] = str(NOW-11)
    if fault == 'equity':
        venue.account['total_equity'] = '10000'
    if fault == 'position':
        venue.account['positions'] = venue.position('0.4')
    if fault == 'foreign_open':
        venue.open.append(venue.orders['REDUCTION'])
    if fault == 'absent_ioc':
        selector = {'ioc_order_id':'0xunknown'}
    with pytest.raises(MarketProbeError, match='no se pudo conciliar'):
        _capture(venue,**selector)


def test_capture_script_preserves_previous_output_and_keeps_new_report_private(tmp_path, monkeypatch):
    venue = _venue()
    out = tmp_path/'capture.json'
    args = ['--sub-account',SUB,'--parent',PARENT,'--out',str(out)]
    monkeypatch.setattr(capture_grvt_market_probe.sys.stdin,'isatty',lambda:True)
    monkeypatch.setattr(capture_grvt_market_probe,'getpass',lambda _:venue.creds[0])
    monkeypatch.setattr(capture_grvt_market_probe,'GrvtTestnetOrderClient',lambda _:venue.client)
    monkeypatch.setattr(capture_grvt_market_probe,'capture_market_probe',
        lambda client,sub,parent,**kwargs:capture_market_probe(client,sub,parent,clock_ns=lambda:NOW,**kwargs))
    assert capture_grvt_market_probe.main(args)==0
    before = out.read_bytes()
    assert json.loads(before)['network']=='TESTNET' and out.stat().st_mode & 0o777==0o600
    calls = len(venue.calls)
    assert capture_grvt_market_probe.main(args)==1
    assert len(venue.calls)==calls and out.read_bytes()==before
