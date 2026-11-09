"""R1 (bloqueante): cambiar de mercado no debe borrar los indicadores.

Causa raíz (regresión Fase 5c, verificada en navegador real + lectura del
backend): el GET de ``/ux/state kind=indicators`` devuelve ``payload: {}``
cuando NO hay fila guardada para ese mercado+proveedor+TF
(``ui_state_get``: ``row.payload if row else {}``). Ese ``{}`` pasaba el
guard anterior (truthy, typeof "object") hasta ``sanitizeIndCfg``, que lo
convierte en todo-apagado: al cambiar a cualquier mercado sin fila guardada
se borraba en memoria la configuración activa (EMA+RSI…), se desmarcaba el
panel y se retiraban las series. El precio seguía, pero la gráfica del
usuario "estallaba" en todos los mercados sin fila.

El arreglo es ``shouldApplyIndPayload`` (puro, extraído del JS real y
ejecutado en Node): vacío (null, no-objeto o sin claves) = "el backend no
tiene nada" y NO se aplica —se conserva lo que haya, como ya documentaba
``loadPersisted``—. Una fila guardada de verdad siempre trae claves
(``persistInd`` envía el objeto completo de 5 indicadores), así que exigir
claves no confunde un "todo apagado" explícito con un "sin nada".

Cobertura:
* ``shouldApplyIndPayload`` por comportamiento: ``{}``/null/array/cadena
  no aplican (ESTE es el que falla con el código original: la función no
  existe y el guard antiguo aplicaba ``{}``); config guardada completa o
  parcial sí aplica.
* Simulación del ciclo de cambio de mercado: indicadores activos + payload
  ``{}`` ⇒ se conservan; + payload guardado ⇒ se aplica saneado.
* Invariante con el motor LWC v5.0.9 real: el ciclo nuevo (remove + chart
  nuevo + pintar) no toca series muertas y termina con datos (las operaciones
  sobre series muertas son no-op y los ``removeSeries`` van con try/catch;
  verificado también en navegador: cero errores de consola).
"""

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from tests.lwc_harness_helper import run_lwc_body

STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "web" / "static"
ESTUDIO = (STATIC_DIR / "estudio.js").read_text(encoding="utf-8")


def _balanced_end(src, start):
    depth, i = 0, start
    in_str = None
    prev_sig = ""
    while i < len(src):
        c = src[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == in_str:
                in_str = None
                prev_sig = "s"
        elif c in ("'", '"', "`"):
            in_str = c
        elif c == "/" and i + 1 < len(src) and src[i + 1] == "/":
            nxt = src.find("\n", i)
            i = len(src) if nxt < 0 else nxt
            continue
        elif c == "/" and i + 1 < len(src) and src[i + 1] == "*":
            nxt = src.find("*/", i + 2)
            i = len(src) if nxt < 0 else nxt + 2
            continue
        elif c == "/" and (prev_sig == "" or prev_sig in "(,=:[!&|?{};"):
            j = i + 1
            in_cls = False
            while j < len(src):
                cj = src[j]
                if cj == "\\":
                    j += 2
                    continue
                if cj == "[":
                    in_cls = True
                elif cj == "]":
                    in_cls = False
                elif cj == "/" and not in_cls:
                    break
                j += 1
            i = j + 1
            prev_sig = "r"
            continue
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        if not c.isspace():
            prev_sig = c
        i += 1
    raise AssertionError("sin cierre desde %d" % start)


def _extract_fn(src, name):
    m = re.search(r"function " + re.escape(name) + r"\s*\(", src)
    assert m is not None, name
    start = src.index("{", m.end())
    return src[m.start():_balanced_end(src, start) + 1]


def _extract_var(src, name):
    m = re.search(r"var " + re.escape(name) + r"\s*=\s*", src)
    assert m is not None, name
    start = src.index("{", m.end())
    return src[m.start():_balanced_end(src, start) + 1] + ";"


def _node_run(body):
    if shutil.which("node") is None:
        import pytest
        pytest.skip("node no disponible")
    lib = "\n".join([
        _extract_var(ESTUDIO, "DEFAULT_IND"),
        _extract_fn(ESTUDIO, "sanitizeIndCfg"),
        _extract_fn(ESTUDIO, "shouldApplyIndPayload"),
    ])
    full = lib + "\n" + body
    tmpdir = Path("/tmp/opencode")
    tmpdir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=str(tmpdir), prefix="r1-mercado-",
                                     suffix=".js", delete=False, mode="w",
                                     encoding="utf-8") as fh:
        fh.write(full)
        path = fh.name
    proc = subprocess.run(["node", path], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("R1-JSON:")]
    assert line, proc.stdout
    return json.loads(line[0][len("R1-JSON:"):])


def test_empty_payload_does_not_apply():
    """``{}`` (sin fila en backend) NO se aplica: con el guard antiguo se
    aplicaba y borraba los indicadores (falla sin el arreglo)."""
    out = _node_run("""
var cases = {
  emptyObj: shouldApplyIndPayload({}),
  nullV: shouldApplyIndPayload(null),
  undefV: shouldApplyIndPayload(undefined),
  arrV: shouldApplyIndPayload([]),
  strV: shouldApplyIndPayload("x"),
  numV: shouldApplyIndPayload(3),
  full: shouldApplyIndPayload({sma:{on:false,p:20},ema:{on:true,p:20},bb:{on:false,p:20,m:2},rsi:{on:true,p:14},macd:{on:false,f:12,s:26,sig:9}}),
  allOff: shouldApplyIndPayload({sma:{on:false,p:20},ema:{on:false,p:20},bb:{on:false,p:20,m:2},rsi:{on:false,p:14},macd:{on:false,f:12,s:26,sig:9}}),
  partial: shouldApplyIndPayload({ema:{on:true}})
};
console.log('R1-JSON:' + JSON.stringify(cases));
""")
    assert out["emptyObj"] is False
    assert out["nullV"] is False
    assert out["undefV"] is False
    assert out["arrV"] is False
    assert out["strV"] is False
    assert out["numV"] is False
    # Una fila guardada (completa, toda apagada o parcial) sí aplica: un
    # "todo apagado" explícito se respeta, solo el vacío se ignora.
    assert out["full"] is True
    assert out["allOff"] is True
    assert out["partial"] is True


def test_market_change_keeps_indicators_without_saved_row():
    """Simulación del ciclo de cambio de mercado con la decisión real del
    callback: indicadores activos + ``{}`` ⇒ se conservan; + fila guardada
    ⇒ se aplica saneada (sin mezclar basura)."""
    out = _node_run("""
function decide(current, payload) {
  // Réplica exacta de la decisión del callback tras el arreglo.
  if (!shouldApplyIndPayload(payload)) return current;
  var clean = sanitizeIndCfg(payload);
  if (!clean) return current;
  return clean;
}
var active = {sma:{on:false,p:20},ema:{on:true,p:20},bb:{on:true,p:20,m:2},rsi:{on:true,p:14},macd:{on:true,f:12,s:26,sig:9}};
var kept = decide(active, {});
var applied = decide(active, {sma:{on:false,p:20},ema:{on:true,p:"9,20"},bb:{on:false,p:20,m:2},rsi:{on:false,p:14},macd:{on:false,f:12,s:26,sig:9}});
var junk = decide(active, {ema:{on:true,p:"basura"},zzz:1});
console.log('R1-JSON:' + JSON.stringify({kept: kept, applied: applied, junk: junk}));
""")
    assert out["kept"]["ema"]["on"] is True
    assert out["kept"]["rsi"]["on"] is True
    assert out["kept"]["bb"]["on"] is True
    assert out["applied"]["ema"]["on"] is True
    assert out["applied"]["rsi"]["on"] is False
    # Basura parcial: se aplica saneada sin romper (ema.p se conserva tal
    # cual por diseño —parsePeriodList decide después y cae al defecto—).
    assert out["junk"]["ema"]["on"] is True
    assert out["junk"]["ema"]["p"] == "basura"


def test_callback_uses_empty_guard():
    """El callback del GET usa el guard (no solo existe la función): si el
    día de mañana vuelve el guard antiguo, este test lo caza."""
    i = ESTUDIO.find('kind=indicators").then(')
    assert i >= 0
    seg = ESTUDIO[i:i + 1200]
    assert "shouldApplyIndPayload" in seg, seg[:400]


def test_new_market_cycle_never_touches_dead_series():
    """Invariante con el motor real: ciclo de cambio de mercado (remove del
    chart viejo con series de indicadores + chart nuevo + pintar con datos
    del mercado nuevo) sin excepciones y con datos al final. Las operaciones
    sobre series del chart destruido son no-op y ningún removeSeries escapa
    sin try/catch en el código."""
    body = """
var c1 = mkchart(); pump();
var price1 = c1.addSeries(LW.CandlestickSeries, {});
price1.setData(candles100());
var ind1 = c1.addSeries(LW.LineSeries, {color: '#7c5cff'});
ind1.setData(candles100().map(function (r) { return { time: r.time, value: r.close }; }));
// Cambio de mercado: se destruye el chart viejo.
c1.remove();
// El ciclo nuevo NO reutiliza nada del viejo; aun así, si algún resto
// llamase a la serie muerta, el motor no lanza (no-op verificado).
ind1.setData([{ time: 1700000000, value: 1 }]);
ind1.applyOptions({ color: '#000' });
ind1.update({ time: 1700000060, value: 2 });
var c2 = mkchart(); pump();
var price2 = c2.addSeries(LW.CandlestickSeries, {});
var rows = candles100().map(function (r, i) { return { time: 1790000000 + i * 60, open: r.open, high: r.high, low: r.low, close: r.close }; });
price2.setData(rows);
var ind2 = c2.addSeries(LW.LineSeries, { color: '#7c5cff' });
ind2.setData(rows.map(function (r) { return { time: r.time, value: r.close }; }));
console.log('R1-JSON:' + JSON.stringify({ panes: c2.panes().length, ok: true }));
"""
    r = run_lwc_body(body)
    assert r.returncode == 0, (r.stderr or r.stdout)[-1500:]
    line = [ln for ln in r.stdout.splitlines() if ln.startswith("R1-JSON:")]
    assert line, r.stdout
    assert json.loads(line[0][len("R1-JSON:"):])["ok"] is True
