"""CSV REAL: vista previa, importación atómica y reintento idempotente."""

import csv
from datetime import datetime, timezone
from io import StringIO
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.portfolio.api import create_real_router
from app.portfolio.csv_import import COLUMNS, COLUMNS_V1


WHEN = datetime(2025, 1, 1, tzinfo=timezone.utc).isoformat()


def csv_text(account_id, events):
    stream = StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerow(COLUMNS)
    for event in events:
        row = {**event, "schema_version": "2", "environment": "REAL",
               "account_id": account_id, "event_id": event["id"],
               "amount": event["gross_amount"] if event["kind"] in ("CONTRIBUTION", "WITHDRAWAL") else ""}
        writer.writerow([row.get(key) if row.get(key) is not None else "" for key in COLUMNS])
    return "\ufeff" + stream.getvalue()


def setup(tmp_path):
    app = FastAPI()
    app.include_router(create_real_router(tmp_path / "vault.db"))
    client = TestClient(app)
    token = client.post("/real/vault/create", json={"passphrase": "csv test passphrase"}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    source = client.post("/real/accounts", json={"name": "Origen"}, headers=headers).json()["id"]
    target = client.post("/real/accounts", json={"name": "Destino"}, headers=headers).json()["id"]
    return client, headers, source, target


def add(client, headers, account, kind, **kwargs):
    response = client.post(f"/real/accounts/{account}/events", headers=headers,
                           json={"request_id": str(uuid4()), "kind": kind,
                                 "quote_currency": "EUR", "occurred_at": WHEN, **kwargs})
    assert response.status_code == 200, response.text
    return response.json()


def test_real_csv_preview_commit_replay_and_lock(tmp_path):
    client, headers, source, target = setup(tmp_path)
    add(client, headers, source, "CONTRIBUTION", amount="1000.000000000000000001")
    add(client, headers, source, "BUY", asset="BTC", quantity="0.125", unit_price="100.50", note="=2+2")
    add(client, headers, source, "SELL", asset="BTC", quantity="0.025", unit_price="120")
    events = client.get(f"/real/accounts/{source}/events", headers=headers).json()
    content = csv_text(source, events)
    preview_url = f"/real/accounts/{target}/csv-preview"
    import_url = f"/real/accounts/{target}/csv-import"
    assert client.post(preview_url, json={"csv_text": content}).status_code == 401
    preview = client.post(preview_url, headers=headers, json={"csv_text": content})
    assert preview.status_code == 200, preview.text
    assert (preview.json()["total"], preview.json()["new"], preview.json()["already_present"]) == (3, 3, 0)
    assert client.get(f"/real/accounts/{target}/events", headers=headers).json() == []
    assert client.post(import_url, headers=headers, json={"csv_text": content,
                       "sha256": "0" * 64, "confirmation": "IMPORTAR"}).status_code == 409
    committed = client.post(import_url, headers=headers, json={"csv_text": content,
                            "sha256": preview.json()["sha256"], "confirmation": "IMPORTAR"})
    assert committed.status_code == 200, committed.text
    assert (committed.json()["new"], committed.json()["already_present"]) == (3, 0)
    imported = client.get(f"/real/accounts/{target}/events", headers=headers).json()
    assert len(imported) == 3
    assert {item["request_id"] for item in imported} == {item["request_id"] for item in events}
    assert next(item for item in imported if item["kind"] == "BUY")["quantity"] == "0.125"
    assert client.get(f"/real/accounts/{target}/snapshot", headers=headers).json()["positions"] == \
           client.get(f"/real/accounts/{source}/snapshot", headers=headers).json()["positions"]
    replay = client.post(import_url, headers=headers, json={"csv_text": content,
                         "sha256": preview.json()["sha256"], "confirmation": "IMPORTAR"})
    assert (replay.json()["new"], replay.json()["already_present"]) == (0, 3)
    assert len(client.get(f"/real/accounts/{target}/events", headers=headers).json()) == 3
    client.post("/real/vault/lock", headers=headers)
    assert client.post(preview_url, headers=headers, json={"csv_text": content}).status_code == 401


def test_real_csv_rejects_invalid_or_conflicting_batch_without_partial_write(tmp_path):
    client, headers, source, target = setup(tmp_path)
    first = add(client, headers, source, "BUY", asset="BTC", quantity="1", unit_price="10")
    second = add(client, headers, source, "SELL", asset="BTC", quantity="1", unit_price="12")
    preview_url = f"/real/accounts/{target}/csv-preview"
    import_url = f"/real/accounts/{target}/csv-import"
    original = csv_text(source, [first, second])
    bad_gross = csv_text(source, [first, {**second, "gross_amount": "-999"}])
    assert client.post(preview_url, headers=headers, json={"csv_text": bad_gross}).status_code == 422
    assert client.get(f"/real/accounts/{target}/events", headers=headers).json() == []
    sell_only = csv_text(source, [second])
    assert client.post(preview_url, headers=headers, json={"csv_text": sell_only}).status_code == 422
    assert client.get(f"/real/accounts/{target}/events", headers=headers).json() == []
    mixed = csv_text(source, [first]) + csv_text(target, [second]).split("\r\n", 1)[1]
    assert client.post(preview_url, headers=headers, json={"csv_text": mixed}).status_code == 422
    assert client.get(f"/real/accounts/{target}/events", headers=headers).json() == []
    preview = client.post(preview_url, headers=headers, json={"csv_text": original}).json()
    assert client.post(import_url, headers=headers, json={"csv_text": original,
                       "sha256": preview["sha256"], "confirmation": "IMPORTAR"}).status_code == 200
    changed = csv_text(source, [{**first, "note": "Movimiento distinto"}, second])
    assert client.post(preview_url, headers=headers, json={"csv_text": changed}).status_code == 409
    assert len(client.get(f"/real/accounts/{target}/events", headers=headers).json()) == 2


def test_real_csv_preserves_reported_trade_gross(tmp_path):
    client, headers, source, target = setup(tmp_path)
    contribution = add(client, headers, source, "CONTRIBUTION", amount="100")
    buy = add(client, headers, source, "BUY", asset="BTC", quantity="0.003",
              unit_price="10000")
    content = csv_text(source, [contribution, {**buy, "gross_amount": "29.99"}])
    preview_url = f"/real/accounts/{target}/csv-preview"
    import_url = f"/real/accounts/{target}/csv-import"
    preview = client.post(preview_url, headers=headers, json={"csv_text": content})
    assert preview.status_code == 200, preview.text
    response = client.post(import_url, headers=headers, json={
        "csv_text": content, "sha256": preview.json()["sha256"], "confirmation": "IMPORTAR",
    })
    assert response.status_code == 200, response.text
    events = client.get(f"/real/accounts/{target}/events", headers=headers).json()
    assert next(item for item in events if item["kind"] == "BUY")["gross_amount"] == "29.99"
    assert client.get(f"/real/accounts/{target}/snapshot", headers=headers).json()["cash"]["EUR"] == "70.01"
    replay = client.post(import_url, headers=headers, json={
        "csv_text": content, "sha256": preview.json()["sha256"], "confirmation": "IMPORTAR",
    })
    assert replay.status_code == 200
    assert replay.json()["already_present"] == 2


def test_real_csv_v2_preserves_base_fee_and_v1_still_imports(tmp_path):
    client, headers, source, target = setup(tmp_path)
    contribution = add(client, headers, source, "CONTRIBUTION", amount="1000")
    purchase = add(client, headers, source, "BUY", asset="BTC", quantity="0.01",
                   unit_price="65000", fee="0.00001", fee_asset="BTC")
    content = csv_text(source, [contribution, purchase])
    preview = client.post(f"/real/accounts/{target}/csv-preview", headers=headers,
                          json={"csv_text": content})
    assert preview.status_code == 200, preview.text
    imported = client.post(f"/real/accounts/{target}/csv-import", headers=headers, json={
        "csv_text": content, "sha256": preview.json()["sha256"], "confirmation": "IMPORTAR",
    })
    assert imported.status_code == 200, imported.text
    events = client.get(f"/real/accounts/{target}/events", headers=headers).json()
    assert next(item for item in events if item["kind"] == "BUY")["fee_asset"] == "BTC"
    snapshot = client.get(f"/real/accounts/{target}/snapshot", headers=headers).json()
    assert snapshot["cash"]["EUR"] == "350.00"
    assert snapshot["positions"][0]["quantity"] == "0.00999"
    assert snapshot["positions"][0]["fees_by_asset"] == {"BTC": "0.00001"}

    # Un CSV anterior sin columna de activo de comisión sigue siendo quote.
    v1 = StringIO(newline="")
    writer = csv.writer(v1, lineterminator="\r\n")
    writer.writerow(COLUMNS_V1)
    legacy = {**purchase, "schema_version": "1", "environment": "REAL",
              "account_id": source, "event_id": purchase["id"],
              "request_id": str(uuid4()), "amount": "", "fee": "0.10"}
    writer.writerow([legacy.get(key) if legacy.get(key) is not None else "" for key in COLUMNS_V1])
    third = client.post("/real/accounts", headers=headers, json={"name": "Legado"}).json()["id"]
    legacy_content = v1.getvalue()
    legacy_preview = client.post(f"/real/accounts/{third}/csv-preview", headers=headers,
                                 json={"csv_text": legacy_content})
    assert legacy_preview.status_code == 200, legacy_preview.text
    legacy_import = client.post(f"/real/accounts/{third}/csv-import", headers=headers, json={
        "csv_text": legacy_content, "sha256": legacy_preview.json()["sha256"],
        "confirmation": "IMPORTAR",
    })
    assert legacy_import.status_code == 200, legacy_import.text
    old_event = client.get(f"/real/accounts/{third}/events", headers=headers).json()[0]
    assert old_event["fee_asset"] == "EUR"
