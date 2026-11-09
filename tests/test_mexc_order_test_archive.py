"""La bóveda conserva ensayos cifrados y los verifica al respaldar/migrar."""

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from app.portfolio.vault import SCHEMA_VERSION
import pytest

from app.portfolio.vault import RealVault, VaultError
from app.portfolio.mexc_order_test_archive import claim_attempt
from app.live_execution.journal import _stored_policy
from app.live_execution.policy import LiveSpotIntent
from tests.test_mexc_draft_order_test_api import setup
from tests.test_mexc_policy_drafts_api import PASSPHRASE


def test_v21_migrates_order_test_table_with_encrypted_preimage(tmp_path):
    path = tmp_path / "real.db"
    vault = RealVault(path)
    vault.unlock(PASSPHRASE, create=True)
    with vault.session() as db:
        db.connection().exec_driver_sql("DROP TABLE real_mexc_order_test_attempts")
        db.connection().exec_driver_sql("PRAGMA user_version = 21")
        db.commit()
    vault.lock()
    reopened = RealVault(path)
    reopened.unlock(PASSPHRASE)
    with reopened.session() as db:
        assert db.connection().exec_driver_sql("PRAGMA user_version").scalar_one() == SCHEMA_VERSION
        assert db.connection().exec_driver_sql(
            "SELECT count(*) FROM real_mexc_order_test_attempts").scalar_one() == 0
    backup = reopened.last_migration_backup
    assert backup is not None and backup.exists()
    assert backup.read_bytes()[:16] != b"SQLite format 3\x00"
    reopened.lock()


def test_backup_rejects_tampered_order_test_attempt(tmp_path):
    client, headers, endpoint, _, path = setup(tmp_path)
    assert client.post("/real/exchanges/mexc/trade/connect", json={
        "api_key": "draft-trade-access", "secret_key": "draft-trade-secret"},
        headers=headers).status_code == 200
    response = client.post(endpoint, json={
        "request_id": str(uuid4()), "quantity_base": "0.001",
        "limit_price_quote": "100000", "confirmation": "PROBAR_SIN_EJECUTAR",
    }, headers=headers)
    assert response.status_code == 200, response.text
    vault = RealVault(path)
    vault.unlock(PASSPHRASE)
    with vault.session() as db:
        db.connection().exec_driver_sql(
            "UPDATE real_mexc_order_test_attempts SET quantity_base = '0.002'")
        db.commit()
    with pytest.raises(VaultError, match="invalid MEXC order-test"):
        vault.backup(Path(tmp_path / "tampered.db"))
    vault.lock()


def test_claimed_attempt_after_interruption_never_reposts(tmp_path):
    client, headers, endpoint, calls, path = setup(tmp_path)
    account_id = endpoint.split("/accounts/")[1].split("/")[0]
    policy_id = endpoint.split("/mexc-policy-drafts/")[1].split("/")[0]
    attempt_id = str(uuid4())
    vault = RealVault(path)
    vault.unlock(PASSPHRASE)
    with vault.session() as db:
        policy = _stored_policy(db, policy_id)
        intent = LiveSpotIntent(
            intent_id=attempt_id, policy_id=policy.policy_id,
            strategy_version_id=policy.strategy_version_id,
            account_ref=policy.account_ref, symbol=policy.symbol, side="BUY",
            quantity_base=Decimal("0.001"), limit_price_quote=Decimal("100000"),
            estimated_fee_quote=Decimal("0.1"), signal_at=datetime.now(timezone.utc))
        claimed, created = claim_attempt(db, account_id, policy, intent,
                                         now=datetime.now(timezone.utc))
        assert created and claimed["status"] == "CLAIMED"
    vault.lock()
    replay = client.post(endpoint, headers=headers, json={
        "request_id": attempt_id, "quantity_base": "0.001",
        "limit_price_quote": "100000", "confirmation": "PROBAR_SIN_EJECUTAR"})
    assert replay.status_code == 200
    assert replay.json()["status"] == "CLAIMED"
    assert replay.json()["replayed_without_post"] is True
    assert calls == []
