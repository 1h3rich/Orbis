"""API → PAPER/LAB cifrados → firma verificada → riesgo → único POST ficticio."""

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app.portfolio.models import GrvtTestnetOrderIntent, GrvtTestnetPreflightEvidence
from app.portfolio.vault import RealVault

from tests.grvt_testnet_ui_fixture import GrvtTestnetScenario, PASSPHRASE, KEY


@pytest.mark.parametrize("direction,lost,closing", [("LONG",False,False),("SHORT",False,False),("SHORT",True,False),
                                                   ("LONG",False,True),("SHORT",False,True)])
def test_signed_ui_contract_survives_lost_reply_and_reopen(tmp_path, direction, lost, closing):
    scenario = GrvtTestnetScenario(tmp_path)
    source = scenario.seed(direction,closing=closing)
    scenario.lose_response = lost
    client = TestClient(scenario.make_app())
    token = client.post("/real/vault/create",json={"passphrase":PASSPHRASE}).json()["token"]
    headers = {"X-Orbis-Vault-Token":token}
    prefix = "/real/exchanges/grvt/testnet"
    candidates = client.get(prefix + "/signals/candidates",headers=headers)
    assert candidates.status_code == 200, candidates.text
    assert candidates.json()["items"][0]["candidate"] is True
    body = scenario.body(source)
    result = client.post(prefix + "/signals/sign-and-submit",json=body,headers=headers)
    assert result.status_code == (422 if lost else 200), result.text
    assert len(scenario.orders) == 1
    assert scenario.orders[0] == {"id":source["client_order_id"],"side":body["side"],"reduce_only":closing,"chain_id":"326"}
    assert KEY not in result.text and body["api_key"] not in result.text
    calls = len(scenario.calls)
    assert client.post(prefix + "/signals/sign-and-submit",json=body,headers=headers).status_code == 409
    assert len(scenario.calls) == calls
    report = client.get(prefix + "/recovery",params={"sub_account_id":source["sub_account_id"]},headers=headers).json()
    item = report["items"][0]
    assert item["client_order_id"] == source["client_order_id"]
    assert item["journal_status"] == ("POST_CLAIMED" if lost else "ACKNOWLEDGED")
    assert item["signal_source"]["direction"] == direction
    assert item["signal_source"]["session_id"] == source["session_id"]
    assert item["price_guard"]["source"] == "GRVT_TESTNET_PUBLIC_TICKER"
    assert report["can_submit"] is False
    assert client.post("/real/vault/lock",headers=headers).status_code == 200
    reopened = TestClient(scenario.make_app())
    token = reopened.post("/real/vault/unlock",json={"passphrase":PASSPHRASE}).json()["token"]
    assert reopened.post(prefix + "/signals/sign-and-submit",json=body,headers={"X-Orbis-Vault-Token":token}).status_code == 409
    assert len(scenario.calls) == calls
    assert (tmp_path / "real.db").read_bytes()[:16] != b"SQLite format 3\x00"
    # Comprobar el contenido descifrado, no sólo la cabecera cifrada del archivo.
    vault = RealVault(tmp_path / "real.db")
    vault.unlock(PASSPHRASE)
    with vault.session() as db:
        journal = str(db.execute(select(GrvtTestnetOrderIntent.__table__)).all())
        evidence = str(db.execute(select(GrvtTestnetPreflightEvidence.__table__)).all())
    vault.lock()
    assert KEY not in journal + evidence and KEY[2:] not in journal + evidence
    assert body["api_key"] not in journal + evidence
    assert "fake-testnet-cookie" not in journal + evidence


@pytest.mark.parametrize("change", ["side","expired","tampered","position","stopped","key"])
def test_invalid_source_or_signature_never_posts(tmp_path, change):
    scenario = GrvtTestnetScenario(tmp_path)
    source = scenario.seed("SHORT")
    client = TestClient(scenario.make_app())
    token = client.post("/real/vault/create",json={"passphrase":PASSPHRASE}).json()["token"]
    body = scenario.body(source)
    if change == "side": body["side"] = "BUY"
    elif change == "key": body["expected_signer"] = "0x" + "b" * 40
    else:
        with scenario.paper.connect() as db:
            if change == "expired":
                db.execute("UPDATE paper_signal_actions SET recorded_at_ns=recorded_at_ns-21000000000")
            elif change == "tampered": db.execute("UPDATE paper_signal_actions SET digest='tampered'")
            elif change == "position": db.execute("UPDATE paper_perp_positions SET closed_at_ns=1")
            elif change == "stopped": db.execute("UPDATE paper_signal_sessions SET status='STOPPED'")
    response = client.post("/real/exchanges/grvt/testnet/signals/sign-and-submit",json=body,headers={"X-Orbis-Vault-Token":token})
    assert response.status_code in (409,422), response.text
    assert scenario.orders == []
    assert KEY not in response.text


@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_scenario_creates_a_causal_entry_when_setup_crosses_an_hour(tmp_path,monkeypatch,direction):
    import time
    boundary = 1_780_002_000  # Límite horario fijo de un proveedor sintético.
    assert boundary % 3600 == 0
    clock = [boundary-1]
    monkeypatch.setattr(time,'time',lambda:clock[0])
    scenario = GrvtTestnetScenario(tmp_path)
    clock[0] = boundary
    source = scenario.seed(direction)
    actions = scenario.monitor.detail(source['session_id'])['actions']
    assert len(actions) == 1 and actions[0]['action'] == 'OPEN'
    assert actions[0]['status'] == 'EXECUTED' and scenario.orders == []


@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_closing_seed_uses_a_single_risk_observation_time_after_delayed_setup(tmp_path,monkeypatch,direction):
    import time
    scenario=GrvtTestnetScenario(tmp_path)
    observed=time.time_ns()+3_000_000_000
    monkeypatch.setattr(time,'time_ns',lambda:observed)
    source=scenario.seed(direction,closing=True)
    actions=scenario.monitor.detail(source['session_id'])['actions']
    assert [action['action'] for action in actions]==['OPEN','CLOSE']
    assert all(action['status']=='EXECUTED' for action in actions)
    assert scenario.orders==[]
