"""Opt-in continuo acotado, nunca una ampliación de los modos existentes."""

from datetime import datetime,timedelta,timezone
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from tests.test_spot_bot_permission import scenario
from tests.test_mexc_bot_api import authorize
from tests.test_mexc_recovery_api import new_anchor
from tests.mexc_continuity_fixture import make_continuity_environment,observe_hour
from app.live_execution.mexc_bot_supervisor import MexcBotPermissionError
from app.live_execution.spot_bot_store import bot_record


def continuous(client,env,headers,**overrides):
    url = f"/real/accounts/{env['account']}/mexc-bots"
    body = {"request_id":str(uuid4()),"draft_id":env["draft"].policy_id,
        "anchor_id":new_anchor(client,headers,env["account"]),"paper_session_id":env["session"]["id"],"research_validation_id":env["validation"]["id"],
        "entry_budget_quote":"20","execution_mode":"CONTINUOUS","confirmation":"ACTIVAR_BOT_CONTINUO",
        "continuous_until":(env["clock"]()+timedelta(days=3)).isoformat(),**overrides}
    return client.post(url,json=body,headers=headers),url,body


def test_continuous_opt_in_fixes_deadline_and_scope_without_starting_an_order(tmp_path,monkeypatch):
    env,workers,app = scenario(tmp_path,monkeypatch)
    with TestClient(app) as client:
        headers = authorize(client)
        created,url,body = continuous(client,env,headers)
        assert created.status_code == 200,created.text
        result = created.json()
        assert result["execution_mode"] == "CONTINUOUS"
        permission = result["execution_permission"]
        assert permission["protocol"] == "CONTINUOUS_V1" and permission["expires_at"] == body["continuous_until"]
        assert permission["scope"]["entry_budget_quote"] == "20"
        deadline = workers[0]._deadline
        assert client.post(url,json=body,headers=headers).status_code == 200
        assert len(workers) == 1 and workers[0]._deadline == deadline
        assert client.post("/real/vault/protect-view",headers=headers).json()["operating"]
        assert workers[0].run_once()["action"] == "WAIT"
        assert client.get(url,headers=headers).status_code == 401
        assert not env["trader"].posts


def test_worker_keeps_running_past_day_one_with_same_grant_then_buys_and_sells(tmp_path,monkeypatch):
    env,workers,app = make_continuity_environment(tmp_path,monkeypatch)
    with TestClient(app) as client:
        headers = authorize(client); created,url,body = continuous(client,env,headers)
        assert created.status_code == 200,created.text
        worker = workers[0]; initial = created.json(); deadline = worker._deadline
        client.post("/real/vault/protect-view",headers=headers)
        env["paper"].stop(env["session"]["id"]); env["paper"].passphrase = None
        for hour in range(1,26):
            result = observe_hour(env,worker,hour)
            assert result["action"] == "WAIT",(hour,result)
        assert worker.state()["policy_id"] != initial["policy_id"] and worker._deadline == deadline
        with worker.vault.session() as db:
            _,config,_ = bot_record(db,body['request_id'])
            assert all(item['research_binding'] == initial['research_binding'] for item in config['authorizations'])
        assert client.get("/real/vault/status").json()["operating"]["active"]
        observe_hour(env,worker,26)
        assert observe_hour(env,worker,27)["action"] == "BUY"
        env["quote"].update(bid="72000",ask="72001")
        assert worker.run_once()["action"] == "SELL"
        worker.vault.backup(tmp_path/"continuous-after-day-one.db")
        assert len(env["trader"].posts) == 2 and len(workers) == 1
        assert client.post("/real/operations/stop",json={"confirmation":"DETENER_Y_BLOQUEAR"}).json()["locked"]


@pytest.mark.parametrize("fault",["missing","naive","past","over_draft","over_30_days","wrong_confirmation","session_with_date"])
def test_continuous_requires_explicit_bounded_date_and_separate_consent(tmp_path,monkeypatch,fault):
    env,workers,app = scenario(tmp_path,monkeypatch)
    changes = {}
    if fault == "missing": changes["continuous_until"] = None
    elif fault == "naive": changes["continuous_until"] = (env["clock"]()+timedelta(days=2)).replace(tzinfo=None).isoformat()
    elif fault == "past": changes["continuous_until"] = (env["clock"]()-timedelta(seconds=1)).isoformat()
    elif fault == "over_draft": changes["continuous_until"] = (env["draft"].expires_at+timedelta(seconds=1)).isoformat()
    elif fault == "over_30_days": changes["continuous_until"] = (env["clock"]()+timedelta(days=31)).isoformat()
    elif fault == "wrong_confirmation": changes["confirmation"] = "ACTIVAR_BOT_REAL"
    else: changes.update(execution_mode="SESSION",confirmation="ACTIVAR_BOT_REAL")
    with TestClient(app) as client:
        headers = authorize(client)
        result,_,_ = continuous(client,env,headers,**changes)
        assert result.status_code in (409,422),result.text
        assert not workers and not env["trader"].posts


@pytest.mark.parametrize("fault",["until","budget"])
def test_activation_nonce_cannot_change_duration_or_budget(tmp_path,monkeypatch,fault):
    env,workers,app = scenario(tmp_path,monkeypatch)
    with TestClient(app) as client:
        headers = authorize(client); result,url,body = continuous(client,env,headers)
        assert result.status_code == 200,result.text
        deadline = workers[0]._deadline
        body["continuous_until" if fault == "until" else "entry_budget_quote"] = (
            (env["clock"]()+timedelta(days=4)).isoformat() if fault == "until" else "21")
        assert client.post(url,json=body,headers=headers).status_code == 409
        assert len(workers) == 1 and workers[0]._deadline == deadline and not env["trader"].posts


@pytest.mark.parametrize("target",["evidence","grant"])
def test_worker_cannot_renew_an_expired_policy_or_extend_grant_by_reauthentication(tmp_path,monkeypatch,target):
    env,workers,app = scenario(tmp_path,monkeypatch)
    with TestClient(app) as client:
        headers = authorize(client); result,url,body = continuous(client,env,headers)
        assert result.status_code == 200,result.text
        worker = workers[0]; original_deadline = worker._deadline
        end = datetime.fromisoformat(result.json()["policy_expires_at"] if target == "evidence" else body["continuous_until"])
        env["clock"].offset = end+timedelta(seconds=1)-datetime.now(timezone.utc)
        headers = authorize(client)
        with pytest.raises(MexcBotPermissionError): worker.run_once()
        assert not env["trader"].posts and worker._deadline == original_deadline
        assert client.get(url,headers=headers).json()[0]["execution_permission"] == result.json()["execution_permission"]


@pytest.mark.parametrize("mode",["CONTINUOUS","SESSION"])
def test_human_rearm_creates_only_requested_mode_and_replay_cannot_restart_it(tmp_path,monkeypatch,mode):
    env,workers,app = scenario(tmp_path,monkeypatch)
    with TestClient(app) as client:
        headers = authorize(client); result,url,body = continuous(client,env,headers)
        assert result.status_code == 200,result.text
        identifier = body["request_id"]
        stop_url = url+"/"+identifier+"/stop"
        assert client.post(stop_url,json={"confirmation":"DETENER_BOT"},headers=headers).status_code == 200
        rearm = {"request_id":str(uuid4()),"draft_id":env["draft"].policy_id,
            "anchor_id":new_anchor(client,headers,env["account"]),"paper_session_id":env["session"]["id"],"research_validation_id":env["validation"]["id"],
            "entry_budget_quote":"20","execution_mode":mode,
            "confirmation":"REANUDAR_BOT_CONTINUO" if mode == "CONTINUOUS" else "REANUDAR_BOT_REAL"}
        if mode == "CONTINUOUS": rearm["continuous_until"] = body["continuous_until"]
        rearm_url = url+"/"+identifier+"/rearm"
        response = client.post(rearm_url,json=rearm,headers=headers)
        assert response.status_code == 200,response.text
        assert response.json()["execution_mode"] == mode
        assert len(workers) == 2 and workers[0].state()["stop_requested"]
        if mode == "CONTINUOUS":
            assert response.json()["execution_permission"]["grant_id"] != result.json()["execution_permission"]["grant_id"]
        assert client.post(stop_url,json={"confirmation":"DETENER_BOT"},headers=headers).status_code == 200
        assert client.post(rearm_url,json=rearm,headers=headers).status_code == 200
        assert len(workers) == 2
        with workers[-1].vault.session() as db: assert bot_record(db,identifier)[0].status == "PAUSED"
        workers[-1].vault.backup(tmp_path/"continuous-rearmed.db")
        assert not env["trader"].posts
