"""Ingresos/retiros MEXC: sólo lectura, sin direcciones ni contabilidad."""

import hashlib
import hmac
from urllib.parse import parse_qs, urlparse

import pytest

from app.integrations.mexc_readonly import MexcReadOnlyClient, MexcReadOnlyError


KEY = "transfer-test-access-key"
SECRET = "transfer-test-secret-key"
NOW = 1_800_000_000_000


def client(*, permissions="SPOT_ACCOUNT_READ,SPOT_WITHDRAW_READ", deposits=None,
           withdrawals=None, catalog=None):
    calls = []

    def get(url, headers):
        parsed = urlparse(url)
        assert parsed.netloc == "api.mexc.com" and parsed.scheme == "https"
        assert headers == {"X-MEXC-APIKEY": KEY}
        query, signature = parsed.query.rsplit("&signature=", 1)
        assert signature == hmac.new(SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
        params = parse_qs(query)
        calls.append((parsed.path, params))
        if parsed.path == "/api/v3/apiKeyInfo":
            return {"status": "VALID", "permissions": permissions, "apikey": KEY}
        if parsed.path == "/api/v3/capital/config/getall":
            return catalog if catalog is not None else []
        assert NOW - 7 * 86_400_000 <= int(params["startTime"][0]) <= int(params["endTime"][0]) <= NOW
        assert int(params["endTime"][0]) - int(params["startTime"][0]) <= 7 * 86_400_000
        assert params["limit"] == ["1000"]
        if parsed.path == "/api/v3/capital/deposit/hisrec":
            return deposits if deposits is not None else [{
                "coin": "BTC", "amount": "0.123456789012345678", "network": "BTC",
                "status": 5, "insertTime": NOW - 1000, "address": "private-wallet-address",
                "memo": "private-tag", "txId": "private-chain-hash",
            }]
        assert parsed.path == "/api/v3/capital/withdraw/history"
        return withdrawals if withdrawals is not None else [{
            "coin": "USDT", "amount": "10.50", "transactionFee": "0.25",
            "network": "ERC20", "status": 7, "applyTime": NOW - 2000,
            "transferType": 0,
            "address": "private-destination", "memo": "private-memo",
            "txId": "private-withdrawal-hash", "id": "external-withdrawal-id",
        }]

    return MexcReadOnlyClient(KEY, SECRET, http_get=get, clock_ms=lambda: NOW), calls


def test_transfer_snapshot_strips_addresses_and_is_not_accounting():
    reader, calls = client()
    result = reader.transfers_recent()
    assert [path for path, _ in calls] == [
        "/api/v3/apiKeyInfo", "/api/v3/capital/deposit/hisrec",
        "/api/v3/capital/withdraw/history"]
    assert result["accounting"] == "OBSERVED_NOT_IMPORTED"
    assert result["coverage"] == "RECENT_WINDOW_BELOW_PAGE_LIMIT"
    assert result["deposits"] == [{"direction": "deposit", "asset": "BTC", "network": "BTC",
                                   "amount": "0.123456789012345678", "fee": "0", "status": 5,
                                   "time_ms": NOW - 1000}]
    assert result["withdrawals"][0]["fee"] == "0.25"
    assert result["withdrawals"][0]["transfer_type"] == 0
    assert not any(secret in str(result) for secret in (
        "private-wallet-address", "private-destination", "private-memo",
        "private-tag", "private-chain-hash", "private-withdrawal-hash", KEY, SECRET))


def test_private_source_ids_are_only_returned_on_internal_request():
    reader, _ = client()
    private = reader.transfers_recent(include_source_ids=True)
    assert private["deposits"][0]["source_identity"] == "private-chain-hash"
    assert private["withdrawals"][0]["source_identity"] == "external-withdrawal-id"
    public = reader.transfers_recent()
    assert "source_identity" not in public["deposits"][0]
    assert "source_identity" not in public["withdrawals"][0]


@pytest.mark.parametrize("network",["ERC20","ETH","BSC","BEP20","BEP20(BSC)"])
def test_evm_deposit_destination_is_internal_only_and_memo_blocks_identity(network):
    address = "0x" + "33" * 20
    deposit = {"coin": "USDT", "amount": "25", "network": network, "status": 5,
               "insertTime": NOW - 1000, "address": address, "memo": "", "txId": "0x" + "ab" * 32}
    reader, _ = client(deposits=[deposit])
    assert reader.transfers_recent(include_source_ids=True)["deposits"][0]["destination_identity"] == address
    assert "destination_identity" not in reader.transfers_recent()["deposits"][0]
    reader, _ = client(deposits=[{**deposit, "memo": "required-tag"}])
    assert "destination_identity" not in reader.transfers_recent(include_source_ids=True)["deposits"][0]


def test_evm_withdrawal_chain_and_recipient_are_internal_only():
    tx_hash = "0x" + "ab" * 32
    address = "0x" + "33" * 20
    withdrawal = {"coin": "USDT", "amount": "25", "transactionFee": "0.5",
                  "network": "ERC20", "status": 7, "applyTime": NOW - 1000,
                  "transferType": 0, "id": "withdrawal-id", "transHash": tx_hash,
                  "txId": None, "address": address, "memo": ""}
    reader, _ = client(withdrawals=[withdrawal])
    private = reader.transfers_recent(include_source_ids=True)["withdrawals"][0]
    assert private["source_identity"] == "withdrawal-id"
    assert private["chain_identity"] == tx_hash
    assert private["destination_identity"] == address
    assert tx_hash not in str(reader.transfers_recent())
    assert address not in str(reader.transfers_recent())
    reader, _ = client(withdrawals=[{**withdrawal, "txId": "0x" + "cd" * 32}])
    assert "chain_identity" not in reader.transfers_recent(include_source_ids=True)["withdrawals"][0]
    reader, _ = client(withdrawals=[{**withdrawal, "memo": "private-tag"}])
    assert "destination_identity" not in reader.transfers_recent(include_source_ids=True)["withdrawals"][0]


@pytest.mark.parametrize("network",[None,"BSC","BEP20","BEP20(BSC)","ETH"])
def test_own_bnb_withdrawal_has_opt_in_private_client_identity_even_when_network_is_absent_or_wrong(network):
    tx_hash,address,request_id = "0x"+"ab"*32,"0x"+"33"*20,"1234567890abcdef1234567890abcdef"
    row = {"coin":"USDT","amount":"25","transactionFee":"0.5","network":network,"status":7,
        "applyTime":NOW-1000,"transferType":0,"id":"own-withdrawal-id","withdrawOrderId":request_id,
        "transHash":tx_hash,"address":address,"memo":""}
    reader,_ = client(withdrawals=[row])
    with pytest.raises(MexcReadOnlyError):
        reader.transfers_recent(include_withdrawal_evm=True)
    public = reader.transfers_recent()
    assert all(value not in str(public) for value in (address,request_id,tx_hash))
    assert "request_identity" not in reader.transfers_recent(include_source_ids=True)["withdrawals"][0]
    private = reader.transfers_recent(include_source_ids=True,include_withdrawal_evm=True)["withdrawals"][0]
    assert private["request_identity"] == request_id and private["chain_identity"] == tx_hash
    assert private["destination_identity"] == address and private["network"] == network
    reader,_ = client(withdrawals=[{**row,"txId":"0x"+"cd"*32}])
    assert "chain_identity" not in reader.transfers_recent(include_source_ids=True,include_withdrawal_evm=True)["withdrawals"][0]


def test_ethereum_token_contract_catalog_requires_unique_enabled_eth_mapping():
    contract = "0x" + "44" * 20
    other = "0x" + "55" * 20
    reader, calls = client(catalog=[
        {"coin": "USDT", "networkList": [{"netWork": "ETH", "depositEnable": True,
                                           "contract": contract}]},
        {"coin": "AMB", "networkList": [
            {"netWork": "ETH", "depositEnable": True, "contract": contract},
            {"netWork": "ETH", "depositEnable": True, "contract": other}]},
        {"coin": "OFF", "networkList": [{"netWork": "ETH", "depositEnable": False,
                                          "contract": other}]},
        {"coin": "UNCERTAIN", "networkList": [
            {"netWork": "ETH", "depositEnable": True, "contract": contract},
            {"netWork": "ETH", "depositEnable": True, "contract": None}]},
        {"coin": "TRX", "networkList": [{"netWork": "TRX", "depositEnable": True,
                                          "contract": "TRrkeSxEypfiTn7gU3Gg3tP756cEhK493b"}]},
    ])
    assert reader.ethereum_token_contracts() == {"USDT": contract}
    assert [path for path, _ in calls] == ["/api/v3/apiKeyInfo", "/api/v3/capital/config/getall"]
    reader, _ = client(permissions="SPOT_ACCOUNT_READ")
    with pytest.raises(MexcReadOnlyError, match="SPOT_WITHDRAW_READ"):
        reader.ethereum_token_contracts()


def test_eth_catalog_separates_deposit_and_withdrawal_contract_eligibility():
    contract = "0x" + "44" * 20
    other = "0x" + "55" * 20
    reader, calls = client(catalog=[
        {"coin": "USDT", "networkList": [{"netWork": "ETH", "depositEnable": True,
                                            "withdrawEnable": True, "contract": contract}]},
        {"coin": "DEPOSIT", "networkList": [{"netWork": "ETH", "depositEnable": True,
                                               "withdrawEnable": False, "contract": contract}]},
        {"coin": "WITHDRAW", "networkList": [{"netWork": "ETH", "depositEnable": False,
                                                "withdrawEnable": True, "contract": other}]},
        {"coin": "AMB", "networkList": [
            {"netWork": "ETH", "depositEnable": False,
             "withdrawEnable": True, "contract": contract},
            {"netWork": "ETH", "depositEnable": False,
             "withdrawEnable": True, "contract": other}]},
    ])
    mapping = reader.ethereum_token_contracts_by_direction()
    assert mapping == {"deposit": {"USDT": contract, "DEPOSIT": contract},
                       "withdrawal": {"USDT": contract, "WITHDRAW": other}}
    assert [path for path, _ in calls] == ["/api/v3/apiKeyInfo", "/api/v3/capital/config/getall"]


def test_transfer_permission_is_separate_and_read_only():
    reader, calls = client(permissions="SPOT_ACCOUNT_READ")
    with pytest.raises(MexcReadOnlyError, match="SPOT_WITHDRAW_READ"):
        reader.transfers_recent()
    assert [path for path, _ in calls] == ["/api/v3/apiKeyInfo"]
    reader, calls = client(permissions="SPOT_ACCOUNT_READ,SPOT_WITHDRAW_WRITE")
    with pytest.raises(MexcReadOnlyError, match="escritura"):
        reader.transfers_recent()
    assert [path for path, _ in calls] == ["/api/v3/apiKeyInfo"]


def test_transfer_route_is_preserved_or_unknown_without_guessing():
    base = {"coin": "USDT", "amount": "10", "status": 7,
            "applyTime": NOW - 1000}
    reader, _ = client(withdrawals=[{**base, "transferType": 1}])
    assert reader.transfers_recent()["withdrawals"][0]["transfer_type"] == 1
    reader, _ = client(withdrawals=[base])
    assert reader.transfers_recent()["withdrawals"][0]["transfer_type"] is None
    reader, _ = client(withdrawals=[{**base, "transferType": "0"}])
    with pytest.raises(MexcReadOnlyError, match="tipo de retiro"):
        reader.transfers_recent()


@pytest.mark.parametrize("bad", [
    {"amount": "NaN"}, {"amount": "-1"}, {"status": "5"},
    {"insertTime": NOW + 1}, {"coin": "BTC/EUR"},
])
def test_transfer_malformed_page_fails_closed(bad):
    row = {"coin": "BTC", "amount": "1", "network": "BTC", "status": 5,
           "insertTime": NOW - 1000}
    row.update(bad)
    reader, _ = client(deposits=[row])
    with pytest.raises(MexcReadOnlyError):
        reader.transfers_recent()


def test_full_transfer_page_is_not_reported_complete():
    row = {"coin": "BTC", "amount": "1", "status": 5, "insertTime": NOW - 1000}
    reader, _ = client(deposits=[row] * 1000)
    with pytest.raises(MexcReadOnlyError, match="rango incompleto"):
        reader.transfers_recent()


def test_transfer_period_must_not_exceed_exchange_window():
    reader, calls = client()
    with pytest.raises(MexcReadOnlyError):
        reader.transfers_recent(days=91)
    assert calls == []


@pytest.mark.parametrize("days, expected_requests", [(30, 10), (90, 26)])
def test_long_transfer_range_is_partitioned_without_overlap(days, expected_requests):
    intervals = {"/api/v3/capital/deposit/hisrec": [],
                 "/api/v3/capital/withdraw/history": []}

    def get(url, headers):
        parsed = urlparse(url)
        if parsed.path == "/api/v3/apiKeyInfo":
            return {"status": "VALID", "permissions": "SPOT_ACCOUNT_READ,SPOT_WITHDRAW_READ",
                    "apikey": KEY}
        params = parse_qs(parsed.query)
        first, last = int(params["startTime"][0]), int(params["endTime"][0])
        assert 0 <= last - first <= 7 * 86_400_000
        intervals[parsed.path].append((first, last))
        return []

    reader = MexcReadOnlyClient(KEY, SECRET, http_get=get, clock_ms=lambda: NOW)
    result = reader.transfers_recent(days=days)
    assert result["requests"] == expected_requests
    assert result["deposits"] == result["withdrawals"] == []
    assert result["start_ms"] == NOW - days * 86_400_000 + (60_000 if days == 90 else 0)
    for windows in intervals.values():
        assert windows[0][0] == result["start_ms"]
        assert windows[-1][1] == NOW
        assert all(previous[1] + 1 == following[0]
                   for previous, following in zip(windows, windows[1:]))


def test_full_page_is_split_and_inconsistent_children_fail_closed():
    seen = []

    def get(url, headers):
        parsed = urlparse(url)
        if parsed.path == "/api/v3/apiKeyInfo":
            return {"status": "VALID", "permissions": "SPOT_ACCOUNT_READ,SPOT_WITHDRAW_READ",
                    "apikey": KEY}
        params = parse_qs(parsed.query)
        first, last = int(params["startTime"][0]), int(params["endTime"][0])
        seen.append((first, last))
        if len(seen) == 1:
            return [{"coin": "BTC", "amount": "1", "status": 5,
                     "insertTime": NOW - 1000}] * 1000
        return []

    reader = MexcReadOnlyClient(KEY, SECRET, http_get=get, clock_ms=lambda: NOW)
    with pytest.raises(MexcReadOnlyError, match="contradictorias"):
        reader.transfers_recent(days=7)
    assert len(seen) == 3
