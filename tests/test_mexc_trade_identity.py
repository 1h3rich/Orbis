"""La futura clave Trade sólo se inspecciona, nunca ejecuta órdenes."""

import hashlib
import hmac
from urllib.parse import parse_qs, urlparse

import pytest

from app.live_execution.mexc_trade_identity import (
    MexcTradeIdentityError, MexcTradeIdentityProbe, verify_same_mexc_account,
)


KEY = "fixture-trade-key"
SECRET = "fixture-trade-secret"


def source(*, permissions="SPOT_ACCOUNT_READ,SPOT_DEAL_WRITE", uid="209302839",
           status="VALID", echoed=KEY, validity=30):
    calls = []

    def fake_get(url, headers):
        parsed = urlparse(url)
        assert parsed.scheme == "https" and parsed.netloc == "api.mexc.com"
        assert parsed.path in ("/api/v3/apiKeyInfo", "/api/v3/uid")
        assert headers == {"X-MEXC-APIKEY": KEY}
        unsigned, signature = parsed.query.rsplit("&signature=", 1)
        assert signature == hmac.new(SECRET.encode(), unsigned.encode(), hashlib.sha256).hexdigest()
        assert parse_qs(unsigned)["timestamp"] == ["1800000000000"]
        assert parse_qs(unsigned)["recvWindow"] == ["5000"]
        calls.append(parsed.path)
        if parsed.path.endswith("apiKeyInfo"):
            assert parse_qs(unsigned)["accessKey"] == [KEY]
            return {"status": status, "permissions": permissions, "apikey": echoed,
                    "remainingValidity": validity}
        return {"uid": uid}

    return fake_get, calls


def probe(**options):
    fake_get, calls = source(**options)
    return MexcTradeIdentityProbe(KEY, SECRET, http_get=fake_get,
                                  clock_ms=lambda: 1_800_000_000_000), calls


def test_pairing_uses_only_signed_get_and_never_exposes_uid_or_secrets():
    candidate, calls = probe()
    assert verify_same_mexc_account("209302839", candidate) is True
    assert calls == ["/api/v3/apiKeyInfo", "/api/v3/uid"]
    assert all(value not in repr(candidate) for value in (KEY, SECRET, "209302839"))


def test_different_account_fails_closed():
    candidate, calls = probe(uid="123456789")
    assert verify_same_mexc_account("209302839", candidate) is False
    assert calls == ["/api/v3/apiKeyInfo", "/api/v3/uid"]


def test_optional_spot_deal_read_is_accepted():
    candidate, calls = probe(permissions="SPOT_ACCOUNT_READ,SPOT_DEAL_READ,SPOT_DEAL_WRITE")
    assert verify_same_mexc_account("209302839", candidate) is True
    assert calls == ["/api/v3/apiKeyInfo", "/api/v3/uid"]


@pytest.mark.parametrize("permissions", [
    "SPOT_ACCOUNT_READ", "SPOT_DEAL_WRITE", "SPOT_ACCOUNT_READ,SPOT_DEAL_READ",
    "SPOT_ACCOUNT_READ,SPOT_DEAL_WRITE,SPOT_TRANSFER_WRITE",
    "SPOT_ACCOUNT_READ,SPOT_DEAL_WRITE,SPOT_ACCOUNT_WRITE",
    "SPOT_ACCOUNT_READ,SPOT_DEAL_WRITE,CONTRACT_DEAL_WRITE",
    "SPOT_ACCOUNT_READ,SPOT_DEAL_WRITE,UNKNOWN",
])
def test_rejects_missing_or_excessive_permissions_before_uid_request(permissions):
    candidate, calls = probe(permissions=permissions)
    with pytest.raises(MexcTradeIdentityError):
        candidate.account_uid()
    assert calls == ["/api/v3/apiKeyInfo"]


@pytest.mark.parametrize("kwargs", [
    {"uid": ""}, {"uid": 209302839}, {"uid": "001"}, {"uid": "not-a-uid"},
    {"status": "INVALID"}, {"echoed": "another-key"}, {"validity": 0},
])
def test_rejects_invalid_key_or_identity(kwargs):
    candidate, _ = probe(**kwargs)
    with pytest.raises(MexcTradeIdentityError):
        candidate.account_uid()


def test_invalid_view_identity_never_queries_trade_key():
    candidate, calls = probe()
    with pytest.raises(MexcTradeIdentityError):
        verify_same_mexc_account("invalid", candidate)
    assert calls == []


def test_network_error_is_sanitized():
    def fails(_url, _headers):
        raise RuntimeError(f"remote failure with {SECRET}")

    candidate = MexcTradeIdentityProbe(KEY, SECRET, http_get=fails,
                                       clock_ms=lambda: 1_800_000_000_000)
    with pytest.raises(MexcTradeIdentityError) as error:
        candidate.account_uid()
    assert KEY not in str(error.value) and SECRET not in str(error.value)


@pytest.mark.parametrize("malformed_path", ["/api/v3/apiKeyInfo", "/api/v3/uid"])
def test_non_object_identity_response_fails_closed(malformed_path):
    normal_get, _ = source()

    def fake_get(url, headers):
        if urlparse(url).path == malformed_path:
            return ["not-an-object"]
        return normal_get(url, headers)

    candidate = MexcTradeIdentityProbe(KEY, SECRET, http_get=fake_get,
                                       clock_ms=lambda: 1_800_000_000_000)
    with pytest.raises(MexcTradeIdentityError) as error:
        candidate.account_uid()
    assert KEY not in str(error.value) and SECRET not in str(error.value)
