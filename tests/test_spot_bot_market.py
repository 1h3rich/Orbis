"""Seguimiento propio REAL: causal, cifrado, continuo y sin adoptar operaciones PAPER."""

from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import json

from fastapi.testclient import TestClient
from app.portfolio.vault import SCHEMA_VERSION
import pytest

from app.live_execution.mexc_bot_supervisor import MexcBotSupervisor
from app.live_execution.spot_bot_market import read_market_stream,observe_bot_market
from app.live_execution.spot_bot_store import SpotBotStoreError,bot_record,encoded,claim_spot_bot_cycle,finish_spot_bot_cycle
from app.portfolio.models import RealSpotBot,RealSpotBotDecision,RealSpotBotMarketObservation,RealSpotBotMarketStream
from app.portfolio.vault import RealVault,VaultError
from tests.mexc_bot_fixture import make_environment,make_app,advance_market
from tests.mexc_dca_fixture import DcaScenario
from tests.test_mexc_bot_api import activate as activate_long,authorize
from tests.test_mexc_dca_bot import activate as activate_dca
from tests.test_mexc_execution_state import PASSWORD


class Driven(MexcBotSupervisor):
    def start(self): self._generation = self.vault.session_generation
    def state(self): return {**super().state(),"running":not self._stop.is_set()}


def long_case(tmp_path,monkeypatch):
    env,workers = make_environment(tmp_path,monkeypatch),[]
    def factory(*args,**kwargs):
        worker = Driven(*args,**kwargs); workers.append(worker); return worker
    return env,workers,make_app(env,supervisor_factory=factory)


def test_long_observes_and_buys_without_advancing_paper(tmp_path,monkeypatch):
    env,workers,app = long_case(tmp_path,monkeypatch)
    with TestClient(app) as client:
        headers = authorize(client)
        response,url,body = activate_long(client,env,headers)
        assert response.status_code == 200,response.text
        assert response.json()["market_tracking"] == "OWN_REAL"
        initial = env["paper"].detail(env["session"]["id"])
        assert workers[0].run_once()["action"] == "WAIT"
        advance_market(env)
        assert workers[0].run_once()["action"] == "BUY"
        assert workers[0].run_once()["action"] == "WAIT"
        assert env["paper"].detail(env["session"]["id"]) == initial
        assert len(env["trader"].posts) == 1
        with workers[0].vault.session() as db:
            seed,bars,pointer = read_market_stream(db,response.json()["policy_id"])
            assert pointer["sequence_no"] == 1 and len(bars) == len(seed["candles"])+1
            assert db.query(RealSpotBotMarketObservation).count() == 1
        decisions = client.get(url+"/"+body["request_id"]+"/decisions",headers=headers).json()
        assert decisions[0]["market_observation"] == pointer
        assert decisions[0]["source"] == seed["source"]
        assert client.get(url,headers=headers).json()[0]["market_observation"]["verified"]


def test_dca_base_and_dip_use_one_real_observation_not_fictitious_fills(tmp_path,monkeypatch):
    scenario = DcaScenario(tmp_path,monkeypatch)
    with TestClient(scenario.make_app(driven=True)) as client:
        headers = authorize(client)
        response,url,body = activate_dca(client,scenario,headers)
        assert response.status_code == 200,response.text
        previous = scenario.paper.detail(scenario.session["id"])
        scenario.advance(2)
        worker = scenario.workers[0]
        for reason in ("DCA_BASE","DCA_DIP"):
            assert worker.run_once()["reason"] == reason
        assert worker.run_once()["action"] == "WAIT"
        assert scenario.paper.detail(scenario.session["id"]) == previous
        rows = client.get(url+"/"+body["request_id"]+"/decisions",headers=headers).json()
        assert len(rows) == 2 and rows[0]["market_observation"] == rows[1]["market_observation"]
        assert len(scenario.env["trader"].posts) == 2


@pytest.mark.parametrize("late",[False,True])
def test_candle_not_published_yet_waits_only_within_authorized_window(tmp_path,monkeypatch,late):
    env,workers,app = long_case(tmp_path,monkeypatch)
    original = env["paper"].adapter_factory
    published = {"value":False}
    class Source(original):
        def get_candles(self,*args,**kwargs):
            rows = super().get_candles(*args,**kwargs)
            return rows if published["value"] else rows[:2]
    env["paper"].adapter_factory = Source
    with TestClient(app) as client:
        headers = authorize(client)
        response,_,_ = activate_long(client,env,headers)
        assert response.status_code == 200,response.text
        advance_market(env)
        assert workers[0].run_once()["action"] == "WAIT" and not env["trader"].posts
        if late:
            env["clock"].offset += timedelta(seconds=61)
            with pytest.raises(SpotBotStoreError): workers[0].run_once()
            assert env["trader"].posts == []
        else:
            published["value"] = True
            assert workers[0].run_once()["action"] == "BUY"
            assert len(env["trader"].posts) == 1


@pytest.mark.parametrize("fault",["overlap","missing","future","duplicate","gap","stop"])
def test_real_market_never_creates_an_order_from_incomplete_changed_or_gapped_data(tmp_path,monkeypatch,fault):
    env,workers,app = long_case(tmp_path,monkeypatch)
    original = env["paper"].adapter_factory
    class Source(original):
        def get_candles(self,*args,**kwargs):
            rows = [row.copy() for row in super().get_candles(*args,**kwargs)]
            if fault == "overlap": rows[0]["v"] = "123"
            if fault == "missing": rows = rows[:1]
            if fault == "future": rows[-1]["t"] += 7200
            if fault == "duplicate": rows.append(rows[-1].copy())
            if fault == "stop": workers[0].request_stop()
            return rows
    env["paper"].adapter_factory = Source
    with TestClient(app) as client:
        headers = authorize(client)
        response,_,_ = activate_long(client,env,headers)
        assert response.status_code == 200,response.text
        advance_market(env)
        if fault == "gap": env["clock"].offset += timedelta(hours=1)
        with pytest.raises(SpotBotStoreError): workers[0].run_once()
        assert env["trader"].posts == []
        with workers[0].vault.session() as db:
            assert db.query(RealSpotBotMarketObservation).count() == 0
            assert db.query(RealSpotBotDecision).count() == 0


@pytest.mark.parametrize("tamper",["seed","observation","pointer"])
def test_market_archive_and_decision_integrity_is_checked_in_encrypted_backup(tmp_path,monkeypatch,tamper):
    env,workers,app = long_case(tmp_path,monkeypatch)
    with TestClient(app) as client:
        headers = authorize(client)
        response,url,body = activate_long(client,env,headers)
        assert response.status_code == 200,response.text
        advance_market(env); workers[0].run_once()
        vault = workers[0].vault
        good = vault.backup(tmp_path/"market-good.db")
        copied = RealVault(good); copied.unlock(PASSWORD)
        try:
            with copied.session() as db:
                assert read_market_stream(db,response.json()["policy_id"])[2]["sequence_no"] == 1
        finally: copied.lock()
        with vault.session() as db:
            if tamper == "seed":
                row = db.query(RealSpotBotMarketStream).one(); payload = json.loads(row.seed_json)
                payload["candles"][0]["v"] = "234"; row.seed_json,row.seed_digest = encoded(payload)
            elif tamper == "observation":
                row = db.query(RealSpotBotMarketObservation).one(); payload = json.loads(row.payload_json)
                payload["candle"]["v"] = "234"; row.payload_json,row.payload_digest = encoded(payload)
            else:
                row = db.query(RealSpotBotDecision).one(); payload = json.loads(row.payload_json)
                payload.pop("market_observation"); row.payload_json,row.payload_digest = encoded(payload)
            db.commit()
        with pytest.raises(VaultError,match="bot"):
            vault.backup(tmp_path/"market-invalid.db")
        client.post(url+"/"+body["request_id"]+"/stop",json={"confirmation":"DETENER_BOT"},headers=headers)


def test_v30_migration_preserves_legacy_bots_without_inventing_market_history(tmp_path,monkeypatch):
    from tests.test_mexc_spot_bot import bot_case
    case = bot_case(tmp_path)
    vault = case[0]
    with vault.session() as db:
        previous = bot_record(db,case[8])[1:]
        db.connection().exec_driver_sql("DROP TABLE real_spot_bot_market_observations")
        db.connection().exec_driver_sql("DROP TABLE real_spot_bot_market_streams")
        db.connection().exec_driver_sql("PRAGMA user_version=30"); db.commit()
    vault.lock(); vault.unlock(PASSWORD)
    assert vault.last_migration_backup and vault.last_migration_backup.exists()
    with vault.session() as db:
        assert db.connection().exec_driver_sql("PRAGMA user_version").scalar_one() == SCHEMA_VERSION
        assert db.query(RealSpotBotMarketStream).count() == db.query(RealSpotBotMarketObservation).count() == 0
        assert bot_record(db,case[8])[1:] == previous and db.query(RealSpotBot).count() == 1
    vault.lock()


def test_two_vault_instances_cannot_append_competing_candles_to_same_cycle(tmp_path,monkeypatch):
    env,workers,app = long_case(tmp_path,monkeypatch)
    with TestClient(app) as client:
        headers = authorize(client)
        response,_,body = activate_long(client,env,headers)
        assert response.status_code == 200,response.text
        vault = workers[0].vault
        other = RealVault(vault.path); other.unlock(PASSWORD)
        advance_market(env)
        claim,_,state = claim_spot_bot_cycle(vault,body["request_id"],now=env["clock"]())
        barrier = Barrier(2)
        def candles(*args,**kwargs):
            result = env["paper"].adapter_factory().get_candles(*args,**kwargs)
            barrier.wait(timeout=5)
            return result
        def observe(instance):
            try:
                return observe_bot_market(instance,body["request_id"],claim,candles=candles,clock=env["clock"],assert_active=lambda:None)
            except SpotBotStoreError: return None
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(observe,(vault,other)))
            assert sum(item is not None for item in outcomes) == 1
            with vault.session() as db:
                assert db.query(RealSpotBotMarketObservation).count() == 1
                assert read_market_stream(db,response.json()["policy_id"])[2]["sequence_no"] == 1
            finish_spot_bot_cycle(vault,body["request_id"],claim,state,now=env["clock"](),pause=True)
            assert not env["trader"].posts
        finally: other.lock()


def test_error_after_archiving_a_candle_keeps_its_durable_head_and_pauses(tmp_path,monkeypatch):
    from app.live_execution import spot_bot_market
    env,workers,app = long_case(tmp_path,monkeypatch)
    with TestClient(app) as client:
        headers = authorize(client)
        response,_,body = activate_long(client,env,headers)
        assert response.status_code == 200,response.text
        advance_market(env)
        def failed_evaluation(*_): raise RuntimeError("fixture failed after market commit")
        monkeypatch.setattr(spot_bot_market,"_observation",failed_evaluation)
        with pytest.raises(RuntimeError,match="after market commit"): workers[0].run_once()
        with workers[0].vault.session() as db:
            bot,_,state = bot_record(db,body["request_id"])
            assert bot.status == "PAUSED" and bot.cycle_id is None
            assert state["market_heads"][response.json()["policy_id"]]["sequence_no"] == 1
            assert read_market_stream(db,response.json()["policy_id"])[2]["sequence_no"] == 1
        assert not env["trader"].posts


def test_backup_rejects_missing_seed_even_before_first_order(tmp_path,monkeypatch):
    env,workers,app = long_case(tmp_path,monkeypatch)
    with TestClient(app) as client:
        headers = authorize(client)
        response,_,_ = activate_long(client,env,headers)
        assert response.status_code == 200,response.text
        vault = workers[0].vault
        with vault.session() as db:
            db.query(RealSpotBotMarketStream).delete(); db.commit()
        with pytest.raises(VaultError,match="bot evidence"):
            vault.backup(tmp_path/"missing-market-seed.db")
        assert not env["trader"].posts
        locked = client.post("/real/vault/lock",headers=headers)
        assert locked.status_code == 200 and locked.json() == {"locked":True,"review_required":True}
        assert not vault.unlocked and workers[0].state()["stop_requested"]
        assert client.get("/real/accounts",headers=headers).status_code == 401
