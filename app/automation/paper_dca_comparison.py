"""Replay observed DCA cash, fills and bid marks without a market request."""

from collections.abc import Callable
from decimal import Context, Decimal, localcontext
from itertools import groupby
import json

from app.automation.dca import DcaState, apply_dca_step, dca_signal_window_key, precision_for_version, seed_dca_ema
from app.automation.paper_dca_gap import dca_paper_overlap_count
from app.lab.engine import Candle, LabError, validate_candles
from app.lab.store import content_hash


DAY = 86400
WINDOW_SECONDS = 900
NS = 1_000_000_000


def initial_paper_state(rule: dict, bars: list[Candle]) -> DcaState:
    reference = max(bar.c for bar in bars) if rule.get("reference_mode") == "high_watermark" else bars[0].c
    return DcaState(cash=Decimal(rule["budget_total"]), reference=reference,
                    ema=seed_dca_ema(rule, (bar.c for bar in bars)))


def decision_payloads(observation: dict, buys: list[dict], skipped: list[dict]) -> list[dict]:
    return [observation, *({**item, "trigger_kind": item["kind"], "kind": "BUY"} for item in buys),
            *({**item, "trigger_kind": item["kind"], "kind": "SKIP"} for item in skipped)]


def replay_paper_comparison(
    session: dict, events: list[dict], rule: dict, seed: list[Candle], *,
    quote_at: Callable[[dict, str, int], tuple[Decimal, Decimal, dict]],
) -> dict:
    engine = session["engine_version"]
    started = session["started_at_ns"]
    first_t = (started // NS // DAY + 1) * DAY
    if seed[-1].t != first_t - 2 * DAY:
        raise LabError("El histórico inicial no coincide con el comienzo de PAPER")
    count = dca_paper_overlap_count(rule)
    window_key = dca_signal_window_key(rule)
    if len(seed) < count:
        raise LabError("El histórico inicial no cubre el solapamiento PAPER")

    with localcontext(Context(prec=precision_for_version(engine))):
        state = initial_paper_state(rule, seed)
        balance = peak = equity = state.cash
        max_dd = Decimal(0)
        observed = buys_total = 0
        last_quote = last_observed = None
        overlap = [bar.json() for bar in seed[-count:]]
        for due, day_rows in groupby(events, key=lambda event: event["execution_t"]):
            daily = list(day_rows)
            first = daily[0]
            observed_at = first["observed_at_ns"]
            if (due != first_t + observed * DAY or not due * NS <= observed_at < (due + WINDOW_SECONDS) * NS
                    or any(row["sequence_no"] != index or row["observed_at_ns"] != observed_at
                           or row["quote_event_ns"] != first["quote_event_ns"] for index, row in enumerate(daily))):
                raise LabError("Fechas de observación PAPER incoherentes")
            recorded = [json.loads(row["event_json"]) for row in daily]
            evidence = recorded[0]
            signal = evidence["signal_candle"]
            bars, gaps = validate_candles([*overlap, signal], "1d", now=due)
            if gaps or bars[-1].t != due - DAY or [bar.json() for bar in bars] != [*overlap, signal]:
                raise LabError("Cierres de PAPER incoherentes con el histórico")
            bid, ask, quote = quote_at(evidence["quote"], session["symbol"], observed_at)
            if quote["event_time_ns"] != first["quote_event_ns"] or quote["event_time_ns"] < due * NS:
                raise LabError("La cotización no pertenece a la ventana PAPER")
            observed += 1
            observation = {"kind": "OBSERVATION", "signal_candle": signal, "quote": quote,
                           "engine_version": engine, "execution_model": "BID_ASK_APPROXIMATION_NO_ORDER"}
            if window_key:
                observation[window_key] = [bar.json() for bar in bars]
            if observed == 1 and state.ema is not None:
                observation["ema_origin"] = state.ema.snapshot()
            buys, skipped = apply_dca_step(state, rule, signal_time=bars[-1].t, signal_close=bars[-1].c,
                execution_time=due, execution_price=ask, mark_price=bid, index=observed,
                engine_version=engine, signal_bars=bars if window_key else None)
            if decision_payloads(observation, buys, skipped) != recorded:
                raise LabError("Las decisiones PAPER no coinciden con la repetición")
            overlap = [bar.json() for bar in bars[-count:]]
            last_quote, last_observed = quote, observed_at
            buys_total += len(buys)
            equity = state.cash + state.units * bid
            peak = max(peak, equity)
            max_dd = max(max_dd, (peak - equity) / peak)
        if (session["tick_index"] != observed or session["next_exec_t"] != first_t + observed * DAY
                or json.loads(session["state_json"]) != state.json()
                or json.loads(session["overlap_json"]) != overlap
                or json.loads(session["last_quote_json"] or "null") != last_quote):
            raise LabError("El estado PAPER no coincide con el diario de observaciones")
        profit = equity - balance
        metrics = {"initial_capital": str(balance), "final_equity": str(equity),
                   "net_profit": str(profit), "return_on_initial_capital_pct": str(profit / balance * 100),
                   "remaining_cash": str(state.cash), "invested": str(state.spent), "fees": str(state.fees),
                   "units": str(state.units), "max_drawdown_pct": str(max_dd * 100), "buys": buys_total}
    return {"session_id": session["id"], "status": session["status"], "observed_days": observed,
            "first_execution_t": first_t if observed else None,
            "last_observed_at_ns": str(last_observed) if observed else None,
            "mark_bid": last_quote["bid"] if last_quote else None, "metrics": metrics,
            "snapshot_hash": content_hash({"session": session, "events": events})}
