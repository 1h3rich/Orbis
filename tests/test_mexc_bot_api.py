"""Activación explícita, lectura pasiva y revocación local sin claves reales."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
import time
from uuid import uuid4

from fastapi.testclient import TestClient

from app.live_execution.mexc_bot_supervisor import MexcBotSupervisor
from tests.mexc_bot_fixture import make_environment, make_app, advance_signal,advance_market
from tests.test_mexc_execution_state import PASSWORD
from tests.test_mexc_policy_drafts_api import draft as limits_payload
from tests.test_mexc_recovery_api import connect, new_anchor


class RecordedSupervisor:
    instances = []

    def __init__(self,*_,deadline,on_exit,**__):
        self.running, self.deadline, self.on_exit = False, deadline, on_exit
        self.instances.append(self)

    def start(self): self.running = True
    def request_stop(self): self.running = False
    def join(self,*_): return True
    def extend_deadline(self,value): self.deadline = value
    def state(self): return {"running":self.running,"status":"RUNNING" if self.running else "STOPPED",
                            "permission_remaining_seconds":max(0,int(self.deadline-time.monotonic()))}


def authorize(client):
    token = client.post("/real/vault/unlock",json={"passphrase":PASSWORD}).json()["token"]
    headers = {"X-Orbis-Vault-Token":token}
    connect(client,headers)
    return headers


def activate(client, env, headers, *, request_id=None, draft_id=None):
    anchor = new_anchor(client,headers,env["account"])
    body = {"request_id":request_id or str(uuid4()),"draft_id":draft_id or env["draft"].policy_id,"anchor_id":anchor,
            "paper_session_id":env["session"]["id"],"research_validation_id":env["validation"]["id"],"entry_budget_quote":"20","confirmation":"ACTIVAR_BOT_REAL"}
    url = f"/real/accounts/{env['account']}/mexc-bots"
    return client.post(url,headers=headers,json=body), url, body


def test_valid_paper_origin_cannot_activate_manual_limits_as_a_bot(tmp_path,monkeypatch):
    env = make_environment(tmp_path,monkeypatch)
    with TestClient(make_app(env,supervisor_factory=RecordedSupervisor)) as client:
        headers = authorize(client)
        limits = limits_payload(None)
        del limits["strategy_version_id"]
        saved = client.post(f"/real/accounts/{env['account']}/mexc-manual-limits", headers=headers,json=limits)
        assert saved.status_code == 200, saved.text
        worker_count = len(RecordedSupervisor.instances)
        rejected, url, _ = activate(client,env,headers,draft_id=saved.json()["id"])
        assert rejected.status_code == 409 and "sus límites no coinciden" in rejected.json()["detail"]
        assert client.get(url,headers=headers).json() == []
        assert len(RecordedSupervisor.instances) == worker_count and env["trader"].posts == []


def test_activation_is_explicit_idempotent_and_stays_stopped_after_reopen(tmp_path,monkeypatch):
    env = make_environment(tmp_path,monkeypatch)
    with TestClient(make_app(env,supervisor_factory=RecordedSupervisor)) as client:
        headers = authorize(client)
        created, url, body = activate(client,env,headers)
        assert created.status_code == 200 and created.json()["execution_enabled"], created.text
        assert client.post(url,json=body).status_code == 401
        assert client.post(url,headers={**headers,"Origin":"https://outside.invalid"},json=body).status_code == 403
        assert client.post(url,headers=headers,json={**body,"confirmation":""}).status_code == 422
        worker_count = len(RecordedSupervisor.instances)
        assert client.post(url,headers=headers,json=body).status_code == 200
        assert len(RecordedSupervisor.instances) == worker_count and env["trader"].posts == []
        assert client.post(url,headers=headers,json={**body,"entry_budget_quote":"21"}).status_code == 409
        candidates = client.get(url+"/candidates",headers=headers)
        assert candidates.status_code == 200 and candidates.json()[0]["ready"]
        assert candidates.headers["cache-control"] == "no-store"
        before = RecordedSupervisor.instances[-1].deadline
        assert client.get(url,headers=headers).status_code == 200
        assert client.get(url+f"/{body['request_id']}/executions-page",headers=headers).status_code == 200
        assert RecordedSupervisor.instances[-1].deadline == before
        policy_list = client.get(f"/real/accounts/{env['account']}/mexc-trading-sessions",headers=headers).json()
        assert any(item["mode"] == "BOT" and not item["execution_enabled"] for item in policy_list)
        stopped = client.post(url+f"/{body['request_id']}/stop",headers=headers,json={"confirmation":"DETENER_BOT"})
        assert stopped.status_code == 200 and not stopped.json()["execution_enabled"]
        assert client.post(url,headers=headers,json=body).json()["execution_enabled"] is False
        client.post("/real/vault/lock",headers=headers)
        headers = {"X-Orbis-Vault-Token":client.post("/real/vault/unlock",json={"passphrase":PASSWORD}).json()["token"]}
        listed = client.get(url,headers=headers).json()
        assert listed[0]["status"] == "PAUSED" and not listed[0]["execution_enabled"]
        assert env["trader"].posts == []


def test_shutdown_and_key_disconnection_revoke_workers(tmp_path,monkeypatch):
    env = make_environment(tmp_path,monkeypatch)
    with TestClient(make_app(env,supervisor_factory=RecordedSupervisor)) as client:
        headers = authorize(client)
        created, _, _ = activate(client,env,headers)
        assert created.status_code == 200
        worker = RecordedSupervisor.instances[-1]
        assert worker.running
        response = client.post("/real/exchanges/mexc/trade/disconnect",headers=headers)
        assert response.status_code == 200 and not worker.running
    assert not worker.running and env["trader"].posts == []


def test_shutdown_with_an_active_worker_does_not_restart_it_on_unlock(tmp_path,monkeypatch):
    env = make_environment(tmp_path,monkeypatch)
    app = make_app(env,supervisor_factory=RecordedSupervisor)
    with TestClient(app) as client:
        headers = authorize(client)
        created, url, body = activate(client,env,headers)
        assert created.status_code == 200 and created.json()["execution_enabled"]
        worker = RecordedSupervisor.instances[-1]
        assert worker.running
    assert not worker.running
    with TestClient(app) as client:
        token = client.post("/real/vault/unlock",json={"passphrase":PASSWORD}).json()["token"]
        data = client.get(url,headers={"X-Orbis-Vault-Token":token}).json()
        assert data[0]["status"] == "PAUSED" and not data[0]["execution_enabled"]
        assert env["trader"].posts == []


def test_stop_is_not_blocked_by_a_status_read_waiting_on_inflight_preflight(tmp_path,monkeypatch):
    env = make_environment(tmp_path,monkeypatch)
    workers, stop_received = [], Event()

    class Worker(MexcBotSupervisor):
        def request_stop(self):
            super().request_stop()
            stop_received.set()

    def factory(*args,**kwargs):
        worker = Worker(*args,interval_seconds=0.1,**kwargs)
        workers.append(worker)
        return worker

    entered, release = Event(), Event()
    env["trader"].after_test = lambda: (entered.set(), release.wait(4))
    with TestClient(make_app(env,supervisor_factory=factory)) as client:
        headers = authorize(client)
        created, url, body = activate(client,env,headers)
        assert created.status_code == 200, created.text
        # REAL puede entrar antes de que PAPER publique otra observación. No
        # esperar su writer lock mientras el preflight está detenido por este test.
        advance_market(env)
        assert entered.wait(3)
        with ThreadPoolExecutor(max_workers=2) as pool:
            state = pool.submit(client.get,url,headers=headers)
            stopped = pool.submit(client.post,url+f"/{body['request_id']}/stop",headers=headers,
                                  json={"confirmation":"DETENER_BOT"})
            try:
                assert stop_received.wait(2), "La consulta de estado bloqueó el permiso de parada"
            finally:
                release.set()
            assert stopped.result(timeout=6).status_code == 200
            assert state.result(timeout=6).status_code == 200
        assert workers[0].join(2) and env["trader"].posts == []


def test_review_rearms_same_bot_and_old_request_never_restarts_it(tmp_path,monkeypatch):
    env = make_environment(tmp_path,monkeypatch)
    with TestClient(make_app(env,supervisor_factory=RecordedSupervisor)) as client:
        headers = authorize(client)
        created, url, activation = activate(client,env,headers)
        assert created.status_code == 200
        bot_url = url + "/" + activation["request_id"]
        client.post(bot_url+"/stop",headers=headers,json={"confirmation":"DETENER_BOT"})
        body = {"request_id":str(uuid4()),"draft_id":env["draft"].policy_id,
                "anchor_id":new_anchor(client,headers,env["account"]),
                "paper_session_id":env["session"]["id"],"research_validation_id":env["validation"]["id"],"entry_budget_quote":"18",
                "confirmation":"REANUDAR_BOT_REAL"}
        count = len(RecordedSupervisor.instances)
        assert client.post(bot_url+"/rearm",json=body).status_code == 401
        assert client.post(bot_url+"/rearm",headers={**headers,"Origin":"https://outside.invalid"},json=body).status_code == 403
        result = client.post(bot_url+"/rearm",headers=headers,json=body)
        assert result.status_code == 200, result.text
        assert result.json()["execution_enabled"] and result.json()["reauthorizations"] == 1
        assert result.json()["policy_id"] != created.json()["policy_id"]
        assert result.json()["entry_budget_quote"] == "18"
        assert len(RecordedSupervisor.instances) == count + 1 and env["trader"].posts == []
        assert client.post(bot_url+"/rearm",headers=headers,json=body).status_code == 200
        assert len(RecordedSupervisor.instances) == count + 1
        assert client.post(bot_url+"/rearm",headers=headers,json={**body,"entry_budget_quote":"19"}).status_code == 409
        policies = client.get(f"/real/accounts/{env['account']}/mexc-trading-sessions",headers=headers).json()
        bot_policies = {created.json()["policy_id"],result.json()["policy_id"]}
        assert len([item for item in policies if item["id"] in bot_policies and item["mode"] == "BOT"]) == 2
        client.post(bot_url+"/stop",headers=headers,json={"confirmation":"DETENER_BOT"})
        assert not client.post(bot_url+"/rearm",headers=headers,json=body).json()["execution_enabled"]
        assert len(RecordedSupervisor.instances) == count + 1
        # Activación inicial mantiene sus términos, aunque el presupuesto actual cambie.
        assert client.post(url,headers=headers,json=activation).status_code == 200
        assert len(RecordedSupervisor.instances) == count + 1



def test_bound_renewed_view_rearms_the_same_bot_only_after_new_approval(tmp_path,monkeypatch):
    env = make_environment(tmp_path,monkeypatch)
    workers = []

    def supervisor(*args,**kwargs):
        worker = MexcBotSupervisor(*args,interval_seconds=0.1,**kwargs)
        workers.append(worker)
        return worker

    with TestClient(make_app(env,supervisor_factory=supervisor)) as client:
        headers = authorize(client)
        created,url,activation = activate(client,env,headers)
        assert created.is_success and created.json()["execution_enabled"],created.text
        bot = url+"/"+activation["request_id"]
        assert client.post("/real/exchanges/mexc/connect",headers=headers,
                           json={"api_key":"fixture-renewed-key","secret_key":"fixture-secret"}).is_success
        renewal = client.post(f"/real/accounts/{env['account']}/mexc-link/renew-view",headers=headers,
                              json={"confirmation":"RENOVAR_VIEW_Y_DETENER"})
        assert renewal.is_success and renewal.json()["stopped"],renewal.text
        assert not client.get(url,headers=headers).json()[0]["execution_enabled"]
        assert len(workers)==1 and workers[0].join(2) and env["trader"].posts == []
        assert client.post("/real/exchanges/mexc/trade/connect",headers=headers,
                           json={"api_key":"fixture-trade","secret_key":"fixture-secret"}).json()["verified"]
        approval = {**activation,"request_id":str(uuid4()),"confirmation":"REANUDAR_BOT_REAL"}
        assert client.post(bot+"/rearm",headers=headers,json=approval).status_code == 409
        approval["anchor_id"] = new_anchor(client,headers,env["account"])
        resumed = client.post(bot+"/rearm",headers=headers,json=approval)
        assert resumed.is_success and resumed.json()["execution_enabled"],resumed.text
        assert resumed.json()["id"] == created.json()["id"] and resumed.json()["policy_id"] != created.json()["policy_id"]
        assert len(workers)==2 and env["trader"].posts == []
        assert advance_signal(env,workers=workers)["poll_result"] == "OBSERVED"
        until = time.monotonic()+8
        while True:
            result = client.get(url,headers=headers).json()[0]
            if result["position"]["verified"] and result["position"]["fill_count"] == 1:
                break
            assert time.monotonic() < until,result
            time.sleep(0.05)
        assert len(env["trader"].posts)==1
        assert client.post(bot+"/stop",headers=headers,json={"confirmation":"DETENER_BOT"}).is_success
        assert workers[1].join(2)

def test_stop_during_rearm_is_received_without_waiting_for_review_network(tmp_path,monkeypatch):
    env = make_environment(tmp_path,monkeypatch)
    with TestClient(make_app(env,supervisor_factory=RecordedSupervisor)) as client:
        headers = authorize(client)
        created, url, activation = activate(client,env,headers)
        bot_url = url + "/" + activation["request_id"]
        client.post(bot_url+"/stop",headers=headers,json={"confirmation":"DETENER_BOT"})
        body = {"request_id":str(uuid4()),"draft_id":env["draft"].policy_id,
                "anchor_id":new_anchor(client,headers,env["account"]),
                "paper_session_id":env["session"]["id"],"research_validation_id":env["validation"]["id"],"entry_budget_quote":"20",
                "confirmation":"REANUDAR_BOT_REAL"}
        entered, release, revoked = Event(), Event(), Event()
        env["trader"].pair_view_account = lambda _: (entered.set(), release.wait(4))
        # El cliente de parada debe alcanzar el registro de permisos mientras
        # la revisión mantiene la bóveda; instrumentamos sólo la señal local.
        from app.live_execution import mexc_bot_service

        class ReviewEvent(Event):
            def set(self):
                super().set()
                revoked.set()

        monkeypatch.setattr(mexc_bot_service,"Event",ReviewEvent)
        count = len(RecordedSupervisor.instances)
        with ThreadPoolExecutor(max_workers=3) as pool:
            review = pool.submit(client.post,bot_url+"/rearm",headers=headers,json=body)
            assert entered.wait(3)
            state = pool.submit(client.get,url,headers=headers)
            stopped = pool.submit(client.post,bot_url+"/stop",headers=headers,json={"confirmation":"DETENER_BOT"})
            try:
                assert revoked.wait(2), "STOP no alcanzó la revisión"
            finally:
                release.set()
            result = review.result(timeout=6)
            assert result.status_code == 409, result.text
            assert stopped.result(timeout=6).status_code == 200
            assert state.result(timeout=6).status_code == 200
        assert len(RecordedSupervisor.instances) == count and env["trader"].posts == []
        assert client.get(url,headers=headers).json()[0]["status"] == "PAUSED"
