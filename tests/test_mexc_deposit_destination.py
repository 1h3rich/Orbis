"""Un destino ERC-20 observado sólo vive en la respuesta de la sesión View."""

import hashlib
import hmac
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.integrations.mexc_readonly import MexcReadOnlyClient, MexcReadOnlyError
from app.portfolio.api import create_real_router


KEY = "deposit-destination-access"
SECRET = "deposit-destination-secret"
ADDRESS = "0x" + "11" * 20
CONTRACT = "0x" + "44" * 20


def reader(*, address_rows=None, catalog_enabled=True):
    calls = []

    def get(url, headers):
        parsed = urlparse(url)
        unsigned, signature = parsed.query.rsplit("&signature=", 1)
        assert parsed.scheme == "https" and parsed.netloc == "api.mexc.com"
        assert headers == {"X-MEXC-APIKEY": KEY}
        assert signature == hmac.new(SECRET.encode(), unsigned.encode(), hashlib.sha256).hexdigest()
        params = parse_qs(unsigned)
        calls.append((parsed.path, params))
        if parsed.path == "/api/v3/apiKeyInfo":
            return {"status": "VALID", "permissions": "SPOT_ACCOUNT_READ,SPOT_WITHDRAW_READ",
                    "apikey": KEY}
        if parsed.path == "/api/v3/capital/config/getall":
            return [{"coin": "USDT", "networkList": [{"netWork": "ETH",
                     "depositEnable": catalog_enabled, "contract": CONTRACT}]}]
        assert parsed.path == "/api/v3/capital/deposit/address"
        assert params["coin"] == ["USDT"] and "network" not in params
        return address_rows if address_rows is not None else [
            {"coin": "USDT", "network": "ERC20", "address": ADDRESS, "memo": None},
            {"coin": "USDT", "network": "TRC20", "address": "TRON-private", "memo": None},
        ]

    return MexcReadOnlyClient(KEY, SECRET, http_get=get,
                              clock_ms=lambda: 1_800_000_000_000), calls


def test_unique_enabled_erc20_destination_is_ephemeral_and_read_only():
    client, calls = reader()
    result = client.ethereum_erc20_deposit_destination("USDT")
    assert result["deposit_address"] == ADDRESS
    assert result["token_contract"] == CONTRACT
    assert result["network"] == "ETHEREUM_MAINNET"
    assert result["stored"] is result["transfer_sent"] is result["ready_for_execution"] is False
    assert [path for path, _ in calls] == [
        "/api/v3/apiKeyInfo", "/api/v3/capital/config/getall",
        "/api/v3/capital/deposit/address"]


@pytest.mark.parametrize("bad_rows", [
    [], [{"coin": "USDT", "network": "TRC20", "address": ADDRESS, "memo": None}],
    [{"coin": "USDT", "network": "ERC20", "address": ADDRESS, "memo": "tag"}],
    [{"coin": "OTHER", "network": "ERC20", "address": ADDRESS, "memo": None}],
    [{"coin": "USDT", "network": "ERC20", "address": "0x" + "00" * 20, "memo": None}],
    [{"coin": "USDT", "network": "ERC20", "address": ADDRESS, "memo": None},
     {"coin": "USDT", "network": "ETH", "address": ADDRESS, "memo": None}],
])
def test_ambiguous_or_unsafe_destination_is_rejected(bad_rows):
    client, _ = reader(address_rows=bad_rows)
    with pytest.raises(MexcReadOnlyError):
        client.ethereum_erc20_deposit_destination("USDT")


def test_disabled_catalog_and_invalid_asset_fail_before_address_request():
    client, calls = reader(catalog_enabled=False)
    with pytest.raises(MexcReadOnlyError):
        client.ethereum_erc20_deposit_destination("USDT")
    assert "/api/v3/capital/deposit/address" not in [path for path, _ in calls]
    for asset in ("ETH", "usdt", "USDT/ETH", ""):
        with pytest.raises(MexcReadOnlyError):
            client.ethereum_erc20_deposit_destination(asset)


def test_bsc_metadata_and_destination_are_not_confused_with_ethereum():
    calls = []
    def get(url,headers):
        path = urlparse(url).path
        calls.append(path)
        if path == "/api/v3/apiKeyInfo":
            return {"status":"VALID","permissions":"SPOT_ACCOUNT_READ,SPOT_WITHDRAW_READ","apikey":KEY}
        if path == "/api/v3/capital/config/getall":
            return [{"coin":"USDT","networkList":[
                {"netWork":"ETH","contract":"0x"+"88"*20,"depositEnable":True},
                {"netWork":"BSC","contract":CONTRACT,"depositEnable":True}]}]
        assert path == "/api/v3/capital/deposit/address"
        return [{"coin":"USDT","network":"ERC20","address":"0x"+"99"*20,"memo":None},
                {"coin":"USDT","network":"BEP20(BSC)","address":ADDRESS,"memo":None}]
    client = MexcReadOnlyClient(KEY,SECRET,http_get=get)
    result = client.evm_token_deposit_destination("USDT",chain_id="0x38")
    assert result["network"] == "BNB_SMART_CHAIN_MAINNET"
    assert result["deposit_address"] == ADDRESS and result["token_contract"] == CONTRACT
    assert result["stored"] is result["transfer_sent"] is result["ready_for_execution"] is False
    count = len(calls)
    with pytest.raises(MexcReadOnlyError):
        client.evm_token_deposit_destination("USDT",chain_id="0x61")
    assert len(calls) == count


class FakeMexc:
    uid = "123456789"

    def __init__(self, key, secret):
        self.key, self.secret = key, secret

    def account_reference(self):
        return hmac.new(self.secret.encode(), self.key.encode(), hashlib.sha256).hexdigest()

    def account_uid(self):
        return self.uid

    def balances(self):
        return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "observed_at": "2026-10-20T00:00:00+00:00", "balances": []}

    def ethereum_erc20_deposit_destination(self, asset):
        assert asset == "USDT"
        return {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                "network": "ETHEREUM_MAINNET", "asset": asset,
                "token_contract": CONTRACT, "deposit_address": ADDRESS,
                "observed_at": "2026-10-20T00:00:00+00:00", "stored": False,
                "transfer_sent": False, "ready_for_execution": False}


def test_destination_api_requires_linked_verified_view_and_never_archives_address(tmp_path):
    app = FastAPI()
    vault_path = tmp_path / "real.db"
    app.include_router(create_real_router(vault_path, mexc_factory=FakeMexc))
    client = TestClient(app)
    token = client.post("/real/vault/create", json={"passphrase": "destination test passphrase"}).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    account = client.post("/real/accounts", json={"name": "MEXC"}, headers=headers).json()["id"]
    other = client.post("/real/accounts", json={"name": "Other"}, headers=headers).json()["id"]
    path = f"/real/accounts/{account}/mexc-erc20-deposit-destination?asset=USDT"
    assert client.get(path).status_code == 401
    assert client.get(path, headers=headers).status_code == 409
    assert client.post("/real/exchanges/mexc/connect", json={"api_key": KEY, "secret_key": SECRET},
                       headers=headers).status_code == 200
    assert client.get(path, headers=headers).status_code == 409
    assert client.post(f"/real/accounts/{account}/mexc-link", json={"confirmation": "VINCULAR"},
                       headers=headers).status_code == 200
    response = client.get(path, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["deposit_address"] == ADDRESS
    assert client.get(path.replace(account, other), headers=headers).status_code == 409
    assert ADDRESS.encode() not in vault_path.read_bytes()
    FakeMexc.uid = "987654321"
    try:
        assert client.get(path, headers=headers).status_code == 503
    finally:
        FakeMexc.uid = "123456789"
