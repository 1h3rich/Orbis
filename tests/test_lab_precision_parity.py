"""LAB y PAPER deben decidir igual aunque el llamador use otra precisión Decimal."""

from decimal import Decimal, localcontext

from app.lab.engine import (CURRENT_ENGINE_VERSION, Candle, IndicatorCache,
                            evaluate, paper_precision_for_engine, run_backtest,
                            validate_strategy)


def near_equal_bars():
    prices = ["0.9999999999999999999999999999"] * 2 + ["1", "1"]
    return [Candle((index + 1) * 3600, Decimal(price), Decimal(price),
                   Decimal(price), Decimal(price), Decimal("1"))
            for index, price in enumerate(prices)]


def test_ema_signal_is_independent_of_caller_decimal_context():
    bars = near_equal_bars()
    rule = {"kind": "CLOSE_ABOVE_EMA", "period": 2}
    with localcontext() as context:
        context.prec = 16
        paper = evaluate(rule, bars, 2, IndicatorCache(bars))
        cached = IndicatorCache(bars)
        cached.value("EMA", 2, 2)
        paper_with_prefilled_cache = evaluate(rule, bars, 2, cached)
    with localcontext() as context:
        context.prec = 80
        lab = evaluate(rule, bars, 2, IndicatorCache(bars))
    assert paper == lab == paper_with_prefilled_cache
    assert paper[0] is True
    assert Decimal(paper[1]["threshold"]) < bars[2].c


def test_legacy_paper_precision_remains_replayable_without_changing_old_lab_runs():
    bars = near_equal_bars()
    rule = {"kind": "CLOSE_ABOVE_EMA", "period": 2}
    assert CURRENT_ENGINE_VERSION == "lab-1.16"
    assert paper_precision_for_engine("lab-1.15") == 28
    assert paper_precision_for_engine(CURRENT_ENGINE_VERSION) == 80
    legacy = evaluate(rule, bars, 2,
                      IndicatorCache(bars, precision=paper_precision_for_engine("lab-1.15")))
    current = evaluate(rule, bars, 2,
                       IndicatorCache(bars, precision=paper_precision_for_engine(CURRENT_ENGINE_VERSION)))
    assert legacy[0] is False
    assert current[0] is True


def test_backtest_and_independent_evaluator_agree_on_near_equal_prices():
    bars = near_equal_bars()
    strategy = validate_strategy({
        "direction": "LONG", "initial_capital": "1000", "allocation": "0.5",
        "fee_rate": "0", "slippage_rate": "0", "stop_loss": "0.1",
        "take_profit": "0.2", "entry": {"kind": "CLOSE_ABOVE_EMA", "period": 2},
    })
    with localcontext() as context:
        context.prec = 16
        observed = evaluate(strategy["entry"], bars, 2)
    result = run_backtest(bars, strategy)
    previous_result = run_backtest(bars, strategy, engine_version="lab-1.15")
    assert result == previous_result
    assert result["events"][0]["signal_time"] == bars[2].t
    assert result["events"][0]["explanation"] == observed[1]
    assert result["trades"][0]["entry_time"] == bars[3].t
