"""Permiso efímero → stream TESTNET → salida sobre fills → recibo final."""

from decimal import Decimal
import time
import pytest

from app.derivatives.paper_math import PerpQuote
from tests.test_grvt_filled_strategy_close import filled_case
from tests.test_grvt_testnet_signer import KEY,SIGNER


def test_watch_waits_for_a_stream_trigger_then_finishes_the_close_without_more_clicks(tmp_path):
    from app.live_execution.grvt_strategy_close_service import GrvtStrategyCloseService
    from app.derivatives.paper_access import paper_perp_access
    from app.lab.access import lab_access
    venue,reader,public,clock,state=filled_case(tmp_path); marks=iter(['100','108.1']); received=[]
    class Stream:
        def __enter__(self): return self
        def __exit__(self,*args): received.append('closed')
        def recv(self,*,timeout):
            clock[0]+=1_000_000_000; mark=next(marks); received.append(mark)
            return ('BTC/USDT:PERP',PerpQuote(venue.cap.instrument,clock[0],Decimal(mark),Decimal('99.99'),Decimal('100.01')))
    def stream(symbol,clock_ns):
        assert symbol=='BTC/USDT:PERP'; return Stream()
    logins=[]
    def factory(key): logins.append(True); return venue.factory(key)
    def wait(stop,seconds): clock[0]+=6_000_000_000; return stop.is_set()
    service=GrvtStrategyCloseService(venue.vault,client_factory=factory,public_post=public,
        clock_ns=lambda:clock[0],waiter=wait,stream_factory=stream)
    paper_perp_access.lock(venue.origin.paper.path); lab_access.lock(venue.origin.lab.path)
    original_deadline=clock[0]+120_000_000_000
    started=service.start_watch(venue.source['client_order_id'],max_attempts=2,ttl_seconds=120,
        api_key='fake-testnet-key-'+venue.cap.sub_account_id,private_key=KEY.hex(),expected_signer=SIGNER)
    assert started['run']['watch']['deadline_ns']==original_deadline
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        result=service.snapshot(venue.source['client_order_id'])
        if not result['run']['active']: break
        time.sleep(.01)
    assert result['run']['status']=='COMPLETE',result
    assert result['request']['schema']=='orbis.grvt.testnet.strategy.close.v2'
    assert result['request']['exit_reason']=='TARGET'
    assert received==['100','108.1','closed']
    assert venue.cancel_sends==venue.close_sends==len(logins)==1 and venue.position==0
    assert len(str(result))<5000
    for secret in (KEY.hex(),SIGNER,'fake-testnet-key-'+venue.cap.sub_account_id): assert secret not in str(result)
    copy=tmp_path/'completed-watch.db'; venue.vault.backup(copy)
    from app.portfolio.vault import RealVault
    from tests.test_grvt_testnet_bracket_submit import PASSWORD
    recovered=RealVault(copy); recovered.unlock(PASSWORD)
    def forbidden(*args): raise AssertionError('Consultar la copia no conecta GRVT')
    restarted=GrvtStrategyCloseService(recovered,client_factory=forbidden,public_post=forbidden)
    saved=restarted.snapshot(venue.source['client_order_id'])
    assert saved['run']['status']=='COMPLETE' and saved['run']['active'] is False
    assert saved['run']['permission_remaining_seconds']==0 and saved['run']['permission_revision'] is None
    recovered.lock()
    venue.vault.lock()


def test_inherited_deadline_is_not_extended_by_the_handoff_to_the_exit_worker(tmp_path):
    from app.live_execution.grvt_strategy_close_service import GrvtStrategyCloseService
    venue,reader,public,clock,state=filled_case(tmp_path)
    source=venue.source['client_order_id']; began=clock[0]; planned=began+120_000_000_000
    entered=[]
    def factory(key):
        entered.append(key.startswith('fake-testnet-key-'))
        return venue.factory(key)
    # El despacho consumió cinco segundos; el nuevo worker conserva el fin
    # absoluto de la concesión en vez de volver a sumar 120 s.
    clock[0]+=5_000_000_000
    service=GrvtStrategyCloseService(venue.vault,client_factory=factory,public_post=public,clock_ns=lambda:clock[0])
    service._run=lambda *args:None  # Sólo se verifica creación/archivo, no se finge una operación.
    result=service.start_watch(source,max_attempts=2,ttl_seconds=120,deadline_ns=planned,
        api_key='fake-testnet-key-'+venue.cap.sub_account_id,private_key=KEY.hex(),expected_signer=SIGNER)
    assert result['run']['watch']['deadline_ns']==planned
    assert result['run']['permission_remaining_seconds']==115 and not entered
    from app.live_execution.grvt_exit_watch_archive import exit_watch_history
    assert exit_watch_history(venue.vault,source)['items'][0]['deadline_ns']==planned
    service.stop(source); venue.vault.lock()


@pytest.mark.parametrize('offset',[0,121_000_000_000])
def test_inherited_deadline_cannot_be_expired_or_exceed_the_explicit_budget(tmp_path,offset):
    from app.live_execution.grvt_strategy_close_service import GrvtStrategyCloseService
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    venue,reader,public,clock,state=filled_case(tmp_path)
    def forbidden(key): raise AssertionError('No se autentica con un plazo inválido')
    service=GrvtStrategyCloseService(venue.vault,client_factory=forbidden,public_post=public,clock_ns=lambda:clock[0])
    with pytest.raises(GrvtTestnetBracketError):
        service.start_watch(venue.source['client_order_id'],max_attempts=2,ttl_seconds=120,deadline_ns=clock[0]+offset,
            api_key='fake-testnet-key-'+venue.cap.sub_account_id,private_key=KEY.hex(),expected_signer=SIGNER)
    venue.vault.lock()
