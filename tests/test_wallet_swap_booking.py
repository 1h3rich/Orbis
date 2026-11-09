"""Recibo DEX → revisión local → asientos exactos y enlace durable, sin fondos."""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4
from app.portfolio.vault import SCHEMA_VERSION
import pytest

from app.portfolio.domain import EventInput, realized_pnl_by_currency
from app.portfolio.repository import append_event, account_snapshot, list_events
from app.portfolio.wallet_links import save_wallet_link
from app.portfolio.wallet_swap import claim_swap, report_swap, check_swap, list_swaps
from app.portfolio.wallet_swap_booking import preview_booking, book_swap, booking_record
from app.portfolio.wallet_swap import WalletSwapError
from tests.test_wallet_swap import setup, prepare, evidence, WALLET, PASSWORD


def ready(tmp_path):
    vault, account, now = setup(tmp_path)
    with vault.session() as db:
        save_wallet_link(db, account, WALLET, "0x38", "Wallet de prueba"); db.commit()
    for item in [EventInput("CONTRIBUTION", "USDT", now-timedelta(days=1), amount=Decimal("1000")),
                 EventInput("BUY", "USDT", now-timedelta(days=1), asset="BNB", quantity=Decimal("0.01"), unit_price=Decimal("300"))]:
        with vault.session() as db:
            append_event(db, account, str(uuid4()), item)
    plan = prepare(vault, account, now)
    with vault.session() as db:
        claim_swap(db, plan["id"], account_id=account, wallet_address=WALLET, plan_digest=plan["plan_digest"], now=now)
    proof = evidence(plan, now, out=30000000000000000)
    with vault.session() as db:
        report_swap(db, plan["id"], account_id=account, tx_hash=proof["tx_hash"], now=now)
    with vault.session() as db:
        check_swap(db, plan["id"], account_id=account, evidence=proof, now=now)
    return vault, account, now, plan


def preview(vault, account, plan, **values):
    with vault.session() as db:
        return preview_booking(db, plan["id"], context_account_id=account, wallet_address=WALLET, **values)


def book(vault, account, now, plan, proposed, **values):
    with vault.session() as db:
        return book_swap(db, plan["id"], context_account_id=account, wallet_address=WALLET,
            expected_revision=proposed["revision"], preview_digest=proposed["preview_digest"], now=now, **values)


def test_confirmed_buy_records_exact_countervalue_and_native_gas_once_across_reopen(tmp_path):
    vault, account, now, plan = ready(tmp_path)
    proposed = preview(vault, account, plan)
    assert proposed["available"] and proposed["target_account_id"] == account
    assert proposed["trade"]["kind"] == "BUY" and proposed["trade"]["quantity"] == "0.03"
    assert proposed["trade"]["gross_amount"] == "12.345678901234567891"
    assert proposed["gas_amount"] == "0.0001"
    assert not proposed["booked"]
    result = book(vault, account, now, plan, proposed)
    assert result["booked"] and result["events_created"] == 2
    with vault.session() as db:
        snapshot = account_snapshot(db, account)
        assert snapshot.cash["USDT"] == Decimal("984.654321098765432109")
        assert snapshot.positions[("WBNB", "USDT")].quantity == Decimal("0.03")
        assert snapshot.positions[("WBNB", "USDT")].cost_basis == Decimal("12.345678901234567891")
        assert snapshot.positions[("BNB", "USDT")].quantity == Decimal("0.0099")
        assert realized_pnl_by_currency(snapshot)["USDT"] == Decimal("-0.03")
        assert snapshot.contributions == {"USDT": Decimal("1000")} and snapshot.withdrawals == {}
        assert len(list_events(db, account)) == 4
        assert list_swaps(db, account, now=now)[0]["booked"]
    vault.lock(); vault.unlock(PASSWORD)
    assert book(vault, account, now, plan, proposed)["booked"]
    with vault.session() as db:
        assert len(list_events(db, account)) == 4
        assert booking_record(db, plan["id"]) is not None
    vault.backup(tmp_path / "booked-swap.db")
    vault.lock()


def settle(vault, account, now, *, nonce, token_in="USDT", amount="1", out=20000000000000000, action="SWAP", success=True):
    from app.portfolio.wallet_swap_receipt import APPROVAL
    from app.portfolio.wallet_swap import TOKENS, ROUTER
    plan = prepare(vault, account, now, nonce=str(nonce), token_in=token_in, amount=amount, action=action)
    with vault.session() as db:
        claim_swap(db, plan["id"], account_id=account, wallet_address=WALLET, plan_digest=plan["plan_digest"], now=now)
    proof = evidence(plan, now, out=out, success=success)
    if success:
        logs = proof["receipt"]["logs"]
        logs[0]["address"], logs[1]["address"] = TOKENS[token_in], TOKENS["WBNB" if token_in == "USDT" else "USDT"]
        if action != "SWAP":
            approved = plan["amount_units"] if action == "APPROVE" else "0"
            proof["pool"], proof["post_allowance"] = None, "0x"+hex(int(approved))[2:].zfill(64)
            logs[0]["topics"] = [APPROVAL, "0x"+WALLET[2:].zfill(64), "0x"+ROUTER[2:].zfill(64)]
            logs[0]["data"] = proof["post_allowance"]
            proof["receipt"]["logs"] = [logs[0]]
    tx_hash = "0x"+hex(nonce)[2:].zfill(64)
    proof["tx_hash"], proof["transaction"]["hash"], proof["receipt"]["transactionHash"] = tx_hash, tx_hash, tx_hash
    for log in proof["receipt"]["logs"]:
        log["transactionHash"] = tx_hash
    with vault.session() as db:
        report_swap(db, plan["id"], account_id=account, tx_hash=tx_hash, now=now)
    with vault.session() as db:
        check_swap(db, plan["id"], account_id=account, evidence=proof, now=now)
    return plan


def test_sell_uses_acquired_units_exact_usdt_proceeds_and_separate_native_gas(tmp_path):
    vault, account, now, bought = ready(tmp_path)
    book(vault, account, now, bought, preview(vault, account, bought))
    sold = settle(vault, account, now+timedelta(seconds=1), nonce=4, token_in="WBNB", amount="0.03", out=13210000000000000000)
    book(vault, account, now+timedelta(seconds=1), sold, preview(vault, account, sold))
    with vault.session() as db:
        snapshot = account_snapshot(db, account)
        assert snapshot.cash["USDT"] == Decimal("997.864321098765432109")
        assert snapshot.positions[("WBNB", "USDT")].quantity == 0
        assert snapshot.positions[("WBNB", "USDT")].cost_basis == 0
        assert realized_pnl_by_currency(snapshot)["USDT"] == Decimal("0.804321098765432109")
        assert snapshot.positions[("BNB", "USDT")].quantity == Decimal("0.0098")
        assert snapshot.contributions["USDT"] == Decimal("1000") and not snapshot.withdrawals
    vault.lock()


@pytest.mark.parametrize("action,success", [("APPROVE", True), ("REVOKE", True), ("SWAP", False)])
def test_approval_revocation_or_reverted_transaction_only_expenses_gas(tmp_path, action, success):
    vault, account, now, _bought = ready(tmp_path)
    plan = settle(vault, account, now+timedelta(seconds=1), nonce=4, action=action, success=success)
    proposed = preview(vault, account, plan)
    assert proposed["trade"] is None
    result = book(vault, account, now+timedelta(seconds=1), plan, proposed)
    assert result["events_created"] == 1
    with vault.session() as db:
        snapshot = account_snapshot(db, account)
        assert snapshot.cash["USDT"] == Decimal("997")
        assert ("WBNB", "USDT") not in snapshot.positions
        assert snapshot.positions[("BNB", "USDT")].quantity == Decimal("0.0099")
    vault.lock()


def test_existing_manual_trade_and_gas_are_linked_not_duplicated(tmp_path):
    from datetime import datetime
    vault, account, now, plan = ready(tmp_path)
    proposed = preview(vault, account, plan)
    at = datetime.fromisoformat(proposed["trade"]["occurred_at"])
    manual = EventInput("BUY", "USDT", at+timedelta(seconds=30), asset="WBNB", quantity=Decimal("0.03"),
        unit_price=Decimal("400"), gross_override=Decimal("12.345678901234567891"), note="Compra ya anotada")
    gas = EventInput("FEE", "USDT", at+timedelta(seconds=30), asset="BNB", quantity=Decimal("0.0001"), note="Gas ya anotado")
    with vault.session() as db:
        trade_id = append_event(db, account, str(uuid4()), manual).id
    with vault.session() as db:
        gas_id = append_event(db, account, str(uuid4()), gas).id
    proposed = preview(vault, account, plan)
    assert not proposed["available"]
    assert proposed["trade_candidates"][0]["id"] == trade_id
    assert proposed["gas_candidates"][0]["id"] == gas_id
    selected = dict(trade_event_id=trade_id, gas_event_ids=[gas_id])
    proposed = preview(vault, account, plan, **selected)
    assert proposed["available"]
    assert book(vault, account, now+timedelta(minutes=1), plan, proposed, **selected)["events_created"] == 0
    with vault.session() as db:
        assert len(list_events(db, account)) == 4
        assert booking_record(db, plan["id"]) is not None
    vault.backup(tmp_path / "manual-linked.db")
    vault.lock()


def test_changed_ledger_or_missing_cash_blocks_booking_without_partial_events(tmp_path):
    vault, account, now, plan = ready(tmp_path)
    proposed = preview(vault, account, plan)
    with vault.session() as db:
        append_event(db, account, str(uuid4()), EventInput("CONTRIBUTION", "EUR", now-timedelta(days=1), amount=Decimal("1")))
    with pytest.raises(WalletSwapError, match="cambió"):
        book(vault, account, now, plan, proposed)
    with vault.session() as db:
        assert booking_record(db, plan["id"]) is None
        assert len(list_events(db, account)) == 3
    # No crear una aportación para cubrir un saldo real no registrado.
    other = settle(vault, account, now+timedelta(seconds=1), nonce=4, amount="999")
    with pytest.raises(WalletSwapError, match="Falta USDT"):
        preview(vault, account, other)
    vault.lock()


def test_gas_uses_cash_then_inventory_without_converting_between_currencies(tmp_path):
    vault, account, now, plan = ready(tmp_path)
    with vault.session() as db:
        append_event(db, account, str(uuid4()), EventInput("CONTRIBUTION", "BNB", now-timedelta(days=1), amount=Decimal("0.00004")))
    proposed = preview(vault, account, plan)
    assert len(proposed["gas_items"]) == 2
    result = book(vault, account, now, plan, proposed)
    assert result["events_created"] == 3
    with vault.session() as db:
        snapshot = account_snapshot(db, account)
        assert snapshot.cash["BNB"] == 0
        assert snapshot.positions[("BNB", "USDT")].quantity == Decimal("0.00994")
        assert realized_pnl_by_currency(snapshot)["BNB"] == Decimal("-0.00004")
        assert realized_pnl_by_currency(snapshot)["USDT"] == Decimal("-0.018")
    vault.lock()


def test_context_exchange_book_is_not_used_as_wallet_owner_or_created_capital(tmp_path):
    from app.portfolio.repository import create_account
    from app.portfolio.wallet_links import remove_wallet_link
    vault, account, now, plan = ready(tmp_path)
    with vault.session() as db:
        wallet_account = create_account(db, "Mi wallet", "USDT").id
    with vault.session() as db:
        from app.portfolio.models import RealMexcAccountLink
        db.add(RealMexcAccountLink(account_ref="e"*64, account_id=account, created_at=now)); db.commit()
    with vault.session() as db:
        from app.portfolio.models import RealWalletLink
        link = db.query(RealWalletLink).one()
        remove_wallet_link(db, account, link.id); db.commit()
    with vault.session() as db:
        save_wallet_link(db, wallet_account, WALLET, "0x38", "Otra cartera"); db.commit()
    for item in [EventInput("CONTRIBUTION", "USDT", now-timedelta(days=1), amount=Decimal("100")),
                 EventInput("CONTRIBUTION", "BNB", now-timedelta(days=1), amount=Decimal("0.01"))]:
        with vault.session() as db:
            append_event(db, wallet_account, str(uuid4()), item)
    proposed = preview(vault, account, plan)
    assert proposed["target_account_id"] == wallet_account
    book(vault, account, now, plan, proposed)
    with vault.session() as db:
        assert len(list_events(db, account)) == 2
        assert len(list_events(db, wallet_account)) == 4
    vault.lock()


def test_failed_booking_rolls_back_trade_fee_and_evidence_together(tmp_path):
    from sqlalchemy.exc import IntegrityError
    vault, account, now, plan = ready(tmp_path)
    proposed = preview(vault, account, plan)
    with vault.session() as db:
        db.connection().exec_driver_sql("CREATE TRIGGER reject_booking BEFORE INSERT ON real_wallet_swap_booking_events BEGIN SELECT RAISE(ABORT, 'fixture rollback'); END")
        db.commit()
    with pytest.raises(IntegrityError):
        book(vault, account, now, plan, proposed)
    with vault.session() as db:
        assert booking_record(db, plan["id"]) is None
        assert len(list_events(db, account)) == 2
    vault.lock()


def test_two_vault_connections_book_same_action_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.portfolio.vault import RealVault
    vault, account, now, plan = ready(tmp_path)
    proposed = preview(vault, account, plan)
    barrier = Barrier(2)
    def worker():
        other = RealVault(vault.path); other.unlock(PASSWORD)
        try:
            barrier.wait(timeout=10)
            return book(other, account, now, plan, proposed)
        finally:
            other.lock()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: worker(), range(2)))
    assert sum(result["events_created"] for result in results) == 2
    with vault.session() as db:
        assert len(list_events(db, account)) == 4
    vault.lock()


def test_changed_booked_fee_is_rejected_by_reads_and_backup_even_after_event_digest_rehash(tmp_path):
    import json
    from app.portfolio.models import RealEvent, RealWalletSwapBooking
    from app.portfolio.repository import _digest, _from_row
    from app.portfolio.vault import VaultError
    from app.portfolio.wallet_funding import _encoded
    vault, account, now, plan = ready(tmp_path)
    book(vault, account, now, plan, preview(vault, account, plan))
    with vault.session() as db:
        row = db.get(RealWalletSwapBooking, plan["id"])
        payload = json.loads(row.payload_json)
        ref = next(item for item in payload["events"] if item["role"] == "GAS")
        fee = db.get(RealEvent, ref["id"])
        fee.quantity = Decimal("0.00009")
        fee.payload_digest = _digest(_from_row(fee).input)
        ref["digest"] = fee.payload_digest
        row.payload_json, row.payload_digest = _encoded(payload)
        db.commit()
    with vault.session() as db:
        with pytest.raises(WalletSwapError):
            booking_record(db, plan["id"])
    with pytest.raises(VaultError, match="swap booking"):
        vault.backup(tmp_path / "bad-booking.db")
    vault.lock()


def test_book_replay_remains_idempotent_after_wallet_is_unlinked(tmp_path):
    from app.portfolio.models import RealWalletLink
    from app.portfolio.wallet_links import remove_wallet_link
    vault, account, now, plan = ready(tmp_path)
    proposed = preview(vault, account, plan)
    book(vault, account, now, plan, proposed)
    with vault.session() as db:
        remove_wallet_link(db, account, db.query(RealWalletLink).one().id); db.commit()
    assert book(vault, account, now, plan, proposed)["booked"]
    vault.backup(tmp_path / "unlinked-valid.db")
    vault.lock()


def test_v32_migration_preserves_unbooked_receipts_and_account_history(tmp_path):
    vault, account, now, plan = ready(tmp_path)
    with vault.session() as db:
        db.connection().exec_driver_sql("DROP TABLE real_wallet_swap_booking_events")
        db.connection().exec_driver_sql("DROP TABLE real_wallet_swap_bookings")
        db.connection().exec_driver_sql("PRAGMA user_version = 32")
        db.commit()
    vault.lock(); vault.unlock(PASSWORD)
    assert vault.last_migration_backup and vault.last_migration_backup.exists()
    with vault.session() as db:
        assert db.connection().exec_driver_sql("PRAGMA user_version").scalar_one() == SCHEMA_VERSION
        assert len(list_events(db, account)) == 2
        assert not list_swaps(db, account, now=now)[0]["booked"]
    assert preview(vault, account, plan)["available"]
    vault.lock()
