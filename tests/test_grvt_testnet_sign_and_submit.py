"""Firma efímera de señales PAPER para GRVT testnet, sin tocar mainnet."""

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
import time

from eth_account import Account
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.live_execution.grvt_testnet_order import GrvtTestnetLimits
from app.live_execution.grvt_testnet_journal import GrvtTestnetJournalError, stop_testnet_subaccount
from app.live_execution.grvt_testnet_preflight import client_id_for_signal
from app.live_execution.grvt_testnet_quote import GrvtTestnetQuote, GrvtTestnetQuoteError
from app.live_execution.grvt_testnet_signal import (
    GrvtTestnetSignalError, sign_and_submit_signal_testnet_once,
)
from app.live_execution.grvt_testnet_signer import GrvtTestnetInstrument
from app.portfolio.api import create_real_router
from app.portfolio.vault import RealVault


NOW = 1_780_000_000_000_000_000
KEY = bytes.fromhex("33" * 32)  # Material ficticio de prueba, nunca clave de usuario.
SIGNER = Account.from_key(KEY).address
BUILDER = "0x" + "a" * 40


def instrument():
    return GrvtTestnetInstrument("BTC_USDT_Perp", 123456789, 3,
                                 Decimal("0.1"), Decimal("0.001"), Decimal("10"))


def quote():
    return GrvtTestnetQuote("BTC_USDT_Perp", NOW, NOW,
                             Decimal("49999"), Decimal("50000"),
                             Decimal("1"), Decimal("1"))


def test_sign_and_submit_builds_verified_order_without_persisting_key():
    limits = GrvtTestnetLimits("123456", "BTC_USDT_Perp", Decimal("100"))
    logged_in = []
    submitted = []
    quoted = []

    def quote_fetcher(name):
        quoted.append(name)
        return quote()

    def client_factory(key):
        logged_in.append(key)
        return object()

    def submit(paper, lab, vault, client, order, policy, *, session_id,
               action_request_id, clock_ns, signature_verifier, pre_claim_guard):
        assert policy is limits and clock_ns() == NOW
        assert order["metadata"]["client_order_id"] == client_id_for_signal(
            session_id, action_request_id)
        assert order["signature"]["signer"] == SIGNER
        assert order["signature"]["chain_id"] == "326"
        assert order["legs"][0]["size"] == "0.001"
        assert order["legs"][0]["limit_price"] == "50000"
        assert signature_verifier(order, policy, now_ns=NOW) == SIGNER
        pre_claim_guard()
        assert KEY.hex() not in str(order)
        submitted.append(order)
        return SimpleNamespace(client_order_id=order["metadata"]["client_order_id"]), "CLAIMED"

    binding, result = sign_and_submit_signal_testnet_once(
        object(), object(), object(), client_factory, "dedicated-testnet-key",
        limits, session_id="paper-session", action_request_id="paper-action",
        dedicated_key_hex="0x" + KEY.hex(), expected_signer=SIGNER,
        builder=BUILDER, side="BUY", size=Decimal("0.001"),
        limit_price=Decimal("50000"), reduce_only=False,
        clock_ns=lambda: NOW, instrument_fetcher=lambda name: instrument(),
        quote_fetcher=quote_fetcher,
        signal_submitter=submit,
        submission_guard=lambda *_: None,
    )
    assert binding.client_order_id == client_id_for_signal("paper-session", "paper-action")
    assert result == "CLAIMED" and len(submitted) == 1
    assert logged_in == ["dedicated-testnet-key"]
    assert quoted == ["BTC_USDT_Perp", "BTC_USDT_Perp"]


def test_bad_signing_key_never_authenticates_or_submits():
    calls = []
    limits = GrvtTestnetLimits("123456", "BTC_USDT_Perp", Decimal("100"))
    common = dict(
        session_id="paper-session", action_request_id="paper-action",
        expected_signer=SIGNER, builder=BUILDER, side="BUY",
        size=Decimal("0.001"), limit_price=Decimal("50000"),
        reduce_only=False, clock_ns=lambda: NOW,
        instrument_fetcher=lambda name: instrument(),
        quote_fetcher=lambda name: quote(),
        signal_submitter=lambda *args, **kwargs: calls.append("submit"),
        submission_guard=lambda *_: None,
    )
    factory = lambda key: calls.append("login")
    with pytest.raises(GrvtTestnetSignalError):
        sign_and_submit_signal_testnet_once(
            object(), object(), object(), factory, "testnet-key", limits,
            dedicated_key_hex="mainnet-view-key", **common,
        )
    from app.live_execution.grvt_testnet_signer import GrvtTestnetSignError
    with pytest.raises(GrvtTestnetSignError):
        sign_and_submit_signal_testnet_once(
            object(), object(), object(), factory, "testnet-key", limits,
            dedicated_key_hex="0x" + KEY.hex(),
            **{**common, "expected_signer": "0x" + "b" * 40},
        )
    assert calls == []


def test_moved_testnet_quote_blocks_before_durable_post_claim():
    limits = GrvtTestnetLimits("123456", "BTC_USDT_Perp", Decimal("100"))
    quotes = [quote(), GrvtTestnetQuote(
        "BTC_USDT_Perp", NOW, NOW, Decimal("48999"), Decimal("49000"),
        Decimal("1"), Decimal("1"),
    )]
    claims = []

    def submit(*args, pre_claim_guard, **kwargs):
        pre_claim_guard()
        claims.append("claimed")

    with pytest.raises(GrvtTestnetQuoteError, match="compra"):
        sign_and_submit_signal_testnet_once(
            object(), object(), object(), lambda key: object(),
            "dedicated-testnet-key", limits,
            session_id="paper-session", action_request_id="paper-action",
            dedicated_key_hex="0x" + KEY.hex(), expected_signer=SIGNER,
            builder=BUILDER, side="BUY", size=Decimal("0.001"),
            limit_price=Decimal("50000"), reduce_only=False,
            clock_ns=lambda: NOW, instrument_fetcher=lambda name: instrument(),
            quote_fetcher=lambda name: quotes.pop(0), signal_submitter=submit,
            submission_guard=lambda *_: None,
        )
    assert claims == [] and quotes == []


def test_default_guard_blocks_stopped_testnet_before_public_or_private_network(tmp_path):
    vault = RealVault(tmp_path / "vault.db")
    vault.unlock("a robust local password", create=True)
    stop_testnet_subaccount(vault, "123456", now=datetime.now(timezone.utc))
    external = []

    def forbidden(*args, **kwargs):
        external.append("called")
        raise AssertionError("La parada local debe impedir la red")

    with pytest.raises(GrvtTestnetJournalError, match="detenida"):
        sign_and_submit_signal_testnet_once(
            object(), object(), vault, forbidden, "dedicated-testnet-key",
            GrvtTestnetLimits("123456", "BTC_USDT_Perp", Decimal("100")),
            session_id="paper-session", action_request_id="paper-action",
            dedicated_key_hex="0x" + KEY.hex(), expected_signer=SIGNER,
            builder=BUILDER, side="BUY", size=Decimal("0.001"),
            limit_price=Decimal("50000"), reduce_only=False,
            clock_ns=time.time_ns, instrument_fetcher=forbidden,
            quote_fetcher=forbidden, signal_submitter=forbidden,
        )
    assert external == []
    vault.lock()


def test_http_route_has_cap_confirmation_and_never_echoes_secrets(tmp_path, monkeypatch):
    import app.portfolio.api as real_api

    class FakePaper:
        def __init__(self, path, *, allow_create):
            assert allow_create is False

        @contextmanager
        def connect(self):
            yield SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchone=lambda: (1,)))

    monkeypatch.setattr(real_api, "PaperPerpStore", FakePaper)
    calls = []

    def signed_submit(paper, lab, vault, client_factory, api_key, limits, **kwargs):
        calls.append((api_key, kwargs["dedicated_key_hex"], str(limits.max_order_notional)))
        return (SimpleNamespace(client_order_id=client_id_for_signal(
                    kwargs["session_id"], kwargs["action_request_id"]),
                    session_id=kwargs["session_id"],
                    action_request_id=kwargs["action_request_id"]),
                SimpleNamespace(status="ACKNOWLEDGED", external_status=None))

    app = FastAPI()
    app.include_router(create_real_router(
        tmp_path / "vault.db", lab_path=tmp_path / "lab.db",
        paper_perp_path=tmp_path / "paper.db",
        grvt_testnet_signal_signed_submitter=signed_submit,
    ))
    client = TestClient(app)
    token = client.post("/real/vault/create", json={
        "passphrase": "a robust local password",
    }).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    body = {
        "api_key": "DEDICATED_TESTNET_KEY", "dedicated_private_key": "0x" + KEY.hex(),
        "expected_signer": SIGNER, "builder": BUILDER,
        "session_id": "paper-session", "action_request_id": "paper-action",
        "sub_account_id": "123456", "instrument": "BTC_USDT_Perp",
        "max_order_notional": "100", "side": "BUY", "size": "0.001",
        "limit_price": "50000", "max_spread_bps": 100,
        "max_slippage_bps": 100, "reduce_only": False,
        "confirmation": "FIRMAR_Y_ENVIAR_GRVT_TESTNET",
    }
    url = "/real/exchanges/grvt/testnet/signals/sign-and-submit"
    assert client.post(url, json=body).status_code == 401
    assert client.post(url, json={**body, "max_order_notional": "101"},
                       headers=headers).status_code == 422
    assert client.post(url, json={**body, "confirmation": "TRADE_MAINNET"},
                       headers=headers).status_code == 422
    assert client.post(url, json={**body, "max_slippage_bps": "100"},
                       headers=headers).status_code == 422
    assert client.post(url, json={**body, "size": "0.003"},
                       headers=headers).status_code == 422
    assert calls == []
    response = client.post(url, json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["network"] == "TESTNET"
    assert response.json()["signed_locally"] is True
    assert response.json()["execution_unverified"] is True
    assert response.json()["mainnet_order_sent"] is False
    assert KEY.hex() not in response.text and body["api_key"] not in response.text
    assert calls == [(body["api_key"], body["dedicated_private_key"], "100")]


def test_missing_paper_action_never_reaches_signer_or_login(tmp_path, monkeypatch):
    import app.portfolio.api as real_api

    class EmptyPaper:
        def __init__(self, path, *, allow_create):
            pass

        @contextmanager
        def connect(self):
            yield SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchone=lambda: None))

    monkeypatch.setattr(real_api, "PaperPerpStore", EmptyPaper)
    app = FastAPI()
    app.include_router(create_real_router(
        tmp_path / "vault.db", lab_path=tmp_path / "lab.db",
        paper_perp_path=tmp_path / "paper.db",
        grvt_testnet_factory=lambda key: (_ for _ in ()).throw(AssertionError("login")),
        grvt_testnet_signal_signed_submitter=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("signer")),
    ))
    client = TestClient(app)
    token = client.post("/real/vault/create", json={
        "passphrase": "a robust local password",
    }).json()["token"]
    response = client.post(
        "/real/exchanges/grvt/testnet/signals/sign-and-submit",
        json={
            "api_key": "DEDICATED_TESTNET_KEY", "dedicated_private_key": "0x" + KEY.hex(),
            "expected_signer": SIGNER, "builder": BUILDER,
            "session_id": "absent", "action_request_id": "absent",
            "sub_account_id": "123456", "instrument": "BTC_USDT_Perp",
            "max_order_notional": "100", "side": "BUY", "size": "0.001",
            "limit_price": "50000", "max_spread_bps": 100,
            "max_slippage_bps": 100, "reduce_only": False,
            "confirmation": "FIRMAR_Y_ENVIAR_GRVT_TESTNET",
        },
        headers={"X-Orbis-Vault-Token": token},
    )
    assert response.status_code == 409
    assert KEY.hex() not in response.text


def test_stopped_subaccount_rejects_both_signal_routes_before_login(tmp_path, monkeypatch):
    import app.portfolio.api as real_api

    class ExistingPaper:
        def __init__(self, path, *, allow_create):
            assert allow_create is False

        @contextmanager
        def connect(self):
            yield SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchone=lambda: (1,)))

    monkeypatch.setattr(real_api, "PaperPerpStore", ExistingPaper)
    external = []

    def forbidden(*args, **kwargs):
        external.append("external")
        raise AssertionError("Una subcuenta detenida no consulta GRVT")

    app = FastAPI()
    app.include_router(create_real_router(
        tmp_path / "vault.db", lab_path=tmp_path / "lab.db",
        paper_perp_path=tmp_path / "paper.db",
        grvt_testnet_factory=forbidden,
        grvt_testnet_signal_submitter=forbidden,
        grvt_testnet_signal_signed_submitter=forbidden,
    ))
    client = TestClient(app)
    token = client.post("/real/vault/create", json={
        "passphrase": "a robust local password",
    }).json()["token"]
    headers = {"X-Orbis-Vault-Token": token}
    stopped = client.post("/real/exchanges/grvt/testnet/subaccounts/123456/stop",
                          json={"confirmation": "DETENER_GRVT_TESTNET"},
                          headers=headers)
    assert stopped.status_code == 200 and stopped.json()["stopped"] is True
    client_id = client_id_for_signal("paper-session", "paper-action")
    unsigned = {"api_key": "DEDICATED_TESTNET_KEY", "session_id": "paper-session",
                "action_request_id": "paper-action", "sub_account_id": "123456",
                "instrument": "BTC_USDT_Perp", "max_order_notional": "100",
                "confirmation": "FIRMAR_Y_ENVIAR_GRVT_TESTNET",
                "dedicated_private_key": "0x" + KEY.hex(),
                "expected_signer": SIGNER, "builder": BUILDER, "side": "BUY",
                "size": "0.001", "limit_price": "50000",
                "max_spread_bps": 100, "max_slippage_bps": 100,
                "reduce_only": False}
    response = client.post("/real/exchanges/grvt/testnet/signals/sign-and-submit",
                           json=unsigned, headers=headers)
    assert response.status_code == 409
    signed = {"api_key": "DEDICATED_TESTNET_KEY", "session_id": "paper-session",
              "action_request_id": "paper-action", "sub_account_id": "123456",
              "instrument": "BTC_USDT_Perp", "max_order_notional": "100",
              "confirmation": "ENVIAR_GRVT_TESTNET",
              "order": {"sub_account_id": "123456", "is_market": False,
                        "time_in_force": "IMMEDIATE_OR_CANCEL", "post_only": False,
                        "reduce_only": False,
                        "legs": [{"instrument": "BTC_USDT_Perp", "size": "0.001",
                                  "limit_price": "50000", "is_buying_asset": True}],
                        "signature": {"signer": SIGNER, "r": "0x" + "1" * 64,
                                      "s": "0x" + "2" * 64, "v": 27,
                                      "nonce": 1,
                                      "expiration": str(time.time_ns() + 60_000_000_000),
                                      "chain_id": "326"},
                        "metadata": {"client_order_id": client_id},
                        "builder": BUILDER, "builder_fee": "0"}}
    response = client.post("/real/exchanges/grvt/testnet/signals/submit",
                           json=signed, headers=headers)
    assert response.status_code == 409
    assert external == []
    assert KEY.hex() not in response.text
