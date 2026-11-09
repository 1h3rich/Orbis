"""Rearme: mismo bot y fills, nueva autorización, nunca un reenvío encubierto."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from uuid import uuid4

import pytest

from app.live_execution.journal import register_spot_policy, stop_spot_account
from app.live_execution.spot_bot_allocation import spot_bot_allocation_review
from app.live_execution.spot_bot_rearm import rearm_mexc_spot_bot
from app.live_execution.spot_bot_store import (
    SpotBotStoreError, bot_record, claim_spot_bot_cycle, finish_spot_bot_cycle,
    owned_spot_bot_ledger, pause_spot_bot,
)
from app.portfolio.models import RealLiveSpotPolicy, RealLiveStop
from tests.test_mexc_execution_recovery import draft, opening
from tests.test_mexc_spot_bot import bot_case, next_entry, tick


def review(case, *, request_id=None, policy_id=None, limits=None, anchor=None, **kwargs):
    vault, previous, account, venue, clock, paper, session, trader, bot_id, _ = case
    limits = limits or draft(vault, previous, clock)
    anchor = anchor or opening(vault, account, venue, clock)
    arguments = dict(request_id=request_id or str(uuid4()), policy_id=policy_id or str(uuid4()),
        draft_id=limits.policy_id, anchor_id=anchor, paper_session_id=session["id"],
        entry_budget_quote=Decimal("20"), assert_authorized=lambda:None,
        clock=clock, quote_get=venue.quote_get, rules_fetch=venue.rules_fetch)
    arguments.update(kwargs)
    return rearm_mexc_spot_bot(vault, venue, trader, paper, bot_id, **arguments), arguments


def test_pause_review_retains_real_position_and_can_sell_once_under_new_policy(tmp_path):
    case = bot_case(tmp_path)
    next_entry(case)
    assert tick(case)["action"] == "BUY"
    vault, previous, _, venue, clock, _, _, trader, bot_id, _ = case
    before, revision = owned_spot_bot_ledger(vault, bot_id)
    pause_spot_bot(vault, bot_id, now=clock())
    result, args = review(case)
    assert not result["replayed"] and len(trader.posts) == 1
    assert owned_spot_bot_ledger(vault, bot_id) == (before, revision)
    with vault.session() as db:
        row, config, state = bot_record(db, bot_id)
        assert row.status == "ACTIVE" and row.policy_id != previous.policy_id
        assert len(config["authorizations"]) == 2
        assert config["authorizations"][-1]["review"]["observation_gap_acknowledged"]
        assert state["last_decision_id"] and state["reason"] == "REARM_REVIEWED"
    assert tick(case)["action"] == "WAIT" and len(trader.posts) == 1
    venue.quote_get = lambda symbol:{"symbol":symbol,"closeTime":int(clock.value.timestamp()*1000),
        "bidPrice":"72000","askPrice":"72001","lastPrice":"72000"}
    assert tick(case)["action"] == "SELL"
    assert owned_spot_bot_ledger(vault, bot_id)[0].fill_count == 2 and len(trader.posts) == 2
    # Revisar un ID antiguo no reactiva un bot que se detuvo después.
    pause_spot_bot(vault, bot_id, now=clock())
    venue.calls.clear()
    replay = rearm_mexc_spot_bot(vault, venue, trader, case[5], bot_id, **args)
    assert replay["replayed"] and venue.calls == []
    with vault.session() as db:
        assert bot_record(db, bot_id)[0].status == "PAUSED"
    vault.lock()


def test_account_stop_and_bot_rearm_commit_together_without_an_order(tmp_path):
    case = bot_case(tmp_path)
    next_entry(case)
    tick(case)
    vault, policy, _, _, clock, _, _, trader, bot_id, _ = case
    pause_spot_bot(vault, bot_id, now=clock())
    stop_spot_account(vault, policy.account_ref, now=clock())
    result, _ = review(case)
    with vault.session() as db:
        assert not db.get(RealLiveStop, policy.account_ref).stopped
        assert bot_record(db, bot_id)[0].policy_id == result["policy_id"]
    assert len(trader.posts) == 1 and owned_spot_bot_ledger(vault, bot_id)[0].fill_count == 1
    vault.lock()


def test_rearm_requires_pause_and_never_steals_an_uncertain_order(tmp_path):
    case = bot_case(tmp_path)
    with pytest.raises(SpotBotStoreError, match="Detén"):
        review(case)
    next_entry(case)
    case[7].timeout_after_accepting = True
    assert tick(case)["order"]["journal_status"] == "SUBMISSION_UNKNOWN"
    with pytest.raises(SpotBotStoreError, match="apertura nueva"):
        review(case, anchor=case[1].reconciliation_anchor_id)
    assert len(case[7].posts) == 1
    case[0].lock()


def test_cash_movement_with_owned_units_requires_confirming_its_review(tmp_path):
    from app.portfolio.domain import EventInput
    from app.portfolio.repository import append_event
    case = bot_case(tmp_path)
    next_entry(case)
    tick(case)
    vault, _, account, venue, clock, _, _, trader, bot_id, _ = case
    pause_spot_bot(vault, bot_id, now=clock())
    with vault.session() as db:
        append_event(db, account, str(uuid4()), EventInput(kind="CONTRIBUTION",quote_currency="USDT",
            amount=Decimal("10"),occurred_at=clock.value))
    venue.quote = str(Decimal(venue.quote) + 10)
    with pytest.raises(SpotBotStoreError, match="asignación"):
        review(case)
    before = owned_spot_bot_ledger(vault,bot_id)
    reviewed = spot_bot_allocation_review(vault,account,bot_id)
    assert reviewed["state"]=="CASH_ONLY" and reviewed["cash_delta_quote"]=="10"
    assert reviewed["movement_count"]==1 and Decimal(reviewed["owned_units"])==before[0].position.units
    result, args = review(case,allocation_review_digest=reviewed["review_digest"])
    assert result["replayed"] is False and owned_spot_bot_ledger(vault,bot_id)==before
    assert len(trader.posts) == 1
    with vault.session() as db:
        row, config, _ = bot_record(db,bot_id)
        assert row.status=="ACTIVE"
        assert config["authorizations"][-1]["review"]["allocation"]==reviewed
    pause_spot_bot(vault,bot_id,now=clock())
    replay = rearm_mexc_spot_bot(vault,venue,trader,case[5],bot_id,**args)
    assert replay["replayed"] and len(trader.posts)==1
    vault.lock()


def test_cash_review_rejects_changed_book_and_asset_movement(tmp_path):
    from app.portfolio.domain import EventInput
    from app.portfolio.repository import append_event

    case=bot_case(tmp_path)
    next_entry(case)
    tick(case)
    vault, _, account, venue, clock, _, _, trader, bot_id, _=case
    pause_spot_bot(vault,bot_id,now=clock())
    def contribute():
        with vault.session() as db:
            append_event(db,account,str(uuid4()),EventInput(kind="CONTRIBUTION",quote_currency="USDT",
                amount=Decimal("10"),occurred_at=clock.value))
        venue.quote=str(Decimal(venue.quote)+10)
    contribute()
    earlier=spot_bot_allocation_review(vault,account,bot_id)
    contribute()
    changed=spot_bot_allocation_review(vault,account,bot_id)
    assert changed["movement_count"]==2 and changed["review_digest"]!=earlier["review_digest"]
    with pytest.raises(SpotBotStoreError,match="asignación vigente"):
        review(case,allocation_review_digest=earlier["review_digest"])
    with vault.session() as db:
        append_event(db,account,str(uuid4()),EventInput(kind="ADJUSTMENT_IN",asset="BTC",quote_currency="USDT",
            quantity=Decimal("0.01"),unit_price=Decimal("65000"),occurred_at=clock.value,note="Fixture asset movement"))
    venue.base=str(Decimal(venue.base)+Decimal("0.01"))
    assert spot_bot_allocation_review(vault,account,bot_id)=={"state":"ASSET_REVIEW_REQUIRED"}
    with pytest.raises(SpotBotStoreError,match="activos ajenos"):
        review(case,allocation_review_digest=changed["review_digest"])
    assert len(trader.posts)==1
    with vault.session() as db:
        assert bot_record(db,bot_id)[0].status=="PAUSED"
    vault.lock()


def manual_order_while_paused(case, *, side="BUY", manual=True, quantity=None):
    from app.live_execution.policy import MEXC_MANUAL_ORIGIN
    from tests.test_mexc_execution import send

    vault, previous, account, venue, clock, _, _, trader, _, _ = case
    clock.value += timedelta(seconds=1)
    identifier = str(uuid4())
    policy = replace(previous, policy_id=str(uuid4()),
                     strategy_version_id=MEXC_MANUAL_ORIGIN if manual else previous.strategy_version_id,
                     reconciliation_anchor_id=opening(vault, account, venue, clock))
    register_spot_policy(vault, policy, now=clock())
    size = quantity or ("0.0002" if side == "BUY" else "0.0001")
    result = send(vault, policy, venue, clock, trader, identifier, side=side, quantity=size)
    assert result["journal_status"] == "RESERVATION_CLOSED", result
    return identifier


def test_manual_buy_rearm_preserves_units_cost_and_leaves_purchase_after_bot_sell(tmp_path):
    case = bot_case(tmp_path)
    next_entry(case)
    assert tick(case)["action"] == "BUY"
    vault, _, account, venue, clock, _, _, trader, bot_id, _ = case
    before = owned_spot_bot_ledger(vault, bot_id)
    pause_spot_bot(vault, bot_id, now=clock())
    manual_order_while_paused(case)
    allocation = spot_bot_allocation_review(vault, account, bot_id)
    assert allocation["state"] == "MANUAL_PURCHASES"
    assert allocation["manual_fill_count"] == allocation["movement_count"] == 1
    assert Decimal(allocation["manual_purchase_units"]) == Decimal("0.0002")
    assert Decimal(allocation["cash_delta_quote"]) == Decimal("-13.02")
    assert Decimal(allocation["owned_units"]) == before[0].position.units
    assert Decimal(allocation["owned_cost_quote"]) == before[0].position.cost_quote
    with pytest.raises(SpotBotStoreError, match="asignación vigente"):
        review(case)
    result, _ = review(case, allocation_review_digest=allocation["review_digest"])
    assert result["replayed"] is False and len(trader.posts) == 2
    assert owned_spot_bot_ledger(vault, bot_id) == before
    with vault.session() as db:
        _, config, _ = bot_record(db, bot_id)
        assert config["authorizations"][-1]["review"]["allocation"] == allocation

    venue.quote_get = lambda symbol:{"symbol":symbol, "closeTime":int(clock.value.timestamp()*1000),
        "bidPrice":"72000", "askPrice":"72001", "lastPrice":"72000"}
    assert tick(case)["action"] == "SELL"
    residual = owned_spot_bot_ledger(vault, bot_id)[0].position.units
    assert len(trader.posts) == 3 and residual == Decimal("0.000001")
    assert Decimal(venue.base) - residual == Decimal("0.0002")
    vault.lock()


@pytest.mark.parametrize("purchase_in_review",[False, True])
def test_manual_sale_rearm_only_consumes_inventory_outside_bot(tmp_path, purchase_in_review):
    case = bot_case(tmp_path)
    next_entry(case)
    assert tick(case)["action"] == "BUY"
    vault, _, account, venue, clock, _, _, trader, bot_id, _ = case
    before = owned_spot_bot_ledger(vault, bot_id)
    pause_spot_bot(vault, bot_id, now=clock())
    manual_order_while_paused(case)
    if not purchase_in_review:
        purchase = spot_bot_allocation_review(vault, account, bot_id)
        review(case, allocation_review_digest=purchase["review_digest"])
        pause_spot_bot(vault, bot_id, now=clock())
    manual_order_while_paused(case, side="SELL")
    allocation = spot_bot_allocation_review(vault, account, bot_id)
    assert allocation["state"] == "MANUAL_SALES"
    assert Decimal(allocation["manual_sale_units"]) == Decimal("0.0001")
    assert Decimal(allocation["outside_units_before"]) == (0 if purchase_in_review else Decimal("0.0002"))
    assert Decimal(allocation["outside_units_after"]) == Decimal("0.0001")
    assert Decimal(allocation["manual_purchase_units"]) == (Decimal("0.0002") if purchase_in_review else 0)
    assert Decimal(allocation["cash_delta_quote"]) == (Decimal("-6.54") if purchase_in_review else Decimal("6.48"))
    with pytest.raises(SpotBotStoreError, match="asignación vigente"):
        review(case)
    result, _ = review(case, allocation_review_digest=allocation["review_digest"])
    assert result["replayed"] is False and len(trader.posts) == 3
    assert owned_spot_bot_ledger(vault, bot_id) == before
    with vault.session() as db:
        _, config, _ = bot_record(db, bot_id)
        assert config["authorizations"][-1]["review"]["allocation"] == allocation
    venue.quote_get = lambda symbol:{"symbol":symbol, "closeTime":int(clock.value.timestamp()*1000),
        "bidPrice":"72000", "askPrice":"72001", "lastPrice":"72000"}
    assert tick(case)["action"] == "SELL"
    residual = owned_spot_bot_ledger(vault, bot_id)[0].position.units
    assert len(trader.posts) == 4 and Decimal(venue.base) - residual == Decimal("0.0001")
    vault.lock()


def test_sale_cannot_use_units_owned_by_another_paused_bot(tmp_path):
    from app.live_execution.spot_bot_source import verified_spot_bot_observation
    from app.live_execution.spot_bot_store import register_spot_bot

    case = bot_case(tmp_path)
    vault, policy, account, venue, clock, paper, session, trader, bot_id, hour = case
    pause_spot_bot(vault, bot_id, now=clock())
    other_id = str(uuid4())
    other_policy = replace(policy, policy_id=str(uuid4()))
    register_spot_policy(vault, other_policy, now=clock())
    source = verified_spot_bot_observation(paper, session["id"], now=clock(), verify_backtest=True)
    register_spot_bot(vault, other_policy, source, bot_id=other_id, entry_budget_quote=Decimal("20"), now=clock())
    other_case = (vault, other_policy, account, venue, clock, paper, session, trader, other_id, hour)
    next_entry(other_case)
    assert tick(other_case)["action"] == "BUY"
    pause_spot_bot(vault, other_id, now=clock())

    prior_factory = paper.adapter_factory

    class TakeProfitSource(prior_factory):
        def get_paper_quote(self, symbol):
            return {**super().get_paper_quote(symbol), "bid":"120", "ask":"121", "last":"120"}

    paper.adapter_factory = TakeProfitSource
    paper.poll(session["id"])
    assert Decimal(paper.detail(session["id"])["state"]["units"]) == 0
    paper.adapter_factory = prior_factory
    # The other bot's verified units already exist at this bot's new baseline.
    review(case)

    class ExtendedSource(prior_factory):
        def get_candles(self, symbol, *, since, limit, **kwargs):
            bars = super().get_candles(symbol, since=since, limit=limit, **kwargs)
            stamp = hour + 7200
            if since <= stamp and len(bars) < limit:
                bars.append({"t":stamp,"o":"98","h":"99","l":"96","c":"97","v":"10"})
            return bars

    paper.adapter_factory = ExtendedSource
    clock.value = datetime.fromtimestamp(hour + 10800 + 1, timezone.utc)
    assert paper.poll(session["id"])["poll_result"] == "OBSERVED"
    assert tick(case)["action"] == "BUY"
    pause_spot_bot(vault, bot_id, now=clock())
    manual_order_while_paused(case, side="SELL")
    # The venue still holds enough for this bot alone. Both bots must be protected.
    assert Decimal(venue.base) > owned_spot_bot_ledger(vault, bot_id)[0].position.units
    assert owned_spot_bot_ledger(vault, other_id)[0].position.units > 0
    assert spot_bot_allocation_review(vault, account, bot_id) == {"state":"ASSET_REVIEW_REQUIRED"}
    assert len(trader.posts) == 3
    vault.lock()


def test_later_purchase_does_not_retroactively_cover_a_manual_sale(tmp_path):
    case = bot_case(tmp_path)
    next_entry(case)
    assert tick(case)["action"] == "BUY"
    vault, _, account, venue, clock, paper, _, trader, bot_id, _ = case
    previous, _ = owned_spot_bot_ledger(vault, bot_id)
    pause_spot_bot(vault, bot_id, now=clock())
    manual_order_while_paused(case, side="SELL")
    manual_order_while_paused(case)
    assert Decimal(venue.base) > previous.position.units
    preview = spot_bot_allocation_review(vault, account, bot_id)
    assert preview["state"] == "MANUAL_BOT_DEBIT"
    assert Decimal(preview["assigned_units"]) == Decimal("0.0001")
    assert Decimal(preview["outside_units_before"]) == 0
    assert Decimal(preview["outside_units_after"]) == Decimal("0.0002")
    with pytest.raises(SpotBotStoreError, match="asignación vigente"):
        review(case)
    with vault.session() as db:
        assert bot_record(db, bot_id)[0].status == "PAUSED"
    result, args = review(case, allocation_review_digest=preview["review_digest"])
    assert result["replayed"] is False and len(trader.posts) == 3
    adjusted, revision = owned_spot_bot_ledger(vault, bot_id)
    with localcontext() as context:
        context.prec = 300
        removed_cost = previous.position.cost_quote * Decimal("0.0001") / previous.position.units
        assert adjusted.position.cost_quote == previous.position.cost_quote - removed_cost
        assert adjusted.realized_pnl_quote == Decimal("6.48") - removed_cost
    assert adjusted.position.units == previous.position.units - Decimal("0.0001")
    assert Decimal(venue.base) - adjusted.position.units == Decimal("0.0002")
    pause_spot_bot(vault, bot_id, now=clock())
    assert rearm_mexc_spot_bot(vault, venue, trader, paper, bot_id, **args)["replayed"]
    assert owned_spot_bot_ledger(vault, bot_id) == (adjusted, revision)
    vault.lock()


def test_manual_sale_consumes_outside_first_and_bot_closes_only_remainder(tmp_path):
    from tests.test_mexc_spot_bot import PASSWORD

    case = bot_case(tmp_path)
    next_entry(case)
    assert tick(case)["action"] == "BUY"
    vault, _, account, venue, clock, _, _, trader, bot_id, _ = case
    previous, _ = owned_spot_bot_ledger(vault, bot_id)
    pause_spot_bot(vault, bot_id, now=clock())
    manual_order_while_paused(case, quantity="0.0001")
    manual_order_while_paused(case, side="SELL", quantity="0.0002")
    preview = spot_bot_allocation_review(vault, account, bot_id)
    assert preview["state"] == "MANUAL_BOT_DEBIT"
    assert Decimal(preview["assigned_units"]) == Decimal("0.0001")
    assert Decimal(preview["assigned_net_quote"]) == Decimal("6.49")
    assert Decimal(preview["outside_units_after"]) == 0
    review(case, allocation_review_digest=preview["review_digest"])
    assert len(trader.posts) == 3
    adjusted, revision = owned_spot_bot_ledger(vault, bot_id)
    assert adjusted.position.units == previous.position.units - Decimal("0.0001")
    assert adjusted.position.units == Decimal(venue.base)
    with vault.session() as db:
        assert bot_record(db, bot_id)[1]["authorizations"][-1]["review"]["allocation"] == preview
    vault.lock()
    vault.unlock(PASSWORD)
    assert owned_spot_bot_ledger(vault, bot_id) == (adjusted, revision)
    venue.quote_get = lambda symbol:{"symbol":symbol,"closeTime":int(clock.value.timestamp()*1000),
        "bidPrice":"72000","askPrice":"72001","lastPrice":"72000"}
    exit_result = tick(case)
    assert exit_result["action"] == "SELL"
    assert Decimal(trader.orders[trader.posts[-1].replace("-", "")]["quantity"]) <= adjusted.position.units
    final, _ = owned_spot_bot_ledger(vault, bot_id)
    assert final.position.units == Decimal(venue.base)
    assert final.fill_count == 2 and final.manual_sale_count == 1
    assert len(trader.posts) == 4
    vault.lock()


@pytest.mark.parametrize("fault",["manual_sale", "different_origin", "order_digest", "missing_settlement", "unlinked_buy"])
def test_allocation_blocks_movements_without_exact_manual_buy_proof(tmp_path, fault):
    from app.portfolio.domain import EventInput
    from app.portfolio.models import RealLiveOrderObservation, RealLiveSpotSettlement
    from app.portfolio.repository import append_event

    case = bot_case(tmp_path)
    next_entry(case)
    assert tick(case)["action"] == "BUY"
    vault, _, account, venue, clock, _, _, trader, bot_id, _ = case
    pause_spot_bot(vault, bot_id, now=clock())
    if fault == "unlinked_buy":
        with vault.session() as db:
            append_event(db, account, str(uuid4()), EventInput(kind="BUY", asset="BTC", quote_currency="USDT",
                quantity=Decimal("0.0002"), unit_price=Decimal("65000"), occurred_at=clock.value,
                note="MEXC Spot · fill verificado"))
        venue.base = str(Decimal(venue.base) + Decimal("0.0002"))
        venue.quote = str(Decimal(venue.quote) - Decimal("13"))
    else:
        intent_id = manual_order_while_paused(case, side="SELL" if fault == "manual_sale" else "BUY",
                                              manual=fault != "different_origin")
        if fault in ("manual_sale", "order_digest", "missing_settlement"):
            expected = "MANUAL_BOT_DEBIT" if fault == "manual_sale" else "MANUAL_PURCHASES"
            assert spot_bot_allocation_review(vault, account, bot_id)["state"] == expected
            with vault.session() as db:
                settlement = db.get(RealLiveSpotSettlement, intent_id)
                if fault == "order_digest":
                    db.get(RealLiveOrderObservation, settlement.observation_id).payload_digest = "0" * 64
                else:
                    db.delete(settlement)
                db.commit()
    assert spot_bot_allocation_review(vault, account, bot_id) == {"state":"ASSET_REVIEW_REQUIRED"}
    sent = len(trader.posts)
    with pytest.raises(SpotBotStoreError, match="activos ajenos"):
        review(case)
    assert len(trader.posts) == sent
    with vault.session() as db:
        assert bot_record(db, bot_id)[0].status == "PAUSED"
    vault.lock()


def test_failed_review_rolls_back_policy_bot_and_account_stop(tmp_path, monkeypatch):
    from app.live_execution import spot_bot_rearm
    case = bot_case(tmp_path)
    vault, policy, _, _, clock, _, _, trader, bot_id, _ = case
    pause_spot_bot(vault, bot_id, now=clock())
    stop_spot_account(vault, policy.account_ref, now=clock())
    identifier = str(uuid4())
    original = spot_bot_rearm.encoded

    def fail(value):
        if "authorizations" in value:
            raise RuntimeError("isolated failed rearm")
        return original(value)

    monkeypatch.setattr(spot_bot_rearm, "encoded", fail)
    with pytest.raises(RuntimeError, match="isolated failed rearm"):
        review(case, policy_id=identifier)
    with vault.session() as db:
        assert db.get(RealLiveSpotPolicy, identifier) is None
        assert db.get(RealLiveStop, policy.account_ref).stopped
        assert bot_record(db, bot_id)[0].policy_id == policy.policy_id
    monkeypatch.setattr(spot_bot_rearm,"encoded",original)
    # Un resultado/replay ajeno de reapertura no confirma el vínculo del bot.
    monkeypatch.setattr(spot_bot_rearm,"resume_spot_execution",lambda *_,**__:None)
    with pytest.raises(SpotBotStoreError,match="no quedó confirmado"):
        review(case)
    assert trader.posts == []
    vault.lock()


def test_old_claim_and_worker_cannot_send_or_overwrite_a_rearmed_bot(tmp_path):
    case = bot_case(tmp_path)
    vault, old, _, _, clock, _, _, _, bot_id, _ = case
    claim, _, state = claim_spot_bot_cycle(vault, bot_id, now=clock())
    pause_spot_bot(vault, bot_id, now=clock())
    review(case)
    with pytest.raises(SpotBotStoreError, match="autorización anterior"):
        claim_spot_bot_cycle(vault, bot_id, now=clock(), expected_policy_id=old.policy_id)
    with pytest.raises(SpotBotStoreError, match="ciclo del bot cambió"):
        finish_spot_bot_cycle(vault, bot_id, claim, state, now=clock())
    pause_spot_bot(vault, bot_id, now=clock(), only_if_active=True, expected_policy_id=old.policy_id)
    with vault.session() as db:
        assert bot_record(db, bot_id)[0].status == "ACTIVE"
    vault.lock()


def test_stop_during_review_wins_even_with_same_wall_clock_time(tmp_path):
    case = bot_case(tmp_path)
    vault, _, _, _, clock, _, _, trader, bot_id, _ = case
    pause_spot_bot(vault, bot_id, now=clock())
    at = clock.value
    clock.value = at
    trader.pair_view_account = lambda _: pause_spot_bot(vault, bot_id, now=at)
    identifier = str(uuid4())
    with pytest.raises(SpotBotStoreError, match="cambiaron"):
        review(case, policy_id=identifier)
    with vault.session() as db:
        assert db.get(RealLiveSpotPolicy, identifier) is None
        assert bot_record(db, bot_id)[0].status == "PAUSED"
    vault.lock()


def test_new_daily_policy_and_fresh_paper_lineage_retain_old_fills(tmp_path,monkeypatch):
    from app.lab.engine import CURRENT_ENGINE_VERSION, run_backtest, validate_candles
    case = bot_case(tmp_path)
    next_entry(case)
    tick(case)
    vault, policy, _, venue, clock, paper, _, trader, bot_id, _ = case
    before = owned_spot_bot_ledger(vault, bot_id)
    pause_spot_bot(vault, bot_id, now=clock())
    clock.value += timedelta(days=2)
    hour = int(clock.value.timestamp()) // 3600 * 3600
    clock.value = datetime.fromtimestamp(hour-3600+1,timezone.utc)
    rows = [{"t":hour + (i-4)*3600,"o":"100","h":"101","l":"98","c":"99","v":"10"}
            for i in range(4)]

    class RefreshedSource:
        def get_candles(self, symbol, *, since, limit, **_):
            return [row for row in rows if row["t"] >= since][:limit]

        def get_paper_quote(self, symbol):
            return {"symbol":symbol,"source":"mexc-spot-public","event_time_ns":int(clock.value.timestamp()*1e9),
                    "bid":"99","ask":"100","last":"99"}

    bars, gaps = validate_candles(rows[:3], "1h", now=int(clock.value.timestamp()))
    dataset = paper.lab.save_dataset("BTC/USDT","1h","mexc-spot-public",bars,gaps,verified_public=True)
    definition = paper.lab.version(policy.strategy_version_id)["definition"]
    run = paper.lab.save_run(dataset["id"],policy.strategy_version_id,
                             run_backtest(bars,definition,"1h"),CURRENT_ENGINE_VERSION)
    paper.adapter_factory = RefreshedSource
    from app.lab import verification

    class LaterDatetime(datetime):
        @classmethod
        def now(cls,tz=None):
            return clock.value if tz is not None else clock.value.replace(tzinfo=None)

    monkeypatch.setattr(verification,"datetime",LaterDatetime)
    session = paper.create(policy.strategy_version_id,dataset["id"],run["id"])
    clock.value = datetime.fromtimestamp(hour+1,timezone.utc)
    assert paper.poll(session["id"])["poll_result"] == "OBSERVED"
    venue.trades = []  # La API reciente ya no devuelve los fills de hace dos días.
    limits = replace(policy,policy_id=str(uuid4()),enabled=False,reconciliation_anchor_id=None,
                     expires_at=clock.value+timedelta(days=7))
    register_spot_policy(vault,limits,now=clock())
    result, _ = review(case,limits=limits,paper_session_id=session["id"])
    assert not result["replayed"] and owned_spot_bot_ledger(vault,bot_id) == before
    assert len(trader.posts) == 1
    assert tick(case)["action"] == "WAIT"  # No compra la señal nueva revisada.
    with vault.session() as db:
        _, config, _ = bot_record(db,bot_id)
        assert config["source"]["dataset_id"] == dataset["id"]
        assert config["authorizations"][0]["source"]["dataset_id"] != dataset["id"]
    backup = vault.backup(tmp_path / "rearmed-backup.db")
    from app.portfolio.vault import RealVault
    from tests.test_mexc_execution_state import PASSWORD
    copied = RealVault(backup)
    copied.unlock(PASSWORD)
    assert owned_spot_bot_ledger(copied,bot_id) == before
    copied.lock()
    vault.lock()


def test_reserved_unsent_order_must_be_released_before_review(tmp_path):
    from app.live_execution.journal import release_unsent_spot_intent
    from app.portfolio.models import RealLiveSpotIntent
    case = bot_case(tmp_path)
    next_entry(case)

    def failure():
        raise RuntimeError("isolated order/test interruption")

    case[7].after_test = failure
    with pytest.raises(RuntimeError,match="interruption"):
        tick(case)
    with pytest.raises(SpotBotStoreError, match="seguimiento"):
        review(case)
    with case[0].session() as db:
        intent = db.query(RealLiveSpotIntent).one().id
    release_unsent_spot_intent(case[0],intent,now=case[4]())
    review(case)
    assert case[7].posts == []
    case[0].lock()


def test_final_bot_authorization_keeps_sqlite_write_gate(tmp_path,monkeypatch):
    from app.live_execution import mexc_execution
    original = mexc_execution.submit_spot_limit_once
    checked = []

    def send(*args,**kwargs):
        authorize = kwargs["assert_authorized_in_transaction"]

        def inside(db):
            authorize(db)
            assert db.connection().connection.driver_connection.in_transaction, "Nested read released the write gate"
            checked.append(True)

        kwargs["assert_authorized_in_transaction"] = inside
        return original(*args,**kwargs)

    monkeypatch.setattr(mexc_execution,"submit_spot_limit_once",send)
    case = bot_case(tmp_path)
    next_entry(case)
    assert tick(case)["action"] == "BUY" and checked == [True]
    case[0].lock()
