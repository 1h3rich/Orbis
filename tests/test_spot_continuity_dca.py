"""Una renovación diaria no vuelve a conceder el dinero ya gastado por DCA."""

from datetime import timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from app.live_execution.spot_bot_store import bot_record,owned_spot_bot_ledger
from tests.mexc_dca_fixture import DcaScenario
from tests.test_mexc_bot_api import authorize
from tests.test_mexc_dca_bot import activate


def test_dca_renews_after_spending_without_resetting_caps_reference_or_triggers(tmp_path,monkeypatch):
    scenario = DcaScenario(tmp_path,monkeypatch)
    with TestClient(scenario.make_app(driven=True)) as client:
        headers = authorize(client)
        response,url,body = activate(client,scenario,headers,execution_mode="CONTINUOUS",
            confirmation="ACTIVAR_BOT_CONTINUO",continuous_until=(scenario.clock()+timedelta(days=3)).isoformat())
        assert response.status_code == 200,response.text
        worker = scenario.workers[-1]
        assert worker.run_once()["action"] == "WAIT"
        scenario.advance(2)
        for reason in ("DCA_BASE","DCA_DIP"):
            result = worker.run_once()
            assert result["action"] == "BUY" and result["reason"] == reason,result
        before = client.get(url,headers=headers).json()[0]
        ledger = owned_spot_bot_ledger(worker.vault,body["request_id"])
        scenario.advance(2,seconds=12*3600+5)
        result = worker.run_once()
        assert result["action"] == "WAIT",result
        after = client.get(url,headers=headers).json()[0]
        assert after["policy_id"] != before["policy_id"]
        assert after["execution_permission"] == before["execution_permission"]
        assert after["dca_limits"] == before["dca_limits"]
        assert owned_spot_bot_ledger(worker.vault,body["request_id"]) == ledger
        assert Decimal(after["position"]["cost_quote"]) <= Decimal("40")
        with worker.vault.session() as db:
            _,_,state = bot_record(db,body["request_id"])
            assert state["dca_reference"] == "65000"
        scenario.advance(3)
        assert worker.run_once()["action"] == "WAIT"
        assert len(scenario.env["trader"].posts) == 2
        worker.vault.backup(tmp_path/"continuous-dca.db")
