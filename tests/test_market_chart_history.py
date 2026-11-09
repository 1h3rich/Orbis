"""El colector de páginas conserva cobertura, proveedor y cursor exclusivos."""

from pathlib import Path
import subprocess


def test_chart_history_terminal_page_scope_progress_and_budget():
    script = r"""
const assert = require('node:assert/strict');
global.window = {};
require(process.argv[1]);
const {collect, yearsBefore} = window.OrbisMarketHistory;
const page = (times, overrides = {}) => ({
  candles: times.map(t => ({t})), available: true, older_pagination_supported: true,
  resolved_provider: 'binance', timeframe: '4h', symbol: 'BTC/EUR', ...overrides,
});
const options = {symbol: 'BTC/EUR', timeframe: '4h', reached: oldest => oldest <= 20};
const reply = data => ({ok: true, data});
(async () => {
  const pages = [page([30,40], {boundary_gap_seconds:5}),
                 page([15,25], {history_exhausted:true})];
  const cursors = [];
  const last = await collect(page([50,60]), before => {
    cursors.push(before);
    return reply(pages.shift());
  }, options);
  assert.deepEqual(cursors, [50,30]);
  assert.deepEqual(last.rows.map(c => c.t), [15,25,30,40,50,60]);
  assert.equal(last.hasMore, false);
  assert.equal(last.exhausted, true);
  assert.equal(last.gaps, true);
  for (const invalid of [
    page([40], {resolved_provider:'kraken'}), page([40], {timeframe:'1d'}),
    page([40], {symbol:'BTC/USD'}),
  ]) {
    const result = await collect(page([50]), () => reply(invalid), options);
    assert.equal(result.problem, 'source');
    assert.deepEqual(result.rows.map(c => c.t), [50]);
  }
  for (const times of [[40,50], [40,40], [41,40], []]) {
    const result = await collect(page([50]), () => reply(page(times)), options);
    assert.equal(result.problem, 'cursor');
    assert.equal(result.hasMore, false);
  }
  let count = 0;
  const bounded = await collect(page([100]), before => {
    count++;
    return reply(page([before-1]));
  }, options);
  assert.equal(count, 11);
  assert.equal(bounded.problem, 'limit');
  assert.equal(bounded.hasMore, true);
  let current = true;
  const cancelled = await collect(page([50]), () => {
    current = false;
    return reply(page([40]));
  }, {...options, current: () => current});
  assert.deepEqual(cancelled.rows.map(c => c.t), [50]);
  assert.equal(yearsBefore(Date.UTC(2024,1,29,12)/1000,1), Date.UTC(2023,1,28)/1000);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    source = Path(__file__).resolve().parents[1] / "app/web/static/market-chart-history.js"
    subprocess.run(["node", "-e", script, str(source)], check=True, timeout=10)
