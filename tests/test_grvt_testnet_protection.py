"""Contratos TP/SL nativos; claves ficticias, sin GRVT ni fondos del usuario."""

from dataclasses import replace
from decimal import Decimal, localcontext
from copy import deepcopy

from eth_account import Account
from eth_account.messages import encode_typed_data
import pytest
import httpx

from app.live_execution.grvt_testnet_order import (
    AUTH_URL, ORDER_URL, LOOKUP_URL, GrvtTestnetLimits, GrvtTestnetOrderClient, GrvtTestnetOrderError,
)
from app.live_execution.grvt_testnet_signer import (
    GrvtTestnetInstrument, GrvtTestnetLimitDraft, _ORDER_TYPES,
)
from app.live_execution.grvt_testnet_protection import (
    GrvtTestnetTrigger, sign_testnet_protection, verify_testnet_protection,
)
from app.live_execution.grvt_testnet_signer import GrvtTestnetSignError


NOW = 1_780_000_000_000_000_000
KEY = bytes.fromhex("11" * 32)  # Clave pública de prueba; nunca una wallet.
SIGNER = Account.from_key(KEY).address
BUILDER = "0x" + "a" * 40
LIMITS = GrvtTestnetLimits("123456", "BTC_USDT_Perp", Decimal("1000"))
INSTRUMENT = GrvtTestnetInstrument(
    "BTC_USDT_Perp", 123456789, 3, Decimal("0.1"), Decimal("0.001"), Decimal("10"),
)


def draft():
    return GrvtTestnetLimitDraft(
        "123456", "BTC_USDT_Perp", "SELL", Decimal("0.01"),
        Decimal("48900"), True, BUILDER, str(2**63 + 42), 42,
        NOW + 86_400_000_000_000,
    )


def test_native_stop_signs_a_gtt_reduction_with_exact_trigger_metadata():
    trigger = GrvtTestnetTrigger("STOP_LOSS", Decimal("49000"))
    order = sign_testnet_protection(
        draft(), LIMITS, INSTRUMENT, trigger,
        dedicated_private_key=KEY, expected_signer=SIGNER, now_ns=NOW,
    )
    assert order["time_in_force"] == "GOOD_TILL_TIME"
    assert order["reduce_only"] is True and order["is_market"] is False
    assert order["metadata"] == {
        "client_order_id": draft().client_order_id,
        "trigger": {"trigger_type": "STOP_LOSS", "tpsl": {
            "trigger_by": "LAST", "trigger_price": "49000",
            "close_position": False, "is_split_position": False,
        }},
    }
    # Contraste independiente con el tipo EIP-712 del SDK oficial: GTT=1.
    signable = encode_typed_data(
        {"name": "GRVT Exchange", "version": "0", "chainId": 326},
        _ORDER_TYPES,
        {"subAccountID": 123456, "isMarket": False, "timeInForce": 1,
         "postOnly": False, "reduceOnly": True,
         "legs": [{"assetID": 123456789, "contractSize": 10,
                   "limitPrice": 48_900_000_000_000, "isBuyingContract": False}],
         "builder": BUILDER, "builderFee": 0, "nonce": 42,
         "expiration": draft().expiration_ns},
    )
    signature = order["signature"]
    assert Account.recover_message(signable, vrs=(
        signature["v"], int(signature["r"], 16), int(signature["s"], 16))) == SIGNER
    assert verify_testnet_protection(
        order, LIMITS, INSTRUMENT, expected_trigger=trigger, now_ns=NOW,
    ) == SIGNER


def test_trigger_tampering_is_rejected_even_though_grvt_does_not_sign_metadata():
    trigger = GrvtTestnetTrigger("STOP_LOSS", Decimal("49000"))
    order = sign_testnet_protection(draft(), LIMITS, INSTRUMENT, trigger,
        dedicated_private_key=KEY, expected_signer=SIGNER, now_ns=NOW)
    changed = deepcopy(order)
    changed["metadata"]["trigger"]["tpsl"]["trigger_price"] = "48950"
    # El cambio no altera la firma, pero sí el contrato local esperado.
    assert changed["signature"] == order["signature"]
    with pytest.raises(GrvtTestnetSignError, match="no verificable"):
        verify_testnet_protection(changed, LIMITS, INSTRUMENT,
                                 expected_trigger=trigger, now_ns=NOW)


@pytest.mark.parametrize(("side", "kind", "trigger_price", "limit_price"), [
    ("SELL", "STOP_LOSS", "49000", "48900"),
    ("SELL", "TAKE_PROFIT", "53000", "52900"),
    ("BUY", "STOP_LOSS", "51000", "51100"),
    ("BUY", "TAKE_PROFIT", "47000", "47100"),
])
def test_both_native_exit_types_sign_for_long_and_short(side, kind, trigger_price, limit_price):
    trigger = GrvtTestnetTrigger(kind, Decimal(trigger_price))
    candidate = replace(draft(), side=side, limit_price=Decimal(limit_price))
    order = sign_testnet_protection(candidate, LIMITS, INSTRUMENT, trigger,
        dedicated_private_key=KEY, expected_signer=SIGNER, now_ns=NOW)
    assert order["legs"][0]["is_buying_asset"] is (side == "BUY")
    assert verify_testnet_protection(order, LIMITS, INSTRUMENT,
        expected_trigger=trigger, now_ns=NOW) == SIGNER


def test_plan_derives_exit_from_observed_position_not_paper_funds():
    candidate = plan()
    assert candidate.side == "BUY" and candidate.size == Decimal("0.01")
    assert candidate.limit_price == Decimal("51127.5")
    assert candidate.reduce_only is True


def plan(*, state_changes=None, quote_changes=None, **changes):
    from app.live_execution.grvt_testnet_protection import compile_testnet_protection
    from app.live_execution.grvt_testnet_quote import GrvtTestnetQuote
    from app.live_execution.grvt_testnet_risk import GrvtTestnetRiskState
    from app.derivatives.paper_math import MarginRules
    state = GrvtTestnetRiskState(
        "123456", "USDT", NOW, NOW, "SIMPLE_CROSS_MARGIN", "SINGLE_ASSET_MODE",
        Decimal("10000"), Decimal("10000"), Decimal("10"), Decimal("5"),
        Decimal("0"), (("BTC_USDT_Perp", Decimal("-0.01")),), 0,
    )
    rules = MarginRules.from_payload({
        "instrument": "BTC_USDT_Perp", "max_position_size": "10", "fetched_at_ns": NOW,
        "risk_brackets": [{"tier": 1, "notional_floor": "0", "notional_cap": None,
            "initial_margin_rate": "0.02", "maintenance_margin_rate": "0.01",
            "max_leverage": 50, "cumulative_maintenance_amount": "0"}],
    }, "BTC_USDT_Perp", NOW)
    quote = GrvtTestnetQuote("BTC_USDT_Perp", NOW, NOW,
        Decimal("49999"), Decimal("50001"), Decimal("10"), Decimal("10"),
        last_price=Decimal("50000"))
    kwargs = {"trigger": GrvtTestnetTrigger("STOP_LOSS", Decimal("51000")),
        "max_slippage_bps": 25, "builder": BUILDER, "client_order_id": draft().client_order_id,
        "nonce": 42, "expiration_ns": draft().expiration_ns, "now_ns": NOW}
    kwargs.update(changes)
    return compile_testnet_protection(replace(state, **(state_changes or {})), rules,
        INSTRUMENT, LIMITS, replace(quote, **(quote_changes or {})), **kwargs)


@pytest.mark.parametrize("changes", [
    {"nonce": True}, {"nonce": -1}, {"nonce": 2**32},
    {"client_order_id": "42"}, {"client_order_id": ""},
    {"client_order_id": str(2**64)}, {"builder": "not-an-address"},
])
def test_plan_rejects_invalid_signing_identity_before_returning_draft(changes):
    with pytest.raises(GrvtTestnetSignError):
        plan(**changes)


def test_native_transport_binds_trigger_ack_and_shares_one_shot_with_ioc():
    trigger = GrvtTestnetTrigger("STOP_LOSS", Decimal("49000"))
    order = sign_testnet_protection(draft(), LIMITS, INSTRUMENT, trigger,
        dedicated_private_key=KEY, expected_signer=SIGNER, now_ns=NOW)
    calls = []
    def post(url, body, headers):
        calls.append((url, body))
        if url == AUTH_URL:
            return httpx.Response(200, json={"status": "success", "sub_account_id": "123456"},
                headers={"X-Grvt-Account-Id": BUILDER,
                         "Set-Cookie": "gravity=fake-testnet-cookie; Path=/"},
                request=httpx.Request("POST", url))
        assert url == ORDER_URL
        return httpx.Response(200, json={"result": {**body["order"], "order_id": "0xstop"}})
    client = GrvtTestnetOrderClient("fake-testnet-key", http_post=post, clock_ns=lambda: NOW)
    # El puente IOC existente no se ensancha implícitamente para admitir GTT.
    with pytest.raises(GrvtTestnetOrderError):
        client.submit_signed_limit(order, LIMITS)
    assert len(calls) == 1
    ack = client.submit_signed_protection(order, LIMITS, expected_trigger=trigger)
    assert ack.accepted_by_testnet and ack.execution_unverified
    assert [url for url, _ in calls] == [AUTH_URL, ORDER_URL]
    with pytest.raises(GrvtTestnetOrderError, match="consumida"):
        client.submit_signed_protection(order, LIMITS, expected_trigger=trigger)
    assert len(calls) == 2


def test_protection_lookup_checks_metadata_and_remains_available_after_expiry():
    trigger = GrvtTestnetTrigger("STOP_LOSS", Decimal("49000"))
    order = sign_testnet_protection(draft(), LIMITS, INSTRUMENT, trigger,
        dedicated_private_key=KEY, expected_signer=SIGNER, now_ns=NOW)
    venue = {**deepcopy(order), "order_id": "0xnative",
        "metadata": {**deepcopy(order["metadata"]), "create_time": str(NOW)},
        "state": {"status": "OPEN", "traded_size": ["0"], "update_time": str(NOW)}}
    clock = [NOW]
    calls = []
    def post(url, body, headers):
        calls.append(url)
        if url == AUTH_URL:
            return httpx.Response(200, json={"status": "success", "sub_account_id": "123456"},
                headers={"X-Grvt-Account-Id": BUILDER,
                         "Set-Cookie": "gravity=fake-testnet-cookie; Path=/"},
                request=httpx.Request("POST", url))
        assert url == LOOKUP_URL
        return httpx.Response(200, json={"result": venue})
    client = GrvtTestnetOrderClient("fake-testnet-key", http_post=post, clock_ns=lambda: clock[0])
    observed = client.lookup_protection(order, LIMITS, expected_trigger=trigger, created_at_ns=NOW)
    assert observed.order.status == "OPEN" and observed.trigger == trigger
    assert observed.active_observed is True and observed.protection_guaranteed is False
    venue["metadata"]["trigger"]["tpsl"]["trigger_price"] = "48950"
    with pytest.raises(GrvtTestnetOrderError, match="no verificable"):
        client.lookup_protection(order, LIMITS, expected_trigger=trigger, created_at_ns=NOW)
    venue["metadata"]["trigger"] = deepcopy(order["metadata"]["trigger"])
    clock[0] = draft().expiration_ns + 1
    # No inferir cancelación del reloj local: se sigue leyendo el estado real.
    observed = client.lookup_protection(order, LIMITS, expected_trigger=trigger, created_at_ns=NOW)
    assert observed.order.status == "OPEN" and observed.active_observed is False
    assert ORDER_URL not in calls


@pytest.mark.parametrize(("held", "kind", "trigger", "side", "limit"), [
    ("0.01", "STOP_LOSS", "49000", "SELL", "48877.5"),
    ("0.01", "TAKE_PROFIT", "53000", "SELL", "52867.5"),
    ("-0.01", "STOP_LOSS", "51000", "BUY", "51127.5"),
    ("-0.01", "TAKE_PROFIT", "47000", "BUY", "47117.5"),
])
def test_plan_places_trigger_on_correct_side_of_last_price(held, kind, trigger, side, limit):
    candidate = plan(state_changes={"positions": (("BTC_USDT_Perp", Decimal(held)),)},
        trigger=GrvtTestnetTrigger(kind, Decimal(trigger)))
    assert candidate.side == side and candidate.limit_price == Decimal(limit)
    assert candidate.size == Decimal("0.01") and candidate.reduce_only is True


@pytest.mark.parametrize("changes", [
    {"trigger": GrvtTestnetTrigger("STOP_LOSS", Decimal("50000"))},
    {"trigger": GrvtTestnetTrigger("STOP_LOSS", Decimal("49000"))},
    {"trigger": GrvtTestnetTrigger("TAKE_PROFIT", Decimal("51000"))},
    {"trigger": GrvtTestnetTrigger("STOP_LOSS", Decimal("51000.01"))},
    {"max_slippage_bps": True}, {"max_slippage_bps": -1}, {"max_slippage_bps": 101},
    {"expiration_ns": NOW}, {"expiration_ns": NOW + 59_000_000_000},
    {"expiration_ns": NOW + 86_400_000_000_001},
    {"quote_changes": {"last_price": None}},
    {"quote_changes": {"last_price": Decimal("NaN")}},
    {"quote_changes": {"event_time_ns": NOW - 6_000_000_000}},
    {"quote_changes": {"instrument": "ETH_USDT_Perp"}},
    {"state_changes": {"positions": ()}},
    {"state_changes": {"positions": (("ETH_USDT_Perp", Decimal("-0.01")),)}},
    {"state_changes": {"positions": (("BTC_USDT_Perp", Decimal("0")),)}},
    {"state_changes": {"positions": (("BTC_USDT_Perp", Decimal("-0.1")),)}},
    {"state_changes": {"positions": (("BTC_USDT_Perp", Decimal("-0.0101")),)}},
    {"state_changes": {"open_order_count": 1}},
    {"state_changes": {"event_time_ns": NOW - 6_000_000_000}},
    {"state_changes": {"total_equity": Decimal("9999")}},
    {"state_changes": {"sub_account_mode": "PORTFOLIO_MARGIN_MODE"}},
])
def test_plan_rejects_stale_ambiguous_out_of_budget_or_already_triggered_exit(changes):
    with pytest.raises(GrvtTestnetSignError):
        plan(**changes)


def test_tick_rounding_never_exceeds_limit_budget_even_with_low_decimal_context():
    with localcontext() as ctx:
        ctx.prec = 6
        long = plan(state_changes={"positions": (("BTC_USDT_Perp", Decimal("0.01")),)},
            trigger=GrvtTestnetTrigger("STOP_LOSS", Decimal("49000.1")), max_slippage_bps=1)
        short = plan(trigger=GrvtTestnetTrigger("STOP_LOSS", Decimal("51000.1")), max_slippage_bps=1)
    assert long.limit_price == Decimal("48995.2")  # ceil tick, no 48995.1
    assert short.limit_price == Decimal("51005.2")  # floor tick, no 51005.3


@pytest.mark.parametrize("change", [
    {"reduce_only": False}, {"side": "OTHER"},
    {"limit_price": Decimal("48000")}, {"limit_price": Decimal("49000.1")},
    {"size": Decimal("0.0101")}, {"expiration_ns": NOW},
    {"expiration_ns": NOW + 86_400_000_000_001},
])
def test_signer_rejects_non_reductions_bad_limits_precision_and_expiry(change):
    with pytest.raises((GrvtTestnetSignError, GrvtTestnetOrderError)):
        sign_testnet_protection(replace(draft(), **change), LIMITS, INSTRUMENT,
            GrvtTestnetTrigger("STOP_LOSS", Decimal("49000")),
            dedicated_private_key=KEY, expected_signer=SIGNER, now_ns=NOW)


def fake_client(reply, *, calls=None):
    def post(url, body, headers):
        if calls is not None:
            calls.append(url)
        if url == AUTH_URL:
            return httpx.Response(200, json={"status": "success", "sub_account_id": "123456"},
                headers={"X-Grvt-Account-Id": BUILDER,
                         "Set-Cookie": "gravity=fake-testnet-cookie; Path=/"},
                request=httpx.Request("POST", url))
        return reply(url, body)
    return GrvtTestnetOrderClient("fake-testnet-key", http_post=post, clock_ns=lambda: NOW)


@pytest.mark.parametrize("field", ["trigger_price", "trigger_by", "close_position", "is_split_position"])
def test_native_ack_cannot_silently_change_unsigned_trigger(field):
    trigger = GrvtTestnetTrigger("STOP_LOSS", Decimal("49000"))
    order = sign_testnet_protection(draft(), LIMITS, INSTRUMENT, trigger,
        dedicated_private_key=KEY, expected_signer=SIGNER, now_ns=NOW)
    def reply(url, body):
        row = deepcopy(body["order"])
        row["order_id"] = "0xmodified"
        row["metadata"]["trigger"]["tpsl"][field] = {
            "trigger_price": "48950", "trigger_by": "MARK", "close_position": True,
            "is_split_position": True}[field]
        return httpx.Response(200, json={"result": row})
    calls = []
    client = fake_client(reply, calls=calls)
    with pytest.raises(GrvtTestnetOrderError, match="desconocido"):
        client.submit_signed_protection(order, LIMITS, expected_trigger=trigger)
    with pytest.raises(GrvtTestnetOrderError, match="consumida"):
        client.submit_signed_protection(order, LIMITS, expected_trigger=trigger)
    assert calls == [AUTH_URL, ORDER_URL]


def test_native_timeout_consumes_post_and_never_retries():
    trigger = GrvtTestnetTrigger("STOP_LOSS", Decimal("49000"))
    order = sign_testnet_protection(draft(), LIMITS, INSTRUMENT, trigger,
        dedicated_private_key=KEY, expected_signer=SIGNER, now_ns=NOW)
    def reply(url, body):
        raise httpx.ReadTimeout("untrusted remote text must not leak")
    calls = []
    client = fake_client(reply, calls=calls)
    with pytest.raises(GrvtTestnetOrderError, match="desconocido") as raised:
        client.submit_signed_protection(order, LIMITS, expected_trigger=trigger)
    assert "untrusted" not in str(raised.value)
    with pytest.raises(GrvtTestnetOrderError, match="consumida"):
        client.submit_signed_protection(order, LIMITS, expected_trigger=trigger)
    assert calls == [AUTH_URL, ORDER_URL]


def test_trigger_signature_is_not_misrepresented_as_a_trustless_trigger_commitment():
    first = GrvtTestnetTrigger("STOP_LOSS", Decimal("49000"))
    second = GrvtTestnetTrigger("STOP_LOSS", Decimal("49050"))
    order = sign_testnet_protection(draft(), LIMITS, INSTRUMENT, first,
        dedicated_private_key=KEY, expected_signer=SIGNER, now_ns=NOW)
    changed = deepcopy(order)
    changed["metadata"]["trigger"] = second.metadata()
    # Una firma GRVT por sí sola no liga la orden al disparador original.
    assert verify_testnet_protection(changed, LIMITS, INSTRUMENT,
        expected_trigger=second, now_ns=NOW) == SIGNER
    with pytest.raises(GrvtTestnetSignError):
        verify_testnet_protection(changed, LIMITS, INSTRUMENT,
            expected_trigger=first, now_ns=NOW)


def test_native_ack_rejects_numeric_boolean_instead_of_exact_trigger_contract():
    trigger = GrvtTestnetTrigger("STOP_LOSS", Decimal("49000"))
    order = sign_testnet_protection(draft(), LIMITS, INSTRUMENT, trigger,
        dedicated_private_key=KEY, expected_signer=SIGNER, now_ns=NOW)
    def reply(url, body):
        row = deepcopy(body["order"])
        row["order_id"] = "0xmalformed"
        row["metadata"]["trigger"]["tpsl"]["close_position"] = 0
        return httpx.Response(200, json={"result": row})
    with pytest.raises(GrvtTestnetOrderError, match="desconocido"):
        fake_client(reply).submit_signed_protection(order, LIMITS, expected_trigger=trigger)


def test_planner_uses_actual_last_price_from_testnet_not_bid_or_mark():
    from app.live_execution.grvt_testnet_quote import fetch_testnet_quote, TESTNET_TICKER_URL
    def post(url, body):
        assert url == TESTNET_TICKER_URL and body == {"instrument": "BTC_USDT_Perp"}
        return httpx.Response(200, json={"result": {
            "instrument": "BTC_USDT_Perp", "event_time": str(NOW),
            "best_bid_price": "49999", "best_ask_price": "50001",
            "best_bid_size": "10", "best_ask_size": "10", "last_price": "51001",
            "mark_price": "50000"}})
    quote = fetch_testnet_quote("BTC_USDT_Perp", http_post=post, clock_ns=lambda: NOW)
    assert quote.last_price == Decimal("51001")
    # SL short a 51000 ya cumplido por LAST aunque mark/bid/ask sean inferiores.
    with pytest.raises(GrvtTestnetSignError):
        plan(quote_changes={"last_price": quote.last_price})


@pytest.mark.parametrize("change", [
    lambda row: row.update(reduce_only=False),
    lambda row: row.update(time_in_force="IMMEDIATE_OR_CANCEL"),
    lambda row: row.update(sub_account_id="999"),
    lambda row: row["signature"].update(chain_id="325"),
    lambda row: row["metadata"].update(client_order_id=str(2**63 + 43)),
    lambda row: row["metadata"]["trigger"]["tpsl"].update(close_position=0),
    lambda row: row["state"].update(status="FILLED", traded_size=["0.001"]),
    lambda row: row["state"].update(status="REJECTED", traded_size=["0.001"]),
    lambda row: row["state"].update(status="OPEN", update_time=str(NOW + 3_000_000_000)),
])
def test_native_lookup_does_not_accept_other_contract_or_inconsistent_fill_state(change):
    trigger = GrvtTestnetTrigger("STOP_LOSS", Decimal("49000"))
    order = sign_testnet_protection(draft(), LIMITS, INSTRUMENT, trigger,
        dedicated_private_key=KEY, expected_signer=SIGNER, now_ns=NOW)
    row = {**deepcopy(order), "order_id": "0xnative",
        "metadata": {**deepcopy(order["metadata"]), "create_time": str(NOW)},
        "state": {"status": "OPEN", "traded_size": ["0"], "update_time": str(NOW)}}
    change(row)
    calls = []
    client = fake_client(lambda url, body: httpx.Response(200, json={"result": row}), calls=calls)
    with pytest.raises(GrvtTestnetOrderError, match="no verificable"):
        client.lookup_protection(order, LIMITS, expected_trigger=trigger, created_at_ns=NOW)
    assert calls == [AUTH_URL, LOOKUP_URL]


def test_ioc_entry_cannot_be_acknowledged_as_a_different_native_trigger_order():
    from app.live_execution.grvt_testnet_signer import sign_testnet_limit
    opening = replace(draft(), side="BUY", reduce_only=False, expiration_ns=NOW + 60_000_000_000)
    order = sign_testnet_limit(opening, LIMITS, INSTRUMENT,
        dedicated_private_key=KEY, expected_signer=SIGNER, now_ns=NOW)
    def reply(url, body):
        row = deepcopy(body["order"])
        row["order_id"] = "0xunexpected"
        row["metadata"]["trigger"] = GrvtTestnetTrigger("STOP_LOSS", Decimal("49000")).metadata()
        return httpx.Response(200, json={"result": row})
    with pytest.raises(GrvtTestnetOrderError, match="desconocido"):
        fake_client(reply).submit_signed_limit(order, LIMITS)


@pytest.mark.parametrize("protective", [False, True])
def test_no_builder_uses_sdk_order_type_not_order_with_builder_fee(protective):
    from app.live_execution.grvt_testnet_signer import sign_testnet_limit, verify_testnet_limit_signature
    zero = "0x" + "0" * 40
    candidate = replace(draft(), builder=zero, expiration_ns=NOW + 60_000_000_000)
    trigger = GrvtTestnetTrigger("STOP_LOSS", Decimal("49000"))
    order = (sign_testnet_protection(candidate, LIMITS, INSTRUMENT, trigger,
                dedicated_private_key=KEY, expected_signer=SIGNER, now_ns=NOW)
             if protective else sign_testnet_limit(candidate, LIMITS, INSTRUMENT,
                dedicated_private_key=KEY, expected_signer=SIGNER, now_ns=NOW))
    # Tipos independientes del firmante ORBIS, contrastados con grvt_raw_signing.py.
    official_types = {
        "Order": [
            {"name": "subAccountID", "type": "uint64"},
            {"name": "isMarket", "type": "bool"},
            {"name": "timeInForce", "type": "uint8"},
            {"name": "postOnly", "type": "bool"},
            {"name": "reduceOnly", "type": "bool"},
            {"name": "legs", "type": "OrderLeg[]"},
            {"name": "nonce", "type": "uint32"},
            {"name": "expiration", "type": "int64"},
        ],
        "OrderLeg": [
            {"name": "assetID", "type": "uint256"},
            {"name": "contractSize", "type": "uint64"},
            {"name": "limitPrice", "type": "uint64"},
            {"name": "isBuyingContract", "type": "bool"},
        ],
    }
    signable = encode_typed_data(
        {"name": "GRVT Exchange", "version": "0", "chainId": 326}, official_types,
        {"subAccountID": 123456, "isMarket": False, "timeInForce": 1 if protective else 3,
         "postOnly": False, "reduceOnly": True,
         "legs": [{"assetID": 123456789, "contractSize": 10,
                   "limitPrice": 48_900_000_000_000, "isBuyingContract": False}],
         "nonce": 42, "expiration": candidate.expiration_ns})
    signature = order["signature"]
    assert Account.recover_message(signable, vrs=(
        signature["v"], int(signature["r"], 16), int(signature["s"], 16))) == SIGNER
    if protective:
        assert verify_testnet_protection(order, LIMITS, INSTRUMENT,
            expected_trigger=trigger, now_ns=NOW) == SIGNER
    else:
        assert verify_testnet_limit_signature(order, LIMITS, INSTRUMENT, now_ns=NOW) == SIGNER
