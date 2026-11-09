"""API BEP-20: View, vínculo, control EOA y permiso separado de un solo uso."""

from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.portfolio.api import create_real_router
from app.portfolio.models import RealWalletFundingIntent
from app.portfolio.vault import RealVault
from tests.test_mexc_deposit_destination import FakeMexc,KEY,SECRET,CONTRACT,ADDRESS


PASSWORD = "funding api fixture password"


class BscFakeMexc(FakeMexc):
    contract = CONTRACT

    def evm_token_deposit_destination(self,asset,*,chain_id):
        assert asset == "USDT" and chain_id == "0x38"
        return {"source":"MEXC_SPOT_PRIVATE_READONLY","network":"BNB_SMART_CHAIN_MAINNET","asset":asset,
                "token_contract":self.contract,"deposit_address":ADDRESS,
                "observed_at":datetime.now(timezone.utc).isoformat(),"stored":False,"transfer_sent":False}

    def evm_token_contracts_by_direction(self,*,chain_id):
        assert chain_id == "0x38"
        return {"deposit":{"USDT":self.contract},"withdrawal":{}}


def setup(tmp_path, *, rpc_factory=None):
    app = FastAPI()
    app.include_router(create_real_router(tmp_path / "real.db",mexc_factory=BscFakeMexc,dex_rpc_factory=rpc_factory))
    client = TestClient(app)
    token = client.post("/real/vault/create",json={"passphrase":PASSWORD}).json()["token"]
    headers = {"X-Orbis-Vault-Token":token}
    account = client.post("/real/accounts",headers=headers,json={"name":"MEXC","base_currency":"USDT"}).json()["id"]
    assert client.post("/real/exchanges/mexc/connect",headers=headers,json={"api_key":KEY,"secret_key":SECRET}).status_code == 200
    assert client.post(f"/real/accounts/{account}/mexc-link",headers=headers,json={"confirmation":"VINCULAR"}).status_code == 200
    return client,headers,account,Account.create()


def prove(client,headers,account,wallet):
    url = f"/real/accounts/{account}/wallet-proof"
    body = {"address":wallet.address,"chain_id":"0x38"}
    started = client.post(url+"/start",headers=headers,json=body).json()
    signature = Account.sign_message(encode_defunct(text=started["message"]),wallet.key).signature.hex()
    result = client.post(url+"/confirm",headers=headers,json={**body,"nonce":started["nonce"],"signature":"0x"+signature})
    assert result.status_code == 200,result.text


def body(wallet):
    return {"request_id":str(uuid4()),"address":wallet.address,"chain_id":"0x38","asset":"USDT",
            "amount":"1.123456789012345678","minimum_deposit_usdt":"1","decimals":18,"nonce":"4","gas":"90000",
            "gas_price":"1000000000","max_gas_bnb":"0.0001"}


def test_funding_requires_origin_view_identity_wallet_proof_and_explicit_claim(tmp_path):
    client,headers,account,wallet = setup(tmp_path)
    path = f"/real/accounts/{account}/wallet-funding"
    request = body(wallet)
    assert client.post(path,json=request).status_code == 401
    assert client.post(path,headers={**headers,"Origin":"https://outside.invalid"},json=request).status_code == 403
    assert client.post(path,headers=headers,json=request).status_code == 409
    prove(client,headers,account,wallet)
    assert client.post(path,headers=headers,json={**request,"chain_id":"0x1"}).status_code == 422
    missing = {key: value for key, value in request.items() if key != "minimum_deposit_usdt"}
    assert client.post(path,headers=headers,json=missing).status_code == 422
    below = client.post(path,headers=headers,json={**request,"minimum_deposit_usdt":"1.123456789012345679"})
    assert below.status_code == 409 and "inferior al mínimo" in below.json()["detail"]
    assert client.get(path,headers=headers).json() == []
    prepared = client.post(path,headers=headers,json=request)
    assert prepared.status_code == 200,prepared.text
    plan = prepared.json()
    assert plan["status"] == "PREPARED" and prepared.headers["cache-control"] == "no-store"
    assert plan["minimum_deposit_units"] == "1000000000000000000"
    assert plan["transaction"]["from"] == wallet.address.lower()
    assert client.post(path,headers=headers,json=request).json()["id"] == plan["id"]
    claim = {"address":wallet.address,"plan_digest":plan["plan_digest"],"confirmation":"SOLICITAR_ENVIO_WALLET"}
    assert client.post(path+"/"+plan["id"]+"/claim",headers=headers,json={**claim,"confirmation":""}).status_code == 422
    granted = client.post(path+"/"+plan["id"]+"/claim",headers=headers,json=claim)
    assert granted.status_code == 200 and granted.json()["claim_granted"],granted.text
    assert not client.post(path+"/"+plan["id"]+"/claim",headers=headers,json=claim).json()["claim_granted"]
    tx_hash = "0x"+"d"*64
    reported = client.post(path+"/"+plan["id"]+"/hash",headers=headers,json={"tx_hash":tx_hash})
    assert reported.status_code == 200 and not reported.json()["exchange_credited"]
    listed = client.get(path,headers=headers)
    assert listed.status_code == 200 and listed.headers["cache-control"] == "no-store"
    assert ADDRESS not in listed.text and tx_hash not in listed.text and wallet.address.lower() not in listed.text
    snapshot = client.get(f"/real/accounts/{account}/snapshot",headers=headers).json()
    assert snapshot["cash"] == {} and snapshot["positions"] == []
    client.post("/real/vault/lock",headers=headers)
    with RealVaultContext(tmp_path / "real.db") as vault:
        with vault.session() as db:
            row = db.query(RealWalletFundingIntent).one()
            assert ADDRESS not in row.payload_json and wallet.address.lower() not in row.payload_json
            assert tx_hash not in row.state_json


class RealVaultContext:
    def __init__(self,path): self.vault = RealVault(path)
    def __enter__(self): self.vault.unlock(PASSWORD); return self.vault
    def __exit__(self,*_): self.vault.lock()


def test_changed_catalog_and_lost_session_keep_funding_unclaimed(tmp_path):
    client,headers,account,wallet = setup(tmp_path)
    prove(client,headers,account,wallet)
    path = f"/real/accounts/{account}/wallet-funding"
    prepared = client.post(path,headers=headers,json=body(wallet)).json()
    claim = {"address":wallet.address,"plan_digest":prepared["plan_digest"],"confirmation":"SOLICITAR_ENVIO_WALLET"}
    BscFakeMexc.contract = "0x"+"55"*20
    try:
        response = client.post(path+"/"+prepared["id"]+"/claim",headers=headers,json=claim)
        assert response.status_code == 409,response.text
    finally:
        BscFakeMexc.contract = CONTRACT
    assert client.get(path,headers=headers).json()[0]["status"] == "PREPARED"
    client.post("/real/vault/lock",headers=headers)
    token = client.post("/real/vault/unlock",json={"passphrase":PASSWORD}).json()["token"]
    headers = {"X-Orbis-Vault-Token":token}
    assert client.post(path+"/"+prepared["id"]+"/claim",headers=headers,json=claim).status_code == 409

class FundingNonceRpc:
    """Only public, bounded reads over an owned final block."""
    def __init__(self, evidence=None):
        self.evidence = evidence
        self.calls = []
        self.pending = False
        self.conflicting_index = False
        self.hook = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def __call__(self, method, params):
        self.calls.append((method,params))
        if self.hook:
            self.hook(method)
        raw = deepcopy(self.evidence)
        block = raw["block"]
        tx = raw["transaction"]
        nonce = int(tx["nonce"],16)
        if method == "eth_chainId":
            return "0x38"
        if method == "eth_getCode":
            assert params == [tx["from"].lower(),block["number"]]
            return "0x"
        if method == "eth_getTransactionCount":
            assert params[0].lower() == tx["from"].lower()
            return hex(nonce+int(not self.pending and int(params[1],16)>=int(block["number"],16)))
        if method == "eth_getBlockByNumber":
            assert params[0] in ("finalized",block["number"])
            return {**block,"transactions":[tx]} if params[1] else block
        assert params == [raw["tx_hash"]]
        if method == "eth_getTransactionByHash":
            return tx
        if method == "eth_getTransactionReceipt":
            if self.pending:
                return None
            receipt = raw["receipt"]
            if self.conflicting_index:
                receipt["transactionIndex"] = "0x1"
            return receipt
        raise AssertionError(method)


@pytest.mark.parametrize("fault",[None,"pending","index","replacement","receipt_failed","other_report"])
def test_discover_funding_by_original_wallet_nonce_after_reopening(tmp_path,fault):
    from tests.test_wallet_funding_credit import evidence

    rpc = FundingNonceRpc()
    client,headers,account,wallet = setup(tmp_path,rpc_factory=lambda:rpc)
    prove(client,headers,account,wallet)
    base = f"/real/accounts/{account}/wallet-funding"
    plan = client.post(base,headers=headers,json=body(wallet)).json()
    path = base+"/"+plan["id"]
    claim = {"address":wallet.address,"plan_digest":plan["plan_digest"],"confirmation":"SOLICITAR_ENVIO_WALLET"}
    assert client.post(path+"/claim",headers=headers,json=claim).json()["claim_granted"] is True
    workflow = f"/real/accounts/{account}/mexc-workflow"
    assert client.get(workflow, headers=headers).json()["funding_action"] == "SEARCH"
    other_book = client.post("/real/accounts", headers=headers, json={"name":"Otro libro","base_currency":"USDT"}).json()["id"]
    assert client.get(f"/real/accounts/{other_book}/mexc-workflow", headers=headers).json() == {"view":"OTHER_ACCOUNT"}
    request = {"address":wallet.address}
    tx_hash = "0x"+"d"*64
    rpc.evidence = evidence(plan,datetime.now(timezone.utc),wallet=wallet.address,destination=ADDRESS,tx_hash=tx_hash)
    if fault == "pending":
        rpc.pending = True
    if fault == "index":
        rpc.conflicting_index = True
    if fault == "replacement":
        rpc.evidence["transaction"]["input"] = "0x"
    if fault == "receipt_failed":
        rpc.evidence["receipt"]["status"] = "0x0"
    if fault == "other_report":
        assert client.post(path+"/hash",headers=headers,json={"tx_hash":"0x"+"a"*64}).status_code==200

    assert client.post("/real/vault/lock",headers=headers).status_code==200
    assert client.get(workflow, headers=headers).status_code == 401
    assert client.post(path+"/discover",headers=headers,json=request).status_code==401
    token = client.post("/real/vault/unlock",json={"passphrase":PASSWORD}).json()["token"]
    headers = {"X-Orbis-Vault-Token":token}
    response = client.get(workflow, headers=headers)
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"view":"DISCONNECTED","funding_action":"CHECK" if fault == "other_report" else "SEARCH","funding_id":plan["id"]}
    assert client.get(f"/real/accounts/{other_book}/mexc-workflow", headers=headers).json() == {"view":"DISCONNECTED"}
    assert client.post(path+"/discover",headers={**headers,"Origin":"https://outside.invalid"},json=request).status_code==403
    assert client.post(path+"/discover",headers=headers,json=request).status_code==409
    other = Account.create()
    prove(client,headers,account,other)
    assert client.post(path+"/discover",headers=headers,json={"address":other.address}).status_code==409
    assert rpc.calls == []
    prove(client,headers,account,wallet)
    result = client.post(path+"/discover",headers=headers,json=request)
    assert result.headers["cache-control"]=="no-store"
    if fault is None:
        assert result.status_code==200,result.text
        assert result.json()["discovered_tx_hash"]==tx_hash and not result.json()["exchange_credited"]
        assert client.post(path+"/discover",headers=headers,json=request).json()==result.json()
    else:
        assert result.status_code==409,result.text
        assert tx_hash not in result.text
    expected_action = "CHECK" if fault in (None, "other_report") else "SEARCH"
    assert client.get(workflow, headers=headers).json() == {"view":"DISCONNECTED","funding_action":expected_action,"funding_id":plan["id"]}
    items = client.get(base,headers=headers)
    assert items.json()[0]["status"]==("HASH_REPORTED" if fault in (None,"other_report") else "WALLET_REQUESTED")
    assert tx_hash not in items.text and wallet.address.lower() not in items.text.lower()
    assert client.get(f"/real/accounts/{account}/snapshot",headers=headers).json()["cash"]=={}
    assert not any(method.startswith("eth_send") for method,_ in rpc.calls)


def signed_funding_close(plan, wallet, now, *, kind, typed=False):
    """Owned legacy failure or an externally signed zero-value cancellation."""
    from eth_utils import to_checksum_address
    from tests.test_wallet_funding_credit import evidence

    raw = evidence(plan,now,wallet=wallet.address,destination=ADDRESS)
    tx, receipt = raw["transaction"], raw["receipt"]
    gas = 21_000 if kind == "CANCELLED" else int(tx["gas"],16)
    if kind == "CANCELLED":
        tx.update(to=wallet.address,input="0x",gas=hex(gas))
        receipt.update(to=wallet.address,gasUsed=hex(gas))
    else:
        receipt.update(status="0x0")
    fields = {"chainId":56,"nonce":int(tx["nonce"],16),"gas":gas,"to":to_checksum_address(tx["to"]),
              "value":0,"data":tx["input"]}
    if typed:
        assert kind=="CANCELLED"
        fields.update(type=2,maxFeePerGas=3_000_000_000,maxPriorityFeePerGas=500_000_000,accessList=[])
        raw["block"]["baseFeePerGas"] = hex(1_000_000_000)
        tx.update(type="0x2",maxFeePerGas=hex(3_000_000_000),maxPriorityFeePerGas=hex(500_000_000),accessList=[])
        receipt.update(type="0x2",effectiveGasPrice=hex(1_500_000_000))
    else:
        fields["gasPrice"] = int(tx["gasPrice"],16)
    signed = Account.sign_transaction(fields,wallet.key)
    hash_text = "0x"+signed.hash.hex()
    tx.update(hash=hash_text,r=hex(signed.r),s=hex(signed.s),v=hex(signed.v))
    raw["tx_hash"] = receipt["transactionHash"] = hash_text
    receipt["logs"] = []
    receipt["contractAddress"] = None
    return raw


@pytest.mark.parametrize(("kind","typed","reported"),[
    ("CANCELLED",False,False),("CANCELLED",True,True),("REVERTED",False,True),
])
def test_finalized_funding_close_records_gas_without_credit_and_allows_only_new_nonce(tmp_path,kind,typed,reported):
    rpc = FundingNonceRpc()
    client, headers, account, wallet = setup(tmp_path,rpc_factory=lambda:rpc)
    prove(client,headers,account,wallet)
    base = f"/real/accounts/{account}/wallet-funding"
    plan = client.post(base,headers=headers,json=body(wallet)).json()
    original = base+"/"+plan["id"]
    def claim(plan):
        return client.post(base+"/"+plan["id"]+"/claim",headers=headers,json={
            "address":wallet.address,"plan_digest":plan["plan_digest"],"confirmation":"SOLICITAR_ENVIO_WALLET"})
    assert claim(plan).json()["claim_granted"]
    raw = rpc.evidence = signed_funding_close(plan,wallet,datetime.now(timezone.utc),kind=kind,typed=typed)
    if reported:
        hash_text = raw["tx_hash"] if kind=="REVERTED" else "0x"+"d"*64
        assert client.post(original+"/hash",headers=headers,json={"tx_hash":hash_text}).is_success
    # An invalid signature cannot free a pending operation.
    scalar = raw["transaction"]["r"]
    raw["transaction"]["r"] = "0x1"
    response = client.post(original+"/discover",headers=headers,json={"address":wallet.address})
    assert response.status_code==409,response.text
    assert client.get(base,headers=headers).json()[0]["status"]==("HASH_REPORTED" if reported else "WALLET_REQUESTED")
    raw["transaction"]["r"] = scalar
    resolved = client.post(original+"/discover",headers=headers,json={"address":wallet.address})
    assert resolved.status_code==200,resolved.text
    receipt = raw["receipt"]
    expected_gas = str(int(receipt["gasUsed"],16)*int(receipt["effectiveGasPrice"],16))
    workflow = client.get(f"/real/accounts/{account}/mexc-workflow", headers=headers)
    assert workflow.status_code == 200 and "funding_action" not in workflow.json()
    closed = resolved.json()
    assert closed["status"]=="CHAIN_CLOSED" and closed["chain_close"]==kind
    assert closed["gas_fee_wei"]==expected_gas
    assert not closed["exchange_credited"] and not closed["booked"] and "discovered_tx_hash" not in closed
    calls = len(rpc.calls)
    assert client.post(original+"/discover",headers=headers,json={"address":wallet.address}).json()==closed
    assert len(rpc.calls)==calls
    assert client.post(original+"/check",headers=headers,json={"evidence":raw}).status_code==409
    assert not claim(plan).json()["claim_granted"]
    same_nonce = client.post(base,headers=headers,json=body(wallet)).json()
    blocked = claim(same_nonce)
    assert blocked.status_code==409 and "consumido" in blocked.text
    new_nonce = client.post(base,headers=headers,json={**body(wallet),"nonce":"5"}).json()
    assert claim(new_nonce).json()["claim_granted"]
    assert client.get(f"/real/accounts/{account}/snapshot",headers=headers).json()["cash"]=={}
    assert client.post("/real/vault/lock",headers=headers).status_code==200
    with RealVaultContext(tmp_path/"real.db") as vault, vault.session() as db:
        from app.portfolio.wallet_funding import funding_record
        row, _, state = funding_record(db,plan["id"])
        assert state["chain_close"]["kind"]==kind
        assert raw["tx_hash"] not in row.state_json and wallet.address.lower() not in row.state_json.lower()


def test_funding_workflow_links_exact_pending_item_and_focused_history_stays_in_book(tmp_path):
    client, headers, account, wallet = setup(tmp_path)
    prove(client,headers,account,wallet)
    base = f"/real/accounts/{account}/wallet-funding"
    original = client.post(base,headers=headers,json=body(wallet)).json()
    permission = {"address":wallet.address,"plan_digest":original["plan_digest"],
                  "confirmation":"SOLICITAR_ENVIO_WALLET"}
    assert client.post(f"{base}/{original['id']}/claim",headers=headers,json=permission).json()["claim_granted"]
    newer = client.post(base,headers=headers,json={**body(wallet),"nonce":"5"}).json()
    assert [row["id"] for row in client.get(base,headers=headers).json()] == [newer["id"],original["id"]]
    workflow = client.get(f"/real/accounts/{account}/mexc-workflow",headers=headers).json()
    assert (workflow["funding_action"],workflow["funding_id"]) == ("SEARCH",original["id"])
    scoped = {**headers,"X-Orbis-Passive":"1"}
    focus = {"focus_id":original["id"]}
    result = client.get(base,headers=scoped,params=focus)
    assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
    assert [row["id"] for row in result.json()] == [original["id"],newer["id"]]
    assert wallet.address.lower() not in result.text.lower() and "transaction" not in result.json()[0]
    other = client.post("/real/accounts",headers=headers,json={"name":"Otro libro","base_currency":"USDT"}).json()["id"]
    assert client.get(base.replace(account,other,1),headers=scoped,params=focus).json() == []
    assert client.get(base,headers={**scoped,"Origin":"https://outside.invalid"},params=focus).status_code == 403
    assert client.get(base,headers=scoped,params={"focus_id":"bad"}).status_code == 422
