"""Renovación subordinada: nunca reinicia posición, presupuesto ni concesión."""

from fastapi.testclient import TestClient
import json
import pytest

from app.live_execution.spot_bot_continuity import renew_bot_policy
from app.live_execution.spot_bot_market import observe_bot_market
from app.live_execution.spot_bot_store import (claim_spot_bot_cycle,assert_spot_bot_cycle,
    finish_spot_bot_cycle,owned_spot_bot_ledger,bot_record,bot_authorizations,encoded)
from app.portfolio.models import RealSpotBotMarketStream
from app.portfolio.vault import VaultError
from app.live_execution.spot_bot_store import SpotBotStoreError
from app.live_execution.mexc_execution_state import MexcExecutionStateError
from app.live_execution.mexc_continuity_review import MexcContinuityError
from tests.mexc_continuity_fixture import make_continuity_environment,observe_hour
from tests.test_spot_continuity_api import continuous
from tests.test_spot_bot_permission import scenario
from tests.test_mexc_bot_api import authorize
from tests.mexc_bot_fixture import advance_market


@pytest.mark.parametrize("alter_seed",[False,True])
def test_refresh_preserves_owned_position_and_immutable_grant(tmp_path,monkeypatch,alter_seed):
    env,workers,app = scenario(tmp_path,monkeypatch)
    with TestClient(app) as client:
        headers = authorize(client); created,_,body = continuous(client,env,headers)
        assert created.status_code == 200,created.text
        worker,identifier = workers[0],body["request_id"]
        grant = created.json()["execution_permission"]; deadline = worker._deadline
        advance_market(env); assert worker.run_once()["action"] == "BUY"
        ledger,revision = owned_spot_bot_ledger(worker.vault,identifier)
        claim,_,_ = claim_spot_bot_cycle(worker.vault,identifier,now=env["clock"](),expected_policy_id=created.json()["policy_id"])
        def active(db=None):
            worker._authorized(); assert_spot_bot_cycle(worker.vault,identifier,claim,db=db)
        source = observe_bot_market(worker.vault,identifier,claim,candles=env["paper"].adapter_factory().get_candles,
            clock=env["clock"],assert_active=active)
        child,config,state,new_source = renew_bot_policy(worker.vault,env["venue"],identifier,claim,source,grant,
            clock=env["clock"],quote_get=env["venue"].quote_get,rules_fetch=env["venue"].rules_fetch,
            assert_active=active,assert_active_in_transaction=active)
        assert child.policy_id != created.json()["policy_id"]
        assert config["execution_permission"] == grant and worker._deadline == deadline
        assert owned_spot_bot_ledger(worker.vault,identifier) == (ledger,revision)
        assert new_source.candle_t == source.candle_t and state["last_decision_id"]
        finish_spot_bot_cycle(worker.vault,identifier,claim,state,now=env["clock"]())
        with worker.vault.session() as db:
            row,config,state = bot_record(db,identifier)
            assert len(bot_authorizations(config)) == 2 and state["continuity_head"]["policy_id"] == child.policy_id
            if alter_seed:
                stream = db.get(RealSpotBotMarketStream,child.policy_id)
                seed = json.loads(stream.seed_json)
                seed["candles"][-1]["h"] = "101"
                stream.seed_json,stream.seed_digest = encoded(seed)
                config["market_stream_seeds"][child.policy_id] = stream.seed_digest
                state["market_heads"][child.policy_id]["digest"] = stream.seed_digest
                row.config_json,row.config_digest = encoded(config)
                row.state_json,row.state_digest = encoded(state); db.commit()
        if alter_seed:
            with pytest.raises(VaultError,match="market evidence"): worker.vault.backup(tmp_path/"renewed-position.db")
        else: worker.vault.backup(tmp_path/"renewed-position.db")
        assert len(env["trader"].posts) == 1


@pytest.mark.parametrize("point",["during_review","after_review"])
def test_stop_during_renewal_never_publishes_a_daughter_policy(tmp_path,monkeypatch,point):
    from app.live_execution import spot_bot_continuity
    env,workers,app = make_continuity_environment(tmp_path,monkeypatch)
    with TestClient(app) as client:
        headers = authorize(client); created,_,body = continuous(client,env,headers)
        assert created.status_code == 200,created.text
        worker = workers[0]
        for hour in range(1,13): assert observe_hour(env,worker,hour)["action"] == "WAIT"
        if point == "during_review": env["venue"].on_window = worker.request_stop
        else:
            original = spot_bot_continuity.record_continuity_review
            def stop_after(*args,**kwargs):
                result = original(*args,**kwargs)
                worker.request_stop()
                return result
            monkeypatch.setattr(spot_bot_continuity,"record_continuity_review",stop_after)
        with pytest.raises((SpotBotStoreError,MexcExecutionStateError,MexcContinuityError)):
            observe_hour(env,worker,13)
        with worker.vault.session() as db:
            row,config,_ = bot_record(db,body["request_id"])
            assert row.policy_id == created.json()["policy_id"] and row.status == "PAUSED"
            assert len(bot_authorizations(config)) == 1
        assert not env["trader"].posts


@pytest.mark.parametrize("field",["entry_budget_quote","expires_at","source"])
def test_rehashed_grant_or_active_scope_cannot_expand_runtime_authority(tmp_path,monkeypatch,field):
    env,workers,app = scenario(tmp_path,monkeypatch)
    with TestClient(app) as client:
        headers = authorize(client); created,_,body = continuous(client,env,headers)
        assert created.status_code == 200,created.text
        worker = workers[0]
        with worker.vault.session() as db:
            row,config,_ = bot_record(db,body["request_id"])
            if field == "entry_budget_quote": config[field] = "21"
            elif field == "expires_at": config["execution_permission"][field] = "2099-01-01T00:00:00+00:00"
            else: config[field]["dataset_hash"] = "0"*64
            row.config_json,row.config_digest = encoded(config); db.commit()
        with pytest.raises((SpotBotStoreError,MexcExecutionStateError)): worker.run_once()
        with pytest.raises(VaultError): worker.vault.backup(tmp_path/"expanded-grant.db")
        assert not env["trader"].posts
