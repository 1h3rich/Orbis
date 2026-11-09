"""Permisos de retirada independientes y transporte controlado, nunca mainnet."""

import hashlib
import hmac
from urllib.parse import parse_qs,urlparse
from uuid import uuid4

import pytest

from app.integrations.mexc_readonly import MexcReadOnlyError
from app.live_execution.mexc_withdrawal_transport import MexcWithdrawalClient,MexcWithdrawalError,WithdrawalRequest
from tests.test_mexc_transfers_readonly import client as view_client,KEY,SECRET,NOW
from tests.test_wallet_funding import WALLET,CONTRACT


def adapter(*,permissions="SPOT_ACCOUNT_READ,SPOT_WITHDRAW_READ,SPOT_WITHDRAW_WRITE",uid="123456789",fail=False):
    posts = []
    def get(url,headers):
        path = urlparse(url).path
        if path == "/api/v3/apiKeyInfo":
            return {"status":"VALID","permissions":permissions,"apikey":KEY}
        assert path == "/api/v3/uid"
        return {"uid":uid}
    def post(url,body,headers):
        assert url == "https://api.mexc.com/api/v3/capital/withdraw" and "?" not in url
        assert headers == {"X-MEXC-APIKEY":KEY,"Content-Type":"application/json"}
        unsigned,signature = body.rsplit("&signature=",1)
        assert hmac.new(SECRET.encode(),unsigned.encode(),hashlib.sha256).hexdigest() == signature
        posts.append(parse_qs(unsigned))
        if fail:
            raise RuntimeError("fixture accepted but lost reply: "+SECRET+WALLET)
        return {"id":"external-withdrawal-id"}
    return MexcWithdrawalClient(KEY,SECRET,http_get=get,http_post=post,clock_ms=lambda:NOW),posts


def test_distinct_permissions_same_uid_raw_body_single_post_and_no_secret_error():
    client,posts = adapter()
    request = WithdrawalRequest(str(uuid4()),WALLET,CONTRACT,"10.123456789012345678")
    with pytest.raises(MexcWithdrawalError):
        client.withdraw_once(request)
    client.pair_view_account("123456789")
    assert client.withdraw_once(request) == "external-withdrawal-id"
    assert posts[0]["netWork"] == ["BSC"] and posts[0]["contractAddress"] == [CONTRACT]
    assert posts[0]["amount"] == [request.amount] and posts[0]["address"] == [WALLET]
    with pytest.raises(MexcWithdrawalError):
        client.withdraw_once(request)
    assert len(posts) == 1
    broken,posts = adapter(fail=True)
    broken.pair_view_account("123456789")
    with pytest.raises(MexcWithdrawalError) as error:
        broken.withdraw_once(request)
    assert SECRET not in str(error.value) and WALLET not in str(error.value)
    with pytest.raises(MexcWithdrawalError):
        broken.withdraw_once(request)
    assert len(posts) == 1 and SECRET not in repr(broken)


@pytest.mark.parametrize("permissions",[
    "SPOT_ACCOUNT_READ,SPOT_WITHDRAW_READ",
    "SPOT_ACCOUNT_READ,SPOT_DEAL_WRITE",
    "SPOT_ACCOUNT_READ,SPOT_WITHDRAW_READ,SPOT_WITHDRAW_WRITE,SPOT_DEAL_WRITE",
])
def test_readonly_trade_or_overpowered_key_cannot_authorize_withdrawal(permissions):
    client,posts = adapter(permissions=permissions)
    with pytest.raises(MexcWithdrawalError):
        client.pair_view_account("123456789")
    assert not posts


def test_other_uid_is_rejected():
    client,posts = adapter(uid="987654321")
    with pytest.raises(MexcWithdrawalError):
        client.pair_view_account("123456789")
    assert not posts


def test_readonly_adapter_still_rejects_withdraw_write_credentials():
    client,_ = view_client(permissions="SPOT_ACCOUNT_READ,SPOT_WITHDRAW_READ,SPOT_WITHDRAW_WRITE")
    with pytest.raises(MexcReadOnlyError,match="escritura"):
        client.verify_permissions()
