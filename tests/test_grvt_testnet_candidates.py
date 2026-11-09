"""Lectura local de candidatas: no firma, no autoriza, no hereda capital PAPER."""

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.derivatives.paper_access import paper_perp_access
from app.derivatives.paper_migration import encrypt_active_paper_perp
from app.live_execution.grvt_testnet_signal import list_testnet_signal_candidates
from app.portfolio.api import create_real_router
from tests.test_signal_monitor import harness, START, STEP, definition


def source(tmp_path, direction="SHORT"):
    monitor, _, state, paper, lab, account, dataset, strategy, _ = harness(tmp_path)
    if direction == "LONG":
        strategy = lab.create_strategy("Long causal", definition("LONG"))
    session = monitor.create(account["id"], strategy["version_id"], dataset["id"], "AUTO_PAPER", "1", "2")
    state["now"] = START + 6 * STEP + 10
    monitor.poll(session["id"])
    return monitor, state, paper, lab, session


@pytest.mark.parametrize("direction,side", [("SHORT", "SELL"), ("LONG", "BUY")])
def test_candidates_derive_side_without_copying_paper_capital(tmp_path, direction, side):
    _, state, paper, _, session = source(tmp_path, direction)
    now = state["now"] * 1_000_000_000
    items = list_testnet_signal_candidates(paper, now_ns=now)
    assert len(items) == 1
    item = items[0]
    assert item["session_id"] == session["id"]
    assert item["side"] == side and item["reduce_only"] is False
    assert item["direction"] == direction and item["candidate"] is True
    assert item["expires_at_ms"] == (now + 20_000_000_000) // 1_000_000
    assert not {"auto_size", "size", "balance", "initial_balance", "auto_leverage"} & item.keys()
    assert list_testnet_signal_candidates(paper, now_ns=now + 20_000_000_001)[0]["reason"] == "EXPIRED"
    assert not list_testnet_signal_candidates(paper, now_ns=now - 1)[0]["candidate"]


def test_candidates_latest_action_only_and_stopped_position(tmp_path):
    monitor, state, paper, _, session = source(tmp_path)
    now = state["now"] * 1_000_000_000
    monitor.stop(session["id"])
    assert list_testnet_signal_candidates(paper, now_ns=now)[0]["reason"] == "STOPPED"
    with paper.connect() as db:
        db.execute("UPDATE paper_signal_sessions SET status='ACTIVE' WHERE id=?", (session["id"],))
        db.execute("UPDATE paper_perp_positions SET closed_at_ns=? WHERE origin_session_id=?", (now, session["id"]))
    assert list_testnet_signal_candidates(paper, now_ns=now)[0]["reason"] == "POSITION_CHANGED"
    with paper.connect() as db:
        db.execute("UPDATE paper_signal_actions SET action='CLOSE' WHERE session_id=?", (session["id"],))
    item = list_testnet_signal_candidates(paper, now_ns=now)[0]
    assert item["action"] == "CLOSE" and item["side"] == "BUY" and item["reduce_only"] is True
    with paper.connect() as db:
        db.execute("UPDATE paper_signal_actions SET status='PENDING' WHERE session_id=?", (session["id"],))
    assert list_testnet_signal_candidates(paper, now_ns=now) == []


def test_candidate_selector_does_not_claim_full_replay_verified(tmp_path):
    _, state, paper, _, session = source(tmp_path)
    with paper.connect() as db:
        db.execute("UPDATE paper_signal_actions SET digest='tampered' WHERE session_id=?", (session["id"],))
    item = list_testnet_signal_candidates(paper, now_ns=state["now"] * 1_000_000_000)[0]
    assert item["candidate"] and item["reason"] == "REVALIDATE_ON_SUBMIT"
    assert "verified" not in item  # El envío ya prueba digest/replay de forma autoritativa.
    with paper.connect() as db:
        db.execute("UPDATE paper_signal_sessions SET definition_json=? WHERE id=?", (json.dumps({}), session["id"]))
    assert list_testnet_signal_candidates(paper, now_ns=state["now"] * 1_000_000_000) == []


def test_candidates_api_requires_both_vaults_without_network_or_writes(tmp_path):
    _, _, paper, _, _ = source(tmp_path)
    encrypt_active_paper_perp(paper, "CIFRAR GRVT PAPER", "paper test passphrase")
    paper_perp_access.unlock(paper.path, "paper test passphrase")
    app = FastAPI()
    app.include_router(create_real_router(tmp_path / "real.db", paper_perp_path=paper.path,
        lab_path=tmp_path / "lab.db", grvt_testnet_factory=lambda *_: pytest.fail("external network")))
    client = TestClient(app)
    url = "/real/exchanges/grvt/testnet/signals/candidates"
    assert client.get(url).status_code == 401
    token = client.post("/real/vault/create", json={"passphrase":"local vault test passphrase"}).json()["token"]
    headers = {"X-Orbis-Vault-Token":token, "X-Orbis-Passive":"1"}
    response = client.get(url, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["network"] == "TESTNET" and response.json()["authorization"] is False
    assert response.json()["items"][0]["reason"] == "EXPIRED"
    assert client.get(url, headers={**headers,"Origin":"https://foreign.invalid"}).status_code == 403
    paper_perp_access.lock(paper.path)
    assert client.get(url, headers=headers).status_code == 409


def test_candidates_clock_is_strict_and_missing_store_is_not_created(tmp_path):
    from app.derivatives.paper_store import PaperPerpStore
    from app.derivatives.paper_math import PaperPerpError
    from app.live_execution.grvt_testnet_signal import GrvtTestnetSignalError
    paper = PaperPerpStore(tmp_path / "absent.db", allow_create=False)
    for clock in (True, 0, -1, 12.5):
        with pytest.raises(GrvtTestnetSignalError):
            list_testnet_signal_candidates(paper, now_ns=clock)
    with pytest.raises(PaperPerpError):
        list_testnet_signal_candidates(paper, now_ns=1)
    assert not paper.path.exists()
