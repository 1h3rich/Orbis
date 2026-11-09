"""Entrada OTOCO real del motor → revisión de salidas con venue ficticio."""

from contextlib import contextmanager
from decimal import Decimal
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.live_execution.grvt_testnet_bracket_submit import submit_bracket_signal_once
from app.portfolio.api import create_real_router
from tests.grvt_bracket_ui_fixture import BracketUiVenue
from tests.test_grvt_testnet_bracket_submit import PASSWORD


ROOT='/real/exchanges/grvt/testnet/brackets'


@contextmanager
def review_case(directory,direction='LONG'):
    venue=BracketUiVenue(directory,direction)
    client=venue.factory('fake-testnet-key-'+venue.cap.sub_account_id)
    submit_bracket_signal_once(venue.origin.paper,venue.origin.lab,venue.vault,client,venue.initial_orders,
        venue.cap,venue.cap,venue.instrument,session_id=venue.source['session_id'],
        action_request_id=venue.source['action_request_id'],public_post=venue.public)
    venue.fills[0]['price']='99' if direction=='LONG' else '101'
    stamp=time.time_ns()+1_000_000_000; venue.clock_ns=lambda:stamp
    public_calls=[]
    def public(url,body):
        public_calls.append(url)
        if url.endswith('/ticker'):
            return venue.origin.response(url,{'result':{'instrument':venue.cap.instrument,'event_time':str(stamp),
                'mark_price':'96.9' if direction=='LONG' else '103.1'}})
        assert url=='https://market-data.testnet.grvt.io/full/v1/kline'
        start=int(body['start_time'])//1_000_000_000; end=(int(body['end_time'])+1)//1_000_000_000
        rows=[{'instrument':venue.cap.instrument,'open_time':str(t*1_000_000_000),
            'close_time':str((t+3600)*1_000_000_000),'open':'100','high':'101',
            'low':'99','close':'100','volume_b':'10'} for t in range(start,end,3600)]
        return venue.origin.response(url,{'result':list(reversed(rows)),'next':''})
    yield venue,client,public,public_calls
    venue.vault.lock()


@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_review_uses_the_sealed_strategy_and_actual_fills_without_paper_or_lab_open(tmp_path,direction):
    from app.live_execution.grvt_bracket_exit_review import review_bracket_strategy_exit
    from app.derivatives.paper_access import paper_perp_access
    from app.lab.access import lab_access
    with review_case(tmp_path,direction) as (venue,client,public,calls):
        paper_perp_access.lock(venue.origin.paper.path); lab_access.lock(venue.origin.lab.path)
        result=review_bracket_strategy_exit(venue.vault,client,venue.source['client_order_id'],
            public_post=public,clock_ns=venue.clock_ns)
        assert result['decision']['action']=='HOLD'
        assert result['decision']['entry_price']==Decimal('99' if direction=='LONG' else '101')
        assert result['review_only'] is True and result['authorization'] is False
        assert len(result['strategy_digest'])==len(result['market_history_digest'])==64
        assert venue.cancel_sends==venue.close_sends==0 and venue.bulk_sends==1
        assert all('market-data.testnet.grvt.io' in url for url in calls)


def test_review_endpoint_is_private_strict_and_never_signs_or_sends_an_order(tmp_path):
    with review_case(tmp_path) as (venue,reader,public,calls):
        app=FastAPI(); logins=[]
        def factory(key): logins.append(True); return venue.factory(key)
        app.include_router(create_real_router(tmp_path/'real.db',grvt_testnet_factory=factory,
            grvt_testnet_bracket_public_post=public,grvt_testnet_bracket_clock_ns=venue.clock_ns))
        path=ROOT+'/'+venue.source['client_order_id']+'/strategy-exit/review'
        body={'api_key':'fake-testnet-key-'+venue.cap.sub_account_id}
        with TestClient(app,base_url='http://127.0.0.1') as api:
            assert api.post(path,json=body).status_code==401
            token=api.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']
            headers={'X-Orbis-Vault-Token':token}
            assert api.post(path,headers={**headers,'Origin':'https://foreign.example'},json=body).status_code==403
            assert api.post(path,headers=headers,json={**body,'size':'9'}).status_code==422
            assert not logins and not calls
            response=api.post(path,headers=headers,json=body)
            assert response.status_code==200,response.text
            result=response.json()
            assert result['decision']['entry_price']=='99' and result['decision']['stop_price']=='96.03'
            assert isinstance(result['decision']['last_fill_at_ns'],str)
            assert result['authorization'] is False and result['decision']['execution_authorized'] is False
            assert response.headers['cache-control']=='no-store'
            assert body['api_key'] not in response.text
            assert venue.cancel_sends==venue.close_sends==0 and venue.bulk_sends==1


def test_a_native_exit_during_market_review_invalidates_the_position_basis(tmp_path):
    from app.live_execution.grvt_bracket_exit_review import review_bracket_strategy_exit
    from app.live_execution.grvt_filled_strategy_exit import GrvtStrategyExitError
    with review_case(tmp_path) as (venue,client,public,calls):
        def racing_public(url,body):
            if url.endswith('/kline'): venue.trigger()
            return public(url,body)
        with pytest.raises(GrvtStrategyExitError):
            review_bracket_strategy_exit(venue.vault,client,venue.source['client_order_id'],
                public_post=racing_public,clock_ns=venue.clock_ns)
        assert venue.cancel_sends==venue.close_sends==0
