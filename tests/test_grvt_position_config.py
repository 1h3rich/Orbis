"""Documented full response shape, scope, exact decimals and no write path."""
from copy import deepcopy
from decimal import Decimal

import httpx
import pytest

from app.integrations.grvt_position_config import configuration_response, verify_configuration_pair, POSITION_CONFIG_URL
from app.integrations.grvt_readonly import GrvtReadOnlyClient, GrvtReadOnlyError, AUTH_URL

NOW = 1_800_000_000_000_000_000
ROW = {'instrument':'BTC_USDT_Perp','margin_type':'ISOLATED','leverage':'10.000000000000000001',
       'min_leverage':'1','max_leverage':'50'}


def read(raw=None, start=NOW, end=NOW):
    return configuration_response({'results':[deepcopy(ROW)]} if raw is None else raw, '123', 'BTC_USDT_Perp',
                                  requested_at_ns=start, received_at_ns=end)


def test_configuration_is_per_instrument_exact_and_observation_only():
    config = read()
    assert Decimal(config.leverage) == Decimal('10.000000000000000001')
    assert config.margin_type == 'ISOLATED' and config.source == POSITION_CONFIG_URL
    assert config.requested_at_ns == str(NOW) and 'event_time' not in str(config.payload())
    verify_configuration_pair(config, read(start=NOW+1,end=NOW+2), now_ns=NOW+3)


@pytest.mark.parametrize('raw', [
    {}, {'result':[ROW]}, {'results':[]}, {'results':[ROW,ROW]}, {'results':[None]},
    {'results':[ROW],'error':{}}, {'results':[{**ROW,'instrument':'ETH_USDT_Perp'}]},
    *({'results':[{**ROW,'leverage':value}]} for value in ('NaN','1e2','0','10001',10,True,'10.0000000000000000001')),
    {'results':[{**ROW,'margin_type':1}]}, {'results':[{**ROW,'margin_type':'SIMPLE_CROSS_MARGIN'}]},
    {'results':[{**ROW,'min_leverage':'11'}]}, {'results':[{**ROW,'max_leverage':'9'}]},
    {'results':[{key:value for key,value in ROW.items() if key!='margin_type'}]},
])
def test_missing_ambiguous_or_malformed_configuration_never_infers_defaults(raw):
    with pytest.raises(ValueError): read(raw)


@pytest.mark.parametrize('start,end,now', [(NOW,NOW-1,NOW), (NOW,NOW+5_000_000_001,NOW+5_000_000_001),
    (NOW,NOW,NOW-1), (NOW,NOW,NOW+5_000_000_001), (True,NOW,NOW)])
def test_old_slow_or_regressed_observation_is_rejected(start,end,now):
    with pytest.raises(ValueError):
        first=read(start=start,end=end)
        verify_configuration_pair(first,first,now_ns=now)


def test_changed_config_and_cross_account_cannot_be_verified_as_pair():
    first=read()
    for changed in (first.model_copy(update={'margin_type':'CROSS'}),first.model_copy(update={'sub_account_id':'124'}),
                    first.model_copy(update={'max_leverage':'49'}),first.model_copy(update={'leverage':'10'})):
        with pytest.raises(ValueError):verify_configuration_pair(first,changed,now_ns=NOW)


def test_readonly_client_only_reads_scoped_full_endpoint_and_redacts_errors():
    calls=[]
    def post(url,body,headers):
        calls.append((url,body))
        if url==AUTH_URL:
            return httpx.Response(200,request=httpx.Request('POST',url),json={'status':'success','sub_account_id':'123'},
                headers={'set-cookie':'gravity=synthetic-cookie-only; Path=/','x-grvt-account-id':'0x'+'2'*40})
        assert url==POSITION_CONFIG_URL and body=={'sub_account_id':'123'}
        return httpx.Response(200,json={'results':[ROW]})
    client=GrvtReadOnlyClient('fixture-view-only-key',http_post=post,clock_ns=lambda:NOW)
    assert client.position_configuration('123','BTC_USDT_Perp')==read()
    count=len(calls)
    for sub,instrument in [('124','BTC_USDT_Perp'),('00123','BTC_USDT_Perp'),('123','https://evil.test'),('123','BTC_USDT_Perp\n')]:
        with pytest.raises(GrvtReadOnlyError):client.position_configuration(sub,instrument)
    assert len(calls)==count
    for path in ('set_initial_leverage','set_position_config','create_order','withdrawal'):
        with pytest.raises(GrvtReadOnlyError):client._query(path)


def test_private_error_never_echoes_configuration_body_or_credentials():
    client=object.__new__(GrvtReadOnlyClient)
    client._bound_sub='123';client._clock_ns=lambda:NOW
    client._query=lambda *args: (_ for _ in ()).throw(RuntimeError('private-key cookie address raw account'))
    with pytest.raises(GrvtReadOnlyError) as error:client.position_configuration('123','BTC_USDT_Perp')
    assert 'private-key' not in str(error.value) and 'cookie' not in str(error.value)
