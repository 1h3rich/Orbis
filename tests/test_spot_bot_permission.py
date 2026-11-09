"""Concesión en memoria, vista protegida y parada sin ampliar límites ni plazos."""

from datetime import datetime,timezone,timedelta
import time

from fastapi.testclient import TestClient
import pytest

from app.live_execution.mexc_bot_supervisor import MexcBotPermissionError,MexcBotSupervisor
from app.live_execution.spot_bot_store import bot_record,encoded
from app.portfolio.api import RealAccess
from app.portfolio.vault import VaultError
from tests.mexc_bot_fixture import make_environment,make_app,advance_market
from tests.test_mexc_bot_api import authorize,activate
from tests.test_mexc_dca_bot import activate as activate_dca
from tests.mexc_dca_fixture import DcaScenario
from tests.test_mexc_execution_state import PASSWORD
from tests.test_spot_bot_market import Driven
from tests.test_mexc_recovery_api import new_anchor
from uuid import uuid4


def scenario(tmp_path,monkeypatch,*,threaded=False):
    env,workers = make_environment(tmp_path,monkeypatch),[]
    def factory(*args,**kwargs):
        worker = (MexcBotSupervisor if threaded else Driven)(*args,interval_seconds=0.1,**kwargs)
        workers.append(worker); return worker
    return env,workers,make_app(env,supervisor_factory=factory)


def grant(client,env,headers):
    # Misma apertura y prueba que SESSION; otra confirmación es obligatoria.
    url = f"/real/accounts/{env['account']}/mexc-bots"
    body = {"request_id":str(uuid4()),"draft_id":env["draft"].policy_id,
            "anchor_id":new_anchor(client,headers,env["account"]),"paper_session_id":env["session"]["id"],"research_validation_id":env["validation"]["id"],
            "entry_budget_quote":"20","execution_mode":"OPERATIONAL","confirmation":"ACTIVAR_BOT_OPERATIVO"}
    response = client.post(url,json=body,headers=headers)
    assert response.status_code == 200,response.text
    return response,url,body


def test_operational_permission_survives_view_protection_not_full_lock(tmp_path,monkeypatch):
    env,workers,app = scenario(tmp_path,monkeypatch)
    with TestClient(app) as client:
        headers = authorize(client)
        created,url,body = grant(client,env,headers)
        worker = workers[0]; deadline = worker._deadline
        assert worker._paper is None
        permission = created.json()["execution_permission"]
        assert datetime.fromisoformat(permission["expires_at"]) <= env["clock"]()+timedelta(days=1)
        assert client.post(url,json=body,headers=headers).status_code == 200
        assert worker._deadline == deadline and len(workers) == 1
        env["paper"].stop(env["session"]["id"])
        env["paper"].passphrase = None
        protected = client.post("/real/vault/protect-view",headers=headers)
        assert protected.status_code == 200 and protected.json() == {"protected":True,"operating":True}
        status = client.get("/real/vault/status").json()
        assert status["unlocked"] and status["view_protected"] and status["operating"]["active"]
        assert client.get(url,headers=headers).status_code == 401
        advance_market(env)
        assert worker.run_once()["action"] == "BUY"
        assert len(env["trader"].posts) == 1
        wrong = client.post("/real/vault/unlock",json={"passphrase":"wrong operational password"})
        assert wrong.status_code == 401 and client.get("/real/vault/status").json()["operating"]["active"]
        token = client.post("/real/vault/unlock",json={"passphrase":PASSWORD}).json()["token"]
        headers = {"X-Orbis-Vault-Token":token}
        assert client.get(url,headers=headers).json()[0]["position"]["fill_count"] == 1
        assert worker._deadline == deadline and len(workers) == 1
        assert client.post("/real/vault/lock",headers=headers).json()["locked"]
        assert not worker.vault.unlocked and worker.state()["stop_requested"]
        assert len(env["trader"].posts) == 1


def test_public_local_stop_has_no_start_or_trading_authority(tmp_path,monkeypatch):
    env,workers,app = scenario(tmp_path,monkeypatch)
    with TestClient(app) as client:
        headers = authorize(client); _,_,_ = grant(client,env,headers)
        client.post("/real/vault/protect-view",headers=headers)
        url = "/real/operations/stop"; body = {"confirmation":"DETENER_Y_BLOQUEAR"}
        assert client.post(url,json=body,headers={"Origin":"https://other.invalid"}).status_code == 403
        assert client.post(url,json={"confirmation":"START"}).status_code == 422
        assert client.get(url).status_code == 405
        assert client.get("/real/vault/status").json()["operating"]["active"]
        assert client.post(url,json=body).json()["locked"]
        assert not workers[0].vault.unlocked and workers[0].state()["stop_requested"]
        assert not env["trader"].posts


def test_session_mode_keeps_original_lock_contract(tmp_path,monkeypatch):
    env,workers,app = scenario(tmp_path,monkeypatch)
    with TestClient(app) as client:
        headers = authorize(client); response,_,_ = activate(client,env,headers)
        assert response.status_code == 200 and response.json()["execution_mode"] == "SESSION"
        result = client.post("/real/vault/protect-view",headers=headers)
        assert result.json() == {"protected":True,"operating":False}
        assert not workers[0].vault.unlocked and workers[0].state()["stop_requested"]
        assert client.get("/real/vault/status").json() == {"exists":True,"unlocked":False,
            "capabilities":{"dex_receipt_order_proof":True,"dex_gas_order_proof":True,"economic_review":True,"economic_review_gas":True,"economic_review_order":True,"economic_review_wallets":True,"external_recovery":True}}


def test_operational_expiry_closes_crypto_when_screen_is_protected(tmp_path,monkeypatch):
    env,workers,app = scenario(tmp_path,monkeypatch,threaded=True)
    with TestClient(app) as client:
        headers = authorize(client); grant(client,env,headers)
        client.post("/real/vault/protect-view",headers=headers)
        worker = workers[0]
        worker._deadline = 0  # Reloj monotónico de permiso vencido, sin alterar guardas de producción.
        assert worker.join(5)
        until = time.monotonic()+3
        while client.get("/real/vault/status").json()["unlocked"] and time.monotonic()<until: time.sleep(0.02)
        assert not worker.vault.unlocked
        assert worker._view is worker._trade is worker._paper is None
        assert not env["trader"].posts


def test_clock_expiry_never_gets_extended_by_reads_or_ui_activity(tmp_path,monkeypatch):
    env,workers,app = scenario(tmp_path,monkeypatch)
    with TestClient(app) as client:
        headers = authorize(client); created,url,_ = grant(client,env,headers)
        worker = workers[0]; deadline = worker._deadline
        for _ in range(2):
            assert client.get(url,headers=headers).status_code == 200
            assert client.get("/real/accounts",headers=headers).status_code == 200
        assert worker._deadline == deadline
        expires = datetime.fromisoformat(created.json()["execution_permission"]["expires_at"])
        env["clock"].offset = expires-datetime.now(timezone.utc)
        with pytest.raises(MexcBotPermissionError): worker.run_once()
        assert not env["trader"].posts


@pytest.mark.parametrize("confirmation",["ACTIVAR_BOT_REAL",""])
def test_operational_mode_requires_own_confirmation(tmp_path,monkeypatch,confirmation):
    env,workers,app = scenario(tmp_path,monkeypatch)
    with TestClient(app) as client:
        headers = authorize(client)
        body = {"request_id":str(uuid4()),"draft_id":env["draft"].policy_id,
            "anchor_id":new_anchor(client,headers,env["account"]),"paper_session_id":env["session"]["id"],"research_validation_id":env["validation"]["id"],
            "entry_budget_quote":"20","execution_mode":"OPERATIONAL","confirmation":confirmation}
        response = client.post(f"/real/accounts/{env['account']}/mexc-bots",json=body,headers=headers)
        assert response.status_code in (409,422) and not workers and not env["trader"].posts


@pytest.mark.parametrize("protected",[False,True])
def test_tampered_operational_permission_is_not_a_valid_backup(tmp_path,monkeypatch,protected):
    env,workers,app = scenario(tmp_path,monkeypatch)
    with TestClient(app) as client:
        headers = authorize(client); _,_,body = grant(client,env,headers)
        vault = workers[0].vault
        if protected:
            assert client.post("/real/vault/protect-view",headers=headers).json()["operating"]
        with vault.session() as db:
            row,config,_ = bot_record(db,body["request_id"])
            config["execution_permission"]["expires_at"] = "2099-01-01T00:00:00+00:00"
            row.config_json,row.config_digest = encoded(config); db.commit()
        with pytest.raises(VaultError,match="bot evidence"): vault.backup(tmp_path/"invalid-grant.db")
        stopped = (client.post("/real/operations/stop",json={"confirmation":"DETENER_Y_BLOQUEAR"}) if protected
                   else client.post("/real/vault/lock",headers=headers))
        assert stopped.json() == {"locked":True,"review_required":True}
        assert not vault.unlocked and not env["trader"].posts


def test_dca_can_work_after_protecting_view_and_closing_paper(tmp_path,monkeypatch):
    env = DcaScenario(tmp_path,monkeypatch)
    with TestClient(env.make_app(driven=True)) as client:
        headers = authorize(client)
        response,url,_ = activate_dca(client,env,headers,execution_mode="OPERATIONAL",confirmation="ACTIVAR_BOT_OPERATIVO")
        assert response.status_code == 200,response.text
        assert client.post("/real/vault/protect-view",headers=headers).json()["operating"]
        env.paper.stop(env.session["id"])
        from app.automation.access import automation_access
        automation_access.lock(env.paper.store.path)
        env.advance(2)
        for reason in ("DCA_BASE","DCA_DIP"): assert env.workers[0].run_once()["reason"] == reason
        assert len(env.env["trader"].posts) == 2


def test_automatic_protected_cleanup_keeps_archive_warning_for_later_stop(tmp_path,monkeypatch):
    accesses = []
    touch = RealAccess._touch
    def touched(self): touch(self); accesses.append(self)
    monkeypatch.setattr(RealAccess,"_touch",touched)
    env,workers,app = scenario(tmp_path,monkeypatch)
    with TestClient(app) as client:
        headers = authorize(client); _,_,body = grant(client,env,headers)
        assert client.post("/real/vault/protect-view",headers=headers).json()["operating"]
        access = accesses[-1]
        with access.vault.session() as db:
            row,config,_ = bot_record(db,body["request_id"])
            config["execution_permission"]["expires_at"] = "2099-01-01T00:00:00+00:00"
            row.config_json,row.config_digest = encoded(config); db.commit()
        workers[0].request_stop()
        # El callback automático llega antes que el clic de parada del usuario.
        access._close_unused_operational_vault()
        assert not access.vault.unlocked
        stopped = client.post("/real/operations/stop",json={"confirmation":"DETENER_Y_BLOQUEAR"})
        assert stopped.json() == {"locked":True,"review_required":True}
        assert client.get("/real/vault/status").json()["review_required"]
        assert not env["trader"].posts


@pytest.mark.parametrize("operational",[False,True])
def test_idle_timer_revokes_http_token_without_renewing_operational_grant(tmp_path,monkeypatch,operational):
    accesses = []
    touch = RealAccess._touch
    def touched(self): touch(self); accesses.append(self)
    monkeypatch.setattr(RealAccess,"_touch",touched)
    env,workers,app = scenario(tmp_path,monkeypatch)
    with TestClient(app) as client:
        headers = authorize(client)
        if operational: created,_,_ = grant(client,env,headers)
        else: created,_,_ = activate(client,env,headers)
        assert created.status_code == 200
        access = accesses[-1]; deadline = workers[0]._deadline
        access._deadline = 0
        status = client.get("/real/vault/status").json()
        assert access._token is None and access._mexc is access._mexc_trade is access._mexc_trade_credentials is None
        assert client.get("/real/accounts",headers=headers).status_code == 401
        assert workers[0]._deadline == deadline
        if operational:
            assert status["view_protected"] and status["operating"]["active"] and access.vault.unlocked
            advance_market(env); assert workers[0].run_once()["action"] == "BUY"
        else: assert not access.vault.unlocked and not env["trader"].posts


def test_explicit_rearm_can_change_permission_mode_without_resetting_caps_or_replaying(tmp_path,monkeypatch):
    env,workers,app = scenario(tmp_path,monkeypatch)
    with TestClient(app) as client:
        headers = authorize(client); created,url,body = grant(client,env,headers)
        client.post(url+"/"+body["request_id"]+"/stop",headers=headers,json={"confirmation":"DETENER_BOT"})
        review = {"request_id":str(uuid4()),"draft_id":env["draft"].policy_id,
                  "anchor_id":new_anchor(client,headers,env["account"]),"paper_session_id":env["session"]["id"],"research_validation_id":env["validation"]["id"],
                  "entry_budget_quote":"20","confirmation":"REANUDAR_BOT_REAL"}
        response = client.post(url+"/"+body["request_id"]+"/rearm",json=review,headers=headers)
        assert response.status_code == 200,response.text
        assert response.json()["execution_mode"] == "SESSION" and response.json()["execution_permission"] is None
        assert len(workers) == 2 and not env["trader"].posts
        assert client.post(url+"/"+body["request_id"]+"/rearm",json=review,headers=headers).status_code == 200
        assert len(workers) == 2
        assert client.post("/real/vault/protect-view",headers=headers).json()["operating"] is False
        assert not workers[-1].vault.unlocked
