"""El navegador no puede afirmar por sí solo que MEXC acreditó un ingreso."""

from datetime import datetime, timezone

import pytest

from tests.test_wallet_funding_api import BscFakeMexc, FundingNonceRpc, PASSWORD, setup, prove, body, signed_funding_close
from tests.test_wallet_funding_credit import evidence,snapshot
from tests.test_mexc_deposit_destination import ADDRESS,CONTRACT
from tests.test_wallet_funding import TX_HASH


def prepared(tmp_path, *, rpc_factory=None, report_hash=True):
    client,headers,account,wallet = setup(tmp_path,rpc_factory=rpc_factory)
    prove(client,headers,account,wallet)
    url = f"/real/accounts/{account}/wallet-funding"
    request = body(wallet)
    plan = client.post(url,headers=headers,json=request).json()
    claim = {"address":wallet.address,"plan_digest":plan["plan_digest"],"confirmation":"SOLICITAR_ENVIO_WALLET"}
    assert client.post(url+"/"+plan["id"]+"/claim",headers=headers,json=claim).json()["claim_granted"]
    if report_hash:
        assert client.post(url+"/"+plan["id"]+"/hash",headers=headers,json={"tx_hash":TX_HASH}).status_code == 200
    now = datetime.now(timezone.utc)
    raw = evidence(plan,now,wallet=wallet.address,destination=ADDRESS)
    return client,headers,account,wallet,url+"/"+plan["id"],plan,raw,now


def test_check_fresh_independent_view_then_explicit_exact_booking_and_lost_reply_replay(tmp_path,monkeypatch):
    client,headers,account,wallet,url,plan,raw,now = prepared(tmp_path)
    observed = snapshot(now,amount="1.123456789012345678",contract=CONTRACT,destination=ADDRESS)
    calls = []
    def transfers(self,*,days,include_source_ids):
        calls.append(days)
        return {**observed,"observed_at":datetime.now(timezone.utc).isoformat()}
    monkeypatch.setattr(BscFakeMexc,"transfers_recent",transfers,raising=False)
    assert client.post(url+"/check",json={"evidence":raw}).status_code == 401
    assert client.post(url+"/check",headers={**headers,"Origin":"https://outside.invalid"},json={"evidence":raw}).status_code == 403
    checked = client.post(url+"/check",headers=headers,json={"evidence":raw})
    assert checked.status_code == 200,checked.text
    assert checked.json()["exchange_credited"] and not checked.json()["booked"] and calls == [7]
    payload = {"mode":"NEW_CONTRIBUTION","address":wallet.address,"confirmation":"REGISTRAR_INGRESO_ACREDITADO"}
    assert client.post(url+"/book",headers=headers,json={**payload,"confirmation":""}).status_code == 422
    first = client.post(url+"/book",headers=headers,json=payload)
    assert first.status_code == 200,first.text
    assert first.json()["booked"]
    assert client.post(url+"/book",headers=headers,json=payload).json()["event_id"] == first.json()["event_id"]
    history = client.get(url.rsplit("/",1)[0],headers=headers).json()[0]
    assert history["exchange_credited"] and history["booked"] and history["status"] == "HASH_REPORTED"
    reported_again = client.post(url+"/hash",headers=headers,json={"tx_hash":TX_HASH})
    assert reported_again.json()["exchange_credited"] and reported_again.json()["booked"]
    assert TX_HASH.lower() not in str(history).lower()
    # La misma referencia con estado posterior regresivo no se oculta.
    observed["deposits"][0]["status"] = 1
    assert client.post(url+"/check",headers=headers,json={"evidence":raw}).status_code == 409
    assert client.get(url.rsplit("/",1)[0],headers=headers).status_code == 409


def test_browser_receipt_cannot_create_credit_without_independent_mexc_deposit(tmp_path,monkeypatch):
    client,headers,account,wallet,url,plan,raw,now = prepared(tmp_path)
    def empty(self,**kwargs):
        return {**snapshot(now),"deposits":[]}
    monkeypatch.setattr(BscFakeMexc,"transfers_recent",empty,raising=False)
    result = client.post(url+"/check",headers=headers,json={"evidence":raw})
    assert result.status_code == 200,result.text
    assert result.json()["status"] == "PENDING_MEXC" and not result.json()["exchange_credited"]
    assert client.post(url+"/book",headers=headers,json={"mode":"NEW_CONTRIBUTION","address":wallet.address,
        "confirmation":"REGISTRAR_INGRESO_ACREDITADO"}).status_code == 409


def test_internal_preview_and_booking_require_original_wallet_proof_and_conserve_books(tmp_path,monkeypatch):
    from uuid import uuid4
    from app.portfolio.vault import RealVault
    from app.portfolio.repository import account_snapshot
    from tests.test_wallet_funding_api import PASSWORD
    from tests.test_wallet_funding_internal import source_wallet
    client,headers,account,wallet,url,plan,raw,now = prepared(tmp_path)
    monkeypatch.setattr(BscFakeMexc,"transfers_recent",lambda self,**kwargs:
        {**snapshot(now,amount="1.123456789012345678",contract=CONTRACT,destination=ADDRESS),
         "observed_at":datetime.now(timezone.utc).isoformat()},raising=False)
    assert client.post(url+"/check",headers=headers,json={"evidence":raw}).status_code == 200
    vault = RealVault(tmp_path / "real.db"); vault.unlock(PASSWORD)
    source,_ = source_wallet(vault,now,address=wallet.address)
    assert client.post(url+"/booking-options",json={"address":wallet.address}).status_code == 401
    assert client.post(url+"/booking-options",headers={**headers,"Origin":"https://outside.invalid"},json={"address":wallet.address}).status_code == 403
    assert client.post(url+"/booking-options",headers=headers,json={"address":"0x"+"7"*40}).status_code == 409
    assert client.post(url+"/booking-options",headers=headers,json={}).json()["internal"] is None
    preview = client.post(url+"/booking-options",headers=headers,json={"address":wallet.address})
    assert preview.status_code == 200 and preview.headers["cache-control"] == "no-store",preview.text
    assert wallet.address.lower() not in preview.text.lower()
    info = preview.json()["internal"]
    payload = {"mode":"NEW_INTERNAL","address":wallet.address,"source_account_id":source,
        "source_revision":info["source_revision"],"confirmation":"REGISTRAR_INGRESO_ACREDITADO"}
    wrong = client.post(url+"/book",headers=headers,json={**payload,"source_account_id":str(uuid4())})
    assert wrong.status_code == 409,wrong.text
    client.post("/real/vault/lock",headers=headers)
    token = client.post("/real/vault/unlock",json={"passphrase":PASSWORD}).json()["token"]
    headers = {"X-Orbis-Vault-Token":token}
    assert client.post(url+"/book",headers=headers,json=payload).status_code == 409
    prove(client,headers,account,wallet)
    first = client.post(url+"/book",headers=headers,json=payload)
    assert first.status_code == 200 and first.json()["booked"],first.text
    assert client.post(url+"/book",headers=headers,json=payload).json() == first.json()
    with vault.session() as db:
        src,dst = account_snapshot(db,source),account_snapshot(db,account)
        assert src.cash["USDT"]+dst.cash["USDT"] == 100
        assert dst.contributions.get("USDT",0) == 0
    vault.lock()

@pytest.mark.parametrize("known_hash,renewed_view",[(True,False),(False,False),(True,True),(False,True)],
                         ids=["hash","nonce","renewed-hash","renewed-nonce"])
def test_server_bnb_credit_requires_view_and_keeps_provenance_after_booking(tmp_path,monkeypatch,known_hash,renewed_view):
    server = FundingNonceRpc()
    client,headers,account,wallet,url,plan,raw,now = prepared(tmp_path,rpc_factory=lambda:server,report_hash=known_hash)
    raw["block"]["transactions"] = [TX_HASH]
    server.evidence = raw
    observed = snapshot(now,amount="1.123456789012345678",contract=CONTRACT,destination=ADDRESS)
    observed_deposits = observed["deposits"]
    observed["deposits"] = []
    def transfers(self,*,days,include_source_ids):
        return {**observed,"observed_at":datetime.now(timezone.utc).isoformat()}
    monkeypatch.setattr(BscFakeMexc,"transfers_recent",transfers,raising=False)
    target = url+"/check-bnb"
    request = {"address":wallet.address,**({"tx_hash":TX_HASH} if known_hash else {})}
    assert client.post(target,json=request).status_code == 401
    assert client.post(target,headers={**headers,"Origin":"https://outside.invalid"},json=request).status_code == 403
    assert client.post(target,headers=headers,json={**request,"tx_hash":"0x"+"f"*64}).status_code == 409
    assert client.post("/real/exchanges/mexc/disconnect",headers=headers).is_success
    assert client.post(target,headers=headers,json=request).status_code == 409
    assert server.calls == []
    from tests.test_mexc_deposit_destination import KEY,SECRET
    if renewed_view:
        original_uid = BscFakeMexc.account_uid
        replacement_key = "fixture-renewed-key"
        wrong_key = "fixture-wrong-view"
        import hashlib
        import hmac
        wrong_ref = hmac.new(SECRET.encode(),wrong_key.encode(),hashlib.sha256).hexdigest()
        monkeypatch.setattr(BscFakeMexc,"account_uid",lambda view:
            "987654321" if view.account_reference() == wrong_ref else original_uid(view))
        assert client.post("/real/exchanges/mexc/connect",headers=headers,json={"api_key":wrong_key,"secret_key":SECRET}).is_success
        assert client.post(target,headers=headers,json=request).status_code == 409
        if known_hash:
            assert client.post(url+"/check",headers=headers,json={"evidence":raw}).status_code == 409
        assert not server.calls
        assert client.post("/real/exchanges/mexc/connect",headers=headers,json={"api_key":replacement_key,"secret_key":SECRET}).is_success
        assert client.post(url.rsplit("/",1)[0],headers=headers,json=body(wallet)).status_code == 409
        assert client.get(f"/real/accounts/{account}/mexc-workflow",headers=headers).json()["view"] == "DIFFERENT_CONNECTION"
    else:
        assert client.post("/real/exchanges/mexc/connect",headers=headers,json={"api_key":KEY,"secret_key":SECRET}).is_success
    pending = client.post(target,headers=headers,json=request)
    assert pending.json() == {"status":"PENDING_MEXC","chain_source":"ORBIS_RPC_OBSERVED","exchange_credited":False,"booked":False}
    sent_wallet = any(wallet.address.lower() in str(params).lower() for _,params in server.calls)
    assert sent_wallet is not known_hash
    history = client.get(url.rsplit("/",1)[0],headers=headers).json()
    assert history[0]["status"] == "HASH_REPORTED" and not history[0]["exchange_credited"]
    assert client.get(f"/real/accounts/{account}/snapshot",headers=headers).json()["cash"] == {}

    observed["deposits"] = observed_deposits
    observed_deposits[0]["status"] = 9
    precredit = client.post(target,headers=headers,json=request)
    assert precredit.is_success and not precredit.json()["exchange_credited"],precredit.text
    status = precredit.json()["mexc_deposit"]
    assert status["code"] == 9 and status["chain_source"] == "ORBIS_RPC_OBSERVED"
    assert client.get(url.rsplit("/",1)[0],headers=headers).json()[0]["mexc_deposit"] == status
    assert client.get(f"/real/accounts/{account}/snapshot",headers=headers).json()["cash"] == {}
    if known_hash and not renewed_view:
        server.evidence = signed_funding_close(plan,wallet,now,kind="CANCELLED",typed=True)
        conflict = client.post(url+"/discover",headers=headers,json={"address":wallet.address})
        assert conflict.status_code == 409 and "contradice" in conflict.json()["detail"],conflict.text
        assert client.get(url.rsplit("/",1)[0],headers=headers).json()[0]["mexc_deposit"] == status
        server.evidence = raw
    observed_deposits[0]["status"] = 5
    confirmed = client.post(target,headers=headers,json=request)
    assert confirmed.is_success and confirmed.headers["cache-control"] == "no-store",confirmed.text
    assert confirmed.json()["status"] == "MEXC_CREDITED" and confirmed.json()["chain_source"] == "ORBIS_RPC_OBSERVED"
    assert TX_HASH not in confirmed.text and wallet.address.lower() not in confirmed.text.lower()
    calls = server.calls.copy()
    assert client.post(target,headers=headers,json=request).json() == confirmed.json()
    assert server.calls == calls
    legacy = client.post(url+"/check",headers=headers,json={"evidence":raw})
    assert legacy.is_success and legacy.json() == confirmed.json(),legacy.text
    payload = {"mode":"NEW_CONTRIBUTION","address":wallet.address,"confirmation":"REGISTRAR_INGRESO_ACREDITADO"}
    assert client.post(url+"/book",headers=headers,json=payload).json()["booked"]
    assert client.post("/real/vault/lock",headers=headers).is_success
    token = client.post("/real/vault/unlock",json={"passphrase":PASSWORD}).json()["token"]
    history = client.get(url.rsplit("/",1)[0],headers={"X-Orbis-Vault-Token":token}).json()
    assert history[0]["booked"] and history[0]["chain_source"] == "ORBIS_RPC_OBSERVED"
    assert "mexc_deposit" not in history[0]


@pytest.mark.parametrize("fault",["pending","wrong_index","lock"])
def test_server_bnb_credit_rejects_unverifiable_or_revoked_observation(tmp_path,fault):
    server = FundingNonceRpc()
    client,headers,account,wallet,url,_,raw,_ = prepared(tmp_path,rpc_factory=lambda:server)
    raw["block"]["transactions"] = [TX_HASH]
    server.evidence = raw
    if fault == "pending":
        server.pending = True
    if fault == "wrong_index":
        raw["block"]["transactions"] = ["0x"+"a"*64]
    if fault == "lock":
        def lock(method):
            if method == "eth_getTransactionReceipt":
                assert client.post("/real/vault/lock",headers=headers).is_success
        server.hook = lock
    response = client.post(url+"/check-bnb",headers=headers,json={"address":wallet.address,"tx_hash":TX_HASH})
    if fault == "pending":
        assert response.json() == {"status":"BNB_PENDING","exchange_credited":False,"booked":False}
    elif fault == "lock":
        assert response.status_code == 401,response.text
        token = client.post("/real/vault/unlock",json={"passphrase":PASSWORD}).json()["token"]
        headers = {"X-Orbis-Vault-Token":token}
    else:
        assert response.status_code == 409,response.text
    assert client.get(f"/real/accounts/{account}/snapshot",headers=headers).json()["cash"] == {}
    assert not client.get(url.rsplit("/",1)[0],headers=headers).json()[0]["exchange_credited"]
