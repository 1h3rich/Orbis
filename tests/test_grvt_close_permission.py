"""Un permiso HTTP retirado no se hereda de una bóveda operativa MEXC."""

import pytest

from app.live_execution.grvt_testnet_strategy_close import read_strategy_close
from tests.test_grvt_testnet_bracket_emergency import emergency_case
from tests.test_grvt_testnet_strategy_close import close_source
from tests.test_grvt_testnet_signer import KEY,SIGNER


@pytest.mark.parametrize('target',['all','parent'])
def test_revocation_invalidates_a_start_authorized_before_it_was_registered(tmp_path,target):
    from app.live_execution.grvt_strategy_close_service import GrvtStrategyCloseService
    case=emergency_case(tmp_path); request=close_source(case); parent=case.origin['client_order_id']; logins=[]
    def factory(key): logins.append(True); return case.reader
    service=GrvtStrategyCloseService(case.vault,client_factory=factory,public_post=case.public,clock_ns=lambda:case.clock[0])
    ticket=service.permission_ticket(parent); generation=case.vault.session_generation
    if target=='all': service.stop_all()
    else: service.stop(parent)
    # Como al proteger la vista con otro bot operativo, la bóveda conserva
    # su generación: eso no conserva el permiso HTTP de este cierre.
    assert case.vault.session_generation==generation
    with pytest.raises(RuntimeError):
        service.start(case.venue.paper,case.venue.lab,parent,action_request_id=request,max_attempts=2,ttl_seconds=120,
            api_key='fake-close-permission-key',private_key=KEY.hex(),expected_signer=SIGNER,
            expected_generation=generation,expected_ticket=ticket)
    assert not logins and not case.controls['sent'] and read_strategy_close(case.vault,parent) is None
    case.second.lock(); case.vault.lock()
