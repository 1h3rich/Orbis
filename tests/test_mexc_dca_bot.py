"""DCA REAL comparte journal/guardián, nunca efectivo ni unidades PAPER."""

from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from tests.mexc_dca_fixture import DcaScenario, EMA_RULE
from tests.test_mexc_bot_api import authorize
from tests.test_mexc_recovery_api import new_anchor
from tests.test_mexc_policy_drafts_api import draft
from tests.test_mexc_execution_state import PASSWORD
from app.automation.access import automation_access
from app.automation.dca import dca_engine_for_rule, simulate_dca
from app.lab.access import lab_access
from app.lab.engine import validate_candles
from app.lab.store import content_hash
from app.live_execution.dca_bot_source import DcaBotSource, SpotBotSourceError
from app.live_execution.mexc_bot_supervisor import MexcBotPermissionError
from app.live_execution.spot_bot_store import SpotBotStoreError
from app.live_execution.spot_bot_store import bot_record,encoded,owned_spot_bot_ledger
from app.portfolio.models import RealSpotBotDecision
from app.portfolio.vault import RealVault


def activate(client,scenario,headers,**changes):
    url = f"/real/accounts/{scenario.env['account']}/mexc-bots"
    body = {"request_id":str(uuid4()),"draft_id":scenario.draft.policy_id,
            "anchor_id":new_anchor(client,headers,scenario.env["account"]),
            "paper_session_id":scenario.session["id"],"research_validation_id":scenario.validation["id"],"entry_budget_quote":"20","source_kind":"DCA",
            "dca_budget_total_quote":"100","dca_monthly_limit_quote":"40","confirmation":"ACTIVAR_BOT_REAL",**changes}
    return client.post(url,json=body,headers=headers),url,body


def test_synthetic_dca_venue_does_not_return_trades_outside_its_requested_window(tmp_path,monkeypatch):
    from datetime import timedelta
    scenario=DcaScenario(tmp_path,monkeypatch)
    venue=scenario.env['venue']
    now=scenario.clock()
    inside=int(now.timestamp()*1000)-1000
    outside=int((now-timedelta(days=1)).timestamp()*1000)-1000
    venue.trades.extend([{'id':'expired-fixture-fill','time_ms':outside},{'id':'current-fixture-fill','time_ms':inside}])
    response=venue.trades_window('BTCUSDT',days=1)
    assert response['trades']==[{'id':'current-fixture-fill','time_ms':inside}]


SMA_RULE = {"sma_gate": {"period": 2, "condition": "below"}}


def test_mexc_volume_plan_requires_verified_bot_and_replays_decision(tmp_path, monkeypatch):
    scenario = DcaScenario(tmp_path, monkeypatch, volume=True)
    with TestClient(scenario.make_app(driven=True)) as client:
        headers = authorize(client)
        base = f"/real/accounts/{scenario.env['account']}"
        versions = client.get(base + "/mexc-bots/dca-versions", headers=headers)
        assert versions.is_success, versions.text
        version_id = scenario.version["version_id"]
        assert version_id in {item["id"] for item in versions.json()}
        candidates = client.get(base + "/mexc-bots/candidates", headers=headers).json()
        candidate = next(item for item in candidates if item["session_id"] == scenario.session["id"])
        assert candidate["ready"] and candidate["research_options"][0]["id"] == scenario.validation["id"]
        created = client.post(base + "/mexc-policy-drafts", headers=headers, json=draft(version_id))
        assert created.is_success, created.text
        blocked = client.post(base + f"/mexc-policy-drafts/{created.json()['id']}/activate", headers=headers,
            json={"request_id":str(uuid4()), "anchor_id":new_anchor(client,headers,scenario.env["account"]),
                  "confirmation":"ACTIVAR_ORDENES_REALES"})
        assert blocked.status_code == 422 and "desde PAPER" in blocked.json()["detail"], blocked.text

        response, url, body = activate(client, scenario, headers, execution_mode="CONTINUOUS",
            confirmation="ACTIVAR_BOT_CONTINUO", continuous_until=(scenario.clock()+timedelta(days=3)).isoformat())
        assert response.is_success, response.text
        assert response.json()["research_binding"]["paper"]["simulation_engine"] == "dca-1.9"
        assert not scenario.env["trader"].posts
        worker = scenario.workers[-1]
        assert worker.run_once()["action"] == "WAIT"
        scenario.advance(2)
        for reason in ("DCA_BASE", "DCA_DIP"):
            outcome = worker.run_once()
            assert outcome["action"] == "BUY" and outcome["reason"] == reason, outcome
        assert (4, scenario.day - 2 * 86400, scenario.day + 2 * 86400) in scenario.candle_requests
        journal = url + "/" + body["request_id"] + "/decisions"
        rows = client.get(journal, headers=headers)
        assert rows.is_success, rows.text
        data = {row["dca"]["kind"]:row for row in rows.json()}
        assert set(data) == {"BASE", "DIP"}
        for kind, requested in (("BASE","100"),("DIP","50")):
            detail = data[kind]["dca"]
            assert detail["volume_ratio"] == detail["volume_min_ratio"] == "2"
            assert detail["volume_period"] == 3 and detail["requested"] == requested
            assert Decimal(detail["budget_limit"]) <= 20
        scenario.advance(2, seconds=12*3600+5)
        assert worker.run_once()["action"] == "WAIT"
        scenario.advance(3)
        assert worker.run_once()["reason"] == "DCA_VOLUME_BELOW_MINIMUM"
        assert len(scenario.env["trader"].posts) == 2

        with worker.vault.session() as db:
            saved = db.get(RealSpotBotDecision, data["BASE"]["intent_id"])
            altered = {key:value for key,value in data["BASE"].items()
                       if key not in ("journal_status","external_status","observed_fills")}
            altered["dca"] = altered["dca"] | {"volume_ratio":"100"}
            saved.payload_json, saved.payload_digest = encoded(altered)
            db.commit()
        invalid = client.get(journal, headers=headers)
        assert invalid.status_code == 409 and "mercado REAL archivado" in invalid.json()["detail"], invalid.text


def test_mexc_volume_buy_stops_when_earliest_denominator_candle_is_revised(tmp_path, monkeypatch):
    scenario = DcaScenario(tmp_path, monkeypatch, volume=True)
    with TestClient(scenario.make_app(driven=True)) as client:
        headers = authorize(client)
        response, url, _ = activate(client, scenario, headers)
        assert response.is_success, response.text
        revised = scenario.rows[scenario.seed_count - 2]
        revised["v"] = "9"
        scenario.advance(2)
        with pytest.raises(SpotBotStoreError, match="Velas MEXC REAL incompletas, cambiadas"):
            scenario.workers[-1].run_once()
        bot, = client.get(url, headers=headers).json()
        assert bot["status"] == "PAUSED" and not bot["execution_enabled"]
        assert scenario.candle_requests[-1] == (4, revised["t"], scenario.day + 2 * 86400)
        assert not scenario.env["trader"].posts


@pytest.mark.parametrize("fields,requested,next_reason", [
    (SMA_RULE, ("100", "50"), "DCA_SMA_CONDITION_NOT_MET"),
    (EMA_RULE, ("25.00", "12.50"), "DCA_MA_ZERO_AMOUNT"),
], ids=["sma_filter", "ema_amount"])
def test_mexc_moving_average_bot_runs_from_own_market_with_verified_limits(tmp_path, monkeypatch, fields, requested, next_reason):
    scenario = DcaScenario(tmp_path, monkeypatch, average_rule=fields)
    sid = scenario.session["id"]
    with pytest.raises(SpotBotSourceError, match="solo está disponible"):
        DcaBotSource(scenario.paper).observation(sid, now=scenario.clock())
    with TestClient(scenario.make_app(driven=True)) as client:
        headers = authorize(client)
        base = f"/real/accounts/{scenario.env['account']}"
        versions = client.get(base + "/mexc-bots/dca-versions", headers=headers)
        assert versions.status_code == 200, versions.text
        assert scenario.version["version_id"] in {item["id"] for item in versions.json()}
        candidates = client.get(base + "/mexc-bots/candidates", headers=headers).json()
        candidate = next(item for item in candidates if item["session_id"] == sid)
        assert candidate["ready"] and candidate["research_options"][0]["id"] == scenario.validation["id"]
        created = client.post(base + "/mexc-policy-drafts", headers=headers, json=draft(scenario.version["version_id"]))
        assert created.status_code == 200, created.text
        anchor_id = new_anchor(client, headers, scenario.env["account"])
        blocked = client.post(base + f"/mexc-policy-drafts/{created.json()['id']}/activate", headers=headers,
                              json={"request_id": str(uuid4()), "anchor_id": anchor_id, "confirmation": "ACTIVAR_ORDENES_REALES"})
        assert blocked.status_code == 422 and "desde PAPER" in blocked.json()["detail"], blocked.text
        entry_limit = "30" if fields == EMA_RULE else "20"
        response, url, body = activate(client, scenario, headers, entry_budget_quote=entry_limit, execution_mode="CONTINUOUS",
            confirmation="ACTIVAR_BOT_CONTINUO", continuous_until=(scenario.clock()+timedelta(days=3)).isoformat())
        assert response.status_code == 200, response.text
        bot = body["request_id"]
        assert response.json()["research_binding"]["paper"]["simulation_engine"] == scenario.session["engine_version"]
        assert not scenario.env["trader"].posts
        worker = scenario.workers[-1]
        automation_access.lock(scenario.paper.store.path)
        lab_access.lock(scenario.paper.lab.path)
        assert worker.run_once()["action"] == "WAIT"
        scenario.advance(2)
        for reason in ("DCA_BASE", "DCA_DIP"):
            outcome = worker.run_once()
            assert outcome["action"] == "BUY" and outcome["reason"] == reason, outcome
        diary = client.get(url + "/" + bot + "/decisions", headers=headers)
        assert diary.status_code == 200, diary.text
        data = {row["dca"]["kind"]: row for row in diary.json()}
        assert tuple(data[kind]["dca"]["requested"] for kind in ("BASE", "DIP")) == requested
        assert all(Decimal(row["dca"]["budget_limit"]) <= Decimal(entry_limit) for row in data.values())
        # A daily renewal may expose that new REAL close as sequence zero of a successor.
        assert all(row["market_observation"]["candle_t"] == scenario.day + 86400 for row in data.values())
        if fields == EMA_RULE:
            assert all(row["dca"]["amount_factor"] == "0.25" and row["dca"]["ma_kind"] == "ema" for row in data.values())
        assert len(scenario.env["trader"].posts) == 2
        scenario.advance(2, seconds=12 * 3600 + 5)
        assert worker.run_once()["action"] == "WAIT"
        scenario.advance(3)
        assert worker.run_once()["reason"] == next_reason
        assert len(scenario.env["trader"].posts) == 2
        assert Decimal(client.get(url, headers=headers).json()[0]["position"]["cost_quote"]) <= 40

        # A fresh checksum does not make an edited signal match its closed candles.
        with worker.vault.session() as db:
            stored = db.get(RealSpotBotDecision, data["BASE"]["intent_id"])
            altered = data["BASE"].copy()
            for key in ("journal_status", "external_status", "observed_fills"):
                altered.pop(key)
            altered["dca"]["ma_value" if fields == EMA_RULE else "sma_value"] = "1"
            stored.payload_json, stored.payload_digest = encoded(altered)
            db.commit()
        invalid = client.get(url + "/" + bot + "/decisions", headers=headers)
        assert invalid.status_code == 409 and "mercado REAL archivado" in invalid.json()["detail"], invalid.text


def test_mexc_rearm_rejects_a_different_ema_prefix_even_with_verified_paper(tmp_path, monkeypatch):
    scenario = DcaScenario(tmp_path, monkeypatch, average_rule=EMA_RULE)
    store, lab = scenario.paper.store, scenario.paper.lab
    from app.automation import verification

    class VerificationClock(datetime):
        @classmethod
        def now(cls, tz=None):
            observed = scenario.clock()
            return observed.astimezone(tz) if tz is not None else observed.replace(tzinfo=None)

    monkeypatch.setattr(verification, "datetime", VerificationClock)
    with TestClient(scenario.make_app(driven=True)) as client:
        headers = authorize(client)
        response, url, body = activate(client, scenario, headers, entry_budget_quote="30")
        assert response.status_code == 200, response.text
        bot = body["request_id"]
        worker = scenario.workers[-1]
        scenario.advance(2)
        assert worker.run_once()["action"] == "BUY"
        assert client.post(f"{url}/{bot}/stop", headers=headers, json={"confirmation": "DETENER_BOT"}).status_code == 200
        assert scenario.observe(2)["poll_result"] == "OBSERVED"

        # An otherwise genuine second PAPER session can start from a revised
        # provider dataset. It cannot replace the closes already in this bot.
        rows = [dict(row) for row in scenario.rows[:5]]
        rows[0]["c"] = "65001"
        candles, gaps = validate_candles(rows, "1d", now=int(scenario.clock().timestamp()))
        changed = lab.save_dataset("BTC/USDT", "1d", "mexc-spot-public", candles, gaps, verified_public=True)
        version = store.version(scenario.version["version_id"])
        rule = version["definition"]
        run = store.save_run(version["id"], changed["id"], changed["hash"],
            simulate_dca(candles, rule, timeframe="1d"), dca_engine_for_rule(rule),
            lab_path=lab.path, expected_definition_hash=content_hash(rule))
        alternate = scenario.paper.create(version["id"], changed["id"], run["id"])
        scenario.advance(3)
        assert scenario.paper.poll(alternate["id"])["poll_result"] == "OBSERVED"
        assert scenario.paper.poll(scenario.session["id"])["poll_result"] == "OBSERVED"
        request = {"request_id": str(uuid4()), "draft_id": scenario.draft.policy_id,
                   "anchor_id": new_anchor(client, headers, scenario.env["account"]),
                   "paper_session_id": alternate["id"], "research_validation_id": scenario.validation["id"],
                   "entry_budget_quote": "30", "confirmation": "REANUDAR_BOT_REAL"}
        rejected = client.post(f"{url}/{bot}/rearm", headers=headers, json=request)
        assert rejected.status_code == 409 and "conservar los cierres" in rejected.json()["detail"], rejected.text
        assert len(scenario.env["trader"].posts) == 1
        request.update(request_id=str(uuid4()), paper_session_id=scenario.session["id"])
        recovered = client.post(f"{url}/{bot}/rearm", headers=headers, json=request)
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["position"]["fill_count"] == 1
        assert len(scenario.env["trader"].posts) == 1


def test_dca_buys_base_and_dip_once_with_real_monthly_budget(tmp_path,monkeypatch):
    scenario = DcaScenario(tmp_path,monkeypatch)
    with TestClient(scenario.make_app(driven=True)) as client:
        headers = authorize(client)
        response,url,body = activate(client,scenario,headers)
        assert response.status_code == 200,response.text
        assert response.json()["source_kind"] == "DCA"
        research = response.json()['research_binding']
        assert research['research']['artifact_id'] == scenario.validation['id']
        assert research['research']['dataset_to_t'] < research['paper']['seed_from_t']
        assert research['dca_anchor'] == {'policy':'RESET_AT_REAL_ACTIVATION',
            'candle_t':scenario.day,'price':'65000'}
        worker = scenario.workers[-1]
        assert worker.run_once()["action"] == "WAIT"
        assert scenario.observe(2)["poll_result"] == "OBSERVED"
        for reason in ("DCA_BASE","DCA_DIP"):
            result = worker.run_once()
            assert result["action"] == "BUY" and result["reason"] == reason,result
            assert result["journal_status"] == "RESERVATION_CLOSED",result
        assert worker.run_once()["action"] == "WAIT"
        assert len(scenario.env["trader"].posts) == 2
        listed = client.get(url,headers=headers).json()[0]
        assert Decimal(listed["position"]["cost_quote"]) <= Decimal("40")
        assert Decimal(listed["position"]["units"]) > 0
        assert scenario.paper.detail(scenario.session["id"])["state"]["units"] != listed["position"]["units"]
        assert client.post(url,json=body,headers=headers).status_code == 200
        assert len(scenario.workers) == 1
        rows = client.get(url+"/"+body["request_id"]+"/decisions",headers=headers).json()
        assert {row["dca"]["kind"] for row in rows} == {"BASE","DIP"}
        assert all(row["side"] == "BUY" for row in rows)
        assert client.post(url+"/"+body["request_id"]+"/stop",json={"confirmation":"DETENER_BOT"},headers=headers).status_code == 200
        rearm = {"request_id":str(uuid4()),"draft_id":scenario.draft.policy_id,
                 "anchor_id":new_anchor(client,headers,scenario.env["account"]),"paper_session_id":scenario.session["id"],"research_validation_id":scenario.validation["id"],
                 "entry_budget_quote":"20","confirmation":"REANUDAR_BOT_REAL"}
        response = client.post(url+"/"+body["request_id"]+"/rearm",json=rearm,headers=headers)
        assert response.status_code == 200,response.text
        assert response.json()["dca_limits"] == listed["dca_limits"]
        assert response.json()['research_binding']['dca_anchor'] == research['dca_anchor']
        assert scenario.workers[-1].run_once()["action"] == "WAIT"
        assert scenario.observe(3)["poll_result"] == "OBSERVED"
        assert scenario.workers[-1].run_once()["action"] == "WAIT"
        assert len(scenario.env["trader"].posts) == 2
        client.post(url+"/"+body["request_id"]+"/stop",json={"confirmation":"DETENER_BOT"},headers=headers)
        vault = scenario.workers[-1].vault
        backup = vault.backup(tmp_path/"with-dca-backup.db")
        assert backup.read_bytes()[:16] != b"SQLite format 3\x00"
        copied = RealVault(backup)
        copied.unlock(PASSWORD)
        try:
            with copied.session() as db:
                _,config,state = bot_record(db,body["request_id"])
                assert config["source"]["kind"] == "DCA" and config["dca_limits"] == listed["dca_limits"]
                assert state["dca_reference"] == "65000"
            assert owned_spot_bot_ledger(copied,body["request_id"])[0] == owned_spot_bot_ledger(vault,body["request_id"])[0]
        finally:
            copied.lock()


@pytest.mark.parametrize("change",[{"dca_budget_total_quote":None},{"dca_monthly_limit_quote":"-1"},{"source_kind":"STRATEGY"}])
def test_dca_requires_explicit_valid_real_allocation(tmp_path,monkeypatch,change):
    scenario = DcaScenario(tmp_path,monkeypatch)
    with TestClient(scenario.make_app(driven=True)) as client:
        headers = authorize(client)
        response,_,_ = activate(client,scenario,headers,**change)
        assert response.status_code in (409,422),response.text
        assert scenario.env["trader"].posts == []


def test_dca_policy_candidates_and_missing_observation_are_not_fake_long(tmp_path,monkeypatch):
    scenario = DcaScenario(tmp_path,monkeypatch)
    with TestClient(scenario.make_app(driven=True)) as client:
        headers = authorize(client)
        base = f"/real/accounts/{scenario.env['account']}"
        versions = client.get(base+"/mexc-bots/dca-versions",headers=headers)
        assert versions.status_code == 200 and versions.headers["cache-control"] == "no-store"
        version = versions.json()[0]
        assert version["kind"] == "DCA" and "direction" not in version["definition"]
        payload = {**draft(version["id"]),"expires_at":scenario.draft.expires_at.isoformat()}
        saved = client.post(base+"/mexc-policy-drafts",json=payload,headers=headers)
        assert saved.status_code == 200,saved.text
        assert not saved.json()["enabled"]
        candidates = client.get(base+"/mexc-bots/candidates",headers=headers).json()
        dca = next(row for row in candidates if row["source_kind"] == "DCA")
        assert dca["ready"] and dca["session_id"] == scenario.session["id"]
        with scenario.paper.store.connect() as db:
            db.execute("UPDATE paper_dca_sessions SET source_run_id=NULL WHERE id=?",(scenario.session["id"],))
        candidates = client.get(base+"/mexc-bots/candidates",headers=headers).json()
        assert not next(row for row in candidates if row["source_kind"] == "DCA")["ready"]
        response,_,_ = activate(client,scenario,headers)
        assert response.status_code == 409 and not scenario.workers
        assert scenario.env["trader"].posts == []


def test_dca_lock_revokes_cycle_and_unlock_never_resumes_or_reallocates(tmp_path,monkeypatch):
    scenario = DcaScenario(tmp_path,monkeypatch)
    with TestClient(scenario.make_app(driven=True)) as client:
        headers = authorize(client)
        response,url,body = activate(client,scenario,headers)
        assert response.status_code == 200,response.text
        worker = scenario.workers[-1]
        scenario.observe(2)
        automation_access.lock(scenario.paper.store.path)
        with pytest.raises(MexcBotPermissionError,match="DCA cifrado"):
            worker.run_once()
        assert scenario.env["trader"].posts == []
        automation_access.unlock(scenario.paper.store.path,PASSWORD)
        client.post("/real/vault/lock",headers=headers)
        headers = {"X-Orbis-Vault-Token":client.post("/real/vault/unlock",json={"passphrase":PASSWORD}).json()["token"]}
        listed = client.get(url,headers=headers).json()[0]
        assert listed["status"] == "PAUSED" and not listed["execution_enabled"]
        assert listed["dca_limits"] == response.json()["dca_limits"]
        assert scenario.env["trader"].posts == []


@pytest.mark.parametrize("fault",["timeout","stop","source"])
def test_dca_ambiguous_send_stop_and_corrupt_source_never_retry(tmp_path,monkeypatch,fault):
    scenario = DcaScenario(tmp_path,monkeypatch)
    with TestClient(scenario.make_app(driven=True)) as client:
        headers = authorize(client)
        response,url,body = activate(client,scenario,headers)
        assert response.status_code == 200,response.text
        worker = scenario.workers[-1]
        scenario.observe(2)
        if fault == "timeout": scenario.env["trader"].timeout_after_accepting = True
        if fault == "stop":
            from app.live_execution.journal import stop_spot_account
            scenario.env["trader"].after_test = lambda:stop_spot_account(worker.vault,scenario.draft.account_ref,now=scenario.clock())
        if fault == "source":
            with scenario.paper.store.connect() as db:
                db.execute("UPDATE dca_runs SET result_json='{}' WHERE id=?",(scenario.session["source_run_id"],))
            with pytest.raises(ValueError,match="DCA probado"):
                worker.run_once()
        else:
            result = worker.run_once()
            assert result["paused"]
        with pytest.raises(SpotBotStoreError,match="pausado"):
            worker.run_once()
        assert len(scenario.env["trader"].posts) == (1 if fault == "timeout" else 0)
        restored = client.get(url,headers=headers).json()[0]
        assert not restored["execution_enabled"]
        # La misma solicitud no vuelve a conceder permiso, ni siquiera tras un timeout.
        assert not client.post(url,json=body,headers=headers).json()["execution_enabled"]
        assert len(scenario.env["trader"].posts) == (1 if fault == "timeout" else 0)
        if fault == "timeout":
            assert client.post(url+"/"+body["request_id"]+"/stop",json={"confirmation":"DETENER_BOT"},headers=headers).status_code == 200
            rows = client.get(url+"/"+body["request_id"]+"/decisions",headers=headers).json()
            follow_url = "/real/execution/mexc/intents/"+rows[0]["intent_id"]+"/synchronize"
            for _ in range(2):
                followed = client.post(follow_url,json={"account_id":scenario.env["account"]},headers=headers)
                assert followed.status_code == 200 and followed.json()["reservation_closed"],followed.text
            assert client.get(url,headers=headers).json()[0]["position"]["fill_count"] == 1
            scenario.env["trader"].timeout_after_accepting = False
            rearm = {"request_id":str(uuid4()),"draft_id":scenario.draft.policy_id,
                     "anchor_id":new_anchor(client,headers,scenario.env["account"]),"paper_session_id":scenario.session["id"],"research_validation_id":scenario.validation["id"],
                     "entry_budget_quote":"20","confirmation":"REANUDAR_BOT_REAL"}
            resumed = client.post(url+"/"+body["request_id"]+"/rearm",json=rearm,headers=headers)
            assert resumed.status_code == 200,resumed.text
            assert scenario.workers[-1].run_once()["action"] == "WAIT"
            assert len(scenario.env["trader"].posts) == 1


def test_delayed_dca_fill_audits_original_order_budget_and_never_hides_actual_cost(tmp_path,monkeypatch):
    scenario = DcaScenario(tmp_path,monkeypatch)
    trader,venue = scenario.env["trader"],scenario.env["venue"]
    original,settled = trader.place_limit_order,{}
    def queue_order(intent):
        before = (venue.base,venue.quote)
        acknowledgement = original(intent)
        key = intent.intent_id.replace("-","")
        settled.update(order=trader.orders[key].copy(),fill=venue.trades.pop(),key=key,base=venue.base,quote=venue.quote)
        venue.base,venue.quote = before
        trader.orders[key].update(status="NEW",executed_quantity="0",cumulative_quote="0")
        return acknowledgement
    trader.place_limit_order = queue_order
    with TestClient(scenario.make_app(driven=True)) as client:
        headers = authorize(client)
        response,url,body = activate(client,scenario,headers)
        assert response.status_code == 200,response.text
        worker = scenario.workers[-1]
        scenario.observe(2)
        pending = worker.run_once()
        assert not pending["paused"] and pending["journal_status"] == "SUBMISSION_UNKNOWN",pending
        assert len(trader.posts) == 1
        # El exchange acaba de ejecutar; la comisión excede lo autorizado.
        # No se altera el hecho real ni se inventa que hubo una cancelación.
        venue.base = settled["base"]
        venue.quote = str(Decimal(settled["quote"])-Decimal("1.98"))
        venue.trades.append({**settled["fill"],"commission":"2"})
        trader.orders[settled["key"]] = settled["order"]
        followed = worker.run_once()
        assert followed["action"] == "FOLLOW_UP" and followed["paused"],followed
        assert followed["reason"] == "EXECUTED_FEE_EXCEEDED"
        listed = client.get(url,headers=headers).json()[0]
        assert listed["position"]["fill_count"] == 1 and Decimal(listed["position"]["cost_quote"]) > 20
        assert len(trader.posts) == 1
