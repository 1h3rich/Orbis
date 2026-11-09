"""Cohort downloads use the normal LAB replay and one chronological cutoff."""
import json
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.lab.api import create_lab_router
from app.lab.engine import validate_candles
from app.lab.store import LabStore, content_hash


START = 1_700_006_400
DAY = 86_400
SOURCE = "etiqueta privada del archivo importado"


def _client(path):
    app = FastAPI()
    app.include_router(create_lab_router(path))
    return TestClient(app)


def _run(store, client, *, symbol="BTC/USDT", start=0, count=63, price_offset=0):
    candles, gaps = validate_candles([
        {"t":START+i*DAY, "o":str(1000-i+price_offset), "h":str(1001-i+price_offset),
         "l":str(998-i+price_offset), "c":str(999-i+price_offset), "v":str(10+i)}
        for i in range(start,start+count)
    ], "1d", now=START+(start+count+1)*DAY)
    dataset = store.save_dataset(symbol,"1d",SOURCE,candles,gaps)
    response = client.post("/lab/research/runs",json={
        "dataset_id":dataset["id"],"rule":{"kind":"BEARISH_STREAK","period":3}})
    assert response.is_success,response.text
    return response.json()["id"]


def test_cohort_purges_at_utc_boundary_and_deduplicates_with_provenance(tmp_path):
    path = tmp_path/"lab.db"
    store, client = LabStore(path), _client(path)
    first = _run(store,client)
    second = _run(store,client,start=16,count=47)
    third = _run(store,client,symbol="ETH/USDT")
    boundary = START+33*DAY
    body = {"run_ids":[first,second,third],"boundary_open_t":boundary}
    response = client.post("/lab/research/export-cohort",json=body)
    assert response.is_success,response.text
    assert response.headers["cache-control"] == "no-store"
    data = response.json()
    assert data["schema"] == "orbis.lab.research.cohort.v1"
    assert data["environment"] == "LAB" and data["theoretical_only"] is True
    assert data["boundary_open_t"] == boundary and data["horizon_seconds"] == 7*DAY
    assert set(data["sources"]) == {first,second,third}
    assert len(data["series"]) == 2 and len(data["rules"]) == 1
    assert SOURCE not in json.dumps(data)
    assert data["content_sha256"] == content_hash({key:value for key,value in data.items() if key!="content_sha256"})
    counts = data["counts"]
    assert counts["input_cases"] == sum(value for key,value in counts.items() if key!="input_cases")
    assert all(counts[key]>0 for key in ("purged_boundary","incomplete","duplicates","development","validation"))
    assert counts["development"] == len(data["development"])
    assert counts["validation"] == len(data["validation"])
    assert all(row["labels_known_at_t"]<boundary for row in data["development"])
    assert all(row["features_at_close"]["signal_time"]>=boundary for row in data["validation"])
    rows = data["development"] + data["validation"]
    assert all(row["labels_known_at_t"]-row["signal_known_at_t"]==7*DAY for row in rows)
    assert any(row["run_ids"]==sorted([first,second]) for row in rows)
    assert not any("future_labels" in row["features_at_close"] for row in rows)
    assert client.post("/lab/research/export-cohort",json={**body,"run_ids":[third,second,first]}).json() == data
    assert _client(path).post("/lab/research/export-cohort",json=body).json() == data


def test_cohort_refuses_conflicting_rows_empty_split_and_invalid_inputs(tmp_path):
    path = tmp_path/"lab.db"
    store, client = LabStore(path), _client(path)
    first = _run(store,client)
    changed = _run(store,client,price_offset=100)
    body = {"run_ids":[first,changed],"boundary_open_t":START+33*DAY}
    failed = client.post("/lab/research/export-cohort",json=body)
    assert failed.status_code == 422 and "discrepan" in failed.json()["detail"]
    assert client.post("/lab/research/export-cohort",json={**body,"run_ids":[first,first]}).status_code == 422
    assert client.post("/lab/research/export-cohort",json={**body,"run_ids":[first,"absent"]}).status_code == 404
    assert client.post("/lab/research/export-cohort",json={**body,"boundary_open_t":"1000"}).status_code == 422
    response = client.post("/lab/research/export-cohort",json={**body,"boundary_open_t":START+500*DAY})
    assert response.status_code == 422 and "discrepan" in response.json()["detail"]
    good = _run(store,client,symbol="ETH/USDT")
    response = client.post("/lab/research/export-cohort",json={**body,"run_ids":[first,good],"boundary_open_t":START+500*DAY})
    assert response.status_code == 422 and "ambos lados" in response.json()["detail"]
    assert client.post("/lab/research/export-cohort",json=body,
                       headers={"Origin":"https://external.invalid"}).status_code == 403


def test_cohort_replays_every_source_before_returning_data(tmp_path):
    path = tmp_path/"lab.db"
    store, client = LabStore(path), _client(path)
    first = _run(store,client)
    tampered = _run(store,client,symbol="ETH/USDT")
    with sqlite3.connect(path) as db:
        db.execute("UPDATE research_events SET event_json=? WHERE run_id=? AND index_no=0",('{"signal_time":0}',tampered))
    response = client.post("/lab/research/export-cohort",json={
        "run_ids":[first,tampered],"boundary_open_t":START+33*DAY})
    assert response.status_code == 409
    assert set(response.json()) == {"detail"}
