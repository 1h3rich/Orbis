"""Heikin-Ashi sólo transforma la serie visual, nunca el histórico de mercado."""

import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest


SOURCE = (Path(__file__).resolve().parent.parent / "app/web/static/estudio.js").read_text(encoding="utf-8")


def extract(name):
    match = re.search(r"function " + re.escape(name) + r"\s*\(", SOURCE)
    assert match, name
    start = SOURCE.index("{", match.end())
    depth = 0
    for pos in range(start, len(SOURCE)):
        if SOURCE[pos] == "{":
            depth += 1
        elif SOURCE[pos] == "}":
            depth -= 1
            if depth == 0:
                return SOURCE[match.start():pos + 1]
    raise AssertionError(name)


def test_heikin_ashi_formula_and_live_last_bar_without_mutating_market_candles():
    if not shutil.which("node"):
        pytest.skip("node no disponible")
    script = "\n".join([extract("num"), extract("heikinAshiBars"),
                        extract("toSeries"), extract("latestSeriesPoint")]) + """
var chartType = 'heikin';
var candles = [
  {time:1,open:10,high:14,low:8,close:12},
  {time:2,open:12,high:20,low:10,close:18},
  {time:3,open:18,high:22,low:17,close:20}
];
const before = JSON.stringify(candles);
const all = toSeries();
const unchanged = before === JSON.stringify(candles);
candles[2].close = 16;
const latest = latestSeriesPoint();
console.log(JSON.stringify({all,latest,unchanged}));
"""
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    result = json.loads(proc.stdout)
    assert result["all"] == [
        {"time": 1, "open": 11, "high": 14, "low": 8, "close": 11},
        {"time": 2, "open": 11, "high": 20, "low": 10, "close": 15},
        {"time": 3, "open": 13, "high": 22, "low": 13, "close": 19.25},
    ]
    assert result["latest"] == {"time": 3, "open": 13, "high": 22, "low": 13, "close": 18.25}
    assert result["unchanged"] is True


def test_heikin_is_a_real_chart_type_but_indicators_stay_on_market_closes():
    assert '["heikin", "Heikin-Ashi"]' in SOURCE
    assert 'chartType === "velas" || chartType === "heikin"' in SOURCE
    assert 'if (chartType === "heikin") return heikinAshiBars(candles)' in SOURCE
    assert "var cl = closes(candles);" in SOURCE
    assert "OHLC real · " in SOURCE
