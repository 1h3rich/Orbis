"""Versioned permissions preserve the historical 15-minute interpretation."""
from datetime import timedelta
import pytest

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_permission import DexPermission
from app.portfolio import dex_bot_journal as bots
from tests.dex_bot_worker_fixture import running_case


@pytest.mark.parametrize('mode',['SESSION','OPERATIONAL'])
@pytest.mark.parametrize('seconds',[1,900,3600,86400])
def test_explicit_duration_roundtrips_without_extending_or_changing_mode(mode,seconds):
    value=DexPermission(mode,seconds)
    assert DexPermission.parse(value.json())==value


@pytest.mark.parametrize('mode,seconds',[('SESSION',True),('SESSION',0),('SESSION',86401),('SESSION',1.5),('other',3600)])
def test_invalid_permissions_are_rejected(mode,seconds):
    with pytest.raises(DexExecutorError):DexPermission(mode,seconds).json()


def test_new_permission_support_does_not_reinterpret_or_extend_v1_archives(tmp_path):
    from app.portfolio.models import RealDexBotRun
    from app.portfolio.wallet_funding import _decoded,_encoded
    with running_case(tmp_path) as (values,started,claim,connection,clock,mine,market):
        vault=values[0]
        with vault.session() as db:
            row=db.get(RealDexBotRun,started['id']);terms=_decoded(row.payload_json,row.payload_digest)
            assert terms['schema']==bots.ENGINE and 'permission' not in terms
            terms['expires_at']=(row.created_at+timedelta(seconds=901)).isoformat()
            row.payload_json,row.payload_digest=_encoded(terms)
            state=_decoded(row.state_json,row.state_digest);state['head']['digest']=row.payload_digest
            row.state_json,row.state_digest=_encoded(state);db.commit()
        with pytest.raises(DexExecutorError):
            with vault.session() as db:bots.run_record(db,started['id'])
