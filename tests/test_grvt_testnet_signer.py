from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

from eth_account import Account
from eth_account.messages import encode_typed_data
import httpx
import pytest

from app.live_execution.grvt_testnet_order import (
    AUTH_URL, ORDER_URL, GrvtTestnetLimits, GrvtTestnetOrderAck,
    GrvtTestnetOrderClient, GrvtTestnetOrderError,
)
from app.live_execution.grvt_testnet_journal import GrvtTestnetJournalError
from app.live_execution.grvt_testnet_submit import submit_testnet_once
from app.live_execution.grvt_testnet_risk import MARGIN_RULES_URL
from app.live_execution.grvt_testnet_risk import (
    GrvtTestnetRiskError, GrvtTestnetRiskState,
)
from app.derivatives.paper_math import MarginRules
from app.live_execution.grvt_testnet_signer import (
    INSTRUMENT_URL, _ORDER_TYPES, GrvtTestnetInstrument,
    GrvtTestnetLimitDraft, GrvtTestnetSignError,
    fetch_testnet_instrument, sign_testnet_limit,
    verify_testnet_limit_signature,
)
from app.live_execution.grvt_testnet_quote import (
    GrvtTestnetPriceGuard, GrvtTestnetQuote, GrvtTestnetQuoteError,
)
from app.live_execution.grvt_testnet_preflight import client_id_for_signal, read_testnet_preflight
from app.live_execution.grvt_testnet_recovery import read_testnet_recovery
from app.portfolio.vault import RealVault


NOW = 1_780_000_000_000_000_000
KEY = bytes.fromhex("11" * 32)  # Clave ficticia pública de prueba, nunca de usuario.
SIGNER = Account.from_key(KEY).address
BUILDER = "0x" + "a" * 40


def metadata():
    return GrvtTestnetInstrument(
        "BTC_USDT_Perp", 123456789, 3,
        Decimal("0.1"), Decimal("0.001"), Decimal("10"),
    )


def limits():
    return GrvtTestnetLimits("123456", "BTC_USDT_Perp", Decimal("1000"))


def draft():
    return GrvtTestnetLimitDraft(
        "123456", "BTC_USDT_Perp", "BUY", Decimal("0.01"),
        Decimal("50000"), False, BUILDER, str(2**63 + 7), 42,
        NOW + 60_000_000_000,
    )


def sign(candidate=None, instrument=None, policy=None, *, key=KEY, expected=SIGNER):
    return sign_testnet_limit(candidate or draft(), policy or limits(),
                              instrument or metadata(),
                              dedicated_private_key=key,
                              expected_signer=expected, now_ns=NOW)


def test_signer_builds_exact_eip712_testnet_signature():
    result = sign()
    assert result["signature"]["chain_id"] == "326"
    assert result["metadata"]["client_order_id"] == str(2**63 + 7)
    assert result["legs"][0]["size"] == "0.01"
    assert KEY.hex() not in str(result)
    message = {
        "subAccountID": 123456, "isMarket": False,
        "timeInForce": 3, "postOnly": False, "reduceOnly": False,
        "legs": [{"assetID": 123456789, "contractSize": 10,
                  "limitPrice": 50_000_000_000_000,
                  "isBuyingContract": True}],
        "builder": BUILDER, "builderFee": 0,
        "nonce": 42, "expiration": NOW + 60_000_000_000,
    }
    signature = result["signature"]
    raw = (int(signature["r"], 16).to_bytes(32, "big")
           + int(signature["s"], 16).to_bytes(32, "big")
           + bytes([signature["v"]]))
    testnet = encode_typed_data(
        {"name": "GRVT Exchange", "version": "0", "chainId": 326},
        _ORDER_TYPES, message,
    )
    mainnet = encode_typed_data(
        {"name": "GRVT Exchange", "version": "0", "chainId": 325},
        _ORDER_TYPES, message,
    )
    assert Account.recover_message(testnet, signature=raw) == SIGNER
    assert Account.recover_message(mainnet, signature=raw) != SIGNER


def test_local_verifier_accepts_exact_testnet_order_and_rejects_tampering():
    order = sign()
    assert verify_testnet_limit_signature(order, limits(), metadata(), now_ns=NOW) == SIGNER
    variants = []
    for field, value in (("size", "0.011"), ("limit_price", "50000.1"),
                         ("is_buying_asset", False)):
        changed = {**order, "legs": [{**order["legs"][0], field: value}]}
        variants.append(changed)
    variants.append({**order, "reduce_only": True})
    variants.append({**order, "builder": "0x" + "b" * 40})
    variants.append({**order, "signature": {**order["signature"], "nonce": 43}})
    variants.append({**order, "signature": {**order["signature"], "signer": "0x" + "b" * 40}})
    for changed in variants:
        with pytest.raises(GrvtTestnetSignError, match="no verificable"):
            verify_testnet_limit_signature(changed, limits(), metadata(), now_ns=NOW)
    with pytest.raises(GrvtTestnetSignError, match="no verificable"):
        verify_testnet_limit_signature(order, limits(),
                                       replace(metadata(), asset_id=123456790), now_ns=NOW)
    with pytest.raises(GrvtTestnetSignError, match="no verificable"):
        verify_testnet_limit_signature(order, limits(), metadata(), now_ns=NOW + 61_000_000_000)


def test_failed_price_guard_does_not_prepare_or_claim_order(tmp_path):
    vault = RealVault(tmp_path / "real.db")
    vault.unlock("a robust test passphrase",create=True)
    calls = []

    def post(url,body,headers):
        calls.append(url)
        if url == AUTH_URL:
            return httpx.Response(200,json={"status":"success","sub_account_id":"123456"},
                headers={"X-Grvt-Account-Id":BUILDER,"Set-Cookie":"gravity=fake-test-cookie; Path=/"},
                request=httpx.Request("POST",url))
        if url.endswith("/open_orders"): return httpx.Response(200,json={"result":[]})
        assert url.endswith("/account_summary")
        return httpx.Response(200,json={"result":{"sub_account_id":"123456","event_time":str(NOW),
            "settle_currency":"USDT","margin_type":"SIMPLE_CROSS_MARGIN","sub_account_mode":"SINGLE_ASSET_MODE",
            "total_equity":"10000","initial_margin":"0","maintenance_margin":"0","unrealized_pnl":"0",
            "positions":[],"spot_balances":[{"currency":"USDT","balance":"10000"}]}})

    def public_post(url,body):
        assert url == MARGIN_RULES_URL
        return httpx.Response(200,json={"result":{"instrument":"BTC_USDT_Perp","max_position_size":"10",
            "risk_brackets":[{"tier":1,"notional_floor":"0","notional_cap":None,"initial_margin_rate":"0.02",
                "maintenance_margin_rate":"0.01","max_leverage":50,"cumulative_maintenance_amount":"0"}]}})

    def reject_quote():
        raise GrvtTestnetQuoteError("Cotización movida")

    with pytest.raises(GrvtTestnetQuoteError, match="movida"):
        submit_testnet_once(vault,GrvtTestnetOrderClient("fake-testnet-api",http_post=post),
            sign(),limits(),clock_ns=lambda:NOW,public_post=public_post,pre_claim_guard=reject_quote)
    assert ORDER_URL not in calls
    assert read_testnet_recovery(vault,"123456").items == ()


@pytest.mark.parametrize("candidate", [
    replace(draft(), size=Decimal("0.0009")),
    replace(draft(), size=Decimal("0.0101")),
    replace(draft(), limit_price=Decimal("50000.01")),
    replace(draft(), limit_price=Decimal("50000.0000000001")),
    replace(draft(), size=Decimal("0.001"), limit_price=Decimal("100")),
    replace(draft(), expiration_ns=NOW + 301_000_000_000),
    replace(draft(), client_order_id="100"),
    replace(draft(), sub_account_id="999"),
])
def test_invalid_size_price_time_identity_are_rejected(candidate):
    with pytest.raises(GrvtTestnetSignError):
        sign(candidate)


def test_wrong_dedicated_key_cannot_sign_and_error_is_sanitized():
    wrong_key = bytes.fromhex("22" * 32)
    with pytest.raises(GrvtTestnetSignError, match="No se pudo firmar") as raised:
        sign(key=wrong_key)
    assert wrong_key.hex() not in str(raised.value)
    with pytest.raises(GrvtTestnetSignError):
        sign(key=b"short")


def test_instrument_metadata_only_from_testnet_endpoint():
    calls = []

    def post(url, body):
        calls.append((url, body))
        return httpx.Response(200, json={"result": {
            "instrument": "BTC_USDT_Perp", "kind": "PERPETUAL",
            "venues": ["ORDERBOOK"], "instrument_hash": "0x75bcd15",
            "base_decimals": 3, "tick_size": "0.1",
            "min_size": "0.001", "min_notional": "10",
        }}, request=httpx.Request("POST", url))

    result = fetch_testnet_instrument("BTC_USDT_Perp", http_post=post)
    assert result == metadata()
    assert calls == [(INSTRUMENT_URL, {"instrument": "BTC_USDT_Perp"})]
    assert "testnet.grvt.io" in calls[0][0]


@pytest.mark.parametrize("update", [
    {"instrument": "ETH_USDT_Perp"},
    {"kind": "FUTURE"},
    {"venues": ["RFQ"]},
    {"instrument_hash": "0"},
    {"base_decimals": 99},
    {"tick_size": "NaN"},
])
def test_bad_instrument_metadata_rejected(update):
    row = {"instrument": "BTC_USDT_Perp", "kind": "PERPETUAL",
           "venues": ["ORDERBOOK"], "instrument_hash": "0x75bcd15",
           "base_decimals": 3, "tick_size": "0.1",
           "min_size": "0.001", "min_notional": "10"}
    row.update(update)
    def post(url, body):
        return httpx.Response(200, json={"result": row},
                              request=httpx.Request("POST", url))
    with pytest.raises(GrvtTestnetSignError, match="no verificables"):
        fetch_testnet_instrument("BTC_USDT_Perp", http_post=post)


@pytest.mark.parametrize("with_source", [False, True])
def test_signed_testnet_order_passes_durable_sender_with_fake_http(tmp_path, with_source):
    source = None
    if with_source:
        session_id, action_id = str(uuid4()), str(uuid4())
        client_id = client_id_for_signal(session_id, action_id)
        source = {"kind": "GRVT_AUTO_PAPER", "session_id": session_id,
                  "action_request_id": action_id, "action": "OPEN",
                  "direction": "LONG", "version_id": str(uuid4()),
                  "dataset_id": str(uuid4()), "dataset_hash": "a" * 64,
                  "client_order_id": client_id}
        signed_order = sign(replace(draft(), client_order_id=client_id))
    else:
        signed_order = sign()
    calls = []
    guarded_at = []

    def post(url, body, headers):
        calls.append(url)
        if url == AUTH_URL:
            return httpx.Response(
                200, json={"status": "success", "sub_account_id": "123456"},
                headers={"Set-Cookie": "gravity=testnet-cookie-123; Path=/",
                         "X-Grvt-Account-Id": BUILDER},
                request=httpx.Request("POST", url),
            )
        if url.endswith("/account_summary"):
            return httpx.Response(200, json={"result": {
                "sub_account_id": "123456", "event_time": str(NOW),
                "settle_currency": "USDT", "margin_type": "SIMPLE_CROSS_MARGIN",
                "sub_account_mode": "SINGLE_ASSET_MODE", "total_equity": "10000",
                "initial_margin": "0", "maintenance_margin": "0",
                "unrealized_pnl": "0", "positions": [],
                "spot_balances": [{"currency": "USDT", "balance": "10000"}]}},
                request=httpx.Request("POST", url))
        if url.endswith("/open_orders"):
            return httpx.Response(200, json={"result": []},
                                  request=httpx.Request("POST", url))
        assert url == ORDER_URL and body == {"order": signed_order}
        return httpx.Response(200, json={"result": {**signed_order,
                                                      "order_id": "0x123abc"}},
                              request=httpx.Request("POST", url))

    def public_post(url, body):
        assert url == MARGIN_RULES_URL
        return httpx.Response(200, json={"result": {
            "instrument": "BTC_USDT_Perp", "max_position_size": "10",
            "risk_brackets": [{"tier": 1, "notional_floor": "0",
                               "notional_cap": None,
                               "initial_margin_rate": "0.02",
                               "maintenance_margin_rate": "0.01",
                               "max_leverage": 50,
                               "cumulative_maintenance_amount": "0"}]}},
            request=httpx.Request("POST", url))

    client = GrvtTestnetOrderClient("testnet-key-123", http_post=post,
                                    clock_ns=lambda: NOW)
    path = tmp_path / "real.db"
    vault = RealVault(path)
    vault.unlock("long test passphrase", create=True)
    def check_price():
        guarded_at.append(len(calls))
        return GrvtTestnetPriceGuard(
            GrvtTestnetQuote("BTC_USDT_Perp", NOW, NOW,
                             Decimal("49999"), Decimal("50000"),
                             Decimal("1"), Decimal("1")),
            100, 100,
        )
    record = submit_testnet_once(vault, client, signed_order, limits(),
                                  clock_ns=lambda: NOW, public_post=public_post,
                                  signal_source=source,
                                  pre_claim_guard=check_price)
    assert record.status == "ACKNOWLEDGED" and record.external_order_id == "0x123abc"
    assert calls.count("https://trades.testnet.grvt.io/full/v1/account_summary") == 2
    assert len(guarded_at) == 1
    assert guarded_at[0] == calls.index(ORDER_URL)
    assert calls.count("https://trades.testnet.grvt.io/full/v1/open_orders") == 2
    vault.lock()
    vault = RealVault(path)
    vault.unlock("long test passphrase")
    preflight = read_testnet_preflight(vault, "123456",
                                       signed_order["metadata"]["client_order_id"])
    assert preflight.get("signal_source") == source
    assert preflight["price_guard"]["source"] == "GRVT_TESTNET_PUBLIC_TICKER"
    assert preflight["price_guard"]["bid"] == "49999"
    assert preflight["price_guard"]["max_slippage_bps"] == 100
    assert read_testnet_recovery(vault, "123456").items[0].signal_source == source
    new_client = GrvtTestnetOrderClient("testnet-key-123", http_post=post,
                                        clock_ns=lambda: NOW)
    with pytest.raises(GrvtTestnetJournalError, match="no preparada"):
        submit_testnet_once(vault, new_client, signed_order, limits(),
                             clock_ns=lambda: NOW, public_post=public_post)
    assert calls.count(ORDER_URL) == 1
    assert all("testnet.grvt.io" in url for url in calls)
    vault.lock()


def test_durable_sender_never_claims_or_posts_when_risk_is_insufficient(tmp_path,
                                                                          monkeypatch):
    signed_order = sign()
    calls = []
    def post(url, body, headers):
        calls.append(url)
        assert url == AUTH_URL
        return httpx.Response(
            200, json={"status": "success", "sub_account_id": "123456"},
            headers={"Set-Cookie": "gravity=testnet-cookie-123; Path=/",
                     "X-Grvt-Account-Id": BUILDER},
            request=httpx.Request("POST", url),
        )
    rules = MarginRules.from_payload({
        "instrument": "BTC_USDT_Perp", "max_position_size": "10",
        "fetched_at_ns": NOW,
        "risk_brackets": [{"tier": 1, "notional_floor": "0",
                           "notional_cap": None,
                           "initial_margin_rate": "0.02",
                           "maintenance_margin_rate": "0.01",
                           "max_leverage": 50,
                           "cumulative_maintenance_amount": "0"}],
    }, "BTC_USDT_Perp", NOW)
    risky = GrvtTestnetRiskState(
        "123456", "USDT", NOW, NOW, "SIMPLE_CROSS_MARGIN",
        "SINGLE_ASSET_MODE", Decimal("100"), Decimal("100"), Decimal(0),
        Decimal(0), Decimal(0), (), 0,
    )
    monkeypatch.setattr("app.live_execution.grvt_testnet_submit.fetch_testnet_risk",
                        lambda *args, **kwargs: (risky, rules))
    client = GrvtTestnetOrderClient("testnet-key-123", http_post=post,
                                    clock_ns=lambda: NOW)
    vault = RealVault(tmp_path / "real.db")
    vault.unlock("long test passphrase", create=True)
    with pytest.raises(GrvtTestnetRiskError, match="EQUITY_BUFFER"):
        submit_testnet_once(vault, client, signed_order, limits(),
                             clock_ns=lambda: NOW)
    assert calls == [AUTH_URL]
    with vault.session() as db:
        assert db.connection().exec_driver_sql(
            "SELECT count(*) FROM grvt_testnet_order_intents").scalar_one() == 0
    vault.lock()


@pytest.mark.parametrize("change", ["equity", "position", "rules", "regressed_event"])
def test_durable_sender_rejects_changed_second_risk_sweep_before_journal_or_post(
    tmp_path, monkeypatch, change,
):
    calls = []

    def post(url, body, headers):
        calls.append(url)
        assert url == AUTH_URL
        return httpx.Response(
            200, json={"status": "success", "sub_account_id": "123456"},
            headers={"Set-Cookie": "gravity=testnet-cookie-123; Path=/",
                     "X-Grvt-Account-Id": BUILDER},
            request=httpx.Request("POST", url),
        )

    rules = MarginRules.from_payload({
        "instrument": "BTC_USDT_Perp", "max_position_size": "10",
        "fetched_at_ns": NOW,
        "risk_brackets": [{"tier": 1, "notional_floor": "0",
                           "notional_cap": None,
                           "initial_margin_rate": "0.02",
                           "maintenance_margin_rate": "0.01",
                           "max_leverage": 50,
                           "cumulative_maintenance_amount": "0"}],
    }, "BTC_USDT_Perp", NOW)
    state = GrvtTestnetRiskState(
        "123456", "USDT", NOW, NOW, "SIMPLE_CROSS_MARGIN",
        "SINGLE_ASSET_MODE", Decimal("10000"), Decimal("10000"),
        Decimal(0), Decimal(0), Decimal(0), (), 0,
    )
    second_state, second_rules = state, rules
    if change == "equity":
        second_state = replace(state, total_equity=Decimal("9000"))
    elif change == "position":
        second_state = replace(state, positions=(("ETH_USDT_Perp", Decimal("0.01")),))
    elif change == "rules":
        second_rules = replace(rules, max_size=Decimal("9"))
    else:
        second_state = replace(state, event_time_ns=NOW - 1)
    snapshots = iter(((state, rules), (second_state, second_rules)))
    monkeypatch.setattr("app.live_execution.grvt_testnet_submit.fetch_testnet_risk",
                        lambda *args, **kwargs: next(snapshots))
    client = GrvtTestnetOrderClient("testnet-key-123", http_post=post,
                                    clock_ns=lambda: NOW)
    vault = RealVault(tmp_path / "real.db")
    vault.unlock("long test passphrase", create=True)
    with pytest.raises(GrvtTestnetRiskError, match="preflight|cambiaron"):
        submit_testnet_once(vault, client, sign(), limits(), clock_ns=lambda: NOW)
    assert calls == [AUTH_URL]
    with vault.session() as db:
        assert db.connection().exec_driver_sql(
            "SELECT count(*) FROM grvt_testnet_order_intents").scalar_one() == 0
    vault.lock()


@pytest.mark.parametrize("clock_change", ["rollback_after_prepare", "rollback_after_claim",
                                           "expiry_after_claim", "stop_after_claim"])
def test_grvt_testnet_sender_blocks_clock_expiry_or_stop_without_order_post(
    tmp_path, monkeypatch, clock_change,
):
    from app.live_execution import grvt_testnet_submit as sender
    from app.portfolio.models import GrvtTestnetOrderIntent

    stable = GrvtTestnetRiskState(
        "123456", "USDT", NOW, NOW, "SIMPLE_CROSS_MARGIN",
        "SINGLE_ASSET_MODE", Decimal("10000"), Decimal("10000"),
        Decimal(0), Decimal(0), Decimal(0), (), 0,
    )
    rules = MarginRules.from_payload({
        "instrument": "BTC_USDT_Perp", "max_position_size": "10",
        "fetched_at_ns": NOW,
        "risk_brackets": [{"tier": 1, "notional_floor": "0", "notional_cap": None,
                           "initial_margin_rate": "0.02", "maintenance_margin_rate": "0.01",
                           "max_leverage": 50, "cumulative_maintenance_amount": "0"}],
    }, "BTC_USDT_Perp", NOW)
    monkeypatch.setattr(sender, "fetch_testnet_risk", lambda *args, **kwargs: (stable, rules))
    clock = {"value": NOW}
    function_name = ("prepare_testnet_order" if clock_change == "rollback_after_prepare"
                     else "claim_testnet_post")
    original = getattr(sender, function_name)

    def rollback_after(*args, **kwargs):
        result = original(*args, **kwargs)
        if clock_change == "stop_after_claim":
            from app.live_execution.grvt_testnet_journal import stop_testnet_subaccount
            stop_testnet_subaccount(vault, "123456", now=sender._datetime_ns(NOW))
        else:
            clock["value"] = (NOW + 61_000_000_000 if clock_change == "expiry_after_claim"
                              else NOW - 1)
        return result

    monkeypatch.setattr(sender, function_name, rollback_after)

    class Client:
        calls = 0

        def submit_signed_limit(self, *_):
            self.calls += 1
            return GrvtTestnetOrderAck("123456", sign()["metadata"]["client_order_id"], "0x123abc", True)

    client = Client()
    vault = RealVault(tmp_path / "real.db")
    vault.unlock("clock rollback test password", create=True)
    candidate = sign()
    expected = (GrvtTestnetOrderError if clock_change == "expiry_after_claim"
                else GrvtTestnetJournalError if clock_change == "stop_after_claim"
                else GrvtTestnetRiskError)
    with pytest.raises(expected):
        submit_testnet_once(vault, client, candidate, limits(),
                            clock_ns=lambda: clock["value"])
    assert client.calls == 0
    with vault.session() as db:
        row = db.get(GrvtTestnetOrderIntent, candidate["metadata"]["client_order_id"])
        assert row is not None and row.status == (
            "PREPARED" if clock_change == "rollback_after_prepare" else "POST_CLAIMED")
    vault.lock()


def test_grvt_testnet_sender_retains_ack_when_clock_rolls_back_after_post(
    tmp_path, monkeypatch,
):
    import threading
    from app.live_execution import grvt_testnet_submit as sender
    from app.live_execution.grvt_testnet_journal import stop_testnet_subaccount
    from app.portfolio.models import GrvtTestnetOrderIntent

    stable = GrvtTestnetRiskState(
        "123456", "USDT", NOW, NOW, "SIMPLE_CROSS_MARGIN",
        "SINGLE_ASSET_MODE", Decimal("10000"), Decimal("10000"),
        Decimal(0), Decimal(0), Decimal(0), (), 0,
    )
    rules = MarginRules.from_payload({
        "instrument": "BTC_USDT_Perp", "max_position_size": "10",
        "fetched_at_ns": NOW,
        "risk_brackets": [{"tier": 1, "notional_floor": "0", "notional_cap": None,
                           "initial_margin_rate": "0.02", "maintenance_margin_rate": "0.01",
                           "max_leverage": 50, "cumulative_maintenance_amount": "0"}],
    }, "BTC_USDT_Perp", NOW)
    monkeypatch.setattr(sender, "fetch_testnet_risk", lambda *args, **kwargs: (stable, rules))
    clock = {"value": NOW}
    candidate = sign()
    stop_done = threading.Event()
    stop_errors = []

    def stop_during_post():
        try:
            stop_testnet_subaccount(vault, "123456", now=sender._datetime_ns(NOW))
        except Exception as exc:
            stop_errors.append(exc)
        finally:
            stop_done.set()

    class Client:
        calls = 0

        def submit_signed_limit(self, *_):
            self.calls += 1
            stopper = threading.Thread(target=stop_during_post, daemon=True)
            stopper.start()
            assert not stop_done.wait(0.05)
            clock["value"] = NOW - 1
            return GrvtTestnetOrderAck("123456", candidate["metadata"]["client_order_id"],
                                       "0x123abc", True)

    client = Client()
    vault = RealVault(tmp_path / "real.db")
    vault.unlock("clock ack test password", create=True)
    result = submit_testnet_once(vault, client, candidate, limits(),
                                 clock_ns=lambda: clock["value"])
    assert client.calls == 1 and result.status == "ACKNOWLEDGED"
    assert stop_done.wait(5) and not stop_errors
    with vault.session() as db:
        row = db.get(GrvtTestnetOrderIntent, candidate["metadata"]["client_order_id"])
        assert row.status == "ACKNOWLEDGED"
        assert row.updated_at.replace(tzinfo=None) == sender._datetime_ns(NOW).replace(tzinfo=None)
    vault.lock()
