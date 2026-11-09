"""Preview, explicit consent and encrypted provenance for two LAB runs."""

import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.lab import api as lab_api
from app.lab.access import lab_access
from app.lab.ai_research import COMPARISON_SCHEMA, MODEL, analyze, evidence_digest
from app.lab.store import LabStore
from tests.test_lab_research import rows
from tests.test_spot_strategy_paper import definition

PATH = "/lab/runs/compare"
KEY = "sk-owned-comparison-key"
PASSWORD = "Owned comparison encryption password"


@pytest.fixture
def lab(tmp_path):
    path = tmp_path / "lab.db"
    app = FastAPI()
    app.include_router(lab_api.create_lab_router(path))
    with TestClient(app) as api:
        created = api.post("/lab/create", json={"passphrase": PASSWORD})
        assert created.is_success, created.text
        try:
            yield api, path
        finally:
            lab_access.lock(path)


def sample(api, *, alternate_history=False, same_parameters=False):
    def post(path, payload):
        response = api.post("/lab" + path, json=payload)
        assert response.is_success, response.text
        return response.json()

    primary = post("/datasets", {"symbol": "BTC/USDT", "timeframe": "1h",
                                 "source": "origin omitted from AI", "candles": rows()})
    alternative = (post("/datasets", {"symbol": "ETH/USDT", "timeframe": "1h",
                                      "source": "another omitted origin", "candles": rows()[:-1]})
                   if alternate_history else primary)
    base_strategy = definition()
    right_strategy = base_strategy if same_parameters else {**base_strategy, "max_holding_bars": 2}
    samples = []
    for source, strategy, name in ((primary, base_strategy, "Hidden base name"),
                                   (alternative, right_strategy, "Hidden alternative name")):
        version = post("/strategies", {"name": name, "definition": strategy})
        samples.append(post("/runs", {"dataset_id": source["id"], "version_id": version["version_id"]}))
    return {"left_id": samples[0]["id"], "right_id": samples[1]["id"]}, samples


def request(pair, preview, *, save=True):
    return {**pair, "confirmation": "ENVIAR COMPARACIÓN A OPENAI",
            "question": "¿Qué hipótesis podemos contrastar?",
            "api_key": KEY, "evidence_sha256": preview["evidence_sha256"], "save_locally": save}


def fake_answer(evidence, question, key):
    assert key == KEY
    return {"answer": "La comparación es exploratoria.", "model": MODEL,
            "evidence_sha256": evidence_digest(evidence), "usage": {"output_tokens": 9}}


def test_two_verified_runs_are_minimized_compared_archived_and_exported(lab, monkeypatch):
    api, path = lab
    pair, runs = sample(api)
    comparison = api.post(PATH, json=pair).json()
    sent = []
    def provider(*args):
        sent.append(args)
        return fake_answer(*args)
    monkeypatch.setattr(lab_api, "analyze_research", provider)

    response = api.post(PATH + "/ai-preview", json=pair)
    assert response.is_success and response.headers["Cache-Control"] == "no-store"
    preview = response.json()
    evidence = preview["evidence"]
    assert not sent and preview["archive_available"] and preview["external_destination"] == "OpenAI API"
    assert evidence["schema"] == COMPARISON_SCHEMA
    assert evidence["environment"] == "LAB" and evidence["theoretical_only"]
    assert evidence["source_run"] == pair["left_id"]
    assert [source["role"] for source in evidence["runs"]] == ["base", "alternative"]
    for source, run in zip(evidence["runs"], runs, strict=True):
        assert source["run_id"] == run["id"] and source["result_hash"] == run["result_hash"]
        assert source["strategy_hash"] == run["strategy_hash"] and source["metrics"] == run["metrics"]
        assert set(source["dataset"]) == {"symbol", "timeframe", "start_t", "end_t", "count", "hash"}
        assert not {"events", "trades", "signals", "equity", "candles", "account", "wallet"} & source.keys()
    assert not any(name in json.dumps(evidence) for name in ("Hidden base name", "Hidden alternative name", "origin omitted from AI"))
    assert evidence["comparison"]["deltas"] == comparison["deltas"]
    assert evidence["comparison"]["comparable"] and not evidence["comparison"]["assumptions_equal"]
    assert comparison["assumptions_equal"] is False
    assert preview["evidence_sha256"] == evidence_digest(evidence)

    body = request(pair, preview)
    assert api.post(PATH + "/ai-analyze", json=body, headers={"Origin": "https://other.invalid"}).status_code == 403
    missing_consent = {key: value for key, value in body.items() if key != "confirmation"}
    rejected = api.post(PATH + "/ai-analyze", json=missing_consent)
    assert rejected.status_code == 422 and KEY not in rejected.text
    rejected = api.post(PATH + "/ai-analyze", json={**body, "api_key": "sk-short"})
    assert rejected.status_code == 422 and "sk-short" not in rejected.text
    reversed_pair = {"left_id": pair["right_id"], "right_id": pair["left_id"]}
    assert api.post(PATH + "/ai-analyze", json={**body, **reversed_pair}).status_code == 409
    assert api.post(PATH + "/ai-preview", json={**pair, "right_id": pair["left_id"]}).status_code == 422
    assert not sent

    answer = api.post(PATH + "/ai-analyze", json=body)
    assert answer.is_success and answer.json()["saved_locally"], answer.text
    assert sent == [(evidence, body["question"], KEY)]
    item = api.get("/lab/ai-archive?kind=backtest").json()["items"][0]
    assert item["comparison"] and item["run_id"] == pair["left_id"] and item["symbol"] == "BTC/USDT"
    identifier = answer.json()["analysis_id"]
    archived = api.get("/lab/ai-archive/" + identifier).json()
    assert archived["evidence"] == evidence and archived["id"] == identifier
    assert not api.get(f"/lab/runs/{pair['right_id']}/ai-analyses").json()
    data = api.post("/lab/ai-archive/export-jsonl", json={
        "records": [{"id": identifier, "expected_hash": archived["hash"]}],
        "confirmation": "EXPORTAR JSONL EN CLARO",
    })
    assert data.is_success
    line = json.loads(data.json()["jsonl"])
    assert line["input"]["evidence"] == evidence and not line["limitations"]["eligible_for_point_in_time_signals"]
    assert KEY not in json.dumps(archived) and KEY.encode() not in path.read_bytes()


def test_different_histories_omit_deltas_and_changed_source_blocks_external_call(lab, monkeypatch):
    api, path = lab
    pair, _ = sample(api, alternate_history=True, same_parameters=True)
    preview = api.post(PATH + "/ai-preview", json=pair).json()
    flags = preview["evidence"]["comparison"]
    assert not flags["comparable"] and not flags["same_dataset"] and flags["same_engine"]
    assert flags["assumptions_equal"] and flags["deltas"] is None
    assert "datasets distintos" in flags["context"]
    sent = []
    def provider(*args):
        sent.append(args)
        return fake_answer(*args)
    monkeypatch.setattr(lab_api, "analyze_research", provider)
    body = request(pair, preview, save=False)
    valid = api.post(PATH + "/ai-analyze", json=body)
    assert valid.is_success and not valid.json()["saved_locally"]
    assert len(sent) == 1
    with LabStore(path, passphrase=PASSWORD).connect() as db:
        db.execute("UPDATE runs SET result_json='{}' WHERE id=?", (pair["right_id"],))
    assert api.post(PATH + "/ai-preview", json=pair).status_code == 409
    assert api.post(PATH + "/ai-analyze", json=body).status_code == 409
    assert len(sent) == 1


def test_changed_alternative_during_provider_response_never_archives_old_pair(lab, monkeypatch):
    api, path = lab
    pair, _ = sample(api)
    preview = api.post(PATH + "/ai-preview", json=pair).json()
    def provider(*args):
        with LabStore(path, passphrase=PASSWORD).connect() as db:
            db.execute("UPDATE runs SET result_json=json_set(result_json, '$.result_hash', ?) WHERE id=?",
                       ("0" * 64, pair["right_id"]))
        return fake_answer(*args)
    monkeypatch.setattr(lab_api, "analyze_research", provider)
    reply = api.post(PATH + "/ai-analyze", json=request(pair, preview))
    assert reply.is_success and not reply.json()["saved_locally"]
    assert "no pudo guardarse" in reply.json()["archive_warning"]
    assert api.get("/lab/ai-archive").json()["items"] == []


def test_comparison_response_is_discarded_after_lock_and_reopen(lab, monkeypatch):
    api, path = lab
    pair, _ = sample(api)
    preview = api.post(PATH + "/ai-preview", json=pair).json()

    def provider(*args):
        lab_access.lock(path)
        lab_access.unlock(path, PASSWORD)
        return fake_answer(*args)

    monkeypatch.setattr(lab_api, "analyze_research", provider)
    reply = api.post(PATH + "/ai-analyze", json=request(pair, preview))
    assert reply.status_code == 423 and KEY not in reply.text
    assert api.get("/lab/ai-archive").json()["items"] == []


def test_comparison_prompt_preserves_responses_privacy_controls():
    def transport(message):
        payload = json.loads(message.content)
        assert message.url.path == "/v1/responses"
        assert payload["store"] is False and payload["tools"] == []
        assert payload["max_output_tokens"] == 700
        assert "no declares una ganadora" in payload["instructions"]
        assert "prueba fuera de muestra" in payload["instructions"]
        assert KEY not in payload["input"]
        return httpx.Response(200, json={"status": "completed", "model": MODEL,
            "output": [{"type":"message", "role":"assistant", "content":[{"type":"output_text", "text":"Muestra breve"}]}]})
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        answer = analyze({"schema": COMPARISON_SCHEMA, "comparison": {"comparable": False}}, "Revisa la muestra", KEY, client=client)
    assert answer["answer"] == "Muestra breve"
