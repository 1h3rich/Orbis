"""External AI is opt-in and sees only verified aggregate LAB research."""

import hashlib
import json
import sqlite3

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.lab import ai_corpus, api as lab_api
from app.lab.access import lab_access
from app.lab.ai_research import MODEL, analyze, evidence_digest
from app.lab.api import create_lab_router
from app.lab.engine import LabError
from app.lab.secure_backup import create_secure_lab_backup, verify_secure_lab_backup
from app.lab.store import LabStore, content_hash
from app.security.sensitive_text import (contains_sensitive_ai_data, contains_sensitive_ai_text,
                                         contains_wallet_address, contains_wallet_address_in_data)
from test_lab_export import _client as backtest_client, _run as make_backtest
from test_lab_research import rows, structure


def _run(tmp_path):
    path = tmp_path / "lab.db"
    with LabStore(path).connect():  # Archivo legado para la prueba de manipulación SQLite.
        pass
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)
    dataset = client.post("/lab/datasets", json={
        "symbol": "BTC/USDT", "timeframe": "1h", "source": "test", "candles": rows(),
    })
    assert dataset.status_code == 200, dataset.text
    run = client.post("/lab/research/runs", json={
        "dataset_id": dataset.json()["id"], "rule": structure(),
    })
    assert run.status_code == 200, run.text
    return client, path, run.json()["id"]


def test_preview_reduces_verified_research_and_requires_confirmation(tmp_path, monkeypatch):
    client, path, run_id = _run(tmp_path)
    base = f"/lab/research/runs/{run_id}"
    preview = client.get(base + "/ai-preview")
    assert preview.status_code == 200, preview.text
    assert preview.headers["cache-control"] == "no-store"
    body = preview.json()
    assert body["model"] == MODEL
    assert body["evidence_sha256"] == evidence_digest(body["evidence"])
    assert body["evidence"]["environment"] == "LAB"
    assert body["evidence"]["summary"]["cases"] == 3
    assert not ({"candles", "cases", "events", "trades", "wallet", "portfolio"} & set(body["evidence"]))
    assert body["key_persisted"] is False and body["reply_persisted"] is False
    seen = []

    def fake_analyze(evidence, question, key):
        seen.append((evidence, question, key))
        return {"answer": "Muestra pequeña.", "model": MODEL,
                "evidence_sha256": evidence_digest(evidence), "usage": {}}

    monkeypatch.setattr(lab_api, "analyze_research", fake_analyze)
    payload = {"evidence_sha256": "0" * 64, "question": "¿Es robusto?", "api_key": "sk-test-secret"}
    assert client.post(base + "/ai-analyze", json=payload).status_code == 409
    assert not seen
    payload["evidence_sha256"] = body["evidence_sha256"]
    sent = client.post(base + "/ai-analyze", json=payload)
    assert sent.status_code == 200 and sent.headers["cache-control"] == "no-store"
    assert sent.json()["answer"] == "Muestra pequeña."
    assert seen == [(body["evidence"], "¿Es robusto?", "sk-test-secret")]
    assert "sk-test-secret" not in path.read_bytes().decode("latin1")
    assert client.post(base + "/ai-analyze", json={**payload, "api_key": "short"}).status_code == 422
    assert client.get(base + "/ai-preview", headers={"Origin": "https://other.invalid"}).status_code == 403


def test_ai_rejects_full_wallet_addresses_before_provider_but_allows_aliases(tmp_path, monkeypatch):
    client, _, run_id = _run(tmp_path)
    base = f"/lab/research/runs/{run_id}"
    preview = client.get(base + "/ai-preview").json()
    address = "0x" + "a1" * 20
    seen = []
    monkeypatch.setattr(lab_api, "analyze_research", lambda evidence, question, key:
                        seen.append(question) or {"answer": "Solo LAB.", "model": MODEL})
    body = {"evidence_sha256": preview["evidence_sha256"],
            "question": "Revisa " + address, "api_key": "sk-test-secret"}
    blocked = client.post(base + "/ai-analyze", json=body)
    assert blocked.status_code == 422 and not seen
    assert address not in blocked.text
    allowed = client.post(base + "/ai-analyze", json={**body, "question": "Revisa alias 0xA13B…8F721"})
    assert allowed.status_code == 200 and seen == ["Revisa alias 0xA13B…8F721"]
    assert contains_wallet_address(address)
    assert contains_wallet_address_in_data({"nested": ["BTC", {"source": address}]})
    assert not contains_wallet_address("0xA13B…8F721")


def test_ai_rejects_hashes_and_key_like_text_before_provider(tmp_path, monkeypatch):
    client, _, run_id = _run(tmp_path)
    base = f"/lab/research/runs/{run_id}"
    preview = client.get(base + "/ai-preview").json()
    seen = []
    monkeypatch.setattr(lab_api, "analyze_research", lambda *args: seen.append(args))
    body = {"evidence_sha256": preview["evidence_sha256"], "api_key": "sk-test-secret"}
    for sensitive in ("0x" + "ab" * 32, "ab" * 32, "sk-proj-" + "x" * 24):
        response = client.post(base + "/ai-analyze", json={**body, "question": "Revisa " + sensitive})
        assert response.status_code == 422 and sensitive not in response.text
    assert not seen
    assert contains_sensitive_ai_text("0x" + "ab" * 32)
    assert contains_sensitive_ai_text("ab" * 32, question=True)
    assert not contains_sensitive_ai_text("ab" * 32)
    assert contains_sensitive_ai_data({"source": "sk-proj-" + "x" * 24})
    assert not contains_sensitive_ai_data({"result_hash": "ab" * 32})


def test_ai_preview_rejects_legacy_dataset_source_with_full_wallet_address(tmp_path, monkeypatch):
    path = tmp_path / "lab.db"
    with LabStore(path).connect():
        pass
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)
    address = "0x" + "b2" * 20
    rejected = client.post("/lab/datasets", json={
        "symbol": "BTC/USDT", "timeframe": "1h", "source": address,
        "candles": rows(),
    })
    assert rejected.status_code == 422
    dataset = client.post("/lab/datasets", json={
        "symbol": "BTC/USDT", "timeframe": "1h", "source": "test",
        "candles": rows(),
    })
    assert dataset.status_code == 200
    _, candles = LabStore(path).dataset(dataset.json()["id"])
    legacy_hash = content_hash({"symbol": "BTC/USDT", "timeframe": "1h",
                                "source": address, "candles": candles})
    with sqlite3.connect(path) as db:
        db.execute("UPDATE datasets SET source=?,hash=? WHERE id=?",
                   (address, legacy_hash, dataset.json()["id"]))
    with pytest.raises(LabError, match="direcciones completas"):
        LabStore(path).create_strategy("Estrategia " + address, {})
    run = client.post("/lab/research/runs", json={
        "dataset_id": dataset.json()["id"], "rule": structure(),
    })
    assert run.status_code == 200
    base = "/lab/research/runs/" + run.json()["id"]
    monkeypatch.setattr(lab_api, "analyze_research", lambda *args: (_ for _ in ()).throw(AssertionError("sent")))
    assert client.get(base + "/ai-preview").status_code == 422
    assert client.post(base + "/ai-analyze", json={
        "evidence_sha256": "0" * 64, "question": "¿Es robusto?",
        "api_key": "sk-test-secret",
    }).status_code == 422


def test_changed_or_corrupt_evidence_never_reaches_provider(tmp_path, monkeypatch):
    client, path, run_id = _run(tmp_path)
    base = f"/lab/research/runs/{run_id}"
    preview = client.get(base + "/ai-preview").json()
    monkeypatch.setattr(lab_api, "analyze_research", lambda *args: (_ for _ in ()).throw(AssertionError("sent")))
    with sqlite3.connect(path) as db:
        db.execute("UPDATE research_events SET event_json='{}' WHERE run_id=? AND index_no=0", (run_id,))
    assert client.get(base + "/ai-preview").status_code == 409
    response = client.post(base + "/ai-analyze", json={
        "evidence_sha256": preview["evidence_sha256"], "question": "¿Es robusto?",
        "api_key": "sk-test-secret",
    })
    assert response.status_code == 409
    assert "sk-test-secret" not in response.text


def test_backtest_preview_minimizes_verified_metrics_and_blocks_changed_run(tmp_path, monkeypatch):
    client, path = backtest_client(tmp_path)
    run, _ = make_backtest(client)
    base = f"/lab/runs/{run['id']}"
    preview = client.get(base + "/ai-preview")
    assert preview.status_code == 200, preview.text
    assert preview.headers["cache-control"] == "no-store"
    evidence = preview.json()["evidence"]
    assert evidence["schema"] == "orbis.lab.ai-backtest-summary.v1"
    assert evidence["environment"] == "LAB" and evidence["theoretical_only"] is True
    assert evidence["metrics"] == run["metrics"]
    assert evidence["trade_count"] == len(run["trades"])
    assert evidence["signal_count"] == len(run["events"])
    assert evidence["result_hash"] == run["result_hash"]
    assert not ({"candles", "signals", "events", "trades", "equity", "portfolio", "wallet"} & set(evidence))
    assert preview.json()["evidence_sha256"] == evidence_digest(evidence)
    sent = []
    monkeypatch.setattr(lab_api, "analyze_research", lambda ev, question, key:
                        sent.append((ev, question, key)) or {"answer": "Solo LAB.", "model": MODEL})
    body = {"evidence_sha256": preview.json()["evidence_sha256"],
            "question": "¿Hay sobreajuste?", "api_key": "sk-test-secret"}
    response = client.post(base + "/ai-analyze", json=body)
    assert response.status_code == 200 and response.json()["answer"] == "Solo LAB."
    assert sent == [(evidence, "¿Hay sobreajuste?", "sk-test-secret")]
    assert "sk-test-secret" not in path.read_bytes().decode("latin1")
    assert client.get(base + "/ai-preview", headers={"Origin": "https://other.invalid"}).status_code == 403

    with sqlite3.connect(path) as db:
        db.execute("UPDATE simulated_trades SET trade_json='{}' WHERE run_id=? AND index_no=0", (run["id"],))
    assert client.get(base + "/ai-preview").status_code == 409
    assert client.post(base + "/ai-analyze", json=body).status_code == 409
    assert len(sent) == 1


def test_provider_request_has_no_tools_and_no_storage():
    seen = []

    def transport(request):
        sent = json.loads(request.content)
        seen.append(sent)
        assert request.url == "https://api.openai.com/v1/responses"
        assert request.headers["authorization"] == "Bearer sk-test-secret"
        return httpx.Response(200, json={
            "status": "completed", "model": MODEL,
            "output": [{"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": "No hay suficiente muestra."}]}],
        })

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        answer = analyze({"environment": "LAB", "summary": {"cases": 3}},
                         "¿Es robusto?", "sk-test-secret", client=client)
    assert answer["answer"] == "No hay suficiente muestra."
    assert seen[0]["store"] is False and seen[0]["tools"] == []
    assert seen[0]["max_output_tokens"] == 700
    assert "sk-test-secret" not in json.dumps(seen[0])


def test_direct_ai_client_rejects_wallet_address_without_network():
    address = "1BoatSLRHtKNngkdXEeobR76b53LETtpyT"
    with httpx.Client(transport=httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(AssertionError("network")))) as client:
        with pytest.raises(ValueError, match="dirección completa"):
            analyze({"environment": "LAB"}, "Revisa " + address,
                    "sk-test-secret", client=client)
        with pytest.raises(ValueError, match="dirección completa"):
            analyze({"environment": "LAB", "source": address}, "¿Es robusto?",
                    "sk-test-secret", client=client)
        with pytest.raises(ValueError, match="identificador sensible"):
            analyze({"environment": "LAB"}, "Revisa " + "0x" + "ab" * 32,
                    "sk-test-secret", client=client)
        with pytest.raises(ValueError, match="identificador sensible"):
            analyze({"environment": "LAB", "source": "sk-proj-" + "x" * 24},
                    "¿Es robusto?", "sk-test-secret", client=client)


def test_opt_in_ai_archive_requires_encrypted_lab_and_survives_backup(tmp_path, monkeypatch):
    password = "LAB AI archive password"
    path = tmp_path / "encrypted-lab.db"
    with LabStore(path, passphrase=password).connect():
        pass
    lab_access.unlock(path, password)
    try:
        app = FastAPI()
        app.include_router(create_lab_router(path))
        client = TestClient(app)
        dataset = client.post("/lab/datasets", json={
            "symbol": "BTC/USDT", "timeframe": "1h", "source": "test", "candles": rows(),
        })
        assert dataset.status_code == 200
        run = client.post("/lab/research/runs", json={
            "dataset_id": dataset.json()["id"], "rule": structure(),
        })
        assert run.status_code == 200
        base = "/lab/research/runs/" + run.json()["id"]
        preview = client.get(base + "/ai-preview").json()
        assert preview["archive_available"] is True
        monkeypatch.setattr(lab_api, "analyze_research", lambda evidence, question, key: {
            "answer": "Muestra insuficiente.", "model": MODEL,
            "evidence_sha256": evidence_digest(evidence), "usage": {"output_tokens": 7},
        })
        body = {"evidence_sha256": preview["evidence_sha256"],
                "question": "¿Qué falta?", "api_key": "sk-test-secret", "save_locally": True}
        answer = client.post(base + "/ai-analyze", json=body)
        assert answer.status_code == 200, answer.text
        assert answer.json()["saved_locally"] is True
        records = client.get(base + "/ai-analyses")
        assert records.status_code == 200 and records.headers["cache-control"] == "no-store"
        assert len(records.json()) == 1
        assert records.json()[0]["evidence"] == preview["evidence"]
        assert records.json()[0]["question"] == "¿Qué falta?"
        assert records.json()[0]["answer"] == "Muestra insuficiente."
        assert "api_key" not in json.dumps(records.json())
        assert b"sk-test-secret" not in path.read_bytes()
        assert b"Muestra insuficiente" not in path.read_bytes()
        second = client.post(base + "/ai-analyze", json={**body, "question": "¿Y la cobertura?"})
        assert second.status_code == 200 and second.json()["saved_locally"] is True
        address = "0x" + "c3" * 20
        monkeypatch.setattr(lab_api, "analyze_research", lambda evidence, question, key: {
            "answer": "Consulta " + address, "model": MODEL,
            "evidence_sha256": evidence_digest(evidence), "usage": {},
        })
        unsafe = client.post(base + "/ai-analyze", json={**body, "question": "¿Y el riesgo?"})
        assert unsafe.status_code == 200 and unsafe.json()["saved_locally"] is False
        assert "archive_warning" in unsafe.json()
        synthetic_key = "sk-proj-" + "y" * 24
        monkeypatch.setattr(lab_api, "analyze_research", lambda evidence, question, key: {
            "answer": "No publicar " + synthetic_key, "model": MODEL,
            "evidence_sha256": evidence_digest(evidence), "usage": {},
        })
        unsafe_key = client.post(base + "/ai-analyze", json={**body, "question": "¿Y la muestra?"})
        assert unsafe_key.status_code == 200 and unsafe_key.json()["saved_locally"] is False
        assert "archive_warning" in unsafe_key.json()
        assert len(client.get(base + "/ai-analyses").json()) == 2
        assert synthetic_key not in json.dumps(client.get(base + "/ai-analyses").json())
        archive = client.get(base + "/ai-archive")
        assert archive.status_code == 200 and archive.headers["cache-control"] == "no-store"
        assert archive.json()["schema"] == "orbis.lab.ai-archive.v1"
        assert archive.json()["environment"] == "LAB" and archive.json()["theoretical_only"] is True
        assert len(archive.json()["records"]) == 2
        index = client.get("/lab/ai-archive", params={"limit": 1})
        assert index.status_code == 200 and index.headers["Cache-Control"] == "no-store"
        assert index.json()["has_more"] and index.json()["offset"] == 0
        [listing] = index.json()["items"]
        assert listing["id"] == second.json()["analysis_id"] and listing["symbol"] == "BTC/USDT"
        assert "evidence" not in listing and "answer" not in listing
        assert client.get("/lab/ai-archive", params={"kind": "backtest"}).json()["items"] == []
        next_page = client.get("/lab/ai-archive", params={"offset": 1, "limit": 1}).json()
        assert not next_page["has_more"] and next_page["items"][0]["id"] == answer.json()["analysis_id"]
        selected = client.get("/lab/ai-archive/" + listing["id"])
        assert selected.status_code == 200 and selected.json() == archive.json()["records"][0]
        assert client.get("/lab/ai-archive", headers={"Origin":"https://outside.invalid"}).status_code == 403
        backup = create_secure_lab_backup(LabStore(path), "independent backup password")
        verified = verify_secure_lab_backup(tmp_path / "lab-backups" / backup["name"],
                                            "independent backup password")
        assert verified["counts"]["ai_analyses"] == 2
        target = base + "/ai-analyses/" + answer.json()["analysis_id"]
        delete_body = {"expected_hash": records.json()[0]["hash"],
                       "confirmation": "BORRAR ANÁLISIS"}
        assert client.request("DELETE", target, json={**delete_body, "confirmation": "BORRAR"}).status_code == 422
        assert client.request("DELETE", target, json={**delete_body, "expected_hash": "0" * 64}).status_code == 409
        removed = client.request("DELETE", target, json=delete_body)
        assert removed.status_code == 200 and removed.json()["deleted"] is True
        assert client.request("DELETE", target, json=delete_body).status_code == 404
        assert len(client.get(base + "/ai-analyses").json()) == 1
        assert client.get("/lab/research/runs/" + run.json()["id"]).status_code == 200
        assert verify_secure_lab_backup(tmp_path / "lab-backups" / backup["name"],
                                        "independent backup password")["counts"]["ai_analyses"] == 2
        with LabStore(path, passphrase=password).connect() as db:
            db.execute("UPDATE ai_analyses SET answer='Alterada' WHERE id=?",
                       (second.json()["analysis_id"],))
        with pytest.raises(LabError, match="huella incorrecta"):
            LabStore(path, passphrase=password).ai_analyses("research", run.json()["id"])
        assert client.get("/lab/ai-archive").status_code == 409
        assert client.get("/lab/ai-archive/" + listing["id"]).status_code == 409
    finally:
        lab_access.lock(path)


def test_export_selected_ai_corpus_is_local_exact_and_atomic(tmp_path, monkeypatch):
    path = tmp_path / "corpus.db"
    password = "owned archive corpus password"
    app = FastAPI()
    app.include_router(create_lab_router(path))
    client = TestClient(app)
    route = "/lab/ai-archive/export-jsonl"

    def forbidden(*args):
        pytest.fail("Exporting the archive contacted the AI provider")

    monkeypatch.setattr(lab_api, "analyze_research", forbidden)
    assert client.post("/lab/create", json={"passphrase": password}).is_success
    try:
        dataset = client.post("/lab/datasets", json={
            "symbol": "BTC/USDT", "timeframe": "1h", "source": "owned-corpus", "candles": rows(),
        }).json()
        run = client.post("/lab/research/runs", json={"dataset_id": dataset["id"], "rule": structure()}).json()
        source = client.get(f"/lab/research/runs/{run['id']}/ai-preview").json()
        store = LabStore(path)
        expected = {}
        for question, answer in (("¿Qué ocurrió?", "Línea uno.\nLínea dos."), ("¿Qué límites hay?", "Muestra pequeña.")):
            record = store.save_ai_analysis("research", run["id"], source["evidence"], question,
                                            {"answer": answer, "model": MODEL, "usage": None})
            expected[record["id"]] = record
        selection = [{"id": item["id"], "expected_hash": item["hash"]} for item in expected.values()]
        request = {"records": selection, "confirmation": "EXPORTAR JSONL EN CLARO"}
        assert client.post(route, json={"records": selection}).status_code == 422
        assert client.post(route, json={**request, "records": selection * 2}).status_code == 422
        assert client.post(route, json={**request, "records": selection * 26}).status_code == 422
        assert client.post(route, json=request, headers={"Origin": "https://outside.invalid"}).status_code == 403
        exported = client.post(route, json=request)
        assert exported.status_code == 200 and exported.headers["cache-control"] == "no-store"
        bundle = exported.json()
        assert bundle["schema"] == "orbis.lab.ai-corpus.v1" and bundle["record_count"] == 2
        data = bundle["jsonl"].encode("utf-8")
        assert data.endswith(b"\n") and hashlib.sha256(data).hexdigest() == bundle["sha256"]
        lines = data.splitlines()
        assert len(lines) == 2
        for line in lines:
            sample = json.loads(line)
            original = expected[sample["provenance"]["analysis_id"]]
            assert sample["input"] == {"question": original["question"], "evidence": source["evidence"]}
            assert sample["annotation"] == {"text": original["answer"], "origin": "model_generated",
                                            "model": MODEL, "usage": None}
            assert sample["provenance"]["archive_sha256"] == original["hash"]
            assert sample["provenance"]["evidence_sha256"] == source["evidence_sha256"]
            assert sample["provenance"]["run_id"] == run["id"]
            assert sample["limitations"] == {"theoretical_only": True, "annotation_verified": False,
                                             "eligible_for_point_in_time_signals": False}
            digest = sample.pop("row_sha256")
            assert digest == content_hash(sample)
        assert client.post(route, json={**request, "records": selection[::-1]}).json() == bundle
        with monkeypatch.context() as local:
            local.setattr(ai_corpus, "MAX_BYTES", 1)
            assert client.post(route, json=request).status_code == 409
        stale = [{**selection[0], "expected_hash": "0" * 64}, selection[1]]
        rejected = client.post(route, json={**request, "records": stale})
        assert rejected.status_code == 409 and "jsonl" not in rejected.json()
        client.post("/lab/lock")
        assert client.post(route, json=request).status_code == 423
        assert client.post("/lab/unlock", json={"passphrase": password}).is_success
        assert client.post(route, json=request).json() == bundle

        target = selection[0]
        original = expected[target["id"]]
        material = {key: original[key] for key in ("kind", "run_id", "evidence", "question", "answer", "model", "usage")}
        sensitive = "sk-proj-" + "z" * 24
        material["answer"] = sensitive
        forged_hash = content_hash(material)
        with store.connect() as db:
            db.execute("UPDATE ai_analyses SET answer=?,hash=? WHERE id=?", (sensitive, forged_hash, target["id"]))
        rejected = client.post(route, json={**request, "records": [{**target, "expected_hash": forged_hash}, selection[1]]})
        assert rejected.status_code == 409 and sensitive not in rejected.text and "jsonl" not in rejected.json()
        material["answer"] = original["answer"]
        material["evidence"] = {**material["evidence"], "metadata": {sensitive: 1}}
        forged_hash = content_hash(material)
        with store.connect() as db:
            db.execute("UPDATE ai_analyses SET answer=?,evidence_json=?,hash=? WHERE id=?",
                       (original["answer"], json.dumps(material["evidence"]), forged_hash, target["id"]))
        rejected = client.post(route, json={**request, "records": [{**target, "expected_hash": forged_hash}, selection[1]]})
        assert rejected.status_code == 409 and sensitive not in rejected.text and "jsonl" not in rejected.json()
        assert store.delete_ai_analysis("research", run["id"], target["id"], forged_hash)
        rejected = client.post(route, json=request)
        assert rejected.status_code == 409 and "jsonl" not in rejected.json()
    finally:
        lab_access.lock(path)


def test_plain_lab_rejects_ai_archive_before_external_call(tmp_path, monkeypatch):
    client, _, run_id = _run(tmp_path)
    base = f"/lab/research/runs/{run_id}"
    preview = client.get(base + "/ai-preview").json()
    assert preview["archive_available"] is False
    monkeypatch.setattr(lab_api, "analyze_research", lambda *args: (_ for _ in ()).throw(AssertionError("sent")))
    response = client.post(base + "/ai-analyze", json={
        "evidence_sha256": preview["evidence_sha256"], "question": "¿Qué falta?",
        "api_key": "sk-test-secret", "save_locally": True,
    })
    assert response.status_code == 409
    assert client.get("/lab/ai-archive").status_code == 409


@pytest.mark.parametrize("save_locally", [False, True])
def test_late_ai_response_is_discarded_after_lock_and_same_password_unlock(tmp_path, monkeypatch, save_locally):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    path = tmp_path / "lab.db"
    password = "Owned archive concurrency password"
    lab_access.create(path, password)
    app = FastAPI()
    app.include_router(create_lab_router(path))
    entered, release = Event(), Event()

    def delayed(evidence, question, key):
        entered.set()
        assert release.wait(10)
        return {"answer": "Respuesta de una sesión anterior", "model": MODEL,
                "evidence_sha256": evidence_digest(evidence), "usage": {}}

    monkeypatch.setattr(lab_api, "analyze_research", delayed)
    try:
        with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as pool:
            dataset = client.post("/lab/datasets", json={
                "symbol": "BTC/USDT", "timeframe": "1h", "source": "owned", "candles": rows(),
            }).json()
            run = client.post("/lab/research/runs", json={"dataset_id": dataset["id"], "rule": structure()}).json()
            endpoint = "/lab/research/runs/" + run["id"]
            preview = client.get(endpoint + "/ai-preview").json()
            future = pool.submit(client.post, endpoint + "/ai-analyze", json={
                "question": "¿Qué límites tiene la muestra?", "api_key": "sk-owned-test-key",
                "evidence_sha256": preview["evidence_sha256"], "save_locally": save_locally,
            })
            try:
                assert entered.wait(10)
                assert client.post("/lab/lock").status_code == 200
                assert client.get("/lab/ai-archive").status_code == 423
                assert client.post("/lab/unlock", json={"passphrase": password}).status_code == 200
            finally:
                release.set()
            response = future.result(timeout=10)
            assert response.status_code == 423 and set(response.json()) == {"detail"}
            assert client.get("/lab/ai-archive").json()["items"] == []
    finally:
        lab_access.lock(path)
