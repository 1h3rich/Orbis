"""Bot nativo probado → decisión durable → POST controlado → posición propia."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

from app.portfolio.vault import SCHEMA_VERSION
import pytest

from app.automation.spot_strategy_paper import SpotStrategyPaper
from app.lab.engine import CURRENT_ENGINE_VERSION, run_backtest, validate_candles
from app.lab.store import LabStore
from app.live_execution.journal import register_spot_policy, stop_spot_account
from app.live_execution.mexc_execution import submit_anchored_spot_limit
from app.live_execution.mexc_spot_bot import activate_mexc_spot_bot, run_mexc_spot_bot_once
from app.live_execution.spot_bot_source import verified_spot_bot_observation
from app.live_execution.spot_bot_store import (
    SpotBotStoreError, bot_record, claim_spot_bot_cycle, owned_spot_bot_ledger, register_spot_bot,
)
from app.portfolio.models import RealLiveSpotIntent, RealLiveSpotPolicy, RealSpotBot, RealSpotBotDecision, MexcObservedFill
from app.portfolio.repository import account_snapshot
from app.portfolio.vault import RealVault, VaultError
from tests.test_mexc_execution import ExchangeOrders
from tests.test_mexc_execution_state import ETH_BALANCE, ETH_OPENING, PASSWORD, setup
from tests.test_spot_strategy_paper import definition


def bot_case(tmp_path, *, register=True, other_asset=False):
    extra = {"book_movements": (ETH_OPENING,), "other": (ETH_BALANCE.copy(),)} if other_asset else {}
    vault, previous, account, venue, clock = setup(tmp_path, **extra)
    hour = int(clock.value.timestamp()) // 3600 * 3600
    values = [(100,99), (100,99), (100,99), (99,100), (98,99)]
    rows = [{"t":hour + (i - 3) * 3600, "o":str(o), "h":str(max(c,o) + 1),
             "l":str(min(c,o) - 1), "c":str(c), "v":"10"} for i,(c,o) in enumerate(values)]

    class Source:
        def get_candles(self, symbol, *, since, limit, **_):
            return [row for row in rows if row["t"] >= since][:limit]

        def get_paper_quote(self, symbol):
            return {"symbol":symbol,"source":"mexc-spot-public", "event_time_ns":int(clock.value.timestamp()*1e9),
                    "bid":"99", "ask":"100", "last":"99"}

    lab = LabStore(tmp_path / "lab.db")
    bars, gaps = validate_candles(rows[:3], "1h", now=int(clock.value.timestamp()))
    dataset = lab.save_dataset("BTC/USDT", "1h", "mexc-spot-public", bars, gaps, verified_public=True)
    strategy = lab.create_strategy("Bot probado", {**definition(),"entry":{"kind":"BEARISH_STREAK","period":2}})
    saved = lab.save_run(dataset["id"], strategy["version_id"],
                          run_backtest(bars, strategy["definition"], "1h"), CURRENT_ENGINE_VERSION)
    paper = SpotStrategyPaper(tmp_path / "spot.db", lab.path, adapter_factory=Source,
                              clock_ns=lambda: int(clock.value.timestamp()*1e9))
    session = paper.create(strategy["version_id"], dataset["id"], saved["id"])
    clock.value = datetime.fromtimestamp(hour + 3600 + 1, timezone.utc)
    assert paper.poll(session["id"])["poll_result"] == "OBSERVED"
    source = verified_spot_bot_observation(paper, session["id"], now=clock(), verify_backtest=True)
    assert source.entry_match is False
    policy = replace(previous, policy_id=str(uuid4()), strategy_version_id=strategy["version_id"],
                      expires_at=clock.value + timedelta(hours=20))
    register_spot_policy(vault, policy, now=clock())
    bot_id = str(uuid4())
    if register:
        register_spot_bot(vault, policy, source, bot_id=bot_id, entry_budget_quote=Decimal("20"), now=clock())
    trader = ExchangeOrders(venue, clock)
    return vault, policy, account, venue, clock, paper, session, trader, bot_id, hour


def tick(case, *, active=lambda: None):
    vault, _, _, venue, clock, paper, _, trader, bot_id, _ = case
    return run_mexc_spot_bot_once(vault, venue, trader, paper, bot_id, clock=clock,
                                  quote_get=venue.quote_get, rules_fetch=venue.rules_fetch, assert_active=active)


def next_entry(case):
    _, _, _, _, clock, paper, session, _, _, hour = case
    clock.value = datetime.fromtimestamp(hour + 7200 + 1, timezone.utc)
    assert paper.poll(session["id"])["poll_result"] == "OBSERVED"


@pytest.mark.parametrize("other_asset", [False, True], ids=["one-pair", "multiasset"])
def test_bot_buys_once_then_exits_its_real_position_and_keeps_dust_honest(tmp_path, other_asset):
    case = bot_case(tmp_path, other_asset=other_asset)
    vault, policy, account, venue, clock, paper, session, trader, bot_id, _ = case
    assert tick(case)["action"] == "WAIT" and trader.posts == []
    next_entry(case)
    purchase = tick(case)
    assert purchase["action"] == "BUY" and purchase["order"]["journal_status"] == "RESERVATION_CLOSED", purchase
    ledger, revision = owned_spot_bot_ledger(vault, bot_id)
    assert 0 < ledger.position.units < Decimal("0.001")
    assert 0 < ledger.position.cost_quote <= Decimal("20")
    assert paper.detail(session["id"])["state"]["units"] != str(ledger.position.units)
    assert tick(case)["action"] == "WAIT" and len(trader.posts) == 1
    venue.quote_get = lambda symbol: {"symbol":symbol,"closeTime":int(clock.value.timestamp()*1000),
                                    "bidPrice":"72000", "askPrice":"72001", "lastPrice":"72000"}
    sale = tick(case)
    assert sale["action"] == "SELL" and sale["order"]["journal_status"] == "RESERVATION_CLOSED", sale
    after, revised = owned_spot_bot_ledger(vault, bot_id)
    assert after.realized_pnl_quote > 0 and 0 < after.position.units < Decimal("0.00001")
    assert revised != revision and after.fill_count == 2
    assert tick(case)["action"] == "WAIT" and len(trader.posts) == 2
    vault.lock()
    vault.unlock(PASSWORD)
    assert owned_spot_bot_ledger(vault, bot_id)[0] == after
    if other_asset:
        with vault.session() as db:
            ether = account_snapshot(db, account).positions[("ETH", "USDT")]
            assert ether.quantity == Decimal("1.25") and ether.cost_basis == Decimal("3000")
    vault.lock()


def test_bot_policy_and_account_cannot_be_used_for_manual_orders_while_active(tmp_path):
    case = bot_case(tmp_path)
    vault, policy, _, venue, clock, _, _, trader, _, _ = case
    with pytest.raises(SpotBotStoreError, match="operativa manual"):
        submit_anchored_spot_limit(vault, venue, trader, policy.policy_id, str(uuid4()), side="BUY",
            quantity_base=Decimal("0.001"), limit_price_quote=Decimal("65000"), clock=clock,
            quote_get=venue.quote_get, rules_fetch=venue.rules_fetch)
    assert trader.posts == []
    vault.lock()


def test_timeout_after_acceptance_pauses_without_ever_resubmitting(tmp_path):
    case = bot_case(tmp_path)
    next_entry(case)
    case[7].timeout_after_accepting = True
    result = tick(case)
    assert result["order"]["journal_status"] == "SUBMISSION_UNKNOWN" and len(case[7].posts) == 1
    with pytest.raises(SpotBotStoreError, match="pausado"):
        tick(case)
    with case[0].session() as db:
        assert db.query(RealSpotBotDecision).count() == db.query(RealLiveSpotIntent).count() == 1
        assert db.get(RealSpotBot, case[8]).status == "PAUSED"
    case[0].lock()


def test_paused_source_and_stop_during_order_test_never_place_an_order(tmp_path):
    case = bot_case(tmp_path)
    next_entry(case)
    case[7].after_test = lambda: stop_spot_account(case[0], case[1].account_ref, now=case[4]())
    result = tick(case)
    assert result["order"]["stage"] == "REVIEW_REQUIRED"
    assert case[7].posts == []
    with case[0].session() as db:
        row, _, state = bot_record(db, case[8])
        assert row.status == "PAUSED" and row.cycle_id is None
    case[0].lock()


def test_cycle_claim_cannot_be_stolen_by_another_process_after_a_crash(tmp_path):
    case = bot_case(tmp_path)
    claim_spot_bot_cycle(case[0], case[8], now=case[4]())
    other = RealVault(case[0].path)
    other.unlock(PASSWORD)
    with pytest.raises(SpotBotStoreError, match="ciclo pendiente"):
        claim_spot_bot_cycle(other, case[8], now=case[4]())
    assert case[7].posts == []
    other.lock()
    case[0].lock()


def test_only_one_active_bot_can_claim_the_exchange_account(tmp_path):
    case = bot_case(tmp_path)
    source = verified_spot_bot_observation(case[5], case[6]["id"], now=case[4]())
    with pytest.raises(SpotBotStoreError, match="bot activo"):
        register_spot_bot(case[0], case[1], source, bot_id=str(uuid4()), entry_budget_quote=Decimal("20"), now=case[4]())
    assert case[7].posts == []
    case[0].lock()


def test_two_independent_process_claims_have_exactly_one_winner(tmp_path):
    case = bot_case(tmp_path)
    other = RealVault(case[0].path)
    other.unlock(PASSWORD)
    at, barrier = case[4](), Barrier(2)

    def claim(vault):
        barrier.wait()
        try:
            claim_spot_bot_cycle(vault, case[8], now=at)
            return True
        except SpotBotStoreError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, (case[0],other)))
    assert results.count(True) == 1 and case[7].posts == []
    other.lock()
    case[0].lock()


def test_missing_own_fill_is_not_treated_as_zero_position(tmp_path):
    case = bot_case(tmp_path)
    next_entry(case)
    assert tick(case)["order"]["journal_status"] == "RESERVATION_CLOSED"
    # Conserva referencias y filas; la evidencia externa queda incoherente.
    with case[0].session() as db:
        db.query(MexcObservedFill).first().quantity = Decimal("0.000001")
        db.commit()
    with pytest.raises(SpotBotStoreError, match="incompleto o alterado"):
        owned_spot_bot_ledger(case[0], case[8])
    case[0].lock()


def test_bot_evidence_is_encrypted_backed_up_and_validated(tmp_path):
    case = bot_case(tmp_path)
    assert case[0].path.read_bytes()[:16] != b"SQLite format 3\x00"
    directory = tmp_path / "backups"
    directory.mkdir()
    backup = case[0].backup(directory / "with-bot.db")
    copied = RealVault(backup)
    copied.unlock(PASSWORD)
    with copied.session() as db:
        assert db.query(RealSpotBot).count() == 1
    copied.lock()
    with case[0].session() as db:
        db.get(RealSpotBot, case[8]).state_digest = "0" * 64
        db.commit()
    with pytest.raises(VaultError, match="bot evidence"):
        case[0].backup(directory / "tampered-bot.db")
    case[0].lock()


def test_v24_migration_preserves_book_and_creates_empty_bot_tables_with_backup(tmp_path):
    vault, _, account, _, _ = setup(tmp_path)
    with vault.session() as db:
        db.connection().exec_driver_sql("DROP TABLE real_spot_bot_decisions")
        db.connection().exec_driver_sql("DROP TABLE real_spot_bots")
        db.connection().exec_driver_sql("PRAGMA user_version = 24")
        db.commit()
    vault.lock()
    vault.unlock(PASSWORD)
    assert vault.last_migration_backup and vault.last_migration_backup.exists()
    with vault.session() as db:
        assert db.connection().exec_driver_sql("PRAGMA user_version").scalar_one() == SCHEMA_VERSION
        assert db.query(RealSpotBot).count() == db.query(RealSpotBotDecision).count() == 0
        assert db.query(RealLiveSpotIntent).count() == 0
    vault.lock()


def test_activation_creates_policy_and_bot_together_without_transmitting(tmp_path):
    case = bot_case(tmp_path, register=False)
    vault, policy, account, venue, clock, paper, session, trader, bot_id, _ = case
    from tests.test_mexc_execution_recovery import opening
    anchor = opening(vault, account, venue, clock)
    draft = replace(policy, policy_id=str(uuid4()), enabled=False, reconciliation_anchor_id=None)
    register_spot_policy(vault, draft, now=clock())
    active_id = str(uuid4())
    assert activate_mexc_spot_bot(vault, venue, trader, paper, draft_id=draft.policy_id, anchor_id=anchor,
        policy_id=active_id, bot_id=bot_id, paper_session_id=session["id"], entry_budget_quote=Decimal("20"),
        clock=clock, quote_get=venue.quote_get, rules_fetch=venue.rules_fetch) == bot_id
    assert trader.posts == []
    with vault.session() as db:
        row, config, state = bot_record(db, bot_id)
        assert row.policy_id == active_id and config["entry_budget_quote"] == "20"
        assert db.get(RealLiveSpotPolicy, active_id) is not None
    vault.lock()


def test_failed_bot_registration_does_not_leave_an_orphan_active_policy(tmp_path, monkeypatch):
    case = bot_case(tmp_path, register=False)
    vault, policy, account, venue, clock, paper, session, trader, bot_id, _ = case
    from tests.test_mexc_execution_recovery import opening
    anchor = opening(vault, account, venue, clock)
    draft = replace(policy, policy_id=str(uuid4()), enabled=False, reconciliation_anchor_id=None)
    register_spot_policy(vault, draft, now=clock())
    active_id = str(uuid4())
    from app.live_execution import spot_bot_store
    original = spot_bot_store._register_spot_policy_in_transaction

    def fail(db, policy, **kwargs):
        original(db, policy, **kwargs)
        raise RuntimeError("isolated registration failure")

    monkeypatch.setattr(spot_bot_store, "_register_spot_policy_in_transaction", fail)
    with pytest.raises(RuntimeError, match="isolated registration failure"):
        activate_mexc_spot_bot(vault, venue, trader, paper, draft_id=draft.policy_id, anchor_id=anchor,
            policy_id=active_id, bot_id=bot_id, paper_session_id=session["id"], entry_budget_quote=Decimal("20"),
            clock=clock, quote_get=venue.quote_get, rules_fetch=venue.rules_fetch)
    with vault.session() as db:
        assert db.get(RealLiveSpotPolicy, active_id) is None and db.get(RealSpotBot, bot_id) is None
    assert trader.posts == []
    vault.lock()


def test_fee_charged_above_approved_cap_pauses_but_preserves_the_executed_fill(tmp_path):
    case = bot_case(tmp_path, register=False)
    vault, previous, _, venue, clock, paper, session, trader, bot_id, _ = case
    policy = replace(previous,policy_id=str(uuid4()),max_fee_bps=Decimal("5"))
    register_spot_policy(vault,policy,now=clock())
    source = verified_spot_bot_observation(paper,session["id"],now=clock())
    register_spot_bot(vault,policy,source,bot_id=bot_id,entry_budget_quote=Decimal("20"),now=clock())
    next_entry(case)
    result = tick(case)
    assert result["order"]["journal_status"] == "RESERVATION_CLOSED"
    assert result["paused"] and result["reason"] == "EXECUTED_FEE_EXCEEDED"
    ledger, _ = owned_spot_bot_ledger(vault,bot_id)
    assert ledger.fill_count == 1 and ledger.position.units > 0 and len(trader.posts) == 1
    with pytest.raises(SpotBotStoreError,match="pausado"):
        tick(case)
    vault.lock()
