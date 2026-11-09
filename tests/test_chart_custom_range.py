"""El control compartido sólo encuadra velas realmente cargadas."""

import subprocess
from pathlib import Path


STATIC = Path(__file__).resolve().parent.parent / "app" / "web" / "static"

def test_calendar_year_range_clamps_a_leap_day():
    source = (STATIC / "chart-range.js").read_text(encoding="utf-8")
    script = """
const vm = require('node:vm');
const window = {};
vm.runInNewContext(process.argv[1], {window});
const feb29 = Date.UTC(2024, 1, 29) / 1000;
const back = window.OrbisChartRange.yearsBeforeDay;
if (back(feb29, 1) !== Date.UTC(2023, 1, 28) / 1000 ||
    back(feb29, 4) !== Date.UTC(2020, 1, 29) / 1000) process.exit(1);
"""
    subprocess.run(["node", "-e", script, source], check=True)


def test_custom_range_clamps_to_loaded_candles_and_rejects_empty_dates():
    source = (STATIC / "chart-range.js").read_text(encoding="utf-8")
    script = r"""
const vm = require('node:vm');
const source = process.argv[1];
const fields = Object.fromEntries([
  'select', '[data-range-coverage]', '[data-range-dates]',
  '[data-range-from]', '[data-range-to]', '[data-range-edit]',
  '[data-range-error]', '[data-range-cancel]', '[data-range-apply]'
].map(key => [key, {value:'', textContent:'', hidden:false, listeners:{},
  addEventListener(name, fn) { this.listeners[name] = fn; },
  setAttribute(name, value) { this[name] = value; }, focus() {}}]));
const host = {dataset:{}, innerHTML:'', querySelector(key) { return fields[key]; }};
const calls = [];
const chart = {timeScale() { return {
  fitContent() { calls.push(['all']); },
  setVisibleRange(range) { calls.push(['time', range]); },
  setVisibleLogicalRange(range) { calls.push(['logical', range]); }
}; }};
const window = {};
vm.runInNewContext(source, {window, Date, Number, Array});
const first = Date.parse('2020-01-01T00:00:00Z') / 1000;
const rows = [0, 1, 2, 3, 4].map(day => ({t:first + day * 86400}));
window.OrbisChartRange.mount(host, chart, rows);
const select = fields.select;
select.value = 'custom'; select.listeners.change();
if (fields['[data-range-dates]'].hidden) throw Error('Fechas ocultas');
if (select.value !== 'all' || host.dataset.view !== 'all') throw Error('Borrador cambiado antes de aplicar');
if (fields['[data-range-from]'].min !== '2020-01-01' ||
    fields['[data-range-to]'].max !== '2020-01-05') throw Error('Cobertura incorrecta');
const beforeEdit = calls.length;
fields['[data-range-from]'].value = '2020-01-02';
fields['[data-range-to]'].value = '2020-01-04';
if (calls.length !== beforeEdit) throw Error('Una fecha sin confirmar alteró la vista');
fields['[data-range-apply]'].listeners.click();
let last = calls.at(-1);
if (last[0] !== 'time' || last[1].from !== rows[1].t || last[1].to !== rows[3].t)
  throw Error('Rango personalizado incorrecto: ' + JSON.stringify(last));
if (select.value !== 'custom' || !fields['[data-range-dates]'].hidden)
  throw Error('Aplicación del rango incompleta');
fields['[data-range-edit]'].listeners.click();
fields['[data-range-from]'].value = '2020-01-03';
fields['[data-range-to]'].value = '2020-01-03';
fields['[data-range-apply]'].listeners.click();
last = calls.at(-1);
if (last[0] !== 'logical' || last[1].from !== 1.5 || last[1].to !== 2.5)
  throw Error('Una vela incorrecta: ' + JSON.stringify(last));
const before = calls.length;
fields['[data-range-edit]'].listeners.click();
fields['[data-range-from]'].value = '2020-01-05';
fields['[data-range-to]'].value = '2020-01-02';
fields['[data-range-apply]'].listeners.click();
if (calls.length !== before || !fields['[data-range-error]'].textContent.includes('en orden'))
  throw Error('Fechas invertidas deberían rechazarse');
fields['[data-range-cancel]'].listeners.click();
if (fields['[data-range-from]'].value !== '2020-01-03' || !fields['[data-range-dates]'].hidden)
  throw Error('Cancelar no recuperó las fechas aplicadas');
select.value = 'all'; select.listeners.change();
if (calls.at(-1)[0] !== 'all' || !fields['[data-range-dates]'].hidden)
  throw Error('Todo disponible no restaura vista');
window.OrbisChartRange.mount(host, chart, rows, {eventTimes:[first + 5 * 86400]});
if (fields['[data-range-to]'].max !== '2020-01-06') throw Error('Operación posterior no seleccionable');
if (!fields['[data-range-coverage]'].textContent.includes('sin OHLC'))
  throw Error('Evento sin vela no identificado');
fields.select.value = '1'; fields.select.listeners.change();
last = calls.at(-1);
if (last[0] !== 'time' || last[1].to !== first + 5 * 86400)
  throw Error('La operación posterior quedó fuera del rango');
window.OrbisChartRange.mount(host, chart, rows, {eventTimes:[first - 86400]});
if (fields['[data-range-from]'].min !== '2019-12-31' ||
    !fields['[data-range-coverage]'].textContent.includes('operaciones desde 2019-12-31 (sin OHLC)'))
  throw Error('Un fill anterior no es seleccionable o se presenta como vela');
fields.select.value = 'custom'; fields.select.listeners.change();
fields['[data-range-from]'].value = '2019-12-31';
fields['[data-range-to]'].value = '2019-12-31';
fields['[data-range-apply]'].listeners.click();
if (calls.at(-1)[0] !== 'logical') throw Error('No se puede inspeccionar la operación anterior sin OHLC');
window.OrbisChartRange.mount(host, chart, rows, {eventTimes:[first + 4 * 86400 + 3600],coverageEnd:first+5*86400});
if (fields['[data-range-coverage]'].textContent.includes('sin OHLC'))
  throw Error('Un fill dentro de la última vela no debe declararse sin cobertura');
"""
    proc = subprocess.run(
        ["node", "-e", script, source], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr


def test_spot_paper_legacy_range_preferences_survive_shared_control():
    source = (STATIC / "chart-range.js").read_text(encoding="utf-8")
    script = r"""
const vm = require('node:vm');
const values = {};
const localStorage = {getItem(key) { return values[key] ?? null; }, setItem(key, value) { values[key] = value; }};
const window = {};
vm.runInNewContext(process.argv[1], {window, Date, Number, Array, localStorage});
const first = Date.parse('2025-01-01T00:00:00Z') / 1000;
const rows = [0, 1, 2].map(i => ({t:first + i * 3600}));
function mount() {
  const fields = Object.fromEntries([
    'select', '[data-range-coverage]', '[data-range-dates]', '[data-range-from]',
    '[data-range-to]', '[data-range-edit]', '[data-range-error]',
    '[data-range-cancel]', '[data-range-apply]'
  ].map(key => [key, {value:'', textContent:'', hidden:false,
    addEventListener() {}, setAttribute() {}, focus() {}}]));
  const host = {dataset:{}, querySelector(key) { return fields[key]; }};
  const calls = [];
  const chart = {timeScale() { return {
    fitContent() { calls.push('all'); },
    setVisibleRange(range) { calls.push(['time',range]); },
    setVisibleLogicalRange(range) { calls.push(['logical',range]); }
  }; }};
  window.OrbisChartRange.mount(host, chart, rows, {
    storageKey:'orbis.chartRange.spotPaper', selected:'last:100',
    lastCounts:[100,500], legacySpot:true
  });
  return {fields,host,calls};
}
values['orbis.chartRange.spotPaper'] = '10y';
let result = mount();
if (result.fields.select.value !== '10' || result.host.dataset.view !== '10')
  throw Error('Periodo antiguo 10y no conservado');
values['orbis.chartRange.spotPaper'] = '100';
result = mount();
if (result.fields.select.value !== 'last:100' || result.calls.at(-1)[0] !== 'logical')
  throw Error('Últimas 100 no conservadas');
values['orbis.chartRange.spotPaper'] = 'custom';
values['orbis.chartRange.spotPaper.from'] = '2025-01-01';
values['orbis.chartRange.spotPaper.to'] = '2025-01-01';
result = mount();
if (result.fields.select.value !== 'custom' || result.host.dataset.view !== 'custom' ||
    result.fields['[data-range-from]'].value !== '2025-01-01')
  throw Error('Fechas antiguas no conservadas');
values['orbis.chartRange.spotPaper.to'] = '2024-12-31';
result = mount();
if (result.fields.select.value !== 'last:100') throw Error('Fechas antiguas inválidas no descartadas');
"""
    proc = subprocess.run(
        ["node", "-e", script, source], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr

