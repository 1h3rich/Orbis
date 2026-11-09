"""Wallet de prueba + View/retirada independientes; ninguna petición de red real."""

from copy import deepcopy
import hashlib
import hmac
from datetime import datetime,timezone
from decimal import Decimal,localcontext
from urllib.parse import parse_qs,urlparse
from uuid import UUID,uuid4

from eth_account import Account
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.portfolio.api import create_real_router
from app.live_execution.mexc_withdrawal_transport import MexcWithdrawalClient
from tests.test_wallet_funding_api import BscFakeMexc,PASSWORD,prove
from tests.test_mexc_deposit_destination import KEY,SECRET,CONTRACT
from tests.test_mexc_withdrawal import source


class WithdrawalView(BscFakeMexc):
    def balances(self):
        with localcontext() as context:
            context.prec = 300
            free = Decimal("100")-sum((Decimal(row["amount"][0]) for row in WithdrawalClient.posts),Decimal(0)) if WithdrawalClient.completed else Decimal("100")
        return {"source":"MEXC_SPOT_PRIVATE_READONLY","accounting":"OBSERVED_NOT_IMPORTED",
            "observed_at":datetime.now(timezone.utc).isoformat(),
            "balances":[{"asset":"USDT","free":str(free),"locked":"0","total":str(free)}]}

    def bsc_withdrawal_route(self,address):
        return {**source(datetime.now(timezone.utc))["route"],"token_contract":CONTRACT}

    def transfers_recent(self,**kwargs):
        from tests.test_mexc_withdrawal_receipt import snapshot
        from tests.test_wallet_funding import TX_HASH
        now = datetime.now(timezone.utc)
        rows = []
        for index,post in enumerate(WithdrawalClient.posts):
            value = snapshot({"id":str(UUID(post["withdrawOrderId"][0]))},WithdrawalClient.times[index],observed=now,
                status=7 if WithdrawalClient.completed else 8 if WithdrawalClient.rejected else 4,
                chain=TX_HASH if WithdrawalClient.completed else None,amount=post["amount"][0],
                fee="0.01" if WithdrawalClient.completed else "0",destination=post["address"][0],
                external=f"fixture-external-withdrawal-id-{index}")
            rows.extend(value["withdrawals"])
        result = snapshot({"id":str(uuid4())},now,observed=now)
        return {**result,"withdrawals":rows}


class WithdrawalClient(MexcWithdrawalClient):
    posts = []
    times = []
    completed = False
    rejected = False
    mode = "ok"
    during_pair = None

    def __init__(self,key,secret):
        def get(url,headers):
            if type(self).during_pair:
                type(self).during_pair()
            if urlparse(url).path == "/api/v3/apiKeyInfo":
                return {"status":"VALID","permissions":"SPOT_ACCOUNT_READ,SPOT_WITHDRAW_READ,SPOT_WITHDRAW_WRITE","apikey":key}
            return {"uid":"123456789" if key == "withdraw-key-fixture" else "987654321"}
        def post(url,body,headers):
            type(self).posts.append(parse_qs(body))
            type(self).times.append(datetime.now(timezone.utc))
            if type(self).mode == "unknown":
                raise RuntimeError("fixture request accepted before connection was lost")
            return {"id":f"fixture-external-withdrawal-id-{len(type(self).posts)-1}"}
        super().__init__(key,secret,http_get=get,http_post=post)


class WithdrawalBnbRpc:
    """Owned BNB evidence, scoped to the withdrawal hash reported by View."""
    def __init__(self, evidence=None):
        self.evidence = evidence
        self.calls = []
        self.chain = "0x38"
        self.hook = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def __call__(self, method, params):
        self.calls.append((method, params))
        if self.hook:
            self.hook(method)
        if method == "eth_chainId":
            assert params == []
            return self.chain
        if self.evidence is None:
            assert method in ("eth_getTransactionByHash", "eth_getTransactionReceipt")
            return None
        evidence = self.evidence
        if method in ("eth_getTransactionByHash", "eth_getTransactionReceipt"):
            assert params == [evidence["tx_hash"]]
            return deepcopy(evidence["transaction" if method == "eth_getTransactionByHash" else "receipt"])
        assert method == "eth_getBlockByNumber"
        assert params in (["finalized", False], [evidence["block"]["number"], False])
        return deepcopy(evidence["finalized" if params[0] == "finalized" else "block"])


def setup(tmp_path, *, rpc_factory=None):
    WithdrawalClient.posts,WithdrawalClient.mode,WithdrawalClient.during_pair = [],"ok",None
    WithdrawalClient.times,WithdrawalClient.completed,WithdrawalClient.rejected = [],False,False
    app = FastAPI()
    app.include_router(create_real_router(tmp_path / "real.db",mexc_factory=WithdrawalView,
        mexc_withdrawal_factory=WithdrawalClient,dex_rpc_factory=rpc_factory))
    client = TestClient(app)
    token = client.post("/real/vault/create",json={"passphrase":PASSWORD}).json()["token"]
    headers = {"X-Orbis-Vault-Token":token}
    account = client.post("/real/accounts",headers=headers,json={"name":"MEXC","base_currency":"USDT"}).json()["id"]
    client.post("/real/exchanges/mexc/connect",headers=headers,json={"api_key":KEY,"secret_key":SECRET})
    client.post(f"/real/accounts/{account}/mexc-link",headers=headers,json={"confirmation":"VINCULAR"})
    event = {"request_id":str(uuid4()),"kind":"CONTRIBUTION","quote_currency":"USDT","amount":"100",
             "occurred_at":datetime.now(timezone.utc).isoformat()}
    assert client.post(f"/real/accounts/{account}/events",headers=headers,json=event).status_code == 200
    wallet = Account.create()
    prove(client,headers,account,wallet)
    return client,headers,account,wallet


def plan(client,headers,account,wallet):
    url = f"/real/accounts/{account}/mexc-withdrawals"
    payload = {"request_id":str(uuid4()),"address":wallet.address,"token_contract":CONTRACT,"amount":"10.123456789012345678","max_fee":"0.02","decimals":18}
    return url,payload


def submit_body(wallet,prepared):
    return {"address":wallet.address,"plan_digest":prepared["plan_digest"],"api_key":"withdraw-key-fixture",
        "secret_key":"withdraw-secret-fixture","confirmation":"SOLICITAR_RETIRADA_REAL","fee_quote_only":True}


def test_estimate_max_requires_proof_stop_and_view_but_creates_no_plan_or_post(tmp_path):
    client,headers,account,wallet = setup(tmp_path)
    url,payload = plan(client,headers,account,wallet)
    path = url+"/estimate-max"
    body = {key:payload[key] for key in ("address","token_contract","decimals")}
    assert client.post(path,json=body).status_code == 401
    assert client.post(path,headers={**headers,"Origin":"https://outside.invalid"},json=body).status_code == 403
    assert client.post(path,headers=headers,json={**body,"api_key":"unused"}).status_code == 422
    assert client.post(path,headers=headers,json=body).status_code == 409
    assert client.post(f"/real/accounts/{account}/mexc-stop",headers=headers,json={"confirmation":"DETENER"}).is_success
    assert client.post("/real/exchanges/mexc/disconnect",headers=headers).is_success
    missing_view = client.post(path,headers=headers,json=body)
    assert missing_view.status_code == 409 and "MEXC View" in missing_view.text
    assert client.post("/real/exchanges/mexc/connect",headers=headers,json={"api_key":KEY,"secret_key":SECRET}).is_success
    assert client.post(path,headers=headers,json={**body,"address":Account.create().address}).status_code == 409
    assert client.post(path,headers=headers,json={**body,"token_contract":"0x"+"45"*20}).status_code == 409
    assert client.post(path,headers=headers,json={**body,"max_fee":"0.005"}).status_code == 409
    quote = client.post(path,headers=headers,json=body)
    assert quote.status_code == 200 and quote.headers["cache-control"] == "no-store",quote.text
    assert wallet.address.lower() not in quote.text.lower()
    assert quote.json()["amount"] == "99.99" and quote.json()["max_fee"] == "0.01"
    chosen = client.post(path,headers=headers,json={**body,"max_fee":"0.025"}).json()
    assert (chosen["amount"],chosen["max_fee"]) == ("99.975","0.025")
    assert client.get(url,headers=headers).json() == [] and not WithdrawalClient.posts
    assert client.get(f"/real/accounts/{account}/snapshot",headers=headers).json()["cash"]["USDT"] == "100"
    result = client.post(url,headers=headers,json={**payload,"amount":chosen["amount"],"max_fee":chosen["max_fee"]})
    assert result.status_code == 200 and result.json()["max_debit"] == chosen["max_debit"],result.text
    assert client.post(path,headers=headers,json=body).status_code == 409 and not WithdrawalClient.posts
    prepared = result.json()
    sent = client.post(url+"/"+prepared["id"]+"/submit",headers=headers,json=submit_body(wallet,prepared))
    assert sent.status_code == 200 and len(WithdrawalClient.posts) == 1,sent.text
    assert Decimal(WithdrawalClient.posts[0]["amount"][0]) == Decimal("99.975")


def test_prepare_requires_stop_and_origin_then_confirm_single_post_no_book_or_receipt(tmp_path):
    client,headers,account,wallet = setup(tmp_path)
    url,payload = plan(client,headers,account,wallet)
    assert client.post(url,json=payload).status_code == 401
    assert client.post(url,headers={**headers,"Origin":"https://outside.invalid"},json=payload).status_code == 403
    assert client.post(url,headers=headers,json=payload).status_code == 409
    assert client.post(f"/real/accounts/{account}/mexc-stop",headers=headers,json={"confirmation":"DETENER"}).status_code == 200
    prepared = client.post(url,headers=headers,json=payload)
    assert prepared.status_code == 200,prepared.text
    assert prepared.headers["cache-control"] == "no-store" and not WithdrawalClient.posts
    value = prepared.json()
    body = submit_body(wallet,value)
    send = url+"/"+value["id"]+"/submit"
    assert client.post(send,headers=headers,json={**body,"fee_quote_only":False}).status_code == 422
    assert client.post(send,headers=headers,json={**body,"api_key":"other-account-fixture"}).status_code == 409
    assert not WithdrawalClient.posts
    accepted = client.post(send,headers=headers,json=body)
    assert accepted.status_code == 200,accepted.text
    assert accepted.json()["status"] == "ACKNOWLEDGED" and not accepted.json()["wallet_received"]
    assert not client.post(send,headers=headers,json=body).json()["post_attempted"]
    assert len(WithdrawalClient.posts) == 1
    assert WithdrawalClient.posts[0]["address"] == [wallet.address.lower()]
    status = client.get(f"/real/accounts/{account}/mexc-execution-status",headers=headers).json()
    assert status["stopped"] and status["withdrawal_pending"] and not status["can_review_resume"]
    assert client.get(url,headers=headers).json()[0]["status"] == "ACKNOWLEDGED"
    assert client.get(f"/real/accounts/{account}/snapshot",headers=headers).json()["cash"]["USDT"] == "100"
    with open(tmp_path / "real.db","rb") as saved:
        encrypted = saved.read()
    assert wallet.address.encode() not in encrypted and body["secret_key"].encode() not in encrypted


def test_lost_post_reply_blocks_resending_new_plan_and_cancel_even_after_unlock(tmp_path):
    client,headers,account,wallet = setup(tmp_path)
    url,payload = plan(client,headers,account,wallet)
    client.post(f"/real/accounts/{account}/mexc-stop",headers=headers,json={"confirmation":"DETENER"})
    prepared = client.post(url,headers=headers,json=payload).json()
    workflow = f"/real/accounts/{account}/mexc-workflow"
    assert client.get(workflow, headers=headers).json()["withdrawal_action"] == "REVIEW_PLAN"
    WithdrawalClient.mode = "unknown"
    body = submit_body(wallet,prepared)
    send = url+"/"+prepared["id"]+"/submit"
    first = client.post(send,headers=headers,json=body)
    assert first.status_code == 200 and first.json()["status"] == "SUBMISSION_UNKNOWN",first.text
    assert client.get(workflow, headers=headers).json()["withdrawal_action"] == "CHECK"
    assert not client.post(send,headers=headers,json=body).json()["post_attempted"]
    assert len(WithdrawalClient.posts) == 1
    assert client.post(url,headers=headers,json={**payload,"request_id":str(uuid4())}).status_code == 409
    assert client.post(url+"/"+prepared["id"]+"/cancel",headers=headers,json={"confirmation":"CANCELAR_PLAN_SIN_ENVIO"}).status_code == 409
    client.post("/real/vault/lock",headers=headers)
    token = client.post("/real/vault/unlock",json={"passphrase":PASSWORD}).json()["token"]
    new_headers = {"X-Orbis-Vault-Token":token}
    assert client.get(url,headers=new_headers).json()[0]["status"] == "SUBMISSION_UNKNOWN"
    assert client.get(workflow, headers=new_headers).json() == {"view":"DISCONNECTED","withdrawal_action":"CHECK","withdrawal_id":prepared["id"]}
    assert len(WithdrawalClient.posts) == 1


def test_lock_during_network_preflight_revokes_authorization_before_post(tmp_path):
    client,headers,account,wallet = setup(tmp_path)
    url,payload = plan(client,headers,account,wallet)
    client.post(f"/real/accounts/{account}/mexc-stop",headers=headers,json={"confirmation":"DETENER"})
    prepared = client.post(url,headers=headers,json=payload).json()
    WithdrawalClient.during_pair = lambda:client.post("/real/vault/lock",headers=headers)
    try:
        result = client.post(url+"/"+prepared["id"]+"/submit",headers=headers,json=submit_body(wallet,prepared))
        assert result.status_code == 409,result.text
        assert not WithdrawalClient.posts
    finally:
        WithdrawalClient.during_pair = None


def select_recovery_view(client, headers, monkeypatch, *, wrong=False):
    key = "fixture-wrong-view" if wrong else "fixture-renewed-key"
    if wrong:
        wrong_ref = hmac.new(SECRET.encode(),key.encode(),hashlib.sha256).hexdigest()
        original_uid = WithdrawalView.account_uid
        monkeypatch.setattr(WithdrawalView,"account_uid",lambda view:
            "987654321" if view.account_reference() == wrong_ref else original_uid(view))
    selected = client.post("/real/exchanges/mexc/connect",headers=headers,
                           json={"api_key":key,"secret_key":SECRET})
    assert selected.is_success,selected.text


@pytest.mark.parametrize("server_rpc,renewed_view",[(False,False),(True,False),(False,True),(True,True)],
                         ids=["wallet","bnb","renewed-wallet","renewed-bnb"])
def test_view_recovery_after_unknown_post_receipt_explicit_internal_booking_and_reopen(tmp_path, monkeypatch, server_rpc, renewed_view):
    from tests.test_mexc_withdrawal_receipt import evidence
    server = WithdrawalBnbRpc()
    client,headers,account,wallet = setup(tmp_path,rpc_factory=lambda:server)
    target = client.post("/real/accounts",headers=headers,json={"name":"Wallet","base_currency":"USDT"}).json()["id"]
    url,payload = plan(client,headers,account,wallet)
    client.post(f"/real/accounts/{account}/mexc-stop",headers=headers,json={"confirmation":"DETENER"})
    prepared = client.post(url,headers=headers,json=payload).json()
    WithdrawalClient.mode = "unknown"
    prefix = url+"/"+prepared["id"]
    assert client.post(prefix+"/submit",headers=headers,json=submit_body(wallet,prepared)).json()["status"] == "SUBMISSION_UNKNOWN"
    assert client.post(prefix+"/observe",headers=headers).json()["status"] == "MEXC_PENDING"
    assert client.post(prefix+"/receipt",headers=headers,json={"address":wallet.address,"evidence":{}}).status_code == 409
    if renewed_view:
        select_recovery_view(client,headers,monkeypatch,wrong=True)
        with monkeypatch.context() as local:
            def unexpected(*args, **kwargs):
                pytest.fail("La consulta de retiradas empezó antes de comprobar el UID")
            local.setattr(WithdrawalView,"transfers_recent",unexpected)
            assert client.post(prefix+"/observe",headers=headers).status_code == 409
            assert client.post(prefix+"/receipt-bnb",headers=headers,json={"address":wallet.address}).status_code == 409
        assert not server.calls
        select_recovery_view(client,headers,monkeypatch)
        assert client.post(url,headers=headers,json={**payload,"request_id":str(uuid4())}).status_code == 409
        assert client.post(prefix+"/submit",headers=headers,json=submit_body(wallet,prepared)).status_code == 409
    WithdrawalClient.completed = True
    observed = client.post(prefix+"/observe",headers=headers)
    assert observed.status_code == 200,observed.text
    assert observed.json()["status"] == "MEXC_SUCCESS_PENDING_RECEIPT"
    with localcontext() as context:
        context.prec = 300
        net = str(Decimal(payload["amount"])-Decimal("0.01"))
        remaining = str(Decimal("100")-Decimal(payload["amount"]))
    rpc = evidence(WithdrawalClient.times[0],received=net,recipient=wallet.address,contract=CONTRACT)
    wrong = client.post(prefix+"/receipt",headers=headers,json={"address":Account.create().address,"evidence":rpc})
    assert wrong.status_code == 409
    if server_rpc:
        rpc["block"]["transactions"] = [rpc["tx_hash"]]
        server.evidence = rpc
        receipt = client.post(prefix+"/receipt-bnb",headers=headers,json={"address":wallet.address})
        calls = server.calls.copy()
        assert len(calls) == 6
        assert rpc["tx_hash"] not in receipt.text and wallet.address.lower() not in receipt.text.lower()
        assert client.post(prefix+"/receipt-bnb",headers=headers,json={"address":wallet.address}).json() == receipt.json()
        assert server.calls == calls
        # A different valid source may recheck the same receipt; the original provenance stays pinned.
        replay = client.post(prefix+"/receipt",headers=headers,json={"address":wallet.address,"evidence":rpc})
        assert replay.status_code == 200 and replay.json() == receipt.json()
    else:
        receipt = client.post(prefix+"/receipt",headers=headers,json={"address":wallet.address,"evidence":rpc})
    assert receipt.status_code == 200,receipt.text
    assert receipt.json()["received"] == net and not receipt.json()["booked"]
    assert receipt.json()["chain_source"] == ("ORBIS_RPC_OBSERVED" if server_rpc else "WALLET_RPC_OBSERVED")
    assert client.get(f"/real/accounts/{account}/snapshot",headers=headers).json()["cash"]["USDT"] == "100"
    workflow = f"/real/accounts/{account}/mexc-workflow"
    assert client.get(workflow, headers=headers).json()["withdrawal_action"] == "BOOK"
    if renewed_view:
        assert client.get(workflow,headers=headers).json()["view"] == "DIFFERENT_CONNECTION"
        wrong_book = prefix.replace(account,target,1)+"/observe"
        assert client.post(wrong_book,headers=headers).status_code == 409
    else:
        assert client.get(f"/real/accounts/{target}/mexc-workflow", headers=headers).json() == {"view":"OTHER_ACCOUNT"}
    options = client.post(prefix+"/booking-options",headers=headers,json={"address":wallet.address})
    assert options.status_code == 200 and {"id":target,"name":"Wallet"} in options.json()["accounts"]
    body = {"address":wallet.address,"mode":"NEW_INTERNAL","target_account_id":target,"confirmation":"CONTABILIZAR_RETIRADA_RECIBIDA"}
    assert client.post(prefix+"/book",headers={**headers,"Origin":"https://outside.invalid"},json=body).status_code == 403
    booked = client.post(prefix+"/book",headers=headers,json=body)
    assert booked.status_code == 200,booked.text
    assert booked.json()["booked"] and not booked.json()["trading_reopened"]
    assert client.post(prefix+"/book",headers=headers,json=body).json() == booked.json()
    left = client.get(f"/real/accounts/{account}/snapshot",headers=headers).json()
    right = client.get(f"/real/accounts/{target}/snapshot",headers=headers).json()
    assert left["cash"]["USDT"] == remaining and right["cash"]["USDT"] == net
    assert Decimal(left["cash_fees"]["USDT"]) == Decimal("0.01") and not right["contributions"]
    status = client.get(f"/real/accounts/{account}/mexc-execution-status",headers=headers).json()
    assert status["stopped"] and not status["withdrawal_pending"] and status["can_review_resume"]
    client.post("/real/vault/lock",headers=headers)
    token = client.post("/real/vault/unlock",json={"passphrase":PASSWORD}).json()["token"]
    headers = {"X-Orbis-Vault-Token":token}
    archived = client.get(url,headers=headers).json()[0]
    assert archived["booked"] and archived["chain_source"] == receipt.json()["chain_source"]
    assert client.get(workflow, headers=headers).json() == {"view":"DISCONNECTED"}
    assert client.post(prefix+"/book",headers=headers,json=body).json()["booked"]
    assert len(WithdrawalClient.posts) == 1


@pytest.mark.parametrize("fault", ["pending", "wrong_chain", "wrong_index", "nonfinal", "lock"])
def test_bnb_receipt_preserves_pending_without_valid_finalized_evidence(tmp_path, fault):
    from tests.test_mexc_withdrawal_receipt import evidence
    from tests.test_wallet_funding import TX_HASH

    rpc = WithdrawalBnbRpc()
    client, headers, account, wallet = setup(tmp_path, rpc_factory=lambda:rpc)
    url, payload = plan(client,headers,account,wallet)
    assert client.post(f"/real/accounts/{account}/mexc-stop",headers=headers,json={"confirmation":"DETENER"}).is_success
    prepared = client.post(url,headers=headers,json=payload).json()
    prefix = url+"/"+prepared["id"]
    assert client.post(prefix+"/submit",headers=headers,json=submit_body(wallet,prepared)).is_success
    target = prefix+"/receipt-bnb"
    body = {"address":wallet.address}
    assert client.post(target,json=body).status_code == 401
    assert client.post(target,headers={**headers,"Origin":"https://outside.invalid"},json=body).status_code == 403
    wrong = Account.create()
    prove(client,headers,account,wrong)
    assert client.post(target,headers=headers,json={"address":wrong.address}).status_code == 409
    assert client.post(target,headers=headers,json={**body,"tx_hash":"0x"+"f"*64}).status_code == 422
    pending = client.post(target,headers=headers,json=body)
    assert pending.json() == {"status":"MEXC_PENDING","wallet_received":False}
    assert rpc.calls == []

    WithdrawalClient.completed = True
    net = str(Decimal(payload["amount"])-Decimal("0.01"))
    data = evidence(WithdrawalClient.times[0],received=net,recipient=wallet.address,chain=TX_HASH)
    data["block"]["transactions"] = [TX_HASH]
    if fault != "pending":
        rpc.evidence = data
    if fault == "wrong_chain":
        rpc.chain = "0x1"
    if fault == "wrong_index":
        data["block"]["transactions"] = ["0x"+"a"*64]
    if fault == "nonfinal":
        data["finalized"]["number"] = "0x1233"
    if fault == "lock":
        def lock(method):
            if method == "eth_getTransactionReceipt":
                assert client.post("/real/vault/lock", headers=headers).is_success
        rpc.hook = lock

    reply = client.post(target,headers=headers,json=body)
    if fault == "pending":
        assert reply.json() == {"status":"BNB_PENDING","wallet_received":False}
    elif fault == "lock":
        assert reply.status_code == 401,reply.text
        reopened = client.post("/real/vault/unlock",json={"passphrase":PASSWORD}).json()
        headers = {"X-Orbis-Vault-Token":reopened["token"]}
    else:
        assert reply.status_code == 409,reply.text
        assert reply.headers["cache-control"] == "no-store"
    assert TX_HASH not in reply.text and wallet.address.lower() not in reply.text.lower()
    assert wallet.address.lower() not in str(rpc.calls).lower()
    assert len(WithdrawalClient.posts) == 1
    rows = client.get(url,headers=headers).json()
    assert not rows[0]["wallet_received"] and not rows[0]["booked"]
    assert client.get(f"/real/accounts/{account}/snapshot",headers=headers).json()["cash"]["USDT"] == "100"


@pytest.mark.parametrize("renewed_view",[False,True],ids=["original","renewed"])
def test_rejected_request_only_resolves_after_fresh_no_debit_evidence_without_reusing_post(tmp_path,monkeypatch,renewed_view):
    client,headers,account,wallet = setup(tmp_path)
    url,payload = plan(client,headers,account,wallet)
    client.post(f"/real/accounts/{account}/mexc-stop",headers=headers,json={"confirmation":"DETENER"})
    prepared = client.post(url,headers=headers,json=payload).json()
    prefix = url+"/"+prepared["id"]
    body = submit_body(wallet,prepared)
    client.post(prefix+"/submit",headers=headers,json=body)
    resolve = {"confirmation":"CONFIRMAR_RECHAZO_SIN_CARGO"}
    assert client.post(prefix+"/resolve-rejected",headers=headers,json=resolve).status_code == 409
    if renewed_view:
        select_recovery_view(client,headers,monkeypatch)
    WithdrawalClient.rejected = True
    assert client.post(prefix+"/observe", headers=headers).status_code == 200
    workflow = f"/real/accounts/{account}/mexc-workflow"
    assert client.get(workflow, headers=headers).json()["withdrawal_action"] == "REVIEW_REJECTION"
    result = client.post(prefix+"/resolve-rejected",headers=headers,json=resolve)
    assert result.status_code == 200,result.text
    assert result.json()["resolved"] and not result.json()["wallet_received"] and not result.json()["booked"]
    assert "withdrawal_action" not in client.get(workflow, headers=headers).json()
    assert client.get(f"/real/accounts/{account}/snapshot",headers=headers).json()["cash"]["USDT"] == "100"
    if renewed_view:
        assert client.post(prefix+"/submit",headers=headers,json=body).status_code == 409
    else:
        assert not client.post(prefix+"/submit",headers=headers,json=body).json()["post_attempted"]
    assert len(WithdrawalClient.posts) == 1


def test_prepared_withdrawal_review_reopens_same_plan_without_sending(tmp_path, monkeypatch):
    client, headers, account, wallet = setup(tmp_path)
    url, payload = plan(client, headers, account, wallet)
    assert client.post(f"/real/accounts/{account}/mexc-stop", headers=headers,
                       json={"confirmation":"DETENER"}).is_success
    prepared = client.post(url, headers=headers, json=payload).json()
    review = f"{url}/{prepared['id']}/review"
    body = {key:payload[key] for key in ("address","token_contract","decimals")}
    body["plan_digest"] = prepared["plan_digest"]
    assert client.post(review, json=body).status_code == 401
    assert client.post(review, headers={**headers,"Origin":"https://outside.invalid"},json=body).status_code == 403
    assert client.post(review, headers=headers,json={**body,"secret_key":"must-not-accept"}).status_code == 422

    assert client.post("/real/vault/lock",headers=headers).is_success
    token = client.post("/real/vault/unlock",json={"passphrase":PASSWORD}).json()["token"]
    headers = {"X-Orbis-Vault-Token":token}
    assert client.post(review, headers=headers,json=body).status_code == 409
    prove(client,headers,account,wallet)
    assert client.post(review, headers=headers,json=body).status_code == 409
    assert client.post("/real/exchanges/mexc/connect",headers=headers,
                       json={"api_key":KEY,"secret_key":SECRET}).is_success

    calls = []
    original = WithdrawalView.bsc_withdrawal_route

    def route(view,address):
        calls.append(address)
        return original(view,address)

    monkeypatch.setattr(WithdrawalView,"bsc_withdrawal_route",route)
    other_wallet = Account.create()
    prove(client,headers,account,other_wallet)
    other_account = client.post("/real/accounts",headers=headers,json={"name":"Otro libro","base_currency":"USDT"}).json()["id"]
    prove(client,headers,other_account,wallet)
    for target,changes in [(review,{"address":other_wallet.address}),
                           (review,{"plan_digest":"0"*64}),
                           (review,{"decimals":6}),
                           (review,{"token_contract":"0x"+"a"*40}),
                           (review.replace(account,other_account,1),{})]:
        assert client.post(target,headers=headers,json={**body,**changes}).status_code == 409
    assert not calls

    for _ in range(2):
        result = client.post(review,headers=headers,json=body)
        assert result.status_code == 200 and result.headers["cache-control"] == "no-store",result.text
        assert result.json() == prepared
        assert wallet.address.lower() not in result.text.lower() and "claim_granted" not in result.json()
    assert len(calls) == 2 and not WithdrawalClient.posts
    assert client.get(url,headers=headers).json() == [prepared]
    assert client.get(f"/real/accounts/{account}/snapshot",headers=headers).json()["cash"]["USDT"] == "100"
    status = client.get(f"/real/accounts/{account}/mexc-execution-status",headers=headers).json()
    assert status["stopped"] and status["withdrawal_pending"]

    submitted = client.post(review.removesuffix("/review")+"/submit",headers=headers,json=submit_body(wallet,prepared))
    assert submitted.is_success and submitted.json()["post_attempted"],submitted.text
    assert client.post(review,headers=headers,json=body).status_code == 409
    assert len(WithdrawalClient.posts) == 1


@pytest.mark.parametrize("change", ["route", "cancel", "lock"])
def test_prepared_withdrawal_review_rejects_changed_context_during_read(tmp_path, monkeypatch, change):
    client, headers, account, wallet = setup(tmp_path)
    url, payload = plan(client, headers, account, wallet)
    assert client.post(f"/real/accounts/{account}/mexc-stop",headers=headers,json={"confirmation":"DETENER"}).is_success
    prepared = client.post(url,headers=headers,json=payload).json()
    path = f"{url}/{prepared['id']}"
    original = WithdrawalView.bsc_withdrawal_route

    def changed(view,address):
        route = original(view,address)
        if change == "route":
            return {**route,"fee":"0.02"}
        if change == "lock":
            assert client.post("/real/vault/lock",headers=headers).is_success
        else:
            assert client.post(path+"/cancel",headers=headers,json={"confirmation":"CANCELAR_PLAN_SIN_ENVIO"}).is_success
        return route

    monkeypatch.setattr(WithdrawalView,"bsc_withdrawal_route",changed)
    body = {key:payload[key] for key in ("address","token_contract","decimals")}
    result = client.post(path+"/review",headers=headers,json={**body,"plan_digest":prepared["plan_digest"]})
    assert result.status_code == 409 and result.headers["cache-control"] == "no-store",result.text
    assert not WithdrawalClient.posts
    if change != "lock":
        record = client.get(url,headers=headers).json()[0]
        assert record["status"] == ("CANCELLED" if change == "cancel" else "PREPARED")


def test_withdrawal_workflow_links_pending_plan_and_focused_history_cannot_cross_books(tmp_path):
    client, headers, account, wallet = setup(tmp_path)
    url, payload = plan(client,headers,account,wallet)
    assert client.post(f"/real/accounts/{account}/mexc-stop",headers=headers,json={"confirmation":"DETENER"}).is_success
    cancelled = client.post(url,headers=headers,json=payload).json()
    assert client.post(f"{url}/{cancelled['id']}/cancel",headers=headers,
                       json={"confirmation":"CANCELAR_PLAN_SIN_ENVIO"}).is_success
    pending = client.post(url,headers=headers,json={**payload,"request_id":str(uuid4())}).json()
    state = client.get(f"/real/accounts/{account}/mexc-workflow",headers=headers).json()
    assert (state["withdrawal_action"],state["withdrawal_id"]) == ("REVIEW_PLAN",pending["id"])
    scoped = {**headers,"X-Orbis-Passive":"1"}
    focus = {"focus_id":cancelled["id"]}
    response = client.get(url,headers=scoped,params=focus)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert [row["id"] for row in response.json()] == [cancelled["id"],pending["id"]]
    assert wallet.address.lower() not in response.text.lower()
    other = client.post("/real/accounts",headers=headers,json={"name":"Otro libro","base_currency":"USDT"}).json()["id"]
    assert client.get(url.replace(account,other,1),headers=scoped,params=focus).json() == []
    assert client.get(url,headers={**scoped,"Origin":"https://outside.invalid"},params=focus).status_code == 403
    assert client.get(url,headers=scoped,params={"focus_id":"bad"}).status_code == 422
    assert not WithdrawalClient.posts
