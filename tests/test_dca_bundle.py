"""A DCA file exchanges a versioned rule, with no authority or ledger data."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.automation.access import automation_access
from app.automation.api import create_automation_router
from app.automation.dca_bundle import BUNDLE_SCHEMA, MAX_DCA_BUNDLE_BYTES, export_dca_bundle, review_dca_bundle
from app.automation.store import AutomationStore
from app.lab.store import LabStore, content_hash
from tests.test_dca_validation import bars, rule


ROOT = "/automation/dca"
PREVIEW = ROOT + "/rules/import/preview"
IMPORT = ROOT + "/rules/import"
PASSWORD = "owned DCA bundle encrypted store password"
RULE = rule(ma_adjustment={"kind": "ema", "period": 3, "condition": "below",
                           "matched_factor": "1.5", "unmatched_factor": "0.5"})


def encode(value):
    return json.dumps(value, ensure_ascii=False).encode()


def app_for(path, lab_path):
    app = FastAPI()
    app.include_router(create_automation_router(path, lab_path))
    return app


def approval(review):
    return {"X-Orbis-Import-Id": str(uuid4()), "X-Orbis-Import-Hash": review["review_hash"]}


def test_volume_bundle_replays_the_exact_plan_version_without_authority():
    definition = rule(volume_gate={"period": 7, "min_ratio": "1.500000000000000001"})
    bundle = export_dca_bundle({"name":"Volumen", "version":3, "definition":definition})
    assert bundle["scope"] == "RULE_ONLY"
    imported = review_dca_bundle(encode(bundle))
    assert imported["definition"] == definition and imported["engine_version"] == "dca-1.9"
    assert imported["source_version"] == 3 and imported["integrity"] == "CHECKSUM_VERIFIED"


def test_dca_file_roundtrip_preserves_selected_version_and_retries_atomically(tmp_path):
    path, lab_path = tmp_path / "automation.db", tmp_path / "lab.db"
    client = TestClient(app_for(path, lab_path))
    assert client.post(ROOT + "/create", json={"passphrase": PASSWORD}).status_code == 200
    try:
        store = AutomationStore(path)
        first = store.create_rule("Aportación EMA", RULE)
        second = store.add_version(first["id"], rule(amount="85"))
        export_path = ROOT + f"/rules/{first['id']}/versions/{first['version_id']}/export"
        response = client.get(export_path)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        bundle = response.json()
        assert bundle["schema"] == BUNDLE_SCHEMA and bundle["scope"] == "RULE_ONLY"
        assert bundle["plan"] == {"name": "Aportación EMA", "version": 1,
                                  "engine_version": "dca-1.8", "definition": RULE}
        assert bundle["sha256"] == content_hash({key: val for key, val in bundle.items() if key != "sha256"})
        assert first["id"] not in response.text and first["version_id"] not in response.text
        wrong_parent = export_path.replace(first["id"], str(uuid4()))
        assert client.get(wrong_parent).status_code == 404
        assert client.get(export_path.replace(first["version_id"], second["version_id"])).json()["plan"]["version"] == 2
        data = encode(bundle)
        preview = client.post(PREVIEW, content=data)
        assert preview.status_code == 200 and preview.headers["cache-control"] == "no-store"
        reviewed = preview.json()
        assert reviewed["integrity"] == "CHECKSUM_VERIFIED" and reviewed["definition"] == RULE
        assert len(store.rules()) == 1
        headers = approval(reviewed)
        assert client.post(IMPORT, content=data + b" ", headers=headers).status_code == 409
        assert len(store.rules()) == 1
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: client.post(IMPORT, content=data, headers=headers), range(2)))
        assert all(item.status_code == 200 for item in responses)
        imported, duplicate = [item.json() for item in responses]
        assert imported == duplicate and imported["id"] != first["id"]
        assert imported["version_id"] != first["version_id"] and imported["version"] == 1
        assert len(store.rules()) == 2
        # Repeating the request after an edit recovers version 1.
        store.add_version(imported["id"], rule(amount="87"))
        assert client.post(IMPORT, content=data, headers=headers).json() == imported
        other = encode({"name": "Otro plan", "definition": rule()})
        other_preview = client.post(PREVIEW, content=other).json()
        assert client.post(IMPORT, content=other, headers=headers | {
            "X-Orbis-Import-Hash": other_preview["review_hash"]}).status_code == 422
        assert store.rules()[1]["version_id"] == second["version_id"]
        with store.connect() as db:
            assert db.execute("SELECT count(*) FROM paper_dca_sessions").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM dca_runs").fetchone()[0] == 0
        assert not lab_path.exists()
        assert path.read_bytes()[:16] != b"SQLite format 3\x00"
        client.post(ROOT + "/lock")
        other_process = TestClient(app_for(path, lab_path))
        assert other_process.get(export_path).status_code == 423
        assert other_process.post(PREVIEW, content=data).status_code == 200
        assert other_process.post(IMPORT, content=data, headers=headers).status_code == 423
        assert other_process.post(ROOT + "/unlock", json={"passphrase": PASSWORD}).status_code == 200
        dataset = LabStore(lab_path).save_dataset("BTC/USDT", "1d", "mexc-spot-public", bars(), [])
        backtest = other_process.post(ROOT + "/runs", json={"version_id": imported["version_id"], "dataset_id": dataset["id"]})
        assert backtest.status_code == 200, backtest.text
        run = backtest.json()
        assert run["engine_version"] == "dca-1.8" and run["rule"] == RULE
        assert other_process.get(ROOT + f"/runs/{run['id']}/verify").json()["verified"]
    finally:
        automation_access.lock(path)


@pytest.mark.parametrize("data", [
    b"[]", b"not JSON", b'{"name":"x","name":"y","definition":{}}',
    b'{"name":"x","definition":{"amount":NaN}}', b'{"name":"\xff"}',
    encode({"name": "Bot", "definition": RULE, "token": "not allowed"}),
    encode({"name": "Bot", "definition": RULE | {"execute": "import os"}}),
    encode({"name": "Bot", "definition": RULE | {"dips": [{"drop_pct":"0.1","amount":"2","wallet":"invalid"}]}}),
    encode({"name": "Bot", "definition": RULE | {"ma_adjustment": RULE["ma_adjustment"] | {"order": "BUY"}}}),
    b'[' * 2000 + b']' * 2000,
])
def test_untrusted_files_fail_before_any_storage_creation(tmp_path, data):
    path = tmp_path / "automation.db"
    client = TestClient(app_for(path, tmp_path / "lab.db"))
    assert client.post(PREVIEW, content=data).status_code == 422
    assert not path.exists()


def test_preview_bounded_local_and_supports_declarative_template(tmp_path):
    path = tmp_path / "automation.db"
    client = TestClient(app_for(path, tmp_path / "lab.db"))
    data = encode({"name": "Plantilla", "definition": RULE})
    assert client.post(PREVIEW, headers={"Origin": "https://outside.invalid"}, content=data).status_code == 403
    assert client.post(PREVIEW, content=b" " * (MAX_DCA_BUNDLE_BYTES + 1)).status_code == 413
    review = client.post(PREVIEW, content=b"\xef\xbb\xbf" + data).json()
    assert review["integrity"] == "TEMPLATE" and review["source_version"] is None
    assert review["definition"] == RULE
    assert client.post(IMPORT, headers=approval(review), content=b"\xef\xbb\xbf" + data).status_code == 423
    assert not path.exists()
    assert client.post(ROOT + "/create", json={"passphrase": PASSWORD}).status_code == 200
    try:
        response = client.post(IMPORT, headers=approval(review), content=b"\xef\xbb\xbf" + data)
        assert response.status_code == 200, response.text
        assert response.json()["definition"] == RULE and response.json()["version"] == 1
    finally:
        automation_access.lock(path)


def test_export_checksum_covers_name_version_and_all_rule_values(tmp_path):
    path = tmp_path / "automation.db"
    store = AutomationStore(path)
    created = store.create_rule("DCA", RULE)
    client = TestClient(app_for(path, tmp_path / "lab.db"))
    path = ROOT + f"/rules/{created['id']}/versions/{created['version_id']}/export"
    exported = client.get(path).json()
    for key, value in (("name", "Otro"), ("version", 2), ("engine_version", "dca-1.6"),
                       ("definition", RULE | {"amount": "700"})):
        changed = deepcopy(exported)
        changed["plan"][key] = value
        assert client.post(PREVIEW, content=encode(changed)).status_code == 422
    assert len(store.rules()) == 1
