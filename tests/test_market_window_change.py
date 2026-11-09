"""La variación de mercado exige cobertura temporal, no sólo dos precios."""

import json
from pathlib import Path
import subprocess
import pytest


APP_JS = Path(__file__).resolve().parents[1] / "app/web/static/app.js"


def test_24h_and_7d_changes_reject_short_or_gapped_series():
    script = r"""
const fs = require('fs');
const source = fs.readFileSync(process.argv[1], 'utf8');
const begin = source.indexOf('function change24h(');
const end = source.indexOf('/* ---------------------- ESTADO GLOBAL', begin);
if (begin < 0 || end < 0) throw new Error('Cálculo de ventana no encontrado');
eval(source.slice(begin, end));
function series(count, step) {
  return Array.from({length:count}, (_, i) => ({t:i*step,close:String(100+i)}));
}
const day = series(25,3600), week = series(8,86400);
const dayGap = day.filter((_,i) => i < 6 || i > 9);
const oneMissing = day.filter((_,i) => i !== 10);
console.log(JSON.stringify({
  day:marketWindowChange(day,'24h'),
  week:marketWindowChange(week,'7d'),
  shortDay:marketWindowChange(day.slice(-2),'24h'),
  shortWeek:marketWindowChange(week.slice(-2),'7d'),
  missingStartDay:marketWindowChange(day.slice(1),'24h'),
  missingStartWeek:marketWindowChange(week.slice(1),'7d'),
  dayGap:marketWindowChange(dayGap,'24h'),
  oneMissing:marketWindowChange(oneMissing,'24h'),
  duplicate:marketWindowChange([...day.slice(0,5),day[4],...day.slice(5)],'24h'),
}));
"""
    result = subprocess.run(["node", "-e", script, str(APP_JS)], check=True,
                            capture_output=True, text=True)
    values = json.loads(result.stdout)
    assert values["day"] == 24
    assert values["week"] == pytest.approx(7)
    assert values["shortDay"] is None
    assert values["shortWeek"] is None
    assert values["missingStartDay"] is None
    assert values["missingStartWeek"] is None
    assert values["dayGap"] is None
    assert values["oneMissing"] is None
    assert values["duplicate"] is None
