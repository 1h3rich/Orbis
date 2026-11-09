"""Una cuenta, una bóveda: wallet → depósito → bot → retirada → misma wallet."""

from datetime import timedelta
from decimal import Decimal,localcontext
import time
from uuid import uuid4

from eth_account import Account
from fastapi.testclient import TestClient
import pytest

from app.portfolio.domain import realized_pnl_by_currency
from app.portfolio.models import RealEvent,RealWalletFundingBooking,RealMexcWithdrawalBooking,RealLiveStop
from app.portfolio.repository import account_snapshot,verify_paired_transfers
from app.portfolio.wallet_funding import _encoded
from app.portfolio.mexc_withdrawal_journal import withdrawal_record
from app.portfolio.vault import RealVault
from tests.mexc_roundtrip_fixture import Roundtrip,AMOUNT,CONTRACT,DEPOSIT_HASH
from tests.test_mexc_execution_state import PASSWORD
from tests.test_wallet_funding_api import prove


def post(client,path,headers,body=None,status=200):
    response = client.post(path,headers=headers,json=body)
    assert response.status_code == status,response.text
    return response.json()


def wait_for_bot(client,url,headers,count):
    until = time.monotonic()+8
    while True:
        response = client.get(url,headers=headers)
        assert response.status_code == 200,response.text
        value = response.json()[0]
        if value["position"]["verified"] and value["position"]["fill_count"] == count:
            return value
        assert time.monotonic() < until,value
        time.sleep(0.03)


@pytest.mark.parametrize("lost_withdrawal_reply,other_destination,legacy_withdrawal", [
    (False,False,False),(True,False,False),(False,True,False),(False,False,True),
], ids=["normal", "lost-reply", "other-wallet", "legacy"])
def test_single_account_roundtrip_conserves_principal_books_fills_fees_and_reopens_without_resending(
        tmp_path,monkeypatch,lost_withdrawal_reply,other_destination,legacy_withdrawal):
    scenario = Roundtrip(tmp_path,monkeypatch)
    env = scenario.env
    wallet = Account.create()
    with TestClient(scenario.make_app()) as client:
        token = post(client,"/real/vault/unlock",{}, {"passphrase":PASSWORD})["token"]
        headers = {"X-Orbis-Vault-Token":token}
        account,venue = env["account"],scenario.venue
        base = f"/real/accounts/{account}"
        source = post(client,"/real/accounts",headers,{"name":"Wallet propia","base_currency":"USDT"})["id"]
        prove(client,headers,source,wallet)
        post(client,f"/real/accounts/{source}/wallet-links",headers,
             {"address":wallet.address,"chain_id":"0x38","alias":"Principal","confirmation":"VINCULAR"})
        post(client,f"/real/accounts/{source}/events",headers,{"request_id":str(uuid4()),"kind":"CONTRIBUTION",
             "quote_currency":"USDT","amount":"100","occurred_at":(scenario.clock()-timedelta(days=1)).isoformat()})
        post(client,"/real/exchanges/mexc/connect",headers,{"api_key":"fixture-key","secret_key":"fixture-secret"})
        prove(client,headers,account,wallet)
        plan = post(client,base+"/wallet-funding",headers,{"request_id":str(uuid4()),"address":wallet.address,
             "chain_id":"0x38","asset":"USDT","amount":AMOUNT,"minimum_deposit_usdt":AMOUNT,
             "decimals":18,"nonce":"3","gas":"90000",
             "gas_price":"1000000000","max_gas_bnb":"0.0001"})
        funding = base+"/wallet-funding/"+plan["id"]
        report_url = funding+"/roundtrip"
        report_body = {"address":wallet.address}

        def report():
            reply = client.post(report_url,headers=headers,json=report_body)
            assert reply.status_code == 200,reply.text
            assert reply.headers["Cache-Control"] == "no-store"
            assert wallet.address.lower() not in reply.text.lower() and DEPOSIT_HASH not in reply.text
            return reply.json()

        def archived_report():
            reply = client.get(report_url,headers={**headers,"X-Orbis-Passive":"1"})
            assert reply.status_code == 200,reply.text
            assert reply.headers["Cache-Control"] == "no-store"
            assert wallet.address.lower() not in reply.text.lower() and DEPOSIT_HASH not in reply.text
            assert "roundtrip_wallet" not in reply.text and "funding_digest" not in reply.text
            return reply.json()

        assert client.get(report_url).status_code == 401
        assert client.get(report_url,headers={**headers,"Origin":"https://outside.invalid"}).status_code == 403
        assert client.get(report_url.replace(account,source,1),headers=headers).status_code == 409
        assert client.post(report_url,json=report_body).status_code == 401
        assert client.post(report_url,json=report_body,headers={**headers,"Origin":"https://outside.invalid"}).status_code == 403
        post(client,report_url.replace(account,source,1),headers,report_body,status=409)
        wrong_wallet = Account.create()
        prove(client,headers,account,wrong_wallet)
        post(client,report_url,headers,{"address":wrong_wallet.address},status=409)
        pending_report = {"deposit":"PENDING","purchase":None,"sale":None,"withdrawal":"MISSING","complete":False,
                          "deposit_amounts":None,"withdrawal_amounts":None,"next_step":None}
        assert report() == archived_report() == pending_report
        grant = {"address":wallet.address,"plan_digest":plan["plan_digest"],"confirmation":"SOLICITAR_ENVIO_WALLET"}
        assert post(client,funding+"/claim",headers,grant)["claim_granted"]
        rpc = scenario.wallet_send(plan["transaction"])["evidence"]
        post(client,funding+"/hash",headers,{"tx_hash":DEPOSIT_HASH})
        pending = post(client,funding+"/check",headers,{"evidence":rpc})
        assert not pending["exchange_credited"] and len(scenario.wallet_sends) == 1
        assert client.get(base+"/snapshot",headers=headers).json()["cash"]["USDT"] == "700"
        scenario.credit_funding()
        credited = post(client,funding+"/check",headers,{"evidence":rpc})
        assert credited["exchange_credited"] and not credited["booked"]
        credited_report = {**pending_report,"deposit":"CREDITED",
                           "deposit_amounts":{"credited":AMOUNT,"gas_bnb":"0.000060000000000000"}}
        assert report() == credited_report
        # La mera acreditación no sustituye el registro ni la nueva apertura.
        different = post(client,base+"/mexc-spot-parity/observations",headers,{"request_id":str(uuid4())})
        assert not different["saved"]
        options = post(client,funding+"/booking-options",headers,{"address":wallet.address})
        origin = options["internal"]
        body = {"mode":"NEW_INTERNAL","address":wallet.address,"source_account_id":source,
                "source_revision":origin["source_revision"],"confirmation":"REGISTRAR_INGRESO_ACREDITADO"}
        assert post(client,funding+"/book",headers,body)["booked"]
        assert post(client,funding+"/book",headers,body)["booked"]
        assert not post(client,funding+"/claim",headers,grant)["claim_granted"]
        assert report() == {**credited_report,"deposit":"BOOKED","next_step":"SPOT"}
        with localcontext() as context:
            context.prec = 300
            assert Decimal(client.get(base+"/snapshot",headers=headers).json()["cash"]["USDT"]) == Decimal("700")+Decimal(AMOUNT)
        post(client,"/real/exchanges/mexc/trade/connect",headers,{"api_key":"fixture-trade","secret_key":"fixture-secret"})
        anchor = post(client,base+"/mexc-spot-parity/observations",headers,{"request_id":str(uuid4())})
        assert anchor["saved"]
        bot_id = str(uuid4())
        bot_url = base+"/mexc-bots"
        activation = {"request_id":bot_id,"draft_id":env["draft"].policy_id,"anchor_id":anchor["id"],
            "paper_session_id":env["session"]["id"],"research_validation_id":env["validation"]["id"],"entry_budget_quote":"20","confirmation":"ACTIVAR_BOT_REAL"}
        bot = post(client,bot_url,headers,activation)
        assert bot["execution_enabled"] and len(env["trader"].posts) == 0
        assert scenario.advance()["poll_result"] == "OBSERVED"
        wait_for_bot(client,bot_url,headers,1)
        bought = report()
        assert bought["purchase"]["symbol"] == "BTC/USDT" and bought["purchase"]["origin"] == "BOT"
        assert bought["sale"] is None and not bought["complete"] and bought["next_step"] == "SPOT"
        scenario.target()
        final_bot = wait_for_bot(client,bot_url,headers,2)
        assert len(env["trader"].posts) == 2
        assert Decimal(final_bot["position"]["units"]) == Decimal(venue.base)
        stopped = post(client,base+"/mexc-stop",headers,{"confirmation":"DETENER"})
        assert stopped["stopped"]
        assert all(worker.join(2) for worker in scenario.workers)
        assert report()["next_step"] == "WITHDRAWAL"
        # La retirada usa otra credencial, no el permiso Trade del bot.
        destination = wallet
        if other_destination:
            destination = wrong_wallet
            scenario.wallet = destination.address.lower()
            prove(client,headers,source,destination)
            post(client,f"/real/accounts/{source}/wallet-links",headers,
                 {"address":destination.address,"chain_id":"0x38","alias":"Secundaria","confirmation":"VINCULAR"})
        withdrawal_plan = post(client,base+"/mexc-withdrawals",headers,{"request_id":str(uuid4()),
             "address":destination.address,"token_contract":CONTRACT,"decimals":18,"amount":AMOUNT,"max_fee":"0.02"})
        if legacy_withdrawal:
            # A plan from the old app retains its original format through
            # submission, finality and booking after upgrading.
            fixture_vault = RealVault(env["vault"].path)
            fixture_vault.unlock(PASSWORD)
            try:
                with fixture_vault.session() as db:
                    row, terms, _ = withdrawal_record(db, withdrawal_plan["id"])
                    terms.pop("roundtrip_wallet")
                    row.payload_json, row.payload_digest = _encoded(terms)
                    withdrawal_plan["plan_digest"] = row.payload_digest
                    db.commit()
            finally:
                fixture_vault.lock()
        withdrawal = base+"/mexc-withdrawals/"+withdrawal_plan["id"]
        scenario.withdrawal_timeout = lost_withdrawal_reply
        submission = {"address":destination.address,"plan_digest":withdrawal_plan["plan_digest"],"api_key":"fixture-withdraw",
            "secret_key":"fixture-withdraw-secret","confirmation":"SOLICITAR_RETIRADA_REAL","fee_quote_only":True}
        result = post(client,withdrawal+"/submit",headers,submission)
        assert result["status"] == ("SUBMISSION_UNKNOWN" if lost_withdrawal_reply else "ACKNOWLEDGED")
        assert not result["wallet_received"] and not result["booked"]
        awaiting_receipt = report()
        assert awaiting_receipt["withdrawal"] == ("OTHER_WALLET" if other_destination else "PENDING")
        assert awaiting_receipt["next_step"] == (None if other_destination else "WITHDRAWAL")
        assert awaiting_receipt["withdrawal_amounts"] is None
        if legacy_withdrawal:
            assert archived_report() == {**awaiting_receipt,"withdrawal":"DESTINATION_UNVERIFIED","next_step":None}
        else:
            assert archived_report() == awaiting_receipt
        assert not post(client,withdrawal+"/submit",headers,submission)["post_attempted"]
        assert post(client,withdrawal+"/observe",headers)["status"] == "MEXC_PENDING"
        rpc = scenario.complete_withdrawal()
        receipt = post(client,withdrawal+"/receipt",headers,{"address":destination.address,"evidence":rpc})
        assert receipt["wallet_received"] and not receipt["booked"]
        received = report()
        assert received["withdrawal"] == ("OTHER_WALLET" if other_destination else "RECEIVED")
        assert received["next_step"] == (None if other_destination else "WITHDRAWAL")
        assert received["withdrawal_amounts"] == (None if other_destination else
            {"received":"40.113456789012345678","actual_fee":"0.010000000000000000","actual_debit":AMOUNT})
        assert not received["complete"]
        before = client.get(f"/real/accounts/{source}/snapshot",headers=headers).json()
        with localcontext() as context:
            context.prec = 300
            assert Decimal(before["cash"]["USDT"]) == Decimal("100")-Decimal(AMOUNT)
        choice = {"address":destination.address,"mode":"NEW_INTERNAL","target_account_id":source,
                  "confirmation":"CONTABILIZAR_RETIRADA_RECIBIDA"}
        recorded = post(client,withdrawal+"/book",headers,choice)
        assert recorded["booked"] and not recorded["trading_reopened"]
        assert post(client,withdrawal+"/book",headers,choice) == recorded
        completed = report()
        if other_destination:
            assert completed == received
        else:
            assert completed == {**received,"withdrawal":"BOOKED","complete":True,"next_step":None}
        assert completed["purchase"] == bought["purchase"]
        assert completed["sale"]["symbol"] == "BTC/USDT" and completed["sale"]["origin"] == "BOT"
        assert completed["purchase"]["intent_id"] != completed["sale"]["intent_id"]
        for key, fill, direction in zip(("purchase","sale"),venue.trades,(1,-1),strict=True):
            order = completed[key]
            gross_base, gross_quote, fee = map(Decimal, (fill["quantity"],fill["quote_quantity"],fill["commission"]))
            assert (order["base_asset"],order["quote_asset"],order["fill_count"]) == ("BTC","USDT",1)
            assert (Decimal(order["gross_base"]),Decimal(order["gross_quote"])) == (gross_base,gross_quote)
            assert {asset:Decimal(amount) for asset,amount in order["net_by_asset"].items()} == {
                "BTC":direction*gross_base,"USDT":-direction*gross_quote-fee}
            assert {asset:Decimal(amount) for asset,amount in order["fee_by_asset"].items()} == {"USDT":fee}
        after = client.get(f"/real/accounts/{source}/snapshot",headers=headers).json()
        assert Decimal(after["cash"]["USDT"]) == scenario.wallet_usdt == Decimal("99.99")
        assert after["contributions"] == {"USDT":"100"} and not after["withdrawals"]
        exchange = client.get(base+"/snapshot",headers=headers).json()
        assert Decimal(exchange["cash"]["USDT"]) == Decimal(venue.quote)
        assert len(scenario.withdrawals) == len(scenario.wallet_sends) == 1
        assert len(env["trader"].posts) == 2
        status = client.get(base+"/mexc-execution-status",headers=headers).json()
        assert status["stopped"] and not status["withdrawal_pending"]
        post(client,"/real/vault/lock",headers)
        headers = {"X-Orbis-Vault-Token":post(client,"/real/vault/unlock",{}, {"passphrase":PASSWORD})["token"]}
        assert client.get(bot_url,headers=headers).json()[0]["execution_enabled"] is False
        assert client.get(base+"/wallet-funding",headers=headers).json()[0]["booked"]
        assert client.get(base+"/mexc-withdrawals",headers=headers).json()[0]["booked"]
        post(client,report_url,headers,report_body,status=409)
        if legacy_withdrawal:
            assert archived_report() == {**completed,"complete":False,"withdrawal":"DESTINATION_UNVERIFIED",
                                         "withdrawal_amounts":None}
        else:
            assert archived_report() == completed
        prove(client,headers,account,wallet)
        assert report() == completed
        # Abrir no concede otra retirada ni reinicia las operaciones anteriores.
        assert client.get(f"/real/accounts/{source}/snapshot",headers=headers).json()["cash"]["USDT"] == after["cash"]["USDT"]
        assert len(env["trader"].posts) == 2 and len(scenario.withdrawals) == 1
        post(client,"/real/vault/lock",headers)
    vault = env["vault"]; vault.unlock(PASSWORD)
    with vault.session() as db:
        assert verify_paired_transfers(db) == 2
        assert db.query(RealWalletFundingBooking).count() == db.query(RealMexcWithdrawalBooking).count() == 1
        assert db.query(RealEvent).filter(RealEvent.kind.in_(("BUY","SELL"))).count() == 2
        assert db.get(RealLiveStop,venue.ref).stopped
        src,dst = account_snapshot(db,source),account_snapshot(db,account)
        assert src.contributions["USDT"]+dst.contributions["USDT"] == 800
        assert not src.withdrawals and not dst.withdrawals
        assert realized_pnl_by_currency(src)["USDT"] == 0
        assert dst.cash_fees["USDT"] == Decimal("0.01")
        with localcontext() as context:
            context.prec = 300
            # A coste, efectivo + inventario restante = capital + P/L realizado.
            # No se promete liquidar el residuo por debajo de tamaño de lote.
            cost = sum((position.cost_basis for position in dst.positions.values()),Decimal(0))
            cash = src.cash["USDT"]+dst.cash["USDT"]
            pnl = realized_pnl_by_currency(src)["USDT"]+realized_pnl_by_currency(dst)["USDT"]
            assert cash+cost == Decimal("800")+pnl
    backup = vault.backup(tmp_path / "roundtrip-backup.db"); vault.lock()
    copied = RealVault(backup); copied.unlock(PASSWORD)
    with copied.session() as db:
        assert verify_paired_transfers(db) == 2 and account_snapshot(db,source).cash["USDT"] == Decimal("99.99")
        from app.portfolio.mexc_roundtrip_audit import review_mexc_roundtrip
        copied_report = review_mexc_roundtrip(db, account, plan["id"])
        assert copied_report["withdrawal"] == ("DESTINATION_UNVERIFIED" if legacy_withdrawal else completed["withdrawal"])
    copied.lock()


@pytest.mark.parametrize("other_assets", [False, True], ids=["one-pair", "multiasset"])
def test_existing_mexc_assets_can_trade_and_return_to_linked_wallet(tmp_path, monkeypatch, other_assets):
    scenario = Roundtrip(tmp_path, monkeypatch)
    scenario.env["vault"] = RealVault(tmp_path / "existing-balance.db")
    scenario.venue.base = "0.002"
    if other_assets:
        scenario.venue.other = [
            {"asset": "ETH", "free": "1.25", "locked": "0", "total": "1.25"},
            {"asset": "BNB", "free": "0.75", "locked": "0", "total": "0.75"},
        ]
    wallet = Account.create()
    scenario.wallet = wallet.address.lower()
    with TestClient(scenario.make_app(manual=True)) as client:
        token = post(client, "/real/vault/create", {}, {"passphrase": PASSWORD})["token"]
        headers = {"X-Orbis-Vault-Token": token}
        exchange = post(client, "/real/accounts", headers, {"name": "Saldo MEXC", "base_currency": "USDT"})["id"]
        wallet_account = post(client, "/real/accounts", headers, {"name": "Wallet", "base_currency": "USDT"})["id"]
        base = f"/real/accounts/{exchange}"
        target = f"/real/accounts/{wallet_account}"
        prove(client, headers, wallet_account, wallet)
        post(client, target + "/wallet-links", headers,
             {"address": wallet.address, "chain_id": "0x38", "alias": "Principal", "confirmation": "VINCULAR"})
        post(client, target + "/events", headers, {
            "request_id": str(uuid4()), "kind": "CONTRIBUTION", "quote_currency": "USDT",
            "amount": "100", "occurred_at": (scenario.clock() - timedelta(days=1)).isoformat()})
        post(client, "/real/exchanges/mexc/connect", headers, {"api_key": "fixture-key", "secret_key": "fixture-secret"})
        opening = {"quote_currency": "USDT", "costs": {"BTC": "60000"}, "costs_confirmed": True}
        if other_assets:
            opening["costs"].update(ETH="2400", BNB="600")
        preview = post(client, base + "/mexc-opening/preview", headers, opening)
        opening.update(observed_at=preview["occurred_at"], preview_hash=preview["preview_hash"],
                       confirmation="IMPORTAR_SALDO_INICIAL")
        recorded = post(client, base + "/mexc-opening/commit", headers, opening)
        assert recorded["counts"]["new"] == (4 if other_assets else 2)
        assert recorded["orders_enabled"] is False
        post(client, "/real/exchanges/mexc/trade/connect", headers, {
            "api_key": "fixture-trade", "secret_key": "fixture-secret"})
        limits = post(client, base + "/mexc-manual-limits", headers, {
            "request_id": str(uuid4()), "symbol": "BTCUSDT", "base_asset": "BTC", "quote_asset": "USDT",
            "max_order_quote": "150", "max_daily_turnover_quote": "500", "max_exposure_quote": "1000",
            "max_daily_loss_quote": "100", "max_fee_bps": "20", "max_slippage_bps": "50",
            "max_data_age_seconds": 60, "expires_at": (scenario.clock() + timedelta(hours=1)).isoformat()})
        anchor = post(client, base + "/mexc-spot-parity/observations", headers, {"request_id": str(uuid4())})
        assert anchor["saved"]
        session = post(client, base + f"/mexc-policy-drafts/{limits['id']}/activate", headers, {
            "request_id": str(uuid4()), "anchor_id": anchor["id"], "confirmation": "ACTIVAR_ORDENES_REALES"})
        assert session["mode"] == "MANUAL" and not scenario.env["trader"].posts
        orders = base + f"/mexc-trading-sessions/{session['id']}/orders"
        for side, price in (("BUY", "65001"), ("SELL", "65000")):
            trade = post(client, orders, headers, {
                "request_id": str(uuid4()), "side": side, "quantity_base": "0.001",
                "limit_price_quote": price, "confirmation": "ENVIAR_ORDEN_REAL"})
            assert trade["journal_status"] == "RESERVATION_CLOSED"
        assert post(client, base + "/mexc-stop", headers, {"confirmation": "DETENER"})["stopped"]
        prove(client, headers, exchange, wallet)
        plan = post(client, base + "/mexc-withdrawals", headers, {
            "request_id": str(uuid4()), "address": wallet.address, "token_contract": CONTRACT,
            "decimals": 18, "amount": AMOUNT, "max_fee": "0.02"})
        withdrawal = base + f"/mexc-withdrawals/{plan['id']}"
        submission = {"address": wallet.address, "plan_digest": plan["plan_digest"],
                      "api_key": "fixture-withdraw", "secret_key": "fixture-withdraw-secret",
                      "confirmation": "SOLICITAR_RETIRADA_REAL", "fee_quote_only": True}
        assert post(client, withdrawal + "/submit", headers, submission)["status"] == "ACKNOWLEDGED"
        receipt = post(client, withdrawal + "/receipt", headers, {
            "address": wallet.address, "evidence": scenario.complete_withdrawal()})
        assert receipt["wallet_received"]
        booked = post(client, withdrawal + "/book", headers, {
            "address": wallet.address, "mode": "NEW_INTERNAL", "target_account_id": wallet_account,
            "confirmation": "CONTABILIZAR_RETIRADA_RECIBIDA"})
        assert booked["booked"] and not booked["trading_reopened"]
        target_snapshot = client.get(target + "/snapshot", headers=headers).json()
        exchange_snapshot = client.get(base + "/snapshot", headers=headers).json()
        assert Decimal(target_snapshot["cash"]["USDT"]) == scenario.wallet_usdt
        assert target_snapshot["contributions"] == {"USDT": "100"}
        assert Decimal(exchange_snapshot["cash"]["USDT"]) == Decimal(scenario.venue.quote)
        assert Decimal(exchange_snapshot["cash_fees"]["USDT"]) == Decimal("0.01")
        if other_assets:
            holdings = {(row["asset"], row["quote_currency"]): row for row in exchange_snapshot["positions"]}
            assert Decimal(holdings[("ETH", "USDT")]["quantity"]) == Decimal("1.25")
            assert Decimal(holdings[("ETH", "USDT")]["cost_basis"]) == Decimal("3000")
            assert Decimal(holdings[("BNB", "USDT")]["quantity"]) == Decimal("0.75")
            assert Decimal(holdings[("BNB", "USDT")]["cost_basis"]) == Decimal("450")
        assert not scenario.wallet_sends and len(scenario.withdrawals) == 1
        assert len(scenario.env["trader"].posts) == 2
        post(client, "/real/vault/lock", headers)


def test_roundtrip_cutoff_uses_when_a_later_deposit_was_booked(tmp_path):
    from app.portfolio.mexc_roundtrip_audit import _next_deposit_at, review_mexc_roundtrip
    from app.portfolio.mexc_transfers import save_transfer_snapshot
    from app.portfolio.wallet_funding import funding_record, report_funding
    from app.portfolio.wallet_funding_credit import book_credit, confirm_credit
    from tests.test_wallet_funding import ACCOUNT_REF, WALLET, claim, prepare
    from tests.test_wallet_funding_credit import credited_setup, evidence, snapshot

    vault,account,now,older,_,_ = credited_setup(tmp_path)
    later = now + timedelta(seconds=4)
    newer = prepare(vault,account,later,nonce="4")
    assert claim(vault,account,later,newer)["claim_granted"]
    tx_hash = "0x"+"9"*64
    batch = snapshot(later,tx_hash=tx_hash)
    batch["deposits"].extend(snapshot(now)["deposits"])
    with vault.session() as db:
        report_funding(db,newer["id"],account_id=account,tx_hash=tx_hash,now=later)
        snapshot_id = save_transfer_snapshot(db,ACCOUNT_REF,batch,view_uid="123456789")["id"]
        proof = evidence(newer,later,tx_hash=tx_hash)
        assert confirm_credit(db,newer["id"],account,proof,snapshot_id,now=later)["exchange_credited"]
        for plan,booked_at in ((newer,later),(older,later+timedelta(microseconds=1))):
            assert book_credit(db,plan["id"],account,mode="NEW_CONTRIBUTION",
                               wallet_address=WALLET,now=booked_at)["booked"]
        first,_,_ = funding_record(db,newer["id"])
        second,_,_ = funding_record(db,older["id"])
        assert _next_deposit_at(db,first,later) == later+timedelta(microseconds=1)
        assert _next_deposit_at(db,second,later+timedelta(microseconds=1)) is None
        assert review_mexc_roundtrip(db, account, newer["id"])["next_step"] is None
        assert review_mexc_roundtrip(db, account, older["id"])["next_step"] == "SPOT"
    vault.lock()


def test_roundtrip_cutoff_includes_an_external_mexc_credit(tmp_path):
    from app.portfolio.mexc_external_deposits import external_deposit_review
    from app.portfolio.mexc_roundtrip_audit import _next_deposit_at
    from app.portfolio.mexc_transfers import read_transfer_snapshot, save_transfer_snapshot
    from app.portfolio.repository import account_book_revision
    from app.portfolio.wallet_funding import funding_record
    from app.portfolio.wallet_funding_credit import book_credit
    from tests.test_wallet_funding import ACCOUNT_REF, WALLET
    from tests.test_wallet_funding_credit import credited_setup, snapshot

    vault, account, now, plan, _, _ = credited_setup(tmp_path)
    confirmed_at = now + timedelta(minutes=2)
    # The exchange credit predates booking the on-chain deposit. Only the
    # confirmation in this book defines when its funds enter this interval.
    observed_at = now - timedelta(seconds=2)
    batch = snapshot(observed_at, amount="5.125", tx_hash="0x"+"66"*32)
    with vault.session() as db:
        assert book_credit(db,plan["id"],account,mode="NEW_CONTRIBUTION",
                           wallet_address=WALLET,now=now)["booked"]
        funding, _, _ = funding_record(db,plan["id"])
        snapshot_id = save_transfer_snapshot(db,ACCOUNT_REF,batch,view_uid="123456789")["id"]
        fresh = save_transfer_snapshot(db,ACCOUNT_REF,{**batch,"observed_at":confirmed_at.isoformat(),
            "end_ms":int(confirmed_at.timestamp()*1000)},view_uid="123456789")["id"]
        refs = [row["source_ref"] for row in read_transfer_snapshot(db,snapshot_id)["deposits"]]
        cash = account_snapshot(db,account).cash["USDT"]
        parity = {"account_id":account,"blockers":["QUANTITY_DIFFERENCE"],
                  "book_revision":account_book_revision(db,account),"checked_at":confirmed_at.isoformat(),
                  "assets":[{"asset":"USDT","quantity_equal":False,"difference_mexc_minus_book":"5.125",
                             "book_quantity":str(cash),"mexc_spot_quantity":str(cash+Decimal("5.125"))}]}
        preview = external_deposit_review(db,account,snapshot_id,refs,parity,now=confirmed_at)
        assert _next_deposit_at(db,funding,now) is None
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        assert external_deposit_review(db,account,snapshot_id,refs,parity,now=confirmed_at,
            expected_hash=preview["preview_hash"],current_snapshot_id=fresh)["status"] == "RECORDED"
        db.commit()
        assert _next_deposit_at(db,funding,now) == confirmed_at
        assert _next_deposit_at(db,funding,confirmed_at+timedelta(microseconds=1)) is None
    vault.lock()
