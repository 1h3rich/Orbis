"""Entrada → auditorías reales → conciliación → cierre, una misma subcuenta."""

import time
from decimal import Decimal

from fastapi.testclient import TestClient
import pytest

from tests.grvt_testnet_loop_fixture import GrvtTestnetLoop
from tests.grvt_testnet_ui_fixture import PASSPHRASE


@pytest.mark.parametrize("direction,funding",[("LONG","0"),("SHORT","0"),("LONG","0.25"),
    ("LONG","-0.10"),("SHORT","0.25"),("SHORT","-0.100000000000000001")])
def test_testnet_entry_reconciles_then_closes_same_account(tmp_path,direction,funding):
    venue = GrvtTestnetLoop(tmp_path)
    venue.funding_on_close = Decimal(funding)
    opening = venue.seed(direction)
    client = TestClient(venue.make_app())
    token = client.post("/real/vault/create",json={"passphrase":PASSPHRASE}).json()["token"]
    headers = {"X-Orbis-Vault-Token":token}
    base = "/real/exchanges/grvt/testnet"
    for closed in (False,True):
        source = venue.close_source(opening) if closed else opening
        body = venue.body(source); body["limit_price"] = str(venue.price)
        if closed and direction == "LONG":
            # Dos fills ajenos compensados no cambian la fotografía de saldo
            # o posición; aun así impiden atribuir el circuito a ORBIS.
            stamp = time.time_ns()
            external = [{**venue.fills[0], "trade_id":"external-"+str(index),
                "order_id":"0xexternal"+str(index), "client_order_id":str(100+index),
                "event_time":str(stamp), "is_buyer":index == 0,
                "fee":"0", "realized_pnl":"0"} for index in range(2)]
            venue.fills.extend(external)
            refused = client.post(base+"/signals/sign-and-submit",json=body,headers=headers)
            assert refused.status_code == 409,refused.text
            assert len(venue.orders) == 1 and venue.position == Decimal("0.5")
            # Retirar la respuesta inyectada permite verificar que el rechazo
            # ocurrió antes de consumir el ID de la nueva señal.
            del venue.fills[-2:]
        posted = client.post(base+"/signals/sign-and-submit",json=body,headers=headers)
        assert posted.status_code == 200,posted.text
        data = {"sub_account_id":source["sub_account_id"],"client_order_id":source["client_order_id"]}
        assertion = {**data,"confirmation":"CONCILIAR_GRVT_TESTNET"}
        assert client.post(base+"/orders/reconcile",json=assertion).status_code == 401
        assert client.post(base+"/orders/reconcile",json={**assertion,"api_key":"forbidden"},headers=headers).status_code == 422
        assert client.post(base+"/orders/reconcile",json=assertion,headers=headers).status_code == 409
        audit = {**data,"api_key":body["api_key"],"confirmation":"AUDITAR_GRVT_TESTNET"}
        observed = client.post(base+"/orders/audit",json=audit,headers=headers)
        assert observed.status_code == 200,observed.text
        assert client.post(base+"/orders/reconcile",json=assertion,headers=headers).status_code == 409
        time.sleep(5.05)  # Separación protocolaria real, no simulación de éxito.
        observed = client.post(base+"/orders/audit",json=audit,headers=headers)
        assert observed.status_code == 200,observed.text
        result = client.post(base+"/orders/reconcile",json=assertion,headers=headers)
        assert result.status_code == 200,result.text
        assert result.headers["cache-control"] == "no-store"
        assert result.json()["reconciliation"]["result"]["settle_balance"] == str(venue.balance)
        if closed and funding != "0":
            assert result.json()["reconciliation"]["result"]["funding_paid_usdt"] == funding
        assert result.json()["mainnet_order_sent"] is False and not result.json()["can_submit"]
        assert client.post(base+"/orders/reconcile",json=assertion,headers=headers).json() == result.json()
        calls = len(venue.calls)
        assert client.post(base+"/signals/sign-and-submit",json=body,headers=headers).status_code == 409
        assert len(venue.calls) == calls
    assert len(venue.orders) == 2 and venue.position == 0
    assert venue.balance == Decimal("10000.98")-Decimal(funding)
    assert client.get("/real/accounts",headers=headers).json() == []
    report = client.get(base+"/recovery",params={"sub_account_id":source["sub_account_id"]},headers=headers)
    assert report.status_code == 200,report.text
    assert all(item["journal_status"] == "RECONCILED" for item in report.json()["items"])
    assert report.json()["items"][0]["history_continuity"] is None
    assert report.json()["items"][1]["history_continuity"]["previous_client_order_id"] == opening["client_order_id"]
    if direction == "LONG" and funding == "0.25":
        # El funding ya explicado por el recibo no debe cobrarse de nuevo ni
        # impedir la siguiente estrategia en la misma subcuenta.
        next_source = {**venue.seed(direction),"sub_account_id":opening["sub_account_id"]}
        body = venue.body(next_source); body["limit_price"] = str(venue.price)
        posted = client.post(base+"/signals/sign-and-submit",json=body,headers=headers)
        assert posted.status_code == 200,posted.text
        records = client.get(base+"/recovery",params={"sub_account_id":opening["sub_account_id"]},headers=headers)
        proof = records.json()["items"][-1]["history_continuity"]
        assert proof["schema"] == "orbis.grvt.testnet.history.v2"
        assert proof["funding"]["rows"][0]["amount_paid"] == funding
        assert len(venue.orders) == 3
