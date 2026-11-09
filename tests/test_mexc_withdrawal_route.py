"""Catálogo BSC y paginación completa de destinos mediante HTTP ficticio."""

from urllib.parse import parse_qs,urlparse

import pytest

from app.integrations.mexc_readonly import MexcReadOnlyClient,MexcReadOnlyError
from tests.test_mexc_transfers_readonly import KEY,SECRET
from tests.test_wallet_funding import WALLET,CONTRACT


def reader(*,routes=None,destinations=None):
    routes = routes if routes is not None else [{"coin":"USDT","netWork":"BSC","contract":CONTRACT,
        "withdrawEnable":True,"withdrawFee":"0.01","withdrawMin":"1","withdrawMax":"1000","withdrawIntegerMultiple":None}]
    destinations = destinations if destinations is not None else [
        {"coin":"USDT","network":"ERC20","address":WALLET,"memo":None},
        {"coin":"USDT","network":"BEP20(BSC)","address":WALLET,"addressTag":"alias-not-memo","memo":None}]
    calls = []
    def get(url,headers):
        parsed = urlparse(url)
        calls.append(parsed.path)
        if parsed.path == "/api/v3/apiKeyInfo":
            return {"status":"VALID","permissions":"SPOT_ACCOUNT_READ,SPOT_WITHDRAW_READ","apikey":KEY}
        if parsed.path == "/api/v3/capital/config/getall":
            return [{"coin":"USDT","networkList":routes}]
        assert parsed.path == "/api/v3/capital/withdraw/address"
        assert parse_qs(parsed.query)["coin"] == ["USDT"]
        return {"data":destinations,"totalRecords":len(destinations),"totalPageNum":1 if destinations else 0,"page":1}
    return MexcReadOnlyClient(KEY,SECRET,http_get=get),calls


def test_unique_enabled_bsc_route_and_saved_destination_without_exposing_private_alias_or_address():
    client,calls = reader()
    result = client.bsc_withdrawal_route(WALLET)
    assert result["network"] == "BSC" and result["token_contract"] == CONTRACT and result["recipient_listed"]
    assert result["fee"] == "0.01" and result["increment"] is None
    assert WALLET not in str(result) and "alias-not-memo" not in str(result)
    assert calls[-1] == "/api/v3/capital/withdraw/address"


@pytest.mark.parametrize("destinations",[
    [],[{"coin":"USDT","network":"ERC20","address":WALLET,"memo":None}],
    [{"coin":"USDT","network":"BSC","address":WALLET,"memo":"required-tag"}],
    [{"coin":"USDT","network":"BSC","address":WALLET,"memo":None}]*2,
])
def test_missing_wrong_network_memo_or_duplicated_destination_is_not_authorized(destinations):
    client,_ = reader(destinations=destinations)
    with pytest.raises(MexcReadOnlyError):
        client.bsc_withdrawal_route(WALLET)


def test_disabled_or_ambiguous_route_is_rejected_before_reading_address_book():
    row = {"coin":"USDT","netWork":"BSC","contract":CONTRACT,"withdrawEnable":False}
    for routes in ([row],[{**row,"withdrawEnable":True}]*2):
        client,calls = reader(routes=routes)
        with pytest.raises(MexcReadOnlyError):
            client.bsc_withdrawal_route(WALLET)
        assert "/api/v3/capital/withdraw/address" not in calls
