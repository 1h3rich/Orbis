"""Marcas REAL: sólo fills propios finales/asentados, revisión estable y sin red."""

from copy import deepcopy
from decimal import Decimal
from hashlib import sha256
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.lab.store import content_hash
from app.live_execution.spot_bot_executions import export_spot_bot_fills, spot_bot_executions_page
from app.live_execution.spot_bot_store import SpotBotStoreError,owned_spot_bot_ledger
from app.portfolio.api import create_real_router
from app.portfolio.models import MexcObservedFill,RealEvent,RealMexcFillBooking,RealSpotBotDecision
from tests.test_mexc_spot_bot import bot_case,next_entry,tick,PASSWORD
from tests.test_spot_bot_rearm import review
from app.live_execution.spot_bot_store import pause_spot_bot


def page(case,**kwargs):
    return spot_bot_executions_page(case[0],case[2],case[8],**kwargs)


def sell(case):
    case[3].quote_get = lambda symbol:{"symbol":symbol,"closeTime":int(case[4].value.timestamp()*1000),
        "bidPrice":"72000","askPrice":"72001","lastPrice":"72000"}
    assert tick(case)["action"] == "SELL"


def test_no_execution_from_signal_or_activation_then_exact_real_fill_not_paper_or_limit(tmp_path):
    case = bot_case(tmp_path)
    empty = page(case)
    assert empty["executions"] == [] and empty["total"] == 0 and empty["next_offset"] is None
    next_entry(case)
    assert page(case)["total"] == 0
    assert tick(case)["action"] == "BUY"
    result = page(case)
    assert result["environment"] == "REAL" and result["evidence"] == "OWN_FINAL_SETTLED_BOOKED_FILLS"
    assert result["symbol"] == "BTC/USDT" and result["base_asset"] == "BTC" and result["provider"] == "mexc"
    fill = result["executions"][0]
    with case[0].session() as db:
        raw = db.get(MexcObservedFill,fill["fill_id"])
        booking = db.query(RealMexcFillBooking).filter_by(fill_id=raw.id).one()
        assert fill["event_id"] == booking.event_id and fill["time_ms"] == raw.time_ms
        assert Decimal(fill["price"]) == raw.price and Decimal(fill["quantity_base"]) == raw.quantity
        assert Decimal(fill["fee"]) == raw.commission and fill["fee_asset"] == raw.commission_asset
    assert fill["reason"] == "ENTRY_RULE" and len(case[7].posts) == 1
    case[0].lock()


def test_pagination_does_not_silently_mix_snapshots_or_drop_fills_across_rearm(tmp_path):
    case = bot_case(tmp_path)
    next_entry(case)
    tick(case)
    first_buy = page(case)
    pause_spot_bot(case[0],case[8],now=case[4]())
    review(case)
    sell(case)
    with pytest.raises(SpotBotStoreError,match="cambiaron"):
        page(case,offset=1,revision=first_buy["revision"])
    with pytest.raises(SpotBotStoreError,match="cambiaron"):
        export_spot_bot_fills(case[0],case[2],case[8],revision=first_buy["revision"])
    first = page(case,limit=1)
    second = page(case,limit=1,offset=first["next_offset"],revision=first["revision"])
    assert first["total"] == second["total"] == 2 and second["next_offset"] is None
    assert [first["executions"][0]["side"],second["executions"][0]["side"]] == ["BUY","SELL"]
    assert first["executions"][0] == first_buy["executions"][0]
    archive = export_spot_bot_fills(case[0],case[2],case[8],revision=first["revision"])
    assert archive["sha256"] == sha256(archive["jsonl"].encode()).hexdigest()
    rows = [json.loads(line) for line in archive["jsonl"].splitlines()]
    assert [row["provenance"]["authorization_index"] for row in rows] == [1,2]
    assert [row["order_index"] for row in rows] == [1,2]
    assert [row["fill"]["side"] for row in rows] == ["BUY","SELL"]
    for index, (row, execution) in enumerate(zip(rows,(first["executions"][0],second["executions"][0])),1):
        assert row["fill_index"] == index
        assert row["execution_authority"] == "NONE" and row["environment"] == "REAL"
        assert row["provenance"]["reserve"] is None
        assert all(row["fill"][key] == execution[key] for key in row["fill"])
        assert all(value not in archive["jsonl"] for value in
                   (case[2],case[8],execution["fill_id"],execution["intent_id"],execution["event_id"]))
        digest = row.pop("row_sha256")
        assert content_hash(row) == digest
    before = owned_spot_bot_ledger(case[0],case[8])
    assert page(case)["total"] == 2 and owned_spot_bot_ledger(case[0],case[8]) == before
    assert len(case[7].posts) == 2
    case[0].lock()


@pytest.mark.parametrize("model,field,value",[
    (MexcObservedFill,"price",Decimal("1")),(RealEvent,"gross_amount",Decimal("1")),
    (RealMexcFillBooking,"event_id",None),(RealSpotBotDecision,"payload_digest","0"*64),
])
def test_corrupt_fill_event_booking_or_decision_never_becomes_a_verified_mark(tmp_path,model,field,value):
    case = bot_case(tmp_path)
    next_entry(case)
    tick(case)
    revision = page(case)["revision"]
    with case[0].session() as db:
        row = db.query(model).first()
        if model is RealEvent:
            row = db.query(RealEvent).filter_by(kind="BUY").one()
        if model is RealMexcFillBooking:
            value = db.query(RealEvent).filter_by(kind="CONTRIBUTION").first().id
        setattr(row,field,value)
        db.commit()
    with pytest.raises((SpotBotStoreError,ValueError)):
        page(case)
    with pytest.raises((SpotBotStoreError,ValueError)):
        export_spot_bot_fills(case[0],case[2],case[8],revision=revision)
    assert len(case[7].posts) == 1
    case[0].lock()


def test_uncertain_or_unsettled_order_is_not_an_executed_mark(tmp_path):
    case = bot_case(tmp_path)
    next_entry(case)
    case[7].timeout_after_accepting = True
    assert tick(case)["order"]["journal_status"] == "SUBMISSION_UNKNOWN"
    with pytest.raises(SpotBotStoreError,match="seguimiento"):
        page(case)
    assert len(case[7].posts) == 1
    case[0].lock()


@pytest.mark.parametrize("arguments",[{"offset":1},{"limit":0},{"limit":501},{"offset":-1},{"revision":"bad"},{"offset":1,"revision":"0"*64}])
def test_invalid_cursor_and_limits_are_rejected(tmp_path,arguments):
    case = bot_case(tmp_path)
    with pytest.raises(SpotBotStoreError):
        page(case,**arguments)
    case[0].lock()


def test_api_is_passive_account_scoped_reopens_without_view_and_never_posts_to_exchange(tmp_path):
    case = bot_case(tmp_path)
    next_entry(case)
    tick(case)
    expected = deepcopy(page(case))
    case[0].lock()
    app = FastAPI()
    app.include_router(create_real_router(case[0].path))
    client = TestClient(app)
    url = f"/real/accounts/{case[2]}/mexc-bots/{case[8]}/executions-page"
    export = url.replace("executions-page","research-export")
    body = {"revision":expected["revision"],"confirmation":"EXPORTAR_JSONL_MEXC_REAL"}
    assert client.get(url).status_code == 401
    assert client.post(export,json=body).status_code == 401
    token = client.post("/real/vault/unlock",json={"passphrase":PASSWORD}).json()["token"]
    headers = {"X-Orbis-Vault-Token":token}
    result = client.get(url,headers=headers)
    assert result.status_code == 200,result.text
    assert result.headers["cache-control"] == "no-store" and result.json() == expected
    assert client.get(url+"?offset=1",headers=headers).status_code == 409
    assert client.get(url+"?limit=501",headers=headers).status_code == 422
    assert client.post(export,headers={**headers,"Origin":"https://outside.invalid"},json=body).status_code == 403
    assert client.post(export,headers=headers,json={**body,"confirmation":"OTRA"}).status_code == 422
    exported = client.post(export,headers=headers,json=body)
    assert exported.status_code == 200 and exported.headers["cache-control"] == "no-store"
    assert exported.json()["record_count"] == 1 and exported.json()["revision"] == expected["revision"]
    assert client.post(export,headers=headers,json=body).json() == exported.json()
    other = client.post("/real/accounts",headers=headers,json={"name":"Otra","base_currency":"USDT"}).json()["id"]
    assert client.get(url.replace(case[2],other),headers=headers).status_code == 409
    assert client.post(export.replace(case[2],other),headers=headers,json=body).status_code == 409
    client.post("/real/vault/lock",headers=headers)
    assert client.get(url,headers=headers).status_code == 401 and len(case[7].posts) == 1
    assert client.post(export,headers=headers,json=body).status_code == 401


def test_empty_and_oversized_fills_are_not_exported(tmp_path, monkeypatch):
    case = bot_case(tmp_path)
    with pytest.raises(SpotBotStoreError,match="no tiene fills"):
        export_spot_bot_fills(case[0],case[2],case[8],revision=page(case)["revision"])
    next_entry(case)
    tick(case)
    monkeypatch.setattr("app.live_execution.spot_bot_executions.MAX_RESEARCH_BYTES", 1)
    with pytest.raises(SpotBotStoreError,match="tamaño máximo"):
        export_spot_bot_fills(case[0],case[2],case[8],revision=page(case)["revision"])
    assert len(case[7].posts) == 1
    case[0].lock()
