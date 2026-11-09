"""La cartera REAL nunca usa la base de datos PAPER ni aritmética float."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.database.database import Base
from app.portfolio.domain import CALC_PRECISION, EventInput, PortfolioError, fold_events
from app.portfolio.models import RealBase
from app.portfolio.repository import (
    RequestConflict,
    account_snapshot,
    append_event,
    create_account,
    import_events,
    list_events,
    transfer_asset_between_accounts,
    transfer_cash_between_accounts,
    verify_paired_transfers,
)


NOW = datetime(2025, 1, 1, tzinfo=timezone.utc)
D = Decimal


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'real-test.db'}")
    RealBase.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def add(db, account_id, item, request_id=None):
    return append_event(db, account_id, request_id or str(uuid4()), item)


def contribution(amount, when=NOW):
    return EventInput("CONTRIBUTION", "EUR", when, amount=D(amount))


def buy(quantity, price, fee="0", when=NOW):
    return EventInput("BUY", "EUR", when, "BTC", D(quantity), D(price), fee=D(fee))


def sell(quantity, price, fee="0", when=NOW):
    return EventInput("SELL", "EUR", when, "BTC", D(quantity), D(price), fee=D(fee))


def test_real_metadata_is_not_part_of_existing_paper_schema():
    assert "real_events" not in Base.metadata.tables
    assert "real_events" in RealBase.metadata.tables


def test_weighted_cost_partial_sale_cash_flows_and_exact_decimals(db):
    account = create_account(db, "Principal")
    add(db, account.id, contribution("1000"))
    add(db, account.id, buy("0.1", "100", "0.01"))
    add(db, account.id, buy("0.2", "200", "0.02", NOW + timedelta(minutes=1)))
    add(db, account.id, sell("0.1", "300", "0.03", NOW + timedelta(minutes=2)))

    snapshot = account_snapshot(db, account.id)
    position = snapshot.positions[("BTC", "EUR")]
    assert position.quantity == D("0.2")
    with localcontext() as context:
        context.prec = CALC_PRECISION
        assert position.cost_basis == D("50.03") * D("2") / D("3")
        assert position.average_cost == position.cost_basis / D("0.2")
        assert position.realized_pnl == D("30") - D("0.03") - D("50.03") / D("3")
    assert snapshot.cash["EUR"] == D("979.94")
    assert snapshot.contributions["EUR"] == D("1000")
    assert position.fees == D("0.06")


def test_full_sale_resets_cost_basis_without_rounding_residue(db):
    account = create_account(db, "Principal")
    add(db, account.id, buy("0.1", "0.2"))
    add(db, account.id, sell("0.1", "0.3", when=NOW + timedelta(seconds=1)))
    position = account_snapshot(db, account.id).positions[("BTC", "EUR")]
    assert (position.quantity, position.cost_basis, position.realized_pnl) == (
        D("0"), D("0"), D("0.01")
    )


def test_real_ledger_keeps_tiny_positive_fee_in_cash_and_cost_basis(db):
    account = create_account(db, "Precisión")
    add(db, account.id, contribution("100"))
    add(db, account.id, buy("1", "100", "1e-100"))
    snapshot = account_snapshot(db, account.id)
    assert snapshot.cash["EUR"] == D("-1e-100")
    assert snapshot.positions[("BTC", "EUR")].cost_basis > D("100")


def test_base_fee_and_tiny_quote_fee_survive_full_round_trip_exactly(db):
    account = create_account(db, "Comisiones extremas")
    initial = D("9e40")
    add(db, account.id, contribution(str(initial)))
    add(db, account.id, EventInput(
        "BUY", "EUR", NOW, "BTC", D("1e-40"), D("9e40"),
        fee=D("1e-80"), fee_asset="BTC",
    ))
    with localcontext() as context:
        context.prec = CALC_PRECISION
        acquired = D("1e-40") - D("1e-80")
    add(db, account.id, EventInput(
        "SELL", "EUR", NOW + timedelta(seconds=1), "BTC", acquired,
        D("1e41"), gross_override=D("10"), fee=D("1e-100"),
    ))
    snapshot = account_snapshot(db, account.id)
    position = snapshot.positions[("BTC", "EUR")]
    with localcontext() as context:
        context.prec = CALC_PRECISION
        expected_profit = D("1") - D("1e-100")
        assert position.quantity == position.cost_basis == 0
        assert position.realized_pnl == expected_profit
        assert snapshot.cash["EUR"] - initial == expected_profit
        assert position.fees_by_asset == {"BTC": D("1e-80"), "EUR": D("1e-100")}


def test_reported_trade_gross_survives_storage_and_controls_cash_and_pnl(db):
    account = create_account(db, "Principal")
    add(db, account.id, contribution("100"))
    buy_event = EventInput("BUY", "EUR", NOW, "BTC", D("0.003"), D("10000"),
                           gross_override=D("29.99"))
    add(db, account.id, buy_event)
    add(db, account.id, EventInput("SELL", "EUR", NOW + timedelta(seconds=1),
                                   "BTC", D("0.003"), D("11000"),
                                   gross_override=D("33.01")))
    events = list_events(db, account.id)
    assert [item.input.gross_amount for item in events[1:]] == [D("29.99"), D("33.01")]
    snapshot = account_snapshot(db, account.id)
    assert snapshot.cash["EUR"] == D("103.02")
    assert snapshot.positions[("BTC", "EUR")].realized_pnl == D("3.02")
    with pytest.raises(PortfolioError, match="greater than zero"):
        EventInput("BUY", "EUR", NOW, "BTC", D("1"), D("10"), gross_override=D("0"))
    with pytest.raises(PortfolioError, match="only valid for trades"):
        EventInput("CONTRIBUTION", "EUR", NOW, amount=D("1"), gross_override=D("1"))


def test_base_asset_fees_reduce_units_and_cost_basis_without_spending_extra_quote(db):
    account = create_account(db, "Principal")
    add(db, account.id, contribution("1000"))
    add(db, account.id, EventInput("BUY", "EUR", NOW, "BTC", D("0.01"), D("65000"),
                                   fee=D("0.00001"), fee_asset="BTC"))
    bought = account_snapshot(db, account.id)
    assert bought.cash["EUR"] == D("350")
    assert bought.positions[("BTC", "EUR")].quantity == D("0.00999")
    assert bought.positions[("BTC", "EUR")].cost_basis == D("650")
    assert bought.positions[("BTC", "EUR")].fees_by_asset == {"BTC": D("0.00001")}
    add(db, account.id, EventInput("SELL", "EUR", NOW + timedelta(seconds=1),
                                   "BTC", D("0.005"), D("70000"),
                                   fee=D("0.000002"), fee_asset="BTC"))
    sold = account_snapshot(db, account.id)
    position = sold.positions[("BTC", "EUR")]
    assert sold.cash["EUR"] == D("700")
    assert position.quantity == D("0.004988")
    assert position.fees_by_asset == {"BTC": D("0.000012")}
    with localcontext() as context:
        context.prec = CALC_PRECISION
        allocated = D("650") / D("0.00999") * D("0.005002")
        assert position.realized_pnl == D("350") - allocated
        assert position.cost_basis == D("650") - allocated
    with pytest.raises(PortfolioError, match="exceeds available"):
        add(db, account.id, EventInput("SELL", "EUR", NOW + timedelta(seconds=2),
                                       "BTC", D("0.004988"), D("70000"),
                                       fee=D("0.000001"), fee_asset="BTC"))
    with pytest.raises(PortfolioError, match="base or quote"):
        EventInput("BUY", "EUR", NOW, "BTC", D("1"), D("10"),
                   fee=D("1"), fee_asset="BNB")


def test_contribution_is_not_profit_and_accounts_do_not_mix(db):
    first = create_account(db, "A")
    second = create_account(db, "B")
    add(db, first.id, contribution("1000"))
    add(db, second.id, contribution("20"))
    assert account_snapshot(db, first.id).cash == {"EUR": D("1000")}
    assert account_snapshot(db, second.id).cash == {"EUR": D("20")}
    with pytest.raises(PortfolioError, match="different accounts"):
        fold_events(first.id, list_events(db, second.id))


def test_oversell_and_backdated_invalid_sale_are_not_saved(db):
    account = create_account(db, "Principal")
    add(db, account.id, buy("1", "10", when=NOW + timedelta(days=2)))
    add(db, account.id, sell("1", "20", when=NOW + timedelta(days=3)))
    with pytest.raises(PortfolioError, match="exceeds"):
        add(db, account.id, sell("0.1", "20", when=NOW + timedelta(days=4)))
    assert len(list_events(db, account.id)) == 2
    with pytest.raises(PortfolioError, match="exceeds"):
        add(db, account.id, sell("1", "20", when=NOW + timedelta(days=1)))
    assert len(list_events(db, account.id)) == 2


def test_idempotent_retry_and_conflicting_reuse(db):
    account = create_account(db, "Principal")
    request_id = str(uuid4())
    original = add(db, account.id, buy("1", "10"), request_id)
    repeated = add(db, account.id, buy("1.0", "10.00"), request_id)
    assert repeated.id == original.id
    assert len(list_events(db, account.id)) == 1
    with pytest.raises(RequestConflict):
        add(db, account.id, buy("2", "10"), request_id)
    assert len(list_events(db, account.id)) == 1


def test_idempotency_digest_does_not_round_long_decimal_inputs(db):
    account = create_account(db, "Principal")
    request_id = str(uuid4())
    first = "0.1234567890123456789012345678901"
    second = "0.1234567890123456789012345678902"
    add(db, account.id, buy(first, "10"), request_id)
    with pytest.raises(RequestConflict):
        add(db, account.id, buy(second, "10"), request_id)
    assert len(list_events(db, account.id)) == 1


def test_reopens_exact_values_from_text_columns(tmp_path):
    path = tmp_path / "real-reopen.db"
    engine = create_engine(f"sqlite:///{path}")
    RealBase.metadata.create_all(engine)
    with Session(engine) as db:
        account = create_account(db, "Principal")
        add(db, account.id, buy("0.123456789012345678", "54321.987654321", "0.000000000000000001"))
        account_id = account.id
    engine.dispose()
    reopened = create_engine(f"sqlite:///{path}")
    with Session(reopened) as db:
        event = list_events(db, account_id)[0]
        assert event.input.quantity == D("0.123456789012345678")
        assert event.input.unit_price == D("54321.987654321")
        assert account_snapshot(db, account_id).positions[("BTC", "EUR")].quantity == event.input.quantity
        storage = db.execute(text("SELECT typeof(quantity), quantity FROM real_events")).one()
        assert storage == ("text", "0.123456789012345678")
    reopened.dispose()


@pytest.mark.parametrize("value", [0.1, D("NaN"), D("Infinity"), D("-1")])
def test_invalid_trade_quantities_are_rejected(value):
    with pytest.raises(PortfolioError):
        EventInput("BUY", "EUR", NOW, "BTC", value, D("100"))


def test_excessive_precision_is_rejected_instead_of_rounded():
    with pytest.raises(PortfolioError, match="40 significant digits"):
        buy("0." + "1" * 41, "100")
    with pytest.raises(PortfolioError, match="exponent"):
        buy("1e100000", "100")
    # Releer notación fija no debe permitir eludir el límite de magnitud.
    with pytest.raises(PortfolioError, match="exponent"):
        buy("1" + "0" * 101, "100")
    with pytest.raises(PortfolioError, match="exponent"):
        buy("1." + "0" * 101, "100")
    assert buy("9" + "0" * 40, "100").quantity == D("9e40")


def test_naive_timestamp_and_asset_alias_are_rejected():
    with pytest.raises(PortfolioError, match="timezone"):
        buy("1", "100", when=datetime(2025, 1, 1))
    with pytest.raises(PortfolioError, match="Invalid asset"):
        EventInput("BUY", "EUR", NOW, "0xAA…999", D("1"), D("1"))


def test_complete_wallet_address_cannot_be_an_account_alias(db):
    with pytest.raises(PortfolioError, match="alias"):
        create_account(db, "0x" + "a" * 40)


def test_internal_transfer_is_audited_without_changing_position_cash_or_profit(db):
    account = create_account(db, "Principal")
    add(db, account.id, buy("2", "100", "1"))
    before = account_snapshot(db, account.id)
    transfer = EventInput("TRANSFER", "EUR", NOW + timedelta(seconds=1), "BTC", D("1"), note="Entre ubicaciones propias")
    stored = add(db, account.id, transfer)
    after = account_snapshot(db, account.id)
    assert stored.input.note == "Entre ubicaciones propias"
    assert len(list_events(db, account.id)) == 2
    assert (after.cash, after.positions) == (before.cash, before.positions)
    with pytest.raises(PortfolioError, match="exceeds"):
        add(db, account.id, EventInput("TRANSFER", "EUR", NOW + timedelta(seconds=2), "BTC", D("3")))


def test_cash_transfer_is_atomic_idempotent_and_not_a_contribution(db):
    wallet = create_account(db, "Wallet")
    exchange = create_account(db, "MEXC")
    add(db, wallet.id, contribution("1000"))
    request_id = str(uuid4())
    args = dict(currency="EUR", amount=D("125.50"), occurred_at=NOW + timedelta(seconds=1))
    preview = transfer_cash_between_accounts(db, wallet.id, exchange.id, request_id,
                                              dry_run=True, **args)
    assert preview["available_at_time"] == "1000"
    assert list_events(db, exchange.id) == []
    with pytest.raises(RequestConflict, match="since preview"):
        transfer_cash_between_accounts(db, wallet.id, exchange.id, request_id,
                                       expected_source_balance=D("900"), **args)
    result = transfer_cash_between_accounts(db, wallet.id, exchange.id, request_id,
                                            expected_source_balance=D("1000"), **args)
    assert result["status"] == "RECORDED"
    assert transfer_cash_between_accounts(db, wallet.id, exchange.id, request_id, **args)["status"] == "ALREADY_RECORDED"
    source, target = account_snapshot(db, wallet.id), account_snapshot(db, exchange.id)
    assert source.cash["EUR"] == D("874.50")
    assert target.cash["EUR"] == D("125.50")
    assert source.contributions == {"EUR": D("1000")}
    assert target.contributions == target.withdrawals == source.withdrawals == {}
    assert [item.input.kind for item in list_events(db, exchange.id)] == ["CASH_TRANSFER_IN"]
    with pytest.raises(RequestConflict):
        transfer_cash_between_accounts(db, wallet.id, exchange.id, request_id,
                                       currency="EUR", amount=D("1"), occurred_at=args["occurred_at"])


def test_cash_transfer_rejects_missing_balance_and_unpaired_event(db):
    wallet = create_account(db, "Wallet")
    exchange = create_account(db, "MEXC")
    add(db, wallet.id, contribution("100"))
    with pytest.raises(PortfolioError, match="available balance"):
        transfer_cash_between_accounts(db, wallet.id, exchange.id, str(uuid4()),
                                       currency="EUR", amount=D("101"), occurred_at=NOW)
    assert list_events(db, exchange.id) == []
    item = EventInput("CASH_TRANSFER_IN", "EUR", NOW, amount=D("1"))
    with pytest.raises(PortfolioError, match="atomic paired"):
        add(db, exchange.id, item)


def test_transfer_between_accounts_moves_exact_cost_without_profit_or_cash(db):
    wallet = create_account(db, "Wallet", "EUR")
    exchange = create_account(db, "MEXC", "EUR")
    add(db, wallet.id, buy("2", "0.3"))
    add(db, wallet.id, buy("1", "0.4", when=NOW + timedelta(seconds=1)))
    request_id = str(uuid4())
    moved = transfer_asset_between_accounts(
        db, wallet.id, exchange.id, request_id, asset="BTC", quote_currency="EUR",
        quantity=D("1"), occurred_at=NOW + timedelta(seconds=2), note="Wallet a MEXC",
    )
    assert moved["status"] == "RECORDED"
    assert transfer_asset_between_accounts(
        db, wallet.id, exchange.id, request_id, asset="BTC", quote_currency="EUR",
        quantity=D("1"), occurred_at=NOW + timedelta(seconds=2), note="Wallet a MEXC",
    )["status"] == "ALREADY_RECORDED"
    source = account_snapshot(db, wallet.id)
    target = account_snapshot(db, exchange.id)
    with localcontext() as context:
        context.prec = CALC_PRECISION
        assert source.positions[("BTC", "EUR")].quantity == D("2")
        assert target.positions[("BTC", "EUR")].quantity == D("1")
        assert source.positions[("BTC", "EUR")].cost_basis + target.positions[("BTC", "EUR")].cost_basis == D("1")
    assert source.positions[("BTC", "EUR")].realized_pnl == 0
    assert target.positions[("BTC", "EUR")].realized_pnl == 0
    assert source.cash["EUR"] == D("-1") and target.cash["EUR"] == 0
    assert [item.input.kind for item in list_events(db, exchange.id)] == ["TRANSFER_IN"]
    with pytest.raises(RequestConflict):
        transfer_asset_between_accounts(
            db, wallet.id, exchange.id, request_id, asset="BTC", quote_currency="EUR",
            quantity=D("0.5"), occurred_at=NOW + timedelta(seconds=2), note="Wallet a MEXC",
        )


def test_asset_transfer_with_network_fee_moves_net_units_and_preserves_basis(db):
    wallet = create_account(db, "Wallet", "EUR")
    exchange = create_account(db, "MEXC", "EUR")
    add(db, wallet.id, buy("2", "100"))
    request_id = str(uuid4())
    args = dict(asset="BTC", quote_currency="EUR", quantity=D("1"),
                fee=D("0.01"), occurred_at=NOW + timedelta(seconds=1))
    preview = transfer_asset_between_accounts(db, wallet.id, exchange.id, request_id,
                                                dry_run=True, **args)
    assert preview["received_quantity"] == "0.99"
    assert preview["cost_basis"] == "100"
    assert list_events(db, exchange.id) == []
    saved = transfer_asset_between_accounts(db, wallet.id, exchange.id, request_id,
                                             expected_cost_basis=D("100"), **args)
    assert saved["received_quantity"] == "0.99"
    assert transfer_asset_between_accounts(db, wallet.id, exchange.id, request_id,
                                           **args)["status"] == "ALREADY_RECORDED"
    source = account_snapshot(db, wallet.id).positions[("BTC", "EUR")]
    target = account_snapshot(db, exchange.id).positions[("BTC", "EUR")]
    assert (source.quantity, target.quantity) == (D("1"), D("0.99"))
    assert source.cost_basis + target.cost_basis == D("200")
    assert source.fees_by_asset == {"BTC": D("0.01")}
    assert source.realized_pnl == target.realized_pnl == 0
    assert verify_paired_transfers(db) == 1
    with pytest.raises(RequestConflict):
        transfer_asset_between_accounts(db, wallet.id, exchange.id, request_id,
                                        **{**args, "fee": D("0.02")})
    for invalid in (D("-0.01"), D("1"), D("1.01")):
        with pytest.raises(PortfolioError):
            transfer_asset_between_accounts(db, wallet.id, exchange.id, str(uuid4()),
                                            **{**args, "fee": invalid})


def test_asset_transfer_fee_subtraction_keeps_all_valid_decimal_digits(db):
    wallet = create_account(db, "Wallet", "EUR")
    exchange = create_account(db, "MEXC", "EUR")
    sent = "0.1234567890123456789012345678901234567890"
    fee = "0.0000000000000000000000000000000000000001"
    received = "0.1234567890123456789012345678901234567889"
    add(db, wallet.id, buy(sent, "1"))
    args = dict(asset="BTC", quote_currency="EUR", quantity=D(sent), fee=D(fee),
                occurred_at=NOW + timedelta(seconds=1))
    request_id = str(uuid4())
    preview = transfer_asset_between_accounts(db, wallet.id, exchange.id, request_id,
                                                dry_run=True, **args)
    assert preview["received_quantity"] == received
    transfer_asset_between_accounts(db, wallet.id, exchange.id, request_id,
                                    expected_cost_basis=D(preview["cost_basis"]), **args)
    assert format(account_snapshot(db, exchange.id).positions[("BTC", "EUR")].quantity, "f") == received
    assert account_snapshot(db, wallet.id).positions[("BTC", "EUR")].quantity == 0
    assert verify_paired_transfers(db) == 1


def test_transfer_between_accounts_rolls_back_both_sides_on_invalid_history(db):
    wallet = create_account(db, "Wallet", "EUR")
    exchange = create_account(db, "MEXC", "EUR")
    add(db, wallet.id, buy("1", "100"))
    add(db, wallet.id, sell("0.5", "150", when=NOW + timedelta(seconds=3)))
    with pytest.raises(PortfolioError, match="exceeds"):
        transfer_asset_between_accounts(
            db, wallet.id, exchange.id, str(uuid4()), asset="BTC", quote_currency="EUR",
            quantity=D("0.75"), occurred_at=NOW + timedelta(seconds=2),
        )
    assert [item.input.kind for item in list_events(db, wallet.id)] == ["BUY", "SELL"]
    assert list_events(db, exchange.id) == []


def test_transfer_preview_is_read_only_and_commit_rejects_changed_cost(db):
    wallet = create_account(db, "Wallet", "EUR")
    exchange = create_account(db, "MEXC", "EUR")
    add(db, wallet.id, buy("2", "100"))
    request_id = str(uuid4())
    arguments = dict(asset="BTC", quote_currency="EUR", quantity=D("0.5"),
                     occurred_at=NOW + timedelta(seconds=1))
    preview = transfer_asset_between_accounts(
        db, wallet.id, exchange.id, request_id, dry_run=True, **arguments)
    assert preview["status"] == "PREVIEW"
    assert preview["available_at_time"] == "2"
    assert D(preview["cost_basis"]) == D("50")
    assert list_events(db, exchange.id) == []
    add(db, wallet.id, buy("1", "200", when=NOW))
    with pytest.raises(RequestConflict, match="since preview"):
        transfer_asset_between_accounts(
            db, wallet.id, exchange.id, request_id,
            expected_cost_basis=D(preview["cost_basis"]), **arguments)
    assert list_events(db, exchange.id) == []
    fresh = transfer_asset_between_accounts(
        db, wallet.id, exchange.id, request_id, dry_run=True, **arguments)
    assert D(fresh["cost_basis"]) != D(preview["cost_basis"])


def test_paired_transfer_sides_cannot_be_imported_or_appended_separately(db):
    account = create_account(db, "Wallet", "EUR")
    item = EventInput("TRANSFER_IN", "EUR", NOW, asset="BTC", quantity=D("1"),
                      gross_override=D("100"))
    with pytest.raises(PortfolioError, match="atomic paired"):
        append_event(db, account.id, str(uuid4()), item)
    with pytest.raises(PortfolioError, match="unpaired"):
        import_events(db, account.id, [(str(uuid4()), item)], dry_run=True)
    assert list_events(db, account.id) == []


def test_concurrent_transfers_cannot_move_the_same_units_twice(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'paired-concurrency.db'}",
                           connect_args={"timeout": 5})
    RealBase.metadata.create_all(engine)
    with Session(engine) as db:
        wallet = create_account(db, "Wallet", "EUR").id
        first = create_account(db, "MEXC", "EUR").id
        second = create_account(db, "GRVT", "EUR").id
        add(db, wallet, buy("1", "100"))
    barrier = Barrier(2)

    def move(target):
        with Session(engine) as db:
            barrier.wait()
            try:
                transfer_asset_between_accounts(
                    db, wallet, target, str(uuid4()), asset="BTC", quote_currency="EUR",
                    quantity=D("0.7"), occurred_at=NOW + timedelta(seconds=1),
                )
                return "saved"
            except PortfolioError:
                return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in (pool.submit(move, first), pool.submit(move, second))]
    assert sorted(results) == ["rejected", "saved"]
    with Session(engine) as db:
        assert account_snapshot(db, wallet).positions[("BTC", "EUR")].quantity == D("0.3")
        targets = [account_snapshot(db, account).positions.get(("BTC", "EUR"))
                   for account in (first, second)]
        assert sum((position.quantity if position else D(0) for position in targets), D(0)) == D("0.7")
    engine.dispose()


def test_adjustments_preserve_cost_without_creating_cash_or_realized_profit(db):
    account = create_account(db, "Principal")
    add(db, account.id, buy("1", "100"))
    add(db, account.id, EventInput("ADJUSTMENT_IN", "EUR", NOW + timedelta(seconds=1), "BTC", D("1"), D("80"), note="Saldo omitido"))
    add(db, account.id, EventInput("ADJUSTMENT_OUT", "EUR", NOW + timedelta(seconds=2), "BTC", D("0.5"), note="Corrección de inventario"))
    snapshot = account_snapshot(db, account.id)
    position = snapshot.positions[("BTC", "EUR")]
    assert (position.quantity, position.cost_basis, position.average_cost, position.realized_pnl) == (D("1.5"), D("135"), D("90"), D("0"))
    assert snapshot.cash == {"EUR": D("-100")}
    assert [event.input.kind for event in list_events(db, account.id)] == ["BUY", "ADJUSTMENT_IN", "ADJUSTMENT_OUT"]


def test_adjustments_require_reason_and_reject_wallet_address():
    with pytest.raises(PortfolioError, match="explanatory note"):
        EventInput("ADJUSTMENT_OUT", "EUR", NOW, "BTC", D("1"))
    with pytest.raises(PortfolioError, match="wallet addresses"):
        EventInput("TRANSFER", "EUR", NOW, "BTC", D("1"), note="Destino 0x" + "a" * 40)


def test_concurrent_sales_cannot_spend_the_same_units(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'concurrent.db'}", connect_args={"timeout": 5})
    RealBase.metadata.create_all(engine)
    with Session(engine) as db:
        account_id = create_account(db, "Principal").id
        add(db, account_id, buy("1", "100"))
    barrier = Barrier(2)

    def attempt():
        with Session(engine) as db:
            barrier.wait()
            try:
                add(db, account_id, sell("0.7", "100", when=NOW + timedelta(seconds=1)))
                return "saved"
            except PortfolioError:
                return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in (pool.submit(attempt), pool.submit(attempt))]
    assert sorted(results) == ["rejected", "saved"]
    with Session(engine) as db:
        assert account_snapshot(db, account_id).positions[("BTC", "EUR")].quantity == D("0.3")
    engine.dispose()
