"""Cancelación de grupo explícita: HTTP ficticio, firma y SQLCipher reales."""

from datetime import timedelta

import httpx
from app.portfolio.vault import SCHEMA_VERSION
import pytest

from app.live_execution.grvt_testnet_journal import stop_testnet_subaccount
from app.live_execution.grvt_testnet_oco import read_oco, GrvtTestnetOcoError
from app.live_execution.grvt_testnet_order import GrvtTestnetOrderClient
from tests.test_grvt_testnet_oco import sender_case
from tests.test_grvt_testnet_journal import NOW, SUB


def cancel_case(tmp_path, *, stopped=True):
    case = sender_case(tmp_path)
    case.send()
    if stopped:
        stop_testnet_subaccount(case.vault, SUB, now=NOW+timedelta(seconds=10))
    case.cancel_requests = []

    def cancelled_states():
        states = case.settings.get("states",{})
        return {i:(status if status in ("FILLED","REJECTED","CANCELLED") else "CANCELLED",size)
                for i,(status,size) in ((i,states.get(i,("OPEN","0"))) for i in range(2))}

    def post(url, body, headers):
        if url == case.bulk_url and body.get("orders") == []:
            # Una segunda conexión ve la reserva antes de abrir la red.
            saved = read_oco(case.second, case.group_id)["cancel_attempt"]
            assert saved["status"] == "CLAIMED"
            case.cancel_requests.append(body)
            if case.settings.get("cancel_lost_reply"):
                case.settings["states"] = cancelled_states()
                raise httpx.ReadTimeout("untrusted-secret-in-remote-error")
            if not case.settings.get("cancel_ack_only"):
                case.settings["states"] = cancelled_states()
            if case.settings.get("cancel_bad_followup"):
                case.settings["bad_trigger"] = True
            case.clock[0] += case.settings.get("cancel_clock_shift_ns",0)
            return httpx.Response(200,json={"result":{"orders":[],"cancel_acks":[{"ack":True}]}})
        return case.http_post(url,body,headers)

    case.cancel_client = GrvtTestnetOrderClient("fake-testnet-key",http_post=post,clock_ns=lambda:case.clock[0])
    case.cancel_http_post = post
    return case


def test_explicit_cancel_persists_before_bulk_and_observes_both_without_financial_release(tmp_path):
    from app.live_execution.grvt_testnet_oco_cancel import cancel_oco_once

    case = cancel_case(tmp_path)
    result = cancel_oco_once(case.vault,case.cancel_client,case.group_id,
        acknowledge_unprotected_position=True,clock_ns=lambda:case.clock[0])
    first_id = result["legs"][0]["client_order_id"]
    assert case.cancel_requests == [{"sub_account_id":SUB,"orders":[],"order_i_ds":[],
        "client_order_i_ds":[first_id],"time_to_live_ms":"5000"}]
    assert result["cancel_attempt"]["status"] == "ACKNOWLEDGED"
    assert [row["status"] for row in result["last_observation"]["orders"]] == ["CANCELLED","CANCELLED"]
    assert result["status"] == "ACKNOWLEDGED" and result["can_submit"] is False
    assert result["protection_guaranteed"] is False


def test_cancel_ack_does_not_prove_terminal_state_or_allow_another_cancel(tmp_path):
    from app.live_execution.grvt_testnet_oco_cancel import cancel_oco_once
    from app.live_execution.grvt_testnet_journal import require_new_testnet_submission, GrvtTestnetJournalError
    from tests.test_grvt_testnet_journal import CID
    case = cancel_case(tmp_path)
    case.settings["cancel_ack_only"] = True
    result = cancel_oco_once(case.vault,case.cancel_client,case.group_id,
        acknowledge_unprotected_position=True,clock_ns=lambda:case.clock[0])
    assert [row["status"] for row in result["last_observation"]["orders"]] == ["OPEN","OPEN"]
    assert result["cancel_attempt"]["financially_reconciled"] is False
    with pytest.raises(GrvtTestnetOcoError,match="consumida"):
        cancel_oco_once(case.vault,case.cancel_client,case.group_id,
            acknowledge_unprotected_position=True,clock_ns=lambda:case.clock[0])
    assert len(case.cancel_requests) == 1
    with pytest.raises(GrvtTestnetJournalError):
        require_new_testnet_submission(case.vault,SUB,str(int(CID)+1))


def test_lost_cancel_reply_survives_restart_and_only_allows_observation(tmp_path):
    from app.live_execution.grvt_testnet_oco_cancel import cancel_oco_once
    from app.live_execution.grvt_testnet_oco_observation import observe_oco_orders
    from app.live_execution.grvt_testnet_order import GrvtTestnetOrderError
    from tests.test_grvt_testnet_journal import vault_at
    case = cancel_case(tmp_path)
    case.settings["cancel_lost_reply"] = True
    with pytest.raises(GrvtTestnetOrderError,match="desconocido") as error:
        cancel_oco_once(case.vault,case.cancel_client,case.group_id,
            acknowledge_unprotected_position=True,clock_ns=lambda:case.clock[0])
    assert "untrusted-secret" not in str(error.value)
    case.vault.lock()
    reopened = vault_at(tmp_path / "real.db")
    restored = read_oco(reopened,case.group_id)
    assert restored["cancel_attempt"]["status"] == "CLAIMED"
    client = GrvtTestnetOrderClient("fake-testnet-key",http_post=case.cancel_http_post,clock_ns=lambda:case.clock[0])
    with pytest.raises(GrvtTestnetOcoError,match="consumida"):
        cancel_oco_once(reopened,client,case.group_id,
            acknowledge_unprotected_position=True,clock_ns=lambda:case.clock[0])
    observed = observe_oco_orders(reopened,client,case.group_id,clock_ns=lambda:case.clock[0])
    assert [row["status"] for row in observed["orders"]] == ["CANCELLED","CANCELLED"]
    assert len(case.cancel_requests) == 1
    assert read_oco(reopened,case.group_id)["cancel_attempt"]["status"] == "CLAIMED"
    reopened.backup(tmp_path / "uncertain-cancel.db")


@pytest.mark.parametrize("consent", [False, 1, "true", None])
def test_cancel_requires_explicit_boolean_consent_before_any_external_read(tmp_path,consent):
    from app.live_execution.grvt_testnet_oco_cancel import cancel_oco_once
    case = cancel_case(tmp_path)
    before = list(case.calls)
    with pytest.raises(GrvtTestnetOcoError,match="Confirma"):
        cancel_oco_once(case.vault,case.cancel_client,case.group_id,
            acknowledge_unprotected_position=consent,clock_ns=lambda:case.clock[0])
    assert case.calls == before and case.cancel_requests == []
    assert read_oco(case.vault,case.group_id)["cancel_attempt"] is None


@pytest.mark.parametrize("states,expected_role", [
    ({0:("CANCELLED","0"),1:("OPEN","0")},"TAKE_PROFIT"),
    ({0:("OPEN","0.004"),1:("CANCELLED","0")},"STOP_LOSS"),
])
def test_cancel_targets_remaining_member_and_never_erases_partial_fill(tmp_path,states,expected_role):
    from app.live_execution.grvt_testnet_oco_cancel import cancel_oco_once
    case = cancel_case(tmp_path)
    case.settings["states"] = states
    result = cancel_oco_once(case.vault,case.cancel_client,case.group_id,
        acknowledge_unprotected_position=True,clock_ns=lambda:case.clock[0])
    target = next(leg for leg in result["legs"] if leg["role"] == expected_role)
    assert case.cancel_requests[0]["client_order_i_ds"] == [target["client_order_id"]]
    assert [row["traded_size"] for row in result["last_observation"]["orders"]] == [states[i][1] for i in range(2)]
    case.vault.backup(tmp_path / "partial-cancel.db")


def test_terminal_pair_needs_no_cancel_post_and_still_needs_financial_reconciliation(tmp_path):
    from app.live_execution.grvt_testnet_oco_cancel import cancel_oco_once
    case = cancel_case(tmp_path)
    case.settings["states"] = {0:("CANCELLED","0"),1:("FILLED","0.01")}
    result = cancel_oco_once(case.vault,case.cancel_client,case.group_id,
        acknowledge_unprotected_position=True,clock_ns=lambda:case.clock[0])
    assert case.cancel_requests == [] and result["cancel_attempt"] is None
    assert result["can_submit"] is False


def test_cancel_cannot_be_hidden_by_forging_abandoned_status(tmp_path):
    from app.live_execution.grvt_testnet_oco_cancel import cancel_oco_once
    from app.live_execution.grvt_testnet_journal import require_new_testnet_submission, GrvtTestnetJournalError
    from app.portfolio.models import GrvtTestnetOcoGroup, GrvtTestnetOcoClaim, GrvtTestnetStop
    from tests.test_grvt_testnet_journal import CID
    case = cancel_case(tmp_path)
    cancel_oco_once(case.vault,case.cancel_client,case.group_id,
        acknowledge_unprotected_position=True,clock_ns=lambda:case.clock[0])
    # Se conserva la evidencia de red pero se falsifica la cabecera como si
    # nunca se hubiese reclamado. No debe habilitar la siguiente operación.
    with case.vault.session() as db:
        db.get(GrvtTestnetOcoGroup,case.group_id).status = "ABANDONED"
        db.get(GrvtTestnetStop,SUB).stopped = False
        db.delete(db.get(GrvtTestnetOcoClaim,case.group_id)); db.commit()
    with pytest.raises(GrvtTestnetJournalError,match="OCO"):
        require_new_testnet_submission(case.vault,SUB,str(int(CID)+1))


def test_v34_migration_preserves_existing_oco_contract_and_observation(tmp_path):
    from app.live_execution.grvt_testnet_oco_observation import observe_oco_orders
    from tests.test_grvt_testnet_journal import vault_at
    case = sender_case(tmp_path); case.send()
    observe_oco_orders(case.vault,case.writer,case.group_id,clock_ns=lambda:case.clock[0])
    before = read_oco(case.vault,case.group_id)
    case.second.lock()
    with case.vault.session() as db:
        db.connection().exec_driver_sql("DROP TABLE grvt_testnet_oco_reconciliations")
        db.connection().exec_driver_sql("DROP TABLE grvt_testnet_oco_audits")
        db.connection().exec_driver_sql("DROP TABLE grvt_testnet_oco_cancels")
        db.connection().exec_driver_sql("PRAGMA user_version=34"); db.commit()
    case.vault.lock()
    reopened = vault_at(tmp_path / "real.db")
    assert reopened.last_migration_backup and reopened.last_migration_backup.exists()
    assert read_oco(reopened,case.group_id) == before
    with reopened.session() as db:
        assert db.connection().exec_driver_sql("PRAGMA user_version").scalar_one() == SCHEMA_VERSION
    reopened.backup(tmp_path / "migrated-existing-oco.db")


@pytest.mark.parametrize("fault", ["target","sequence","clock","scope","ack","ack_type"])
def test_cancel_evidence_corruption_is_rejected_even_after_rehash(tmp_path,fault):
    import hashlib
    import json
    from app.live_execution.grvt_testnet_oco_cancel import cancel_oco_once
    from app.portfolio.models import GrvtTestnetOcoCancel
    from app.portfolio.vault import VaultError
    case = cancel_case(tmp_path)
    cancel_oco_once(case.vault,case.cancel_client,case.group_id,
        acknowledge_unprotected_position=True,clock_ns=lambda:case.clock[0])
    with case.vault.session() as db:
        row = db.get(GrvtTestnetOcoCancel,case.group_id)
        is_ack = fault in ("ack","ack_type")
        column = "ack_json" if is_ack else "payload_json"
        p = json.loads(getattr(row,column))
        if fault == "target": p["target_client_order_id"] = "18446744073709551615"
        if fault == "sequence": p["observation_sequence"] += 100
        if fault == "clock": p["claimed_at_ns"] -= 10_000_000_000
        if fault == "scope": p["plan_digest"] = "f"*64
        if fault == "ack": p["cancel_digest"] = "f"*64
        if fault == "ack_type": p["acknowledgement_received"] = 1
        text = json.dumps(p,sort_keys=True,separators=(",",":"),ensure_ascii=True)
        setattr(row,column,text)
        setattr(row,"ack_digest" if is_ack else "payload_digest",hashlib.sha256(text.encode("ascii")).hexdigest())
        db.commit()
    with pytest.raises(GrvtTestnetOcoError): read_oco(case.vault,case.group_id)
    with pytest.raises(VaultError,match="OCO"): case.vault.backup(tmp_path / "bad-cancel.db")


def test_two_connections_cannot_claim_two_group_cancellations(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from app.live_execution.grvt_testnet_oco_cancel import cancel_oco_once
    from tests.test_grvt_testnet_journal import vault_at
    case = cancel_case(tmp_path)
    competing = vault_at(tmp_path / "real.db")
    def cancel(vault):
        client = GrvtTestnetOrderClient("fake-testnet-key",http_post=case.cancel_http_post,clock_ns=lambda:case.clock[0])
        try:
            return cancel_oco_once(vault,client,case.group_id,
                acknowledge_unprotected_position=True,clock_ns=lambda:case.clock[0])
        except GrvtTestnetOcoError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(cancel,(case.vault,competing)))
    assert len(case.cancel_requests) == 1
    assert sum(item is not None for item in results) == 1
    assert read_oco(competing,case.group_id)["cancel_attempt"]["status"] == "ACKNOWLEDGED"


def test_stop_is_required_before_querying_or_removing_protective_orders(tmp_path):
    from app.live_execution.grvt_testnet_oco_cancel import cancel_oco_once
    case = cancel_case(tmp_path,stopped=False)
    before = list(case.calls)
    with pytest.raises(GrvtTestnetOcoError,match="Detén"):
        cancel_oco_once(case.vault,case.cancel_client,case.group_id,
            acknowledge_unprotected_position=True,clock_ns=lambda:case.clock[0])
    assert case.calls == before and case.cancel_requests == []
    assert read_oco(case.vault,case.group_id)["cancel_attempt"] is None


@pytest.mark.parametrize("fault", ["bad_followup","clock_regressed"])
def test_received_ack_is_kept_even_if_post_cancel_observation_fails(tmp_path,fault):
    from app.live_execution.grvt_testnet_oco_cancel import cancel_oco_once
    case = cancel_case(tmp_path)
    if fault == "bad_followup": case.settings["cancel_bad_followup"] = True
    else: case.settings["cancel_clock_shift_ns"] = -1_000_000_000
    with pytest.raises(GrvtTestnetOcoError):
        cancel_oco_once(case.vault,case.cancel_client,case.group_id,
            acknowledge_unprotected_position=True,clock_ns=lambda:case.clock[0])
    result = read_oco(case.vault,case.group_id)
    assert result["cancel_attempt"]["status"] == "ACKNOWLEDGED"
    assert [row["status"] for row in result["last_observation"]["orders"]] == ["OPEN","OPEN"]
    assert len(case.cancel_requests) == 1


def test_expired_signature_does_not_prevent_cancelling_an_order_still_observed_open(tmp_path):
    from app.live_execution.grvt_testnet_oco_cancel import cancel_oco_once
    case = cancel_case(tmp_path)
    case.clock[0] += 3_601_000_000_000
    result = cancel_oco_once(case.vault,case.cancel_client,case.group_id,
        acknowledge_unprotected_position=True,clock_ns=lambda:case.clock[0])
    assert len(case.cancel_requests) == 1
    assert result["cancel_attempt"]["status"] == "ACKNOWLEDGED"
