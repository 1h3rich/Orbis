"""El avance artificial de mercado no puede partir una lectura del bot REAL."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event,current_thread
import time

from fastapi.testclient import TestClient

from tests.mexc_roundtrip_fixture import Roundtrip
from tests.test_mexc_bot_api import authorize,activate


def test_roundtrip_advances_time_between_cycles_not_inside_an_exchange_read(tmp_path,monkeypatch):
    scenario=Roundtrip(tmp_path,monkeypatch); armed,entered,release,requested=Event(),Event(),Event(),Event()
    original=scenario.quote_get
    def slow_quote(symbol):
        result=original(symbol)
        if armed.is_set() and current_thread().name.startswith('orbis-mexc-bot-'):
            armed.clear();entered.set();assert release.wait(5)
        return result
    scenario.venue.quote_get=slow_quote
    with TestClient(scenario.make_app()) as client:
        headers=authorize(client)
        response,url,body=activate(client,scenario.env,headers)
        assert response.status_code==200,response.text
        worker=scenario.workers[0]
        until=time.monotonic()+5
        while not worker.state()['cycles']:
            assert time.monotonic()<until
            time.sleep(.01)
        armed.set();assert entered.wait(5)
        before=scenario.clock.offset
        def advance():
            requested.set()
            return scenario.advance()
        with ThreadPoolExecutor(max_workers=1) as executor:
            advanced=executor.submit(advance);assert requested.wait(2)
            try:
                until=time.monotonic()+.2
                while time.monotonic()<until:
                    assert scenario.clock.offset==before,'El reloj cambió con una lectura en curso'
                    time.sleep(.01)
            finally: release.set()
            assert advanced.result(timeout=5)['poll_result']=='OBSERVED'
        until=time.monotonic()+8
        while True:
            state=client.get(url,headers=headers).json()[0]
            if state['position']['fill_count']==1: break
            assert state['execution_enabled'] and time.monotonic()<until,state
            time.sleep(.03)
        assert len(scenario.env['trader'].posts)==1
