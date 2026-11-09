"""Compare reproducible derivative runs under explicitly matched conditions."""
from collections import Counter
from decimal import Decimal

from app.lab.engine import INTERVALS, LabError, number
from app.lab.perp_book_types import exact
from app.lab.store import content_hash


METRICS = ("trades", "final_equity", "net_equity_change", "return_pct", "fees_paid", "funding_paid",
           "max_drawdown_pct", "win_rate_pct", "expectancy", "profit_factor", "exposure_pct",
           "partial_exits", "liquidated", "open_position")
PROFILE_CONTROLS = ("margin_mode", "leverage", "trade_path", "mark_path", "execution")
COST_FIELDS = ("fee_rate", "slippage_rate")
SECOND = 1_000_000_000


def _scenario(saved: dict, result: dict) -> dict:
    profile, definition = saved["profile"].payload(), saved["definition"]
    return {
        "id": saved["id"], "hash": saved["hash"], "created_at": saved["created_at"],
        "dataset_id": saved["dataset_id"], "dataset_hash": result["dataset_hash"],
        "profile_id": saved["profile_id"], "profile_hash": result["profile_hash"],
        "strategy_version_id": profile["strategy_version_id"], "strategy_hash": result["strategy_hash"],
        "symbol": saved["dataset"].symbol, "timeframe": saved["dataset"].timeframe,
        "engine": result["engine"], "direction": definition["direction"],
        "status": result["status"], "halt_reason": result["halt_reason"], "warnings": result["warnings"],
        "window": result["window"], "result_hash": content_hash(result),
        "controls": {key: profile[key] for key in PROFILE_CONTROLS} |
                    {key: definition[key] for key in ("initial_capital", *COST_FIELDS)},
        "metrics": {key: result["metrics"].get(key) for key in METRICS},
    }


def _changed(a: dict, b: dict) -> list[str]:
    return sorted(key for key in a.keys() | b.keys() if a.get(key) != b.get(key))


def _sample_equity(scenario: dict, result: dict) -> list[str]:
    """Last recorded equity at each candle boundary, plus the verified final equity."""
    window = scenario["window"]
    start, end = int(window["requested_start_ns"]), int(window["requested_end_ns"])
    step = INTERVALS[scenario["timeframe"]] * SECOND
    events = iter(result["equity"])
    event = next(events, None)
    equity = format(Decimal(scenario["controls"]["initial_capital"]), "f")
    values = []
    for boundary in range(start, end, step):
        while event is not None and int(event["time_ns"]) <= boundary:
            equity = event["equity"]
            event = next(events, None)
        values.append(equity)
    return values + [scenario["metrics"]["final_equity"]]


def _complete(scenario: dict) -> bool:
    window = scenario["window"]
    return (scenario["status"] == "COMPLETE" and not scenario["metrics"]["open_position"]
            and window["processed_through_ns"] == window["requested_end_ns"])


def _metric_deltas(first: dict, second: dict) -> dict:
    deltas = {}
    for key in METRICS:
        a, b = first[key], second[key]
        if type(a) is str and type(b) is str:
            difference = Decimal(b) - Decimal(a)
            deltas[key] = "0" if difference.is_zero() else format(difference, "f")
        elif type(a) is int and type(b) is int:
            deltas[key] = b - a
    return deltas


@exact
def build_cost_comparison(saved: dict, base: dict, alternative: dict, definition: dict) -> dict:
    def outcome(result: dict, strategy: dict) -> dict:
        return {
            "costs": {key: strategy[key] for key in COST_FIELDS},
            "strategy_hash": result["strategy_hash"], "profile_hash": result["profile_hash"],
            "result_hash": content_hash(result), "status": result["status"],
            "halt_reason": result["halt_reason"], "warnings": result["warnings"],
            "window": result["window"], "metrics": {key: result["metrics"].get(key) for key in METRICS},
        }

    original, scenario = outcome(base, saved["definition"]), outcome(alternative, definition)
    comparable = _complete(original) and _complete(scenario)
    profile = saved["profile"].payload()
    report = {
        "schema": "orbis.lab.perp.cost-comparison.v1", "environment": "LAB",
        "mode": "EXPERIMENTAL_EXECUTION_COSTS", "execution_authority": "NONE",
        "original_run": {"id": saved["id"], "hash": saved["hash"]},
        "dataset_hash": base["dataset_hash"], "strategy_version_id": profile["strategy_version_id"],
        "symbol": saved["dataset"].symbol, "timeframe": saved["dataset"].timeframe,
        "direction": saved["definition"]["direction"], "engine": base["engine"],
        "controls": {key: profile[key] for key in PROFILE_CONTROLS}
        | {key: profile[key] for key in ("entry_order", "partial_targets") if key in profile}
        | {"initial_capital": saved["definition"]["initial_capital"]},
        "base": original, "alternative": scenario,
        "deltas": _metric_deltas(original["metrics"], scenario["metrics"]) if comparable else None,
    }
    return {**report, "hash": content_hash(report)}


MARGIN_MODES = ("ISOLATED", "CROSS")
ENTRY_BLOCKS = frozenset(("ORDER_REJECTED", "ENTRY_PRE_TRADE_CANCELLED", "SKIPPED_SIZE_OR_MARGIN",
                          "SKIPPED_PRICE_BELOW_TICK"))


def margin_grid(leverages: list[str], margin_modes: list[str]) -> list[tuple[str, str]]:
    if not 1 <= len(leverages) <= 3 or not 1 <= len(margin_modes) <= 2 or any(
            mode not in MARGIN_MODES for mode in margin_modes):
        raise LabError("Elige hasta tres apalancamientos y un tipo de margen")
    values = sorted({number(raw, positive=True) for raw in leverages})
    if any(not 1 <= value <= 10000 for value in values):
        raise LabError("Apalancamiento fuera de rango")
    plain = [format(value, "f") for value in values]
    canonical = [value.rstrip("0").rstrip(".") if "." in value else value for value in plain]
    return [(mode, leverage) for leverage in canonical for mode in MARGIN_MODES if mode in margin_modes]


def margin_outcome(result: dict, *, base: bool) -> dict:
    liquidation = next((event for event in result["journal"] if event["kind"] in
                        ("ISOLATED_LIQUIDATION", "CROSS_POSITION_LIQUIDATED")), None)
    reasons = Counter((signal["action"], signal.get("reason") or "") for signal in result["signals"]
                      if signal["action"] in ENTRY_BLOCKS)
    outcome = {
        "base": base, "margin_mode": result["profile"]["margin_mode"], "leverage": result["profile"]["leverage"],
        "profile_hash": result["profile_hash"], "result_hash": content_hash(result),
        "status": result["status"], "halt_reason": result["halt_reason"], "window": result["window"],
        "warnings": result["warnings"],
        "metrics": {key: result["metrics"].get(key) for key in METRICS} |
                   {"signal_count": result["metrics"]["signal_count"]},
        "entry_restrictions": [{"action": action, "reason": reason or None, "count": count}
                               for (action, reason), count in sorted(reasons.items())],
        "liquidation": {"time_ns": liquidation["cursor"]["time_ns"], "mark_price": liquidation["price"],
                        "cause": liquidation["reason"]} if liquidation else None,
    }
    return {**outcome, "complete": _complete(outcome)}


def build_margin_comparison(saved: dict, scenarios: list[dict]) -> dict:
    profile, definition = saved["profile"].payload(), saved["definition"]
    report = {
        "schema": "orbis.lab.perp.margin-comparison.v1", "environment": "LAB",
        "mode": "EXPERIMENTAL_MARGIN_AND_LEVERAGE", "execution_authority": "NONE",
        "original_run": {"id": saved["id"], "hash": saved["hash"]},
        "dataset_hash": saved["payload"]["result"]["dataset_hash"], "strategy_version_id": profile["strategy_version_id"],
        "strategy_hash": profile["strategy_hash"], "symbol": saved["dataset"].symbol,
        "timeframe": saved["dataset"].timeframe, "direction": definition["direction"],
        "engine": saved["payload"]["result"]["engine"],
        "controls": {key: profile[key] for key in ("trade_path", "mark_path", "execution", "entry_order", "partial_targets")
                     if key in profile} | {key: definition[key] for key in ("initial_capital", *COST_FIELDS)},
        "scenarios": scenarios,
    }
    return {**report, "hash": content_hash(report)}


@exact
def build_run_comparison(base: dict, other: dict, base_result: dict, other_result: dict) -> dict:
    first, second = _scenario(base, base_result), _scenario(other, other_result)
    first_controls, second_controls = first["controls"], second["controls"]
    first_window, second_window = first["window"], second["window"]
    reasons = []
    if (first["symbol"], first["timeframe"]) != (second["symbol"], second["timeframe"]):
        reasons.append("MARKET_OR_TIMEFRAME")
    if first["dataset_hash"] != second["dataset_hash"]:
        reasons.append("DATASET")
    if any(first_window[key] != second_window[key] for key in ("requested_start_ns", "requested_end_ns")):
        reasons.append("WINDOW")
    if first["engine"] != second["engine"]:
        reasons.append("ENGINE")
    if Decimal(first_controls["initial_capital"]) != Decimal(second_controls["initial_capital"]):
        reasons.append("STARTING_CAPITAL")
    if any(Decimal(first_controls[key]) != Decimal(second_controls[key]) for key in COST_FIELDS):
        reasons.append("COSTS")
    if (Decimal(first_controls["leverage"]) != Decimal(second_controls["leverage"])
            or any(first_controls[key] != second_controls[key] for key in PROFILE_CONTROLS if key != "leverage")):
        reasons.append("SIMULATION_ASSUMPTIONS")
    if not all(_complete(scenario) for scenario in (first, second)):
        reasons.append("INCOMPLETE")

    deltas, equity = None, None
    if not reasons:
        deltas = _metric_deltas(first["metrics"], second["metrics"])
        equity = {
            "start_t": int(first_window["requested_start_ns"]) // SECOND,
            "interval_seconds": INTERVALS[first["timeframe"]],
            "base": _sample_equity(first, base_result),
            "other": _sample_equity(second, other_result),
        }
    first_profile, second_profile = base["profile"].payload(), other["profile"].payload()
    profile_parameters = ("entry_order", "partial_targets")
    report = {
        "schema": "orbis.lab.perp.run-comparison.v1", "environment": "LAB",
        "mode": "EXPERIMENTAL_CURRENT_RULES", "execution_authority": "NONE",
        "base": first, "other": second, "difference_blockers": reasons, "deltas": deltas, "equity": equity,
        "strategy_fields_changed": _changed(base["definition"], other["definition"]),
        "execution_parameters_changed": [key for key in profile_parameters
                                         if first_profile.get(key) != second_profile.get(key)],
    }
    return {**report, "hash": content_hash(report)}
