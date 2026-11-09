"""Lecturas acotadas de linajes sin cambiar permiso, posición ni autoridad."""

from contextlib import contextmanager
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine
import json
import pytest

from app.live_execution.spot_bot_market import read_market_stream
from app.live_execution.spot_bot_store import bot_record,encoded,SpotBotStoreError
from app.portfolio.models import RealSpotBotMarketStream
from app.portfolio.vault import RealVault,VaultError
from sqlalchemy.exc import OperationalError
from tests.test_mexc_execution_state import PASSWORD

from tests.mexc_continuity_fixture import make_dca_continuity_environment
from tests.test_mexc_bot_api import authorize
from tests.test_mexc_dca_bot import activate


@contextmanager
def select_work():
    count = {"selects":0}
    def observed(connection,cursor,statement,parameters,context,many):
        if statement.lstrip().upper().startswith("SELECT"): count["selects"] += 1
    event.listen(Engine,"before_cursor_execute",observed)
    try: yield count
    finally: event.remove(Engine,"before_cursor_execute",observed)


def test_status_read_work_is_bounded_by_lineage_not_repeated_ancestor_histories(tmp_path,monkeypatch):
    scenario = make_dca_continuity_environment(tmp_path,monkeypatch)
    with TestClient(scenario.make_app(driven=True)) as client:
        headers = authorize(client)
        response,url,_ = activate(client,scenario,headers,execution_mode="CONTINUOUS",
            confirmation="ACTIVAR_BOT_CONTINUO",continuous_until=(scenario.clock()+timedelta(days=30)).isoformat())
        assert response.status_code == 200,response.text
        worker = scenario.workers[0]; samples = []
        assert worker.run_once()["action"] == "WAIT"
        for half_day in range(2,10):
            scenario.advance(1+half_day//2,seconds=(half_day%2)*43200+5+(half_day-2)*60)
            assert worker.run_once()["action"] in ("WAIT","BUY")
            if half_day in (4,9):
                with select_work() as work:
                    listed = client.get(url,headers=headers)
                assert listed.status_code == 200,listed.text
                samples.append((listed.json()[0],work["selects"]))
        before,after = samples
        assert before[0]["position"] == after[0]["position"]
        assert before[0]["execution_permission"] == after[0]["execution_permission"]
        assert before[0]["renewals"] == 3 and after[0]["renewals"] == 8
        # 2,7 veces más permisos: un margen de 3x admite trabajo lineal y
        # coste fijo, pero no volver a validar todo el historial por antecesor.
        assert after[1] <= 3*before[1],(before[1],after[1])


def test_a_new_read_inside_the_same_transaction_rechecks_changed_bot_terms(tmp_path,monkeypatch):
    scenario = make_dca_continuity_environment(tmp_path,monkeypatch)
    with TestClient(scenario.make_app(driven=True)) as client:
        headers = authorize(client)
        response,url,body = activate(client,scenario,headers,execution_mode="CONTINUOUS",
            confirmation="ACTIVAR_BOT_CONTINUO",continuous_until=(scenario.clock()+timedelta(days=30)).isoformat())
        assert response.status_code == 200,response.text
        worker = scenario.workers[0]
        scenario.advance(2)
        assert worker.run_once()["action"] == "BUY"
        current = client.get(url,headers=headers).json()[0]
        with worker.vault.session() as db:
            _,_,pointer = read_market_stream(db,current["policy_id"])
            assert pointer["policy_id"] == current["policy_id"]
            row,config,_ = bot_record(db,body["request_id"])
            config["entry_budget_quote"] = "21"
            row.config_json,row.config_digest = encoded(config)
            db.flush()
            with pytest.raises(SpotBotStoreError): read_market_stream(db,current["policy_id"])
            db.rollback()
        assert client.get(url,headers=headers).json()[0]["entry_budget_quote"] == "20"


def test_warm_status_does_not_hide_rehashed_market_changes_in_later_requests(tmp_path,monkeypatch):
    scenario = make_dca_continuity_environment(tmp_path,monkeypatch)
    with TestClient(scenario.make_app(driven=True)) as client:
        headers = authorize(client)
        response,url,body = activate(client,scenario,headers,execution_mode="CONTINUOUS",
            confirmation="ACTIVAR_BOT_CONTINUO",continuous_until=(scenario.clock()+timedelta(days=30)).isoformat())
        assert response.status_code == 200,response.text
        worker = scenario.workers[0]
        scenario.advance(2)
        assert worker.run_once()["action"] == "BUY"
        current = client.get(url,headers=headers).json()[0]
        assert current["market_observation"]["verified"]
        with worker.vault.session() as db:
            row,config,state = bot_record(db,body["request_id"])
            stream = db.get(RealSpotBotMarketStream,current["policy_id"])
            seed = json.loads(stream.seed_json)
            seed["candles"][-1]["h"] = str(int(seed["candles"][-1]["h"])+1)
            stream.seed_json,stream.seed_digest = encoded(seed)
            config["market_stream_seeds"][current["policy_id"]] = stream.seed_digest
            state["market_heads"][current["policy_id"]]["digest"] = stream.seed_digest
            row.config_json,row.config_digest = encoded(config)
            row.state_json,row.state_digest = encoded(state); db.commit()
        assert not client.get(url,headers=headers).json()[0]["market_observation"]["verified"]
        with pytest.raises(SpotBotStoreError): worker.run_once()
        with pytest.raises(VaultError,match="REAL bot.*evidence"): worker.vault.backup(tmp_path/"changed-after-read.db")
        assert len(scenario.env["trader"].posts) == 1


def test_stream_read_does_not_commit_the_callers_pending_write(tmp_path,monkeypatch):
    scenario = make_dca_continuity_environment(tmp_path,monkeypatch)
    with TestClient(scenario.make_app(driven=True)) as client:
        headers = authorize(client)
        response,url,body = activate(client,scenario,headers,execution_mode="CONTINUOUS",
            confirmation="ACTIVAR_BOT_CONTINUO",continuous_until=(scenario.clock()+timedelta(days=30)).isoformat())
        assert response.status_code == 200,response.text
        worker = scenario.workers[0]
        with worker.vault.session() as db:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            row,_,state = bot_record(db,body["request_id"])
            state["reason"] = "uncommitted-lineage-read"
            row.state_json,row.state_digest = encoded(state); db.flush()
            assert read_market_stream(db,response.json()["policy_id"])[2]["sequence_no"] == 0
            db.rollback()
        assert client.get(url,headers=headers).json()[0]["reason"] != "uncommitted-lineage-read"


def test_sql_snapshot_blocks_a_competing_commit_then_next_request_sees_it(tmp_path,monkeypatch):
    scenario = make_dca_continuity_environment(tmp_path,monkeypatch)
    with TestClient(scenario.make_app(driven=True)) as client:
        headers = authorize(client)
        response,url,body = activate(client,scenario,headers,execution_mode="CONTINUOUS",
            confirmation="ACTIVAR_BOT_CONTINUO",continuous_until=(scenario.clock()+timedelta(days=30)).isoformat())
        assert response.status_code == 200,response.text
        worker = scenario.workers[0]
        competitor = RealVault(worker.vault.path); competitor.unlock(PASSWORD)
        try:
            with worker.vault.session() as reader:
                read_market_stream(reader,response.json()["policy_id"])
                with competitor.session() as writer:
                    writer.connection().exec_driver_sql("PRAGMA busy_timeout=0")
                    row,_,state = bot_record(writer,body["request_id"])
                    state["reason"] = "concurrent-lineage-read"
                    row.state_json,row.state_digest = encoded(state)
                    with pytest.raises(OperationalError,match="locked"): writer.commit()
                    writer.rollback()
                assert bot_record(reader,body["request_id"])[2]["reason"] != "concurrent-lineage-read"
            with competitor.session() as writer:
                row,_,state = bot_record(writer,body["request_id"])
                state["reason"] = "concurrent-lineage-read"
                row.state_json,row.state_digest = encoded(state); writer.commit()
            assert client.get(url,headers=headers).json()[0]["reason"] == "concurrent-lineage-read"
            assert not scenario.env["trader"].posts
        finally: competitor.lock()
