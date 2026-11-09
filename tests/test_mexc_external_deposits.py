"""External USDT credits use current View and an atomic local book."""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from eth_account import Account
from fastapi.testclient import TestClient
import pytest

from app.portfolio.domain import PortfolioError
from app.portfolio.mexc_transfer_matches import verify_mexc_transfer_match
from app.portfolio.models import RealMexcTransferMatch
from app.portfolio.repository import account_snapshot
from app.portfolio.vault import RealVault
from tests.mexc_roundtrip_fixture import AMOUNT, CONTRACT, Roundtrip
from tests.test_mexc_execution_state import PASSWORD
from tests.test_mexc_roundtrip import post
from tests.test_wallet_funding_api import prove


PRIVATE_REF = "0x" + "ab" * 32
PRIVATE_DESTINATION = "0x" + "cd" * 20


def add_credit(scenario, amount, *, source=PRIVATE_REF, status=5):
    row = {"direction": "deposit", "asset": "USDT", "network": "BSC", "amount": amount,
           "fee": "0", "status": status, "time_ms": int(scenario.clock().timestamp()*1000),
           "source_identity": source, "destination_identity": PRIVATE_DESTINATION}
    scenario.external_deposits.append(row)
    return row


def archive(client, headers):
    response = client.get("/real/exchanges/mexc/transfers?days=7", headers=headers)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["identity_verified"]
    assert PRIVATE_REF not in response.text and PRIVATE_DESTINATION not in response.text
    return {"snapshot_id": result["archive"]["id"],
            "source_refs": [row["source_ref"] for row in result["deposits"]]}


def connect(client):
    token = post(client, "/real/vault/unlock", {}, {"passphrase": PASSWORD})["token"]
    headers = {"X-Orbis-Vault-Token": token}
    post(client, "/real/exchanges/mexc/connect", headers, {"api_key":"fixture-key", "secret_key":"fixture-secret"})
    return headers


def confirm_body(body, preview):
    return {**body, "preview_hash": preview["preview_hash"], "confirmation": "REGISTRAR INGRESOS EXTERNOS"}


def test_external_deposits_restore_cash_without_repeat_and_can_fund_order(tmp_path, monkeypatch):
    scenario = Roundtrip(tmp_path, monkeypatch)
    add_credit(scenario, "15.125")
    add_credit(scenario, "24.875", source="0x" + "ef" * 32)
    scenario.venue.quote = "740"
    account = scenario.env["account"]
    base = f"/real/accounts/{account}"
    path = base + "/mexc-external-deposits"
    with TestClient(scenario.make_app(manual=True)) as client:
        headers = connect(client)
        body = archive(client, headers)
        post(client, path + "/preview", {}, body, status=401)
        post(client, path + "/preview", {**headers, "Origin":"https://outside.invalid"}, body, status=403)
        post(client, path + "/preview", headers, {**body,"source_refs":body["source_refs"][:1]}, status=409)
        preview = post(client, path + "/preview", headers, body)
        assert (preview["book_before"], preview["book_after"], Decimal(preview["amount"])) == ("700", "740", Decimal(40))
        assert not preview["orders_enabled"] and len(preview["lines"]) == 2
        assert client.get(base + "/snapshot", headers=headers).json()["cash"]["USDT"] == "700"
        commit = confirm_body(body, preview)
        post(client, path + "/commit", headers, {**commit,"preview_hash":"0"*64}, status=409)
        saved = post(client, path + "/commit", headers, commit)
        assert saved["status"] == "RECORDED" and len(saved["event_ids"]) == 2
        assert not scenario.env["trader"].posts
        snapshot = client.get(base + "/snapshot", headers=headers).json()
        assert snapshot["cash"]["USDT"] == snapshot["contributions"]["USDT"] == "740.000"
        assert snapshot["realized_pnl"]["USDT"] == "0"

        journeys = path + "/journeys"
        assert client.get(journeys).status_code == 401
        assert client.get(journeys, headers={**headers,"Origin":"https://outside.invalid"}).status_code == 403
        listed = client.get(journeys, headers=headers)
        assert listed.headers["Cache-Control"] == "no-store"
        batch, = listed.json()["items"]
        assert batch["credits"] == 2 and batch["id"] in saved["event_ids"]
        journey = journeys + "/" + batch["id"]
        initial = client.get(journey, headers=headers).json()
        assert Decimal(initial["deposit_amounts"]["credited"]) == 40
        assert initial["source_wallet"] == "UNKNOWN" and initial["withdrawal"] == "MISSING"
        assert initial["next_step"] == "SPOT"
        assert initial["purchase"] is initial["sale"] is None and not initial["complete"]
        for event_id in saved["event_ids"]:
            assert client.get(journeys + "/" + event_id, headers=headers).json() == initial
        assert client.get(journeys + "/" + str(uuid4()), headers=headers).status_code == 409

        # Activar exige una nueva apertura y la clave Trade de la misma cuenta.
        post(client, "/real/exchanges/mexc/trade/connect", headers,
             {"api_key":"fixture-trade", "secret_key":"fixture-secret"})
        limits = post(client, base + "/mexc-manual-limits", headers, {
            "request_id":str(uuid4()), "symbol":"BTCUSDT", "base_asset":"BTC", "quote_asset":"USDT",
            "max_order_quote":"740", "max_daily_turnover_quote":"2000", "max_exposure_quote":"1000",
            "max_daily_loss_quote":"100", "max_fee_bps":"20", "max_slippage_bps":"50",
            "max_data_age_seconds":60, "expires_at":(scenario.clock()+timedelta(hours=1)).isoformat()})
        anchor = post(client, base + "/mexc-spot-parity/observations", headers, {"request_id":str(uuid4())})
        assert anchor["saved"]
        policy = post(client, base + f"/mexc-policy-drafts/{limits['id']}/activate", headers, {
            "request_id":str(uuid4()), "anchor_id":anchor["id"], "confirmation":"ACTIVAR_ORDENES_REALES"})
        orders = base + f"/mexc-trading-sessions/{policy['id']}/orders"
        for side, price, quantity in (("BUY", "65001", "0.011"), ("SELL", "65000", "0.01")):
            trade = post(client, orders, headers, {
                "request_id":str(uuid4()), "side":side, "quantity_base":quantity,
                "limit_price_quote":price, "confirmation":"ENVIAR_ORDEN_REAL"})
            assert trade["journal_status"] == "RESERVATION_CLOSED", trade
        traded = client.get(journey, headers=headers).json()
        assert traded["purchase"]["symbol"] == traded["sale"]["symbol"] == "BTC/USDT"
        assert traded["purchase"]["origin"] == traded["sale"]["origin"] == "MANUAL"
        assert not traded["complete"] and traded["next_step"] == "WITHDRAWAL"

        wallet = Account.create()
        scenario.wallet = wallet.address.lower()
        wallet_book = post(client, "/real/accounts", headers, {"name":"Wallet de destino", "base_currency":"USDT"})["id"]
        prove(client, headers, wallet_book, wallet)
        post(client, f"/real/accounts/{wallet_book}/wallet-links", headers,
             {"address":wallet.address,"chain_id":"0x38","alias":"Destino","confirmation":"VINCULAR"})
        assert client.get(journey.replace(account, wallet_book, 1), headers=headers).status_code == 409
        assert post(client, base+"/mexc-stop", headers, {"confirmation":"DETENER"})["stopped"]
        prove(client, headers, account, wallet)
        plan = post(client, base+"/mexc-withdrawals", headers, {
            "request_id":str(uuid4()), "address":wallet.address, "token_contract":CONTRACT,
            "decimals":18, "amount":AMOUNT, "max_fee":"0.02"})
        withdrawal = base+"/mexc-withdrawals/"+plan["id"]
        submitted = post(client, withdrawal+"/submit", headers, {
            "address":wallet.address, "plan_digest":plan["plan_digest"], "api_key":"fixture-withdraw",
            "secret_key":"fixture-withdraw-secret", "confirmation":"SOLICITAR_RETIRADA_REAL", "fee_quote_only":True})
        assert submitted["status"] == "ACKNOWLEDGED"
        assert client.get(journey, headers=headers).json()["withdrawal"] == "PENDING"
        receipt = post(client, withdrawal+"/receipt", headers,
                       {"address":wallet.address, "evidence":scenario.complete_withdrawal()})
        assert receipt["wallet_received"]
        returned = client.get(journey, headers=headers).json()
        assert returned["withdrawal"] == "RECEIVED" and not returned["complete"]
        assert returned["next_step"] == "WITHDRAWAL"
        post(client, withdrawal+"/book", headers, {
            "address":wallet.address, "mode":"NEW_INTERNAL", "target_account_id":wallet_book,
            "confirmation":"CONTABILIZAR_RETIRADA_RECIBIDA"})
        reply = client.get(journey, headers=headers)
        assert wallet.address.lower() not in reply.text.lower() and PRIVATE_REF not in reply.text
        finished = reply.json()
        assert finished == {**returned,"withdrawal":"BOOKED","complete":True,"next_step":None}
        assert finished["source_wallet"] == "UNKNOWN"
        assert finished["withdrawal_amounts"]["received"] == "40.113456789012345678"
        assert len(scenario.env["trader"].posts) == 2 and len(scenario.withdrawals) == 1 and not scenario.wallet_sends
        post(client, "/real/vault/lock", headers)
        reopened = post(client, "/real/vault/unlock", {}, {"passphrase": PASSWORD})["token"]
        assert client.get(journey, headers={"X-Orbis-Vault-Token":reopened}).json() == finished
        again = post(client, path + "/commit", {"X-Orbis-Vault-Token":reopened}, commit)
        assert again == {"status":"ALREADY_RECORDED", "event_ids":saved["event_ids"]}
        headers = {"X-Orbis-Vault-Token":reopened}
        post(client, "/real/exchanges/mexc/connect", headers, {"api_key":"fixture-key", "secret_key":"fixture-secret"})
        add_credit(scenario, "7.125", source="0x" + "19"*32)
        scenario.venue.quote = str(Decimal(scenario.venue.quote) + Decimal("7.125"))
        following = archive(client, headers)
        following["source_refs"] = sorted(set(following["source_refs"]) - set(body["source_refs"]))
        later = post(client, path+"/commit", headers, confirm_body(
            following, post(client, path+"/preview", headers, following)))
        after_withdrawal = client.get(journeys+"/"+later["event_ids"][0], headers=headers).json()
        assert after_withdrawal["withdrawal"] == "MISSING" and not after_withdrawal["complete"]
        assert client.get(journey, headers=headers).json() == finished
        post(client, "/real/vault/lock", headers)

    vault = RealVault(scenario.env["vault"].path)
    vault.unlock(PASSWORD)
    backup = RealVault(vault.backup(tmp_path / "external-deposits-backup.db"))
    backup.unlock(PASSWORD)
    with backup.session() as db:
        rows = db.query(RealMexcTransferMatch).all()
        assert len(rows) == 3 and {row.event_id for row in rows} == set(saved["event_ids"]+later["event_ids"])
        for row in rows:
            verify_mexc_transfer_match(db, row, check_history=True)
        assert account_snapshot(db, account).contributions["USDT"] == Decimal("747.125")
        from app.portfolio.mexc_roundtrip_audit import review_mexc_direct_journey
        assert review_mexc_direct_journey(db, account, batch["id"]) == finished
    backup.lock(); vault.lock()


def test_later_direct_credit_cuts_prior_interval_and_is_a_separate_group(tmp_path, monkeypatch):
    from app.portfolio.mexc_roundtrip_audit import _next_deposit_at, review_mexc_direct_journey

    scenario = Roundtrip(tmp_path, monkeypatch)
    add_credit(scenario, "40")
    scenario.venue.quote = "740"
    account = scenario.env["account"]
    path = f"/real/accounts/{account}/mexc-external-deposits"
    with TestClient(scenario.make_app(manual=True)) as client:
        headers = connect(client)
        first = archive(client, headers)
        saved = post(client, path+"/commit", headers, confirm_body(first, post(client, path+"/preview", headers, first)))
        scenario.clock.offset += timedelta(seconds=2)
        add_credit(scenario, "7.125", source="0x" + "ef" * 32)
        scenario.venue.quote = "747.125"
        following = archive(client, headers)
        following["source_refs"] = list(set(following["source_refs"]) - set(first["source_refs"]))
        later = post(client, path+"/commit", headers, confirm_body(
            following, post(client, path+"/preview", headers, following)))
        response = client.get(path+"/journeys?limit=1", headers=headers).json()
        assert response["has_more"] and response["items"][0]["id"] == later["event_ids"][0]
        older = client.get(path+"/journeys?limit=1&offset=1", headers=headers).json()
        assert not older["has_more"] and older["items"][0]["id"] == saved["event_ids"][0]
        post(client, "/real/vault/lock", headers)
    vault = RealVault(scenario.env["vault"].path)
    vault.unlock(PASSWORD)
    with vault.session() as db:
        first_match = db.query(RealMexcTransferMatch).filter_by(event_id=saved["event_ids"][0]).one()
        second_match = db.query(RealMexcTransferMatch).filter_by(event_id=later["event_ids"][0]).one()
        assert _next_deposit_at(db, first_match, first_match.confirmed_at) == second_match.confirmed_at
        assert _next_deposit_at(db, second_match, second_match.confirmed_at) is None
        assert review_mexc_direct_journey(db, account, first_match.event_id)["next_step"] is None
        assert review_mexc_direct_journey(db, account, second_match.event_id)["next_step"] == "SPOT"
        second_match.source_digest = "0"*64
        db.commit()
        with pytest.raises(PortfolioError):
            review_mexc_direct_journey(db, account, second_match.event_id)
    vault.lock()


@pytest.mark.parametrize("change", ["balance", "status", "manual-duplicate", "duplicate-selection"])
def test_external_deposit_conflicts_leave_book_unchanged(tmp_path, monkeypatch, change):
    scenario = Roundtrip(tmp_path, monkeypatch)
    credit = add_credit(scenario, "40")
    scenario.venue.quote = "740"
    base = f"/real/accounts/{scenario.env['account']}"
    path = base + "/mexc-external-deposits"
    with TestClient(scenario.make_app(manual=True)) as client:
        headers = connect(client)
        body = archive(client, headers)
        if change == "duplicate-selection":
            post(client, path + "/preview", headers, {**body, "source_refs":body["source_refs"]*2}, status=409)
        else:
            preview = post(client, path + "/preview", headers, body)
            if change == "status":
                credit["status"] = 4
            elif change == "manual-duplicate":
                post(client, base + "/events", headers, {"request_id":str(uuid4()), "kind":"CONTRIBUTION",
                    "quote_currency":"USDT", "amount":"40", "occurred_at":scenario.clock().isoformat()})
                scenario.venue.quote = "780"  # A same-sized gap must not hide the manual event.
            else:
                scenario.venue.quote = "741"
            post(client, path + "/commit", headers, confirm_body(body, preview), status=409)
        snapshot = client.get(base + "/snapshot", headers=headers).json()
        assert Decimal(snapshot["cash"]["USDT"]) == (740 if change == "manual-duplicate" else 700)
        assert not scenario.env["trader"].posts


def test_external_deposits_rollback_all_entries_if_second_write_fails(tmp_path, monkeypatch):
    from app.portfolio import mexc_external_deposits

    scenario = Roundtrip(tmp_path, monkeypatch)
    add_credit(scenario, "15")
    add_credit(scenario, "25", source="0x" + "ef" * 32)
    scenario.venue.quote = "740"
    base = f"/real/accounts/{scenario.env['account']}"
    path = base + "/mexc-external-deposits"
    with TestClient(scenario.make_app(manual=True)) as client:
        headers = connect(client)
        body = archive(client, headers)
        preview = post(client, path + "/preview", headers, body)
        original = mexc_external_deposits.append_event_in_transaction
        calls = []

        def fail_second(*args):
            calls.append(True)
            if len(calls) == 2:
                raise PortfolioError("second write failed")
            return original(*args)

        with monkeypatch.context() as patch:
            patch.setattr(mexc_external_deposits, "append_event_in_transaction", fail_second)
            post(client, path + "/commit", headers, confirm_body(body, preview), status=409)
        assert len(calls) == 2
        assert client.get(base + "/snapshot", headers=headers).json()["cash"]["USDT"] == "700"
        saved = post(client, path + "/commit", headers, confirm_body(body, preview))
        assert saved["status"] == "RECORDED" and len(saved["event_ids"]) == 2
