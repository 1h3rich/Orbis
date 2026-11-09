from datetime import datetime, timezone, timedelta
from decimal import Decimal
import hashlib
import json
from uuid import uuid4

from app.portfolio.vault import SCHEMA_VERSION
import pytest
import httpx

from app.derivatives.paper_math import MarginRules
from app.live_execution.grvt_testnet_journal import (
    GrvtTestnetJournalError, abandon_testnet_prepared, close_rejected_testnet_zero_fill,
    claim_testnet_cancel,
    claim_testnet_post, prepare_testnet_order, record_testnet_ack,
    record_testnet_cancel_observation, record_testnet_observation,
    require_new_testnet_submission, stop_testnet_subaccount,
)
from app.live_execution.grvt_testnet_order import (
    AUTH_URL, ORDER_URL, LOOKUP_URL, CANCEL_URL, FILL_HISTORY_URL, FUNDING_HISTORY_URL,
    TRANSFER_HISTORY_URL, WITHDRAWAL_HISTORY_URL, DEPOSIT_HISTORY_URL,
    INTEREST_HISTORY_URL,
    GrvtTestnetLimits, GrvtTestnetOrderAck,
    GrvtTestnetOrderClient, GrvtTestnetOrderError, GrvtTestnetOrderObservation,
)
from app.live_execution.grvt_testnet_submit import (
    audit_testnet_cancel, audit_testnet_intent, observe_testnet_cancel,
    observe_testnet_order,
    stop_and_cancel_testnet_once, submit_testnet_once,
)
from app.live_execution.grvt_testnet_evidence import (
    GrvtTestnetEvidenceError, list_testnet_audits,
)
from app.live_execution.grvt_testnet_risk import (
    MARGIN_RULES_URL, GrvtTestnetRiskState, evaluate_testnet_risk,
)
from app.live_execution.grvt_testnet_preflight import (
    GrvtTestnetPreflightError, read_testnet_preflight,
    record_testnet_preflight,
    client_id_for_signal,
)
from app.live_execution.grvt_testnet_signer import GrvtTestnetLimitDraft
from app.live_execution.grvt_testnet_quote import GrvtTestnetPriceGuard, GrvtTestnetQuote
from app.live_execution.grvt_testnet_recovery import (
    _fill_summary, _findings, list_testnet_recovery_subaccounts,
    read_testnet_recovery,
)
from app.portfolio.models import (GrvtTestnetAuditEvidence, GrvtTestnetCancelAttempt,
                                  GrvtTestnetOrderIntent, GrvtTestnetPreflightEvidence)
from app.portfolio.vault import RealVault


NOW_NS = 1_780_000_000_000_000_000
NOW = datetime.fromtimestamp(NOW_NS // 1_000_000_000, timezone.utc)
CID = str(2**63 + 123)
SUB = "123456"


def order():
    address = "0x" + "a" * 40
    return {
        "sub_account_id": SUB, "is_market": False,
        "time_in_force": "IMMEDIATE_OR_CANCEL", "post_only": False,
        "reduce_only": False,
        "legs": [{"instrument": "BTC_USDT_Perp", "size": "0.01",
                  "limit_price": "50000", "is_buying_asset": True}],
        "signature": {"signer": address, "r": "0x" + "1" * 64,
                      "s": "0x" + "2" * 64, "v": 27,
                      "expiration": str(NOW_NS + 60_000_000_000),
                      "nonce": 1, "chain_id": "326"},
        "metadata": {"client_order_id": CID}, "builder": address,
        "builder_fee": "0",
    }


def limits():
    return GrvtTestnetLimits(SUB, "BTC_USDT_Perp", Decimal("1000"))


def vault_at(path):
    vault = RealVault(path)
    vault.unlock("long test passphrase", create=not path.exists())
    return vault


def archive_preflight(vault, *, candidate=None, signal_source=None, price_guard=None, risk_state=None):
    candidate = candidate or order()
    client_id = candidate["metadata"]["client_order_id"]
    state = risk_state or GrvtTestnetRiskState(
        SUB, "USDT", NOW_NS, NOW_NS, "SIMPLE_CROSS_MARGIN",
        "SINGLE_ASSET_MODE", Decimal("10000"), Decimal("10000"),
        Decimal(0), Decimal(0), Decimal(0), (), 0,
    )
    rules = MarginRules.from_payload({
        "instrument": "BTC_USDT_Perp", "max_position_size": "10",
        "fetched_at_ns": NOW_NS,
        "risk_brackets": [{"tier": 1, "notional_floor": "0",
                           "notional_cap": None, "initial_margin_rate": "0.02",
                           "maintenance_margin_rate": "0.01",
                           "max_leverage": 50,
                           "cumulative_maintenance_amount": "0"}],
    }, "BTC_USDT_Perp", NOW_NS)
    leg = candidate["legs"][0]
    draft = GrvtTestnetLimitDraft(SUB,leg["instrument"],"BUY" if leg["is_buying_asset"] else "SELL",
        Decimal(leg["size"]),Decimal(leg["limit_price"]),candidate["reduce_only"],candidate["builder"],
        client_id,candidate["signature"]["nonce"],int(candidate["signature"]["expiration"]))
    decision = evaluate_testnet_risk(draft, limits(), state, rules, now_ns=NOW_NS)
    return record_testnet_preflight(vault, candidate, limits(), state, rules,
                                    decision, now=NOW, now_ns=NOW_NS,
                                    signal_source=signal_source,
                                    price_guard=price_guard)


def claim_prepared(vault):
    archive_preflight(vault)
    return claim_testnet_post(vault, order(), limits(), now=NOW, now_ns=NOW_NS)


def test_grvt_signal_origin_survives_recovery_without_keys(tmp_path):
    path = tmp_path / "real.db"
    vault = vault_at(path)
    session_id, action_id = str(uuid4()), str(uuid4())
    client_id = client_id_for_signal(session_id, action_id)
    candidate = order()
    candidate["metadata"] = {"client_order_id": client_id}
    source = {"kind": "GRVT_AUTO_PAPER", "session_id": session_id,
              "action_request_id": action_id, "action": "OPEN", "direction": "LONG",
              "version_id": str(uuid4()), "dataset_id": str(uuid4()),
              "dataset_hash": "a" * 64, "client_order_id": client_id}
    prepare_testnet_order(vault, candidate, limits(), now=NOW, now_ns=NOW_NS)
    archive_preflight(vault, candidate=candidate, signal_source=source)
    claim_testnet_post(vault, candidate, limits(), now=NOW, now_ns=NOW_NS)
    vault.lock()
    reopened = vault_at(path)
    assert read_testnet_preflight(reopened, SUB, client_id)["signal_source"] == source
    report = read_testnet_recovery(reopened, SUB)
    assert report.items[0].signal_source == source
    assert not report.can_submit
    with reopened.session() as db:
        preflight = db.get(GrvtTestnetPreflightEvidence, client_id)
        payload = json.loads(preflight.payload_json)
        payload["request_digest"] = "0" * 64
        preflight.payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        preflight.payload_digest = hashlib.sha256(preflight.payload_json.encode("ascii")).hexdigest()
        db.commit()
    with pytest.raises(GrvtTestnetPreflightError, match="ajeno"):
        read_testnet_recovery(reopened, SUB)


def test_grvt_signal_origin_rejects_wrong_client_id(tmp_path):
    vault = vault_at(tmp_path / "real.db")
    prepare_testnet_order(vault, order(), limits(), now=NOW, now_ns=NOW_NS)
    source = {"kind": "GRVT_AUTO_PAPER", "session_id": str(uuid4()),
              "action_request_id": str(uuid4()), "action": "OPEN", "direction": "LONG",
              "version_id": str(uuid4()), "dataset_id": str(uuid4()),
              "dataset_hash": "a" * 64, "client_order_id": CID}
    with pytest.raises(GrvtTestnetPreflightError, match="Procedencia"):
        archive_preflight(vault, signal_source=source)
    with pytest.raises(GrvtTestnetPreflightError, match="no archivado"):
        read_testnet_preflight(vault, SUB, CID)


def observation(*, status="FILLED", update_ns=NOW_NS,
                side="BUY", size="0.01", traded=None,
                order_id="0x123abc"):
    return GrvtTestnetOrderObservation(
        SUB, CID, order_id, status, "BTC_USDT_Perp", side,
        size, size if traded is None else traded, "50000", str(update_ns),
    )


def test_cancel_ack_is_followed_by_lookup_without_second_cancel_post():
    calls = []

    def post(url, body, headers):
        calls.append(url)
        if url == AUTH_URL:
            return httpx.Response(
                200, json={"status": "success", "sub_account_id": SUB},
                headers={"Set-Cookie": "gravity=testnet-cookie-123; Path=/",
                         "X-Grvt-Account-Id": "0x" + "a" * 40},
                request=httpx.Request("POST", url),
            )
        if url == CANCEL_URL:
            assert body == {"sub_account_id": SUB, "client_order_id": CID,
                            "time_to_live_ms": "5000"}
            return httpx.Response(200, json={"result": {"ack": True}},
                                  request=httpx.Request("POST", url))
        assert url == LOOKUP_URL
        candidate = {**order(), "order_id": "0x123abc",
                     "metadata": {"client_order_id": CID, "create_time": str(NOW_NS)},
                     "state": {"status": "OPEN", "traded_size": ["0"],
                               "update_time": str(NOW_NS)}}
        return httpx.Response(200, json={"result": candidate},
                              request=httpx.Request("POST", url))

    client = GrvtTestnetOrderClient("testnet-key-123", http_post=post,
                                    clock_ns=lambda: NOW_NS)
    observed = client.cancel_by_client_id(SUB, CID)
    assert observed.status == "OPEN"  # El acuse no afirma CANCELLED.
    assert calls == [AUTH_URL, CANCEL_URL, LOOKUP_URL]
    with pytest.raises(GrvtTestnetOrderError, match="ya consumida"):
        client.cancel_by_client_id(SUB, CID)
    assert calls == [AUTH_URL, CANCEL_URL, LOOKUP_URL]


def test_cancel_ack_without_visible_order_is_uncertain_and_never_reposted():
    calls = []

    def post(url, body, headers):
        calls.append(url)
        if url == AUTH_URL:
            return httpx.Response(
                200, json={"status": "success", "sub_account_id": SUB},
                headers={"Set-Cookie": "gravity=testnet-cookie-123; Path=/",
                         "X-Grvt-Account-Id": "0x" + "a" * 40},
                request=httpx.Request("POST", url),
            )
        if url == CANCEL_URL:
            return httpx.Response(200, json={"result": {"ack": "true"}},
                                  request=httpx.Request("POST", url))
        assert url == LOOKUP_URL
        return httpx.Response(404, json={"code": 1004},
                              request=httpx.Request("POST", url))

    client = GrvtTestnetOrderClient("testnet-key-123", http_post=post,
                                    clock_ns=lambda: NOW_NS)
    with pytest.raises(GrvtTestnetOrderError, match="desconocido"):
        client.cancel_by_client_id(SUB, CID)
    with pytest.raises(GrvtTestnetOrderError, match="ya consumida"):
        client.cancel_by_client_id(SUB, CID)
    assert calls == [AUTH_URL, CANCEL_URL, LOOKUP_URL]


def test_recovery_findings_distinguish_missing_proof_from_mismatched_state():
    base = {"fills": {"sizes_match": True, "external_activity_detected": False},
            "comparison": {"available": True, "position_delta_matches": True}}
    assert _findings(base) == ()
    assert _findings(base | {"comparison": {"available": False}}) == ("PREFLIGHT_MISSING",)
    assert _findings(base | {"fills": {"sizes_match": False,
                                         "external_activity_detected": True},
                             "comparison": {"available": True,
                                            "position_delta_matches": False}}) == (
        "FILL_TOTAL_MISMATCH", "POSITION_DELTA_MISMATCH", "EXTERNAL_FILLS")


def test_rejected_zero_fill_closes_only_after_two_clean_independent_audits(tmp_path):
    vault = vault_at(tmp_path / "real.db")
    prepare_testnet_order(vault, order(), limits(), now=NOW, now_ns=NOW_NS)
    claim_prepared(vault)
    record_testnet_observation(vault, observation(status="REJECTED", traded="0"), now=NOW)
    with vault.session() as db:
        preflight_digest = db.get(GrvtTestnetPreflightEvidence, CID).payload_digest

    def archive(seconds, *, external=False, changed_balance=False):
        seen = NOW + timedelta(seconds=seconds)
        payload = {
            "schema": "orbis.grvt.testnet.audit.v1",
            "order": {"client_order_id": CID, "sub_account_id": SUB,
                      "order_id": "0x123abc", "status": "REJECTED",
                      "update_time_ns": str(NOW_NS), "traded_size": "0"},
            "fills": {"rows": [], "external_rows": ["other"] if external else [],
                      "count": 0, "external_fill_count": 1 if external else 0,
                      "external_activity_detected": external,
                      "sizes_match": True, "pages_drained": 1,
                      "gross_notional": "0", "realized_pnl": "0",
                      "builder_fee": "0", "fees_by_currency": []},
            "comparison": {"available": True, "preflight_digest": preflight_digest,
                           "position_delta_matches": True,
                           "position_delta": "0", "expected_fill_delta": "0"},
            "account": {"event_time_ns": NOW_NS + seconds * 1_000_000_000,
                        "settle_currency": "USDT",
                        "settle_balance": "9999" if changed_balance else "10000",
                        "total_equity": "10000", "initial_margin": "0",
                        "maintenance_margin": "0", "positions": [],
                        "open_order_count": 0},
        }
        for section, count in (("funding", "payment_count"),
                               ("transfers", "transfer_count"),
                               ("withdrawals", "withdrawal_count"),
                               ("deposits", "deposit_count"),
                               ("interest", "payment_count")):
            payload[section] = {"rows": [], count: 0, "pages_drained": 1}
            if section in ("transfers", "withdrawals"):
                payload[section]["unrelated_row_count"] = 0
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with vault.session() as db:
            db.add(GrvtTestnetAuditEvidence(
                id=str(uuid4()), client_order_id=CID, sub_account_id=SUB,
                observed_at=seen, payload_json=encoded,
                payload_digest=hashlib.sha256(encoded.encode("ascii")).hexdigest()))
            db.commit()

    archive(1)
    with pytest.raises(GrvtTestnetJournalError, match="dos auditorías"):
        close_rejected_testnet_zero_fill(vault, SUB, CID, now=NOW + timedelta(seconds=2))
    archive(7, external=True)
    with pytest.raises(GrvtTestnetJournalError, match="no conciliado"):
        close_rejected_testnet_zero_fill(vault, SUB, CID, now=NOW + timedelta(seconds=8))
    archive(13, changed_balance=True)
    with pytest.raises(GrvtTestnetJournalError, match="no conciliado"):
        close_rejected_testnet_zero_fill(vault, SUB, CID, now=NOW + timedelta(seconds=14))
    archive(19)
    archive(25)
    with pytest.raises(GrvtTestnetJournalError, match="no conciliado"):
        close_rejected_testnet_zero_fill(vault, SUB, CID, now=NOW + timedelta(seconds=26))

    vault = vault_at(tmp_path / "clean.db")
    prepare_testnet_order(vault, order(), limits(), now=NOW, now_ns=NOW_NS)
    claim_prepared(vault)
    record_testnet_observation(vault, observation(status="REJECTED", traded="0"), now=NOW)
    with vault.session() as db:
        preflight_digest = db.get(GrvtTestnetPreflightEvidence, CID).payload_digest
    archive(19)
    archive(25)
    closed = close_rejected_testnet_zero_fill(vault, SUB, CID,
                                               now=NOW + timedelta(seconds=26))
    assert closed.status == "REJECTED_CLOSED"
    assert read_testnet_recovery(vault, SUB).items[0].next_check == "REJECTED_ZERO_FILL_ARCHIVED"
    with pytest.raises(GrvtTestnetJournalError):
        close_rejected_testnet_zero_fill(vault, SUB, CID, now=NOW + timedelta(seconds=27))
    next_order = order()
    next_order["metadata"] = {"client_order_id": str(int(CID) + 1)}
    assert prepare_testnet_order(vault, next_order, limits(),
                                 now=NOW + timedelta(seconds=26),
                                 now_ns=NOW_NS + 26_000_000_000).status == "PREPARED"


@pytest.mark.parametrize(("status", "traded"), [
    ("CANCELLED", "0"), ("REJECTED", "0.001"), ("FILLED", "0.01"),
])
def test_zero_fill_closure_never_accepts_other_terminal_outcomes(tmp_path, status, traded):
    vault = vault_at(tmp_path / "real.db")
    prepare_testnet_order(vault, order(), limits(), now=NOW, now_ns=NOW_NS)
    claim_prepared(vault)
    record_testnet_observation(vault, observation(status=status, traded=traded), now=NOW)
    with pytest.raises(GrvtTestnetJournalError, match="sin ejecución"):
        close_rejected_testnet_zero_fill(vault, SUB, CID, now=NOW + timedelta(seconds=1))


def test_recovery_does_not_use_old_matching_audit_after_newer_contradiction(tmp_path):
    vault = vault_at(tmp_path / "real.db")
    prepare_testnet_order(vault, order(), limits(), now=NOW, now_ns=NOW_NS)
    claim_prepared(vault)
    record_testnet_observation(vault, observation(), now=NOW)

    def archive(status: str, at: datetime):
        payload = {"schema": "orbis.grvt.testnet.audit.v1",
                   "order": {"client_order_id": CID, "sub_account_id": SUB,
                             "order_id": "0x123abc", "status": status,
                             "update_time_ns": str(NOW_NS), "traded_size": "0.01"},
                   "fills": {"rows": [], "sizes_match": True,
                             "external_activity_detected": False},
                   "comparison": {"available": True,
                                  "position_delta_matches": True}}
        data = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with vault.session() as db:
            db.add(GrvtTestnetAuditEvidence(
                id=str(uuid4()), client_order_id=CID, sub_account_id=SUB,
                observed_at=at, payload_json=data,
                payload_digest=hashlib.sha256(data.encode("ascii")).hexdigest()))
            db.commit()

    archive("FILLED", NOW)
    assert read_testnet_recovery(vault, SUB).items[0].current_state_audited is True
    archive("CANCELLED", NOW + timedelta(seconds=1))
    item = read_testnet_recovery(vault, SUB).items[0]
    assert item.verified_audit_count == 2
    assert item.current_state_audited is False
    assert item.current_audit_findings == ()
    assert item.next_check == "AUDIT_ORDER_FILLS_ACCOUNT"
    assert read_testnet_recovery(vault, SUB).can_submit is False
    vault.lock()


def test_recovered_fill_summary_recalculates_weighted_price_and_rejects_tampering(tmp_path):
    vault = vault_at(tmp_path / "real.db")
    prepare_testnet_order(vault, order(), limits(), now=NOW, now_ns=NOW_NS)
    claim_prepared(vault)
    record_testnet_observation(vault, observation(), now=NOW)
    fill_rows = [
        {"trade_id": "trade-1", "event_time_ns": NOW_NS, "size": "0.004",
         "price": "49990", "fee": "0.10", "fee_currency": "USDT",
         "realized_pnl": "0", "builder_fee": "0", "is_taker": True},
        {"trade_id": "trade-2", "event_time_ns": NOW_NS + 1, "size": "0.006",
         "price": "50010", "fee": "0.15", "fee_currency": "USDT",
         "realized_pnl": "0", "builder_fee": "0", "is_taker": True},
    ]
    payload = {"schema": "orbis.grvt.testnet.audit.v1",
               "order": {"client_order_id": CID, "sub_account_id": SUB,
                         "order_id": "0x123abc", "status": "FILLED",
                         "update_time_ns": str(NOW_NS), "traded_size": "0.01"},
               "fills": {"count": 2, "rows": fill_rows,
                         "sizes_match": True, "external_activity_detected": False,
                         "gross_notional": "500.02", "realized_pnl": "0",
                         "builder_fee": "0", "fees_by_currency": [["USDT", "0.25"]]},
               "comparison": {"available": True,
                              "position_delta_matches": True}}
    assert _fill_summary(payload) == {
        "fill_count": 2, "filled_size": "0.010", "sizes_match_order": True,
        "weighted_average_price": "50002.000000000000000000",
        "gross_notional": "500.020", "fees_by_currency": [["USDT", "0.25"]],
        "realized_pnl_observed": "0", "builder_fee": "0",
        "last_fill_time_ns": NOW_NS + 1, "causal_proof": False,
    }
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    with vault.session() as db:
        row = GrvtTestnetAuditEvidence(
            id=str(uuid4()), client_order_id=CID, sub_account_id=SUB,
            observed_at=NOW, payload_json=data,
            payload_digest=hashlib.sha256(data.encode("ascii")).hexdigest())
        db.add(row)
        db.commit()
        row_id = row.id
    report = read_testnet_recovery(vault, SUB)
    assert report.items[0].fill_summary == _fill_summary(payload)
    assert not report.can_submit
    with vault.session() as db:
        row = db.get(GrvtTestnetAuditEvidence, row_id)
        payload["fills"]["gross_notional"] = "900"
        row.payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        row.payload_digest = hashlib.sha256(row.payload_json.encode("ascii")).hexdigest()
        db.commit()
    with pytest.raises(GrvtTestnetEvidenceError, match="Resumen de fills"):
        read_testnet_recovery(vault, SUB)
    vault.lock()


def test_recovery_lists_only_local_testnet_subaccounts(tmp_path):
    vault = vault_at(tmp_path / "real.db")
    assert list_testnet_recovery_subaccounts(vault) == ()
    stop_testnet_subaccount(vault, "9", now=NOW)
    stop_testnet_subaccount(vault, "2", now=NOW)
    assert list_testnet_recovery_subaccounts(vault) == ("2", "9")
    vault.lock()


def test_local_submission_guard_blocks_stopped_duplicate_and_unreconciled(tmp_path):
    vault = vault_at(tmp_path / "real.db")
    require_new_testnet_submission(vault, SUB, CID)
    prepare_testnet_order(vault, order(), limits(), now=NOW, now_ns=NOW_NS)
    with pytest.raises(GrvtTestnetJournalError, match="ya archivada"):
        require_new_testnet_submission(vault, SUB, CID)
    with pytest.raises(GrvtTestnetJournalError, match="sin conciliar"):
        require_new_testnet_submission(vault, SUB, str(int(CID) + 1))
    stop_testnet_subaccount(vault, SUB, now=NOW)
    with pytest.raises(GrvtTestnetJournalError, match="detenida"):
        require_new_testnet_submission(vault, SUB, str(int(CID) + 1))
    vault.lock()


def test_claim_survives_restart_without_signature_or_second_post(tmp_path):
    path = tmp_path / "real.db"
    vault = vault_at(path)
    initial = prepare_testnet_order(vault, order(), limits(), now=NOW, now_ns=NOW_NS)
    assert initial.status == "PREPARED"
    claimed = claim_prepared(vault)
    assert claimed.status == "POST_CLAIMED"
    with vault.session() as db:
        row = db.get(GrvtTestnetOrderIntent, CID)
        assert row is not None
        assert row.request_digest and len(row.request_digest) == 64
        assert row.max_order_notional == Decimal("1000")
    vault.lock()
    assert b"grvt_testnet_order_intents" not in path.read_bytes()
    assert ("0x" + "1" * 64).encode() not in path.read_bytes()
    vault = vault_at(path)
    assert prepare_testnet_order(vault, order(), limits(), now=NOW, now_ns=NOW_NS).status == "POST_CLAIMED"
    with pytest.raises(GrvtTestnetJournalError, match="no preparada"):
        claim_testnet_post(vault, order(), limits(), now=NOW, now_ns=NOW_NS)
    vault.lock()


def test_claim_requires_fresh_untampered_preflight(tmp_path):
    vault = vault_at(tmp_path / "real.db")
    prepare_testnet_order(vault, order(), limits(), now=NOW, now_ns=NOW_NS)
    with pytest.raises(GrvtTestnetPreflightError, match="no archivado"):
        claim_testnet_post(vault, order(), limits(), now=NOW, now_ns=NOW_NS)
    archive_preflight(vault)
    assert read_testnet_preflight(vault, SUB, CID)["state"]["settle_balance"] == "10000"
    with pytest.raises(GrvtTestnetPreflightError, match="obsoleto"):
        claim_testnet_post(vault, order(), limits(),
                           now=NOW + timedelta(seconds=6),
                           now_ns=NOW_NS + 6_000_000_000)
    with vault.session() as db:
        db.connection().exec_driver_sql(
            "UPDATE grvt_testnet_preflight_evidence SET payload_json = '{}'"
        )
        db.commit()
    with pytest.raises(GrvtTestnetPreflightError, match="alterado"):
        claim_testnet_post(vault, order(), limits(), now=NOW, now_ns=NOW_NS)
    with vault.session() as db:
        assert db.get(GrvtTestnetOrderIntent, CID).status == "PREPARED"
    vault.lock()


def test_claim_binds_archived_testnet_quote_to_exact_order(tmp_path):
    vault = vault_at(tmp_path / "real.db")
    prepare_testnet_order(vault, order(), limits(), now=NOW, now_ns=NOW_NS)
    price = GrvtTestnetPriceGuard(
        GrvtTestnetQuote("BTC_USDT_Perp", NOW_NS, NOW_NS,
                         Decimal("49999"), Decimal("50000"),
                         Decimal("1"), Decimal("1")),
        100, 100,
    )
    archive_preflight(vault, price_guard=price)
    stored = read_testnet_preflight(vault, SUB, CID)
    assert stored["price_guard"]["source"] == "GRVT_TESTNET_PUBLIC_TICKER"
    assert stored["price_guard"]["limit_price"] == "50000"
    recovered = read_testnet_recovery(vault, SUB)
    assert recovered.can_submit is False
    assert recovered.items[0].price_guard == stored["price_guard"]
    with vault.session() as db:
        row = db.get(GrvtTestnetPreflightEvidence, CID)
        payload = json.loads(row.payload_json)
        payload["price_guard"]["limit_price"] = "49000"
        row.payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        row.payload_digest = hashlib.sha256(row.payload_json.encode("ascii")).hexdigest()
        db.commit()
    # La huella coincide con el JSON manipulado, pero no con el intento.
    with pytest.raises(GrvtTestnetPreflightError, match="Precio"):
        claim_testnet_post(vault, order(), limits(), now=NOW, now_ns=NOW_NS)
    with vault.session() as db:
        assert db.get(GrvtTestnetOrderIntent, CID).status == "PREPARED"
    vault.lock()


def test_same_client_id_cannot_change_signed_order_or_limit(tmp_path):
    vault = vault_at(tmp_path / "real.db")
    prepare_testnet_order(vault, order(), limits(), now=NOW, now_ns=NOW_NS)
    changed = order()
    changed["signature"]["nonce"] = 2
    with pytest.raises(GrvtTestnetJournalError, match="reutilizado"):
        prepare_testnet_order(vault, changed, limits(), now=NOW, now_ns=NOW_NS)
    with pytest.raises(GrvtTestnetJournalError, match="reutilizado"):
        prepare_testnet_order(vault, order(),
                              GrvtTestnetLimits(SUB, "BTC_USDT_Perp", Decimal("2000")),
                              now=NOW, now_ns=NOW_NS)
    vault.lock()


def test_second_client_id_waits_until_first_intent_is_resolved(tmp_path):
    vault = vault_at(tmp_path / "real.db")
    prepare_testnet_order(vault, order(), limits(), now=NOW, now_ns=NOW_NS)
    second = order()
    second["metadata"]["client_order_id"] = str(2**63 + 124)
    with pytest.raises(GrvtTestnetJournalError, match="sin conciliar"):
        prepare_testnet_order(vault, second, limits(), now=NOW, now_ns=NOW_NS)
    claim_prepared(vault)
    with pytest.raises(GrvtTestnetJournalError, match="sin conciliar"):
        prepare_testnet_order(vault, second, limits(), now=NOW, now_ns=NOW_NS)
    with vault.session() as db:
        assert db.connection().exec_driver_sql(
            "SELECT count(*) FROM grvt_testnet_order_intents").scalar_one() == 1
    vault.lock()


def test_only_unsent_preparation_can_be_abandoned_and_id_stays_used(tmp_path):
    path = tmp_path / "real.db"
    vault = vault_at(path)
    prepare_testnet_order(vault, order(), limits(), now=NOW, now_ns=NOW_NS)
    assert abandon_testnet_prepared(vault, SUB, CID, now=NOW).status == "ABANDONED"
    with pytest.raises(GrvtTestnetJournalError, match="sin POST"):
        abandon_testnet_prepared(vault, SUB, CID, now=NOW)
    with pytest.raises(GrvtTestnetJournalError, match="no preparada"):
        claim_testnet_post(vault, order(), limits(), now=NOW, now_ns=NOW_NS)
    next_order = order()
    next_order["metadata"]["client_order_id"] = str(2**63 + 124)
    vault.lock()
    vault = vault_at(path)
    assert prepare_testnet_order(vault, next_order, limits(), now=NOW,
                                 now_ns=NOW_NS).status == "PREPARED"
    vault.lock()


def test_claimed_or_foreign_testnet_preparation_cannot_be_abandoned(tmp_path):
    vault = vault_at(tmp_path / "real.db")
    prepare_testnet_order(vault, order(), limits(), now=NOW, now_ns=NOW_NS)
    with pytest.raises(GrvtTestnetJournalError, match="sin POST"):
        abandon_testnet_prepared(vault, "999", CID, now=NOW)
    claim_prepared(vault)
    with pytest.raises(GrvtTestnetJournalError, match="sin POST"):
        abandon_testnet_prepared(vault, SUB, CID, now=NOW)
    vault.lock()


def test_stop_is_durable_and_preserves_claimed_unknown(tmp_path):
    path = tmp_path / "real.db"
    vault = vault_at(path)
    prepare_testnet_order(vault, order(), limits(), now=NOW, now_ns=NOW_NS)
    assert stop_testnet_subaccount(vault, SUB, now=NOW) == 1
    with pytest.raises(GrvtTestnetJournalError, match="no preparada"):
        claim_testnet_post(vault, order(), limits(), now=NOW, now_ns=NOW_NS)
    vault.lock()
    vault = vault_at(path)
    changed = order()
    changed["metadata"]["client_order_id"] = str(2**63 + 124)
    with pytest.raises(GrvtTestnetJournalError, match="detenida"):
        prepare_testnet_order(vault, changed, limits(), now=NOW, now_ns=NOW_NS)
    vault.lock()


def test_ack_and_observation_do_not_book_trade_and_reject_regression(tmp_path):
    vault = vault_at(tmp_path / "real.db")
    prepare_testnet_order(vault, order(), limits(), now=NOW, now_ns=NOW_NS)
    claim_prepared(vault)
    ack = GrvtTestnetOrderAck(SUB, CID, "0x123abc", True)
    assert record_testnet_ack(vault, ack, now=NOW).status == "ACKNOWLEDGED"
    assert record_testnet_observation(vault, observation(status="OPEN", traded="0.005"), now=NOW).status == "OBSERVED"
    with pytest.raises(GrvtTestnetJournalError, match="retrocedió"):
        record_testnet_observation(vault, observation(status="OPEN", traded="0.004",
                                                      update_ns=NOW_NS + 1), now=NOW)
    filled = observation(status="FILLED", update_ns=NOW_NS + 1)
    final = record_testnet_observation(vault, filled, now=NOW + timedelta(seconds=1))
    assert final.external_status == "FILLED" and final.traded_size == Decimal("0.01")
    with pytest.raises(GrvtTestnetJournalError, match="anterior"):
        record_testnet_observation(vault, observation(status="FILLED", update_ns=NOW_NS - 1), now=NOW)
    with pytest.raises(GrvtTestnetJournalError, match="terminal"):
        record_testnet_observation(vault, observation(status="CANCELLED", update_ns=NOW_NS + 2), now=NOW)
    with vault.session() as db:
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_events").scalar_one() == 0
    vault.lock()


@pytest.mark.parametrize("change", [
    {"side": "SELL"}, {"size": "0.02"}, {"order_id": "0xother"},
])
def test_observation_must_match_exact_intent(change, tmp_path):
    vault = vault_at(tmp_path / "real.db")
    prepare_testnet_order(vault, order(), limits(), now=NOW, now_ns=NOW_NS)
    claim_prepared(vault)
    record_testnet_ack(vault, GrvtTestnetOrderAck(SUB, CID, "0x123abc", True), now=NOW)
    with pytest.raises(GrvtTestnetJournalError, match="ajena"):
        record_testnet_observation(vault, observation(**change), now=NOW)
    vault.lock()


def test_v9_migrates_to_v13_with_cifrada_preimage(tmp_path):
    path = tmp_path / "real.db"
    vault = vault_at(path)
    with vault.session() as db:
        db.connection().exec_driver_sql("DROP TABLE grvt_testnet_order_intents")
        db.connection().exec_driver_sql("DROP TABLE grvt_testnet_stops")
        db.connection().exec_driver_sql("PRAGMA user_version = 9")
        db.commit()
    vault.lock()
    vault = vault_at(path)
    assert vault.last_migration_backup is not None
    assert vault.last_migration_backup.exists()
    with vault.session() as db:
        assert db.connection().exec_driver_sql("PRAGMA user_version").scalar_one() == SCHEMA_VERSION
        assert db.connection().exec_driver_sql(
            "SELECT count(*) FROM grvt_testnet_order_intents").scalar_one() == 0
    vault.lock()


def test_v10_migrates_audit_table_without_losing_intent(tmp_path):
    path = tmp_path / "real.db"
    vault = vault_at(path)
    prepare_testnet_order(vault, order(), limits(), now=NOW, now_ns=NOW_NS)
    with vault.session() as db:
        db.connection().exec_driver_sql("DROP TABLE grvt_testnet_audit_evidence")
        db.connection().exec_driver_sql("PRAGMA user_version = 10")
        db.commit()
    vault.lock()
    vault = vault_at(path)
    assert vault.last_migration_backup is not None
    assert vault.last_migration_backup.read_bytes()[:16] != b"SQLite format 3\x00"
    with vault.session() as db:
        assert db.connection().exec_driver_sql("PRAGMA user_version").scalar_one() == SCHEMA_VERSION
        assert db.get(GrvtTestnetOrderIntent, CID).status == "PREPARED"
        assert db.connection().exec_driver_sql(
            "SELECT count(*) FROM grvt_testnet_audit_evidence").scalar_one() == 0
    vault.lock()


def test_v11_migrates_preflight_table_without_losing_intent(tmp_path):
    path = tmp_path / "real.db"
    vault = vault_at(path)
    prepare_testnet_order(vault, order(), limits(), now=NOW, now_ns=NOW_NS)
    with vault.session() as db:
        db.connection().exec_driver_sql("DROP TABLE grvt_testnet_preflight_evidence")
        db.connection().exec_driver_sql("PRAGMA user_version = 11")
        db.commit()
    vault.lock()
    vault = vault_at(path)
    backup = vault.last_migration_backup
    assert backup is not None and backup.read_bytes()[:16] != b"SQLite format 3\x00"
    with vault.session() as db:
        assert db.connection().exec_driver_sql("PRAGMA user_version").scalar_one() == SCHEMA_VERSION
        assert db.get(GrvtTestnetOrderIntent, CID).status == "PREPARED"
        assert db.connection().exec_driver_sql(
            "SELECT count(*) FROM grvt_testnet_preflight_evidence").scalar_one() == 0
    vault.lock()


def test_v12_migrates_cancel_table_without_losing_intent(tmp_path):
    path = tmp_path / "real.db"
    vault = vault_at(path)
    prepare_testnet_order(vault, order(), limits(), now=NOW, now_ns=NOW_NS)
    with vault.session() as db:
        db.connection().exec_driver_sql("DROP TABLE grvt_testnet_cancel_attempts")
        db.connection().exec_driver_sql("PRAGMA user_version = 12")
        db.commit()
    vault.lock()
    vault = vault_at(path)
    backup = vault.last_migration_backup
    assert backup is not None and backup.read_bytes()[:16] != b"SQLite format 3\x00"
    with vault.session() as db:
        assert db.connection().exec_driver_sql("PRAGMA user_version").scalar_one() == SCHEMA_VERSION
        assert db.get(GrvtTestnetOrderIntent, CID).status == "PREPARED"
        assert db.connection().exec_driver_sql(
            "SELECT count(*) FROM grvt_testnet_cancel_attempts").scalar_one() == 0
    vault.lock()


def test_stopped_cancel_is_claimed_before_unknown_post_and_never_resent(tmp_path):
    path = tmp_path / "real.db"
    vault = vault_at(path)
    prepare_testnet_order(vault, order(), limits(), now=NOW, now_ns=NOW_NS)
    claim_prepared(vault)

    class UncertainClient:
        calls = 0

        def cancel_by_client_id(self, sub, client_id):
            self.calls += 1
            raise GrvtTestnetOrderError("Resultado de cancelación testnet desconocido")

        def lookup_order(self, sub, client_id):
            return observation(status="CANCELLED", traded="0")

    client = UncertainClient()
    with pytest.raises(GrvtTestnetOrderError, match="desconocido"):
        stop_and_cancel_testnet_once(vault, client, SUB, CID,
                                     clock_ns=lambda: NOW_NS)
    assert client.calls == 1
    with vault.session() as db:
        row = db.get(GrvtTestnetCancelAttempt, CID)
        assert row.status == "CLAIMED" and row.external_status is None
        assert db.get(GrvtTestnetOrderIntent, CID).status == "POST_CLAIMED"
    pending_report = read_testnet_recovery(vault, SUB)
    assert pending_report.stopped and not pending_report.can_submit
    assert pending_report.items[0].next_check == "LOOKUP_CANCEL_AND_AUDIT"
    assert pending_report.items[0].current_audit_findings == ()
    vault.lock()
    assert b"grvt_testnet_cancel_attempts" not in path.read_bytes()
    vault = vault_at(path)
    with pytest.raises(GrvtTestnetJournalError, match="ya reclamada"):
        stop_and_cancel_testnet_once(vault, client, SUB, CID,
                                     clock_ns=lambda: NOW_NS)
    assert client.calls == 1
    observed = observe_testnet_cancel(vault, client, SUB, CID,
                                      clock_ns=lambda: NOW_NS)
    assert observed.status == "RESPONSE_OBSERVED"
    assert observed.external_status == "CANCELLED"
    assert read_testnet_recovery(vault, SUB).items[0].next_check == "AUDIT_AFTER_CANCEL"
    with vault.session() as db:
        assert db.get(GrvtTestnetOrderIntent, CID).status == "OBSERVED"
    vault.lock()


def test_successful_cancel_still_keeps_original_intent_blocked(tmp_path):
    vault = vault_at(tmp_path / "real.db")
    prepare_testnet_order(vault, order(), limits(), now=NOW, now_ns=NOW_NS)
    claim_prepared(vault)

    class CancelClient:
        calls = 0

        def cancel_by_client_id(self, sub, client_id):
            self.calls += 1
            return observation(status="CANCELLED", traded="0")

    client = CancelClient()
    result = stop_and_cancel_testnet_once(vault, client, SUB, CID,
                                          clock_ns=lambda: NOW_NS)
    assert result.external_status == "CANCELLED" and client.calls == 1
    with vault.session() as db:
        assert db.get(GrvtTestnetOrderIntent, CID).status == "OBSERVED"
    with pytest.raises(GrvtTestnetJournalError, match="ya reclamada"):
        claim_testnet_cancel(vault, SUB, CID, now=NOW)
    vault.lock()


def test_cancel_audit_requires_claim_before_external_lookup(tmp_path):
    vault = vault_at(tmp_path / "real.db")
    prepare_testnet_order(vault, order(), limits(), now=NOW, now_ns=NOW_NS)
    claim_prepared(vault)
    report = read_testnet_recovery(vault, SUB)
    assert not report.stopped and not report.can_submit
    assert report.items[0].next_check == "LOOKUP_ORDER_BY_CLIENT_ID"
    with pytest.raises(GrvtTestnetJournalError, match="Subcuenta"):
        read_testnet_recovery(vault, "not-an-id")

    class ForbiddenClient:
        def lookup_order(self, *_args):
            raise AssertionError("No debe consultar sin cancelación reclamada")

    with pytest.raises(GrvtTestnetJournalError, match="no reclamada"):
        audit_testnet_cancel(vault, ForbiddenClient(), SUB, CID,
                             clock_ns=lambda: NOW_NS)
    vault.lock()


@pytest.mark.parametrize(("terminal_status", "traded_size"), [
    ("FILLED", "0.01"),
    ("CANCELLED", "0.004"),
])
def test_durable_submit_after_timeout_can_only_be_observed(
        tmp_path, terminal_status, traded_size):
    path = tmp_path / "real.db"
    calls = []
    observed_fee = ["0.25"]
    external_fee = ["0.10"]
    observed_funding = ["0.03"]
    observed_transfer = ["5.00"]
    observed_withdrawal = ["2.00"]
    observed_l1_hash = [""]
    observed_deposit = ["3.00"]
    observed_confirmation = [""]
    observed_interest = ["0.07"]

    def post(url, body, headers):
        calls.append(url)
        if url == AUTH_URL:
            return httpx.Response(
                200, json={"status": "success", "sub_account_id": SUB},
                headers={"Set-Cookie": "gravity=testnet-cookie-123; Path=/",
                         "X-Grvt-Account-Id": "0x" + "a" * 40},
                request=httpx.Request("POST", url),
            )
        if url.endswith("/account_summary"):
            return httpx.Response(200, json={"result": {
                "sub_account_id": SUB, "event_time": str(NOW_NS),
                "settle_currency": "USDT", "margin_type": "SIMPLE_CROSS_MARGIN",
                "sub_account_mode": "SINGLE_ASSET_MODE", "total_equity": "10000",
                "initial_margin": "0", "maintenance_margin": "0",
                "unrealized_pnl": "0", "positions": [],
                "spot_balances": [{"currency": "USDT", "balance": "10000"}]}},
                request=httpx.Request("POST", url))
        if url.endswith("/open_orders"):
            return httpx.Response(200, json={"result": []},
                                  request=httpx.Request("POST", url))
        if url == ORDER_URL:
            raise httpx.ReadTimeout("ambiguous")
        if url == FILL_HISTORY_URL:
            return httpx.Response(200, json={"result": [{
                "sub_account_id": SUB, "order_id": "0x123abc",
                "client_order_id": CID, "trade_id": "trade-1",
                "instrument": "BTC_USDT_Perp", "is_buyer": True,
                "size": traded_size, "price": "50000",
                "fee": observed_fee[0], "fee_currency": "USDT",
                "realized_pnl": "0", "builder_fee": "0",
                "is_taker": True, "is_liquidation": False,
                "event_time": str(NOW_NS),
            }, {
                "sub_account_id": SUB, "order_id": "0xother",
                "client_order_id": str(2**63 + 124), "trade_id": "other-1",
                "instrument": "ETH_USDT_Perp", "is_buyer": False,
                "size": "0.1", "price": "3000", "fee": external_fee[0],
                "fee_currency": "USDT", "realized_pnl": "0",
                "builder_fee": "0", "is_taker": True,
                "is_liquidation": False, "event_time": str(NOW_NS),
            }], "next": ""}, request=httpx.Request("POST", url))
        if url == FUNDING_HISTORY_URL:
            return httpx.Response(200, json={"result": [{
                "sub_account_id": SUB, "tx_id": "funding-1",
                "instrument": "BTC_USDT_Perp", "currency": "USDT",
                "amount": observed_funding[0], "event_time": str(NOW_NS),
            }], "next": ""}, request=httpx.Request("POST", url))
        if url == TRANSFER_HISTORY_URL:
            return httpx.Response(200, json={"result": [{
                "tx_id": "transfer-1", "event_time": str(NOW_NS),
                "from_sub_account_id": "0", "to_sub_account_id": SUB,
                "currency": "USDT", "num_tokens": observed_transfer[0],
                "from_account_id": "0xDoNotArchiveFullAddress",
                "to_account_id": "0xDoNotArchiveOtherAddress",
                "signature": {"signer": "0xDoNotArchiveSigner"},
            }], "next": ""}, request=httpx.Request("POST", url))
        if url == WITHDRAWAL_HISTORY_URL:
            return httpx.Response(200, json={"result": [{
                "tx_id": "withdrawal-1", "event_time": str(NOW_NS),
                "from_account_id": SUB, "currency": "USDT",
                "num_tokens": observed_withdrawal[0], "l_1_hash": observed_l1_hash[0],
                "to_eth_address": "0xDoNotArchiveDestination",
                "signature": {"signer": "0xDoNotArchiveSigner"},
            }], "next": ""}, request=httpx.Request("POST", url))
        if url == DEPOSIT_HISTORY_URL:
            return httpx.Response(200, json={"result": [{
                "l_1_hash": "0xabcdef1234567890",
                "l_2_hash": "0xDoNotArchiveL2Hash",
                "to_account_id": "0x" + "a" * 40,
                "from_address": "0xDoNotArchiveSourceWallet",
                "currency": "USDT", "num_tokens": observed_deposit[0],
                "initiated_time": str(NOW_NS),
                "confirmed_time": observed_confirmation[0],
            }], "next": ""}, request=httpx.Request("POST", url))
        if url == INTEREST_HISTORY_URL:
            return httpx.Response(200, json={"result": [{
                "event_time": str(NOW_NS), "charge_time": str(NOW_NS),
                "sub_account_id": SUB, "currency": "USDT",
                "amount": observed_interest[0], "borrowed_amount": "10",
            }], "next": ""}, request=httpx.Request("POST", url))
        assert url == LOOKUP_URL
        body = {**order(), "order_id": "0x123abc",
                "metadata": {"client_order_id": CID,
                                 "create_time": str(NOW_NS)},
                "state": {"status": terminal_status,
                          "traded_size": [traded_size],
                          "update_time": str(NOW_NS)}}
        return httpx.Response(200, json={"result": body},
                              request=httpx.Request("POST", url))

    def sender():
        return GrvtTestnetOrderClient("testnet-key-123", http_post=post,
                                      clock_ns=lambda: NOW_NS)

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

    vault = vault_at(path)
    client = sender()
    with pytest.raises(GrvtTestnetOrderError, match="desconocido"):
        submit_testnet_once(vault, client, order(), limits(),
                            clock_ns=lambda: NOW_NS, public_post=public_post)
    vault.lock()
    vault = vault_at(path)
    new_client = sender()
    with pytest.raises(GrvtTestnetJournalError, match="no preparada"):
        submit_testnet_once(vault, new_client, order(), limits(),
                            clock_ns=lambda: NOW_NS, public_post=public_post)
    stop_testnet_subaccount(vault, SUB, now=NOW)
    claim_testnet_cancel(vault, SUB, CID, now=NOW)
    observed = observe_testnet_order(vault, new_client, SUB, CID,
                                     clock_ns=lambda: NOW_NS)
    assert observed.status == "OBSERVED" and observed.external_status == terminal_status
    cancel_audit = audit_testnet_cancel(vault, new_client, SUB, CID,
                                        clock_ns=lambda: NOW_NS)
    assert cancel_audit.cancellation.external_status == terminal_status
    assert cancel_audit.cancellation.status == "AUDITED"
    recovery = read_testnet_recovery(vault, SUB)
    assert recovery.stopped and not recovery.can_submit
    assert recovery.items[0].current_state_audited is True
    assert recovery.items[0].fill_summary["fill_count"] == 1
    assert recovery.items[0].fill_summary["weighted_average_price"] == "50000.000000000000000000"
    assert recovery.items[0].fill_summary["fees_by_currency"] == [["USDT", "0.25"]]
    assert recovery.items[0].fill_summary["causal_proof"] is False
    assert recovery.items[0].next_check == "REVIEW_AUDIT_FINDINGS_NO_RELEASE"
    assert set(recovery.items[0].current_audit_findings) >= {
        "EXTERNAL_FILLS", "FUNDING_ACTIVITY", "TRANSFER_ACTIVITY",
        "WITHDRAWAL_ACTIVITY", "DEPOSIT_CONTEXT", "INTEREST_ACTIVITY",
    }
    audited = cancel_audit.audit
    assert audited.record.status == "OBSERVED"
    assert audited.fills.sizes_match is True
    assert audited.fills.filled_size == Decimal(traded_size)
    assert len(audited.fills.other_fills) == 1
    assert audited.funding.paid_by_currency == (("USDT", Decimal("0.03")),)
    assert audited.transfers.net_by_currency == (("USDT", Decimal("5.00")),)
    assert audited.withdrawals.withdrawals[0].status == "PENDING"
    assert audited.deposits.deposits[0].status == "PENDING"
    assert not audited.deposits.attributed_to_sub_account
    assert audited.interest.reported_by_currency == (("USDT", Decimal("0.07")),)
    assert audited.fills.complete_for_execution is False
    assert audited.account.settle_balance == Decimal("10000")
    assert audited.account.complete_for_execution is False
    assert audited.evidence.complete_for_execution is False
    archived = list_testnet_audits(vault, SUB, CID)
    assert len(archived) == 1
    assert archived[0]["order"]["status"] == terminal_status
    assert archived[0]["order"]["size"] == "0.01"
    assert archived[0]["order"]["traded_size"] == traded_size
    assert archived[0]["fills"]["rows"][0]["size"] == traded_size
    assert archived[0]["fills"]["rows"][0]["trade_id"] == "trade-1"
    assert archived[0]["fills"]["external_rows"][0]["trade_id"] == "other-1"
    assert archived[0]["fills"]["external_activity_detected"] is True
    assert archived[0]["funding"]["rows"][0]["tx_id"] == "funding-1"
    assert archived[0]["transfers"]["rows"][0]["direction"] == "IN"
    assert archived[0]["withdrawals"]["rows"][0]["amount"] == "2.00"
    assert archived[0]["deposits"]["rows"][0]["amount"] == "3.00"
    assert archived[0]["deposits"]["attributed_to_sub_account"] is False
    assert archived[0]["interest"]["sign_semantics_verified"] is False
    assert archived[0]["interest"]["rows"][0]["borrowed_amount"] == "10"
    assert "DoNotArchive" not in str(archived)
    assert archived[0]["account"]["settle_balance"] == "10000"
    assert archived[0]["comparison"]["available"] is True
    assert archived[0]["comparison"]["position_delta_matches"] is False
    assert archived[0]["comparison"]["funding_paid_settle"] == "0.03"
    assert archived[0]["comparison"]["transfer_net_settle"] == "5.00"
    assert archived[0]["comparison"]["withdrawal_out_settle_observed"] == "2.00"
    assert "deposit_net_settle" not in archived[0]["comparison"]
    assert "interest_adjusted_settle" not in archived[0]["comparison"]
    assert archived[0]["comparison"]["causal_proof"] is False
    assert archived[0]["complete_for_execution"] is False
    duplicate = audit_testnet_intent(vault, new_client, SUB, CID,
                                     clock_ns=lambda: NOW_NS)
    assert duplicate.evidence.id == audited.evidence.id
    assert len(list_testnet_audits(vault, SUB, CID)) == 1
    observed_fee[0] = "0.30"
    with pytest.raises(GrvtTestnetEvidenceError, match="cambiaron"):
        audit_testnet_intent(vault, new_client, SUB, CID,
                             clock_ns=lambda: NOW_NS)
    observed_fee[0] = "0.25"
    external_fee[0] = "0.11"
    with pytest.raises(GrvtTestnetEvidenceError, match="cambiaron"):
        audit_testnet_intent(vault, new_client, SUB, CID,
                             clock_ns=lambda: NOW_NS)
    external_fee[0] = "0.10"
    observed_funding[0] = "0.04"
    with pytest.raises(GrvtTestnetEvidenceError, match="cambiaron"):
        audit_testnet_intent(vault, new_client, SUB, CID,
                             clock_ns=lambda: NOW_NS)
    observed_funding[0] = "0.03"
    observed_transfer[0] = "5.01"
    with pytest.raises(GrvtTestnetEvidenceError, match="cambiaron"):
        audit_testnet_intent(vault, new_client, SUB, CID,
                             clock_ns=lambda: NOW_NS)
    observed_transfer[0] = "5.00"
    observed_withdrawal[0] = "2.01"
    with pytest.raises(GrvtTestnetEvidenceError, match="cambiaron"):
        audit_testnet_intent(vault, new_client, SUB, CID,
                             clock_ns=lambda: NOW_NS)
    observed_withdrawal[0] = "2.00"
    observed_l1_hash[0] = "0xFinalizedButNeverArchived"
    finalized = audit_testnet_intent(vault, new_client, SUB, CID,
                                     clock_ns=lambda: NOW_NS)
    assert finalized.evidence.id != audited.evidence.id
    archived = list_testnet_audits(vault, SUB, CID)
    assert len(archived) == 2
    assert any(row["withdrawals"]["rows"][0]["status"] == "FINALIZED"
               for row in archived)
    assert "FinalizedButNeverArchived" not in str(archived)
    observed_l1_hash[0] = ""
    with pytest.raises(GrvtTestnetEvidenceError, match="cambiaron"):
        audit_testnet_intent(vault, new_client, SUB, CID,
                             clock_ns=lambda: NOW_NS)
    observed_l1_hash[0] = "0xFinalizedButNeverArchived"
    observed_deposit[0] = "3.01"
    with pytest.raises(GrvtTestnetEvidenceError, match="cambiaron"):
        audit_testnet_intent(vault, new_client, SUB, CID,
                             clock_ns=lambda: NOW_NS)
    observed_deposit[0] = "3.00"
    observed_confirmation[0] = str(NOW_NS)
    confirmed_deposit = audit_testnet_intent(vault, new_client, SUB, CID,
                                             clock_ns=lambda: NOW_NS)
    assert confirmed_deposit.evidence.id != finalized.evidence.id
    archived = list_testnet_audits(vault, SUB, CID)
    assert len(archived) == 3
    assert any(row["deposits"]["rows"][0]["status"] == "CONFIRMED"
               for row in archived)
    assert "DoNotArchive" not in str(archived)
    observed_confirmation[0] = ""
    with pytest.raises(GrvtTestnetEvidenceError, match="cambiaron"):
        audit_testnet_intent(vault, new_client, SUB, CID,
                             clock_ns=lambda: NOW_NS)
    observed_confirmation[0] = str(NOW_NS)
    observed_interest[0] = "0.08"
    with pytest.raises(GrvtTestnetEvidenceError, match="cambiaron"):
        audit_testnet_intent(vault, new_client, SUB, CID,
                             clock_ns=lambda: NOW_NS)
    assert calls.count(ORDER_URL) == 1 and calls.count(LOOKUP_URL) == 14
    assert calls.count(FILL_HISTORY_URL) == 13
    assert calls.count(FUNDING_HISTORY_URL) == 13
    assert calls.count(TRANSFER_HISTORY_URL) == 13
    assert calls.count(WITHDRAWAL_HISTORY_URL) == 13
    assert calls.count(DEPOSIT_HISTORY_URL) == 13
    assert calls.count(INTEREST_HISTORY_URL) == 13
    with vault.session() as db:
        assert db.connection().exec_driver_sql("SELECT count(*) FROM real_events").scalar_one() == 0
    vault.lock()
    vault = vault_at(path)
    assert list_testnet_audits(vault, SUB, CID) == archived
    assert read_testnet_preflight(vault, SUB, CID)["allowed"] is True
    assert b"trade-1" not in path.read_bytes()
    later = observation(status=terminal_status, traded=traded_size,
                        update_ns=NOW_NS + 1)
    record_testnet_observation(vault, later, now=NOW + timedelta(seconds=1))
    record_testnet_cancel_observation(vault, later,
                                       now=NOW + timedelta(seconds=1))
    changed = read_testnet_recovery(vault, SUB)
    assert changed.items[0].cancel_status == "RESPONSE_OBSERVED"
    assert changed.items[0].current_state_audited is False
    assert changed.items[0].next_check == "AUDIT_AFTER_CANCEL"
    with vault.session() as db:
        db.connection().exec_driver_sql(
            "UPDATE grvt_testnet_audit_evidence SET payload_json = '{}'"
        )
        db.commit()
    with pytest.raises(GrvtTestnetEvidenceError, match="alterada"):
        list_testnet_audits(vault, SUB, CID)
    vault.lock()
