from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import hmac
from urllib.parse import parse_qs
from uuid import UUID

import pytest

from app.live_execution.journal import RecordedIntent
from app.live_execution.mexc_instrument import MexcPreflight
from app.live_execution.mexc_test_order import MexcOrderTestError, MexcSpotOrderTestClient
from app.live_execution.policy import LiveSpotIntent


INTENT_ID = "812a529b-398a-438f-9050-7a62bba71711"
POLICY_ID = "651a674d-3427-40a7-94cc-290168123310"
KEY = "fake-access-key"
SECRET = "fake-secret-key"


def case():
    intent = LiveSpotIntent(
        intent_id=INTENT_ID, policy_id=POLICY_ID,
        strategy_version_id="v1", account_ref="a" * 64,
        symbol="BTCUSDT", side="BUY", quantity_base=Decimal("0.001"),
        limit_price_quote=Decimal("100000"), estimated_fee_quote=Decimal("0.1"),
        signal_at=datetime(2026, 10, 13, tzinfo=timezone.utc),
    )
    recorded = RecordedIntent(INTENT_ID, "RESERVED", "AUTHORIZED_FOR_RESERVATION",
                              Decimal("100"), Decimal("100.1"), Decimal(0))
    return intent, recorded, MexcPreflight(True, "LOCAL_PREFLIGHT_ONLY")


def test_signed_request_reaches_only_nonexecuting_test_endpoint():
    calls = []

    def fake_post(url, body, headers):
        calls.append((url, body, headers))
        return {}

    client = MexcSpotOrderTestClient(KEY, SECRET, http_post=fake_post,
                                     clock_ms=lambda: 1791850000000)
    intent, recorded, preflight = case()
    result = client.test_reserved_limit(intent, recorded, preflight)
    assert result.accepted_by_test_endpoint is True and result.executed is False
    assert result.client_order_id == UUID(INTENT_ID).hex
    assert len(calls) == 1
    url, body, headers = calls[0]
    assert url == "https://api.mexc.com/api/v3/order/test"
    assert headers == {"X-MEXC-APIKEY": KEY, "Content-Type": "application/json"}
    signed_part, supplied_signature = body.rsplit("&signature=", 1)
    assert supplied_signature == hmac.new(SECRET.encode(), signed_part.encode(), hashlib.sha256).hexdigest()
    params = parse_qs(signed_part)
    assert params == {
        "symbol": ["BTCUSDT"], "side": ["BUY"], "type": ["LIMIT"],
        "quantity": ["0.001"], "price": ["100000"],
        "newClientOrderId": [UUID(INTENT_ID).hex],
        "recvWindow": ["5000"], "timestamp": ["1791850000000"],
    }
    assert SECRET not in repr(client) and KEY not in repr(client)


def test_sell_test_uses_order_quantity_not_extra_base_fee_reservation():
    calls = []
    client = MexcSpotOrderTestClient(
        KEY, SECRET, http_post=lambda url, body, headers: (calls.append(body), {})[1],
        clock_ms=lambda: 1791850000000,
    )
    intent, recorded, preflight = case()
    sell = replace(intent, side="SELL")
    covered = replace(recorded, reserved_quote=Decimal(0), reserved_base=Decimal("0.001001"))
    assert client.test_reserved_limit(sell, covered, preflight).executed is False
    assert parse_qs(calls[0].split("&signature=")[0])["quantity"] == ["0.001"]
    with pytest.raises(MexcOrderTestError, match="reserva"):
        client.test_reserved_limit(sell, replace(covered, reserved_base=Decimal("0.001")), preflight)
    assert len(calls) == 1


@pytest.mark.parametrize("change", [
    {"status": "REJECTED"}, {"intent_id": POLICY_ID},
    {"reserved_quote": Decimal(0)},
])
def test_no_request_without_matching_reservation(change):
    calls = []
    client = MexcSpotOrderTestClient(KEY, SECRET,
                                     http_post=lambda *args: calls.append(args))
    intent, recorded, preflight = case()
    with pytest.raises(MexcOrderTestError, match="reserva"):
        client.test_reserved_limit(intent, replace(recorded, **change), preflight)
    assert calls == []


def test_no_request_without_local_preflight():
    calls = []
    client = MexcSpotOrderTestClient(KEY, SECRET,
                                     http_post=lambda *args: calls.append(args))
    intent, recorded, _ = case()
    with pytest.raises(MexcOrderTestError):
        client.test_reserved_limit(intent, recorded, MexcPreflight(False, "MIN_NOTIONAL"))
    assert calls == []


@pytest.mark.parametrize("payload", [None, [], {"code": 30002, "msg": "rejected"}, {"orderId": "x"}])
def test_ambiguous_or_rejected_test_response_fails_closed(payload):
    client = MexcSpotOrderTestClient(KEY, SECRET, http_post=lambda *args: payload)
    with pytest.raises(MexcOrderTestError, match="no confirmó"):
        client.test_reserved_limit(*case())


def test_transport_failure_is_sanitized_and_never_retried():
    attempts = []

    def fail(*args):
        attempts.append(args)
        raise RuntimeError("secret upstream response")

    client = MexcSpotOrderTestClient(KEY, SECRET, http_post=fail)
    with pytest.raises(MexcOrderTestError, match="No se pudo probar") as exc:
        client.test_reserved_limit(*case())
    assert len(attempts) == 1
    assert "secret" not in str(exc.value)


def test_invalid_credentials_or_clock_are_rejected_without_transport():
    with pytest.raises(MexcOrderTestError, match="credencial"):
        MexcSpotOrderTestClient("short", SECRET)
    client = MexcSpotOrderTestClient(KEY, SECRET, clock_ms=lambda: True,
                                     http_post=lambda *args: pytest.fail("no request expected"))
    with pytest.raises(MexcOrderTestError, match="Reloj"):
        client.test_reserved_limit(*case())
