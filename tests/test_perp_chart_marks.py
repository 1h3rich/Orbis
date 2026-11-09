"""Las ejecuciones GRVT PAPER conservan precio y no inventan velas en huecos."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


SOURCE = (Path(__file__).resolve().parent.parent / "app/web/static/perp.js").read_text(encoding="utf-8")


def test_chart_marks_uses_price_points_for_missing_and_pending_candles():
    if not shutil.which("node"):
        pytest.skip("node no disponible")
    function = SOURCE.split("  function chartMarks(", 1)[1].split("\n  function loadPerpChart()", 1)[0]
    function = "function chartMarks(" + function
    script = function + """
const bars = [{time:1000},{time:4600},{time:11800}];
const positions = [{id:'p1',side:'LONG'}];
function event(id, second) { return {id,position_id:'p1',kind:'OPEN',observed_at_ns:String(second*1e9),
  details:{fill_price:id === 'b' ? '102.5' : '101'}}; }
const result = chartMarks(bars, positions, [event('a',2000),event('b',6400),
  event('gap',8200),event('c',12000),event('pending',15450),event('late',20000)],
  [{session_id:'s1',candle_t:4600,direction:'LONG',entry_match:true,exit_match:false}],3600);
console.log(JSON.stringify(result));
"""
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    result = json.loads(proc.stdout)
    assert result["paperCount"] == 5
    assert result["signalCount"] == 1
    assert result["outside"] == 1
    assert result["pending"] == 2
    assert [(action["marker"]["id"], action["time"], action["price"])
            for action in result["actions"]] == [
        ("a", 1000, 101), ("b", 4600, 102.5), ("gap", 8200, 101),
        ("c", 11800, 101), ("pending", 15400, 101),
    ]
    assert len(result["items"]) == 1 and result["items"][0]["text"] == "S·IN"


def test_chart_marker_loader_pages_same_timestamp_without_truncating():
    if not shutil.which("node"):
        pytest.skip("node no disponible")
    function = "function fetchSignalMarkerPages(" + SOURCE.split(
        "  function fetchSignalMarkerPages(", 1)[1].split("\n  function loadPerpChart()", 1)[0]
    script = function + """
const paths = [];
const pages = [
  {markers:[{session_id:'a',candle_t:1000}],has_more:true,next_after_t:1000,next_after_session_id:'a'},
  {markers:[{session_id:'b',candle_t:1000}],has_more:true,next_after_t:1000,next_after_session_id:'b'},
  {markers:[{session_id:'c',candle_t:4600}],has_more:false,next_after_t:null,next_after_session_id:null}
];
function request(path) {
  paths.push(path);
  return Promise.resolve(Object.assign({source:'GRVT_PAPER_OBSERVED_SIGNALS',
    execution:'SIGNALS_SEPARATE_FROM_PAPER_EVENTS',symbol:'BTC/USDT:PERP',timeframe:'1h'},pages.shift()));
}
fetchSignalMarkerPages(request,()=>true,'account','BTC/USDT:PERP','1h',
  [{time:1000},{time:4600}]).then(result=>console.log(JSON.stringify({result,paths})));
"""
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    data = json.loads(proc.stdout)
    assert [row["session_id"] for row in data["result"]["signals"]] == ["a", "b", "c"]
    assert len(data["paths"]) == 3
    assert "since_t=1000&until_t=4600" in data["paths"][0]
    assert "after_t=1000&after_session_id=a" in data["paths"][1]
    assert "after_t=1000&after_session_id=b" in data["paths"][2]
