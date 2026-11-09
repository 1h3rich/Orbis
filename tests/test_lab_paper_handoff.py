"""Un backtest verificado conserva la versión exacta al preparar PAPER."""

from datetime import datetime, timezone
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.lab.api import create_lab_router
from app.lab.engine import validate_candles
from app.lab.store import LabStore
from app.lab import api as lab_api


START = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())


def candles():
    raw = [{"t": START + i * 3600, "o": str(100 + i),
            "h": str(101 + i), "l": str(99 + i),
            "c": str(100 + i), "v": "10"} for i in range(8)]
    return validate_candles(raw, "1h", now=START + 20 * 3600)[0]


def strategy(direction):
    return {"direction": direction, "initial_capital": "1000", "allocation": "1",
            "fee_rate": "0", "slippage_rate": "0", "stop_loss": "0.1",
            "take_profit": "0.2", "entry": {"kind": "CLOSE_ABOVE_EMA", "period": 2}}


@pytest.mark.parametrize("source,symbol,direction,verified_public,venue", [
    ("mexc-spot-public", "BTC/USDT", "LONG", True, "SPOT"),
    ("grvt-perp-trade-public", "BTC/USDT:PERP", "SHORT", True, "GRVT"),
    ("binance-spot-public", "BTC/USDT", "LONG", True, None),
    ("mexc-spot-public", "BTC/USDT", "SHORT", True, None),
    ("mexc-spot-public", "BTC/USDT", "LONG", False, None),
])
def test_verified_run_targets_only_compatible_paper_with_exact_old_version(
        tmp_path, source, symbol, direction, verified_public, venue):
    path = tmp_path / "lab.db"
    store = LabStore(path)
    dataset = store.save_dataset(symbol, "1h", source, candles(), [], verified_public=verified_public)
    original = store.create_strategy("Versión de origen", strategy(direction))
    newer = store.add_version(original["id"], strategy("SHORT" if direction == "LONG" else "LONG"))
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)
    created = client.post("/lab/runs", json={"dataset_id": dataset["id"],
                                            "version_id": original["version_id"]})
    assert created.status_code == 200, created.text
    checked = client.get(f"/lab/runs/{created.json()['id']}/verify")
    assert checked.status_code == 200 and checked.json()["verified"] is True
    target = checked.json()["paper_target"]
    if venue:
        assert target == {"venue": venue, "run_id": created.json()["id"], "dataset_id": dataset["id"],
                          "strategy_id": original["id"], "version_id": original["version_id"]}
    else:
        assert target is None
    # Opening a verified result already repeats the original calculation.
    # The next step must be available without a second verification click.
    opened = client.get(f"/lab/runs/{created.json()['id']}?view=chart&require_verified=true")
    assert opened.status_code == 200 and opened.json()["verified"] is True
    assert opened.json()["paper_target"] == target
    unverified = client.get(f"/lab/runs/{created.json()['id']}")
    assert "paper_target" not in unverified.json()
    versions = client.get("/lab/strategy-versions")
    assert versions.status_code == 200
    assert [(item["id"], item["version"]) for item in versions.json()] == [
        (original["version_id"], 1), (newer["version_id"], 2),
    ]


@pytest.mark.parametrize('changed',['strategy','dataset_source'])
def test_verified_view_rechecks_handoff_inputs_after_replay(tmp_path,monkeypatch,changed):
    store=LabStore(tmp_path/'lab.db')
    dataset=store.save_dataset('BTC/USDT','1h','mexc-spot-public',candles(),[],verified_public=True)
    original=store.create_strategy('Origen',strategy('LONG'))
    app=FastAPI();app.include_router(create_lab_router(store.path));client=TestClient(app)
    response=client.post('/lab/runs',json={'dataset_id':dataset['id'],'version_id':original['version_id']})
    assert response.status_code==200,response.text
    verify=lab_api.verify_backtest_run;calls=[]
    def race(current,saved):
        checked=verify(current,saved);assert checked['verified'];calls.append(saved['id'])
        with store.connect() as db:
            if changed=='strategy':
                db.execute('UPDATE strategy_versions SET definition_json=? WHERE id=?',
                           (json.dumps({**original['definition'],'take_profit':'0.3'}),original['version_id']))
            else:db.execute("UPDATE datasets SET source='binance-spot-public' WHERE id=?",(dataset['id'],))
        return checked
    monkeypatch.setattr(lab_api,'verify_backtest_run',race)
    opened=client.get('/lab/runs/'+response.json()['id']+'?view=chart&require_verified=true')
    assert opened.status_code==409,opened.text
    assert 'paper_target' not in opened.json()
    assert calls==[response.json()['id']]
