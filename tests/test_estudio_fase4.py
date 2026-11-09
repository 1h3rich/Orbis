"""Fase 4: intervalos 1s/30s reales, refresco coherente con la vela y tiempo real.

Cubre con COMPORTAMIENTO (funciones extraídas de app/web/static/estudio.js
ejecutadas en Node con el patrón brace-matching de las fases anteriores, y
llamadas reales al backend con http_get mockeado como ya hace la suite):

* 4.1 qué proveedores sirven 1s/30s (adapters + derivación observada en el
  frontend: cada available:false cachea proveedor/TF con su reason exacto
  y deshabilita la opción con ese texto) y URLs/parámetros reales que usa
  cada adapter (interval=1s, bar=1s/30s); el resto rechaza con motivo honesto.
* 4.2 regla única de refresco (cuarta parte de la vela, 1 s–60 s) ejecutada
  en Node; mergeCandleRows real (actualiza in situ / añade ordenado); el
  timer llama al tick combinado (ticker + vela), no solo al ticker.
* 4.3 el tick de WS repinta indicadores; el fallback sin WS refresca lo
  mismo que el timer.
* 4.0 invariante con editDrag activo en loadAll/applyLayout/cleanup
  (estados a null + navegación según herramienta) y Delete que cancela el
  arrastre (el borrado deja un solo paso en el undo).
* Cambio de TF/proveedor: recarga + recálculo (clave de caché invalida).

Este fichero solo añade; las dos equivalencias con literales antiguos están
documentadas en test_estudio_mejoras.py y test_estudio_reparacion_escape.py.
"""

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from app.marketdata import cache as _cache
from app.marketdata import service as svc
from app.marketdata.adapters import binance as _binance_mod
from app.marketdata.adapters import bitget as _bitget_mod
from app.marketdata.adapters import bybit as _bybit_mod
from app.marketdata.adapters import coinbase as _coinbase_mod
from app.marketdata.adapters import gate as _gate_mod
from app.marketdata.adapters import grvt as _grvt_mod
from app.marketdata.adapters import kraken as _kraken_mod
from app.marketdata.adapters import kucoin as _kucoin_mod
from app.marketdata.adapters import mexc as _mexc_mod
from app.marketdata.adapters import okx as _okx_mod

STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "web" / "static"
ESTUDIO = (STATIC_DIR / "estudio.js").read_text(encoding="utf-8")


def _balanced_end(src, start):
    """Índice de la `}` que cierra la `{` en `start` (misma técnica que las
    fases anteriores: strings, comentarios y regex ignorados)."""
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
    end = _balanced_end(src, start) + 1
    return src[m.start():end] + ";"


def _node_run(body, extra=""):
    if shutil.which("node") is None:
        import pytest
        pytest.skip("node no disponible")
    lib = "\n".join([
        _extract_fn(ESTUDIO, "num"),
        _extract_var(ESTUDIO, "TF_SECONDS"),
        _extract_fn(ESTUDIO, "tfSeconds"),
        _extract_fn(ESTUDIO, "autoRefreshFor"),
        _extract_fn(ESTUDIO, "mapCandleRow"),
        _extract_fn(ESTUDIO, "mergeCandleRows"),
        _extract_fn(ESTUDIO, "indCacheKey"),
        _extract_fn(ESTUDIO, "cancelEditDragApply"),
        _extract_fn(ESTUDIO, "setNavEnabled"),
        _extract_fn(ESTUDIO, "cancelDrawingInteractions"),
    ])
    full = lib + "\n" + extra + "\n" + body
    tmpdir = Path("/tmp/opencode")
    tmpdir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=str(tmpdir), prefix="fase4-",
                                     suffix=".js", delete=False, mode="w",
                                     encoding="utf-8") as fh:
        fh.write(full)
        path = fh.name
    proc = subprocess.run(["node", path], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("FASE4-JSON:")]
    assert line, proc.stdout
    return json.loads(line[0][len("FASE4-JSON:"):])


def _node_observed_helpers():
    """Helpers observados REALES de estudio.js (sin copiar lógica): la
    caché de pares proveedor/TF con su reason exacto."""
    if shutil.which("node") is None:
        import pytest
        pytest.skip("node no disponible")
    var_src = re.search(r"var TF_OBSERVED_UNAVAILABLE\s*=\s*\{.*?\};", ESTUDIO, re.S)
    assert var_src is not None, "TF_OBSERVED_UNAVAILABLE no encontrado"
    full = "\n".join([
        var_src.group(0),
        _extract_fn(ESTUDIO, "tfUnavailableReason"),
        _extract_fn(ESTUDIO, "markTfUnavailable"),
        _extract_fn(ESTUDIO, "markTfAvailable"),
    ])
    return full


def _node_run_observed(body):
    full = _node_observed_helpers() + "\n" + body
    tmpdir = Path("/tmp/opencode")
    tmpdir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=str(tmpdir), prefix="fase4o-",
                                     suffix=".js", delete=False, mode="w",
                                     encoding="utf-8") as fh:
        fh.write(full)
        path = fh.name
    proc = subprocess.run(["node", path], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("FASE4-JSON:")]
    assert line, proc.stdout
    return json.loads(line[0][len("FASE4-JSON:"):])


# -- 4.1 backend: quién sirve 1s/30s --------------------------------------


def test_adapters_declare_1s_30s_per_probe_table():
    """Solo binance/okx/gate anuncian 1s y solo okx anuncia 30s (probes
    reales del usuario); el resto ni los declara."""
    assert "1s" in _binance_mod.TF and "30s" not in _binance_mod.TF
    assert "1s" in _okx_mod.TF and "30s" in _okx_mod.TF
    assert "1s" in _gate_mod.TF and "30s" not in _gate_mod.TF
    for mod in (_kraken_mod, _bybit_mod, _coinbase_mod, _mexc_mod,
                _kucoin_mod, _bitget_mod, _grvt_mod):
        if mod is _grvt_mod:
            assert "1s" not in _grvt_mod.GrvtAdapter.timeframes
            assert "30s" not in _grvt_mod.GrvtAdapter.timeframes
            continue
        if mod is _coinbase_mod:
            assert "1s" not in mod.GRAN and "30s" not in mod.GRAN
            continue
        tfs = getattr(mod, "TF", None) or getattr(mod, "TF_TO_INTERVAL", None)
        assert tfs is not None, mod.__name__
        assert "1s" not in tfs and "30s" not in tfs, mod.__name__
    assert "1s" in _binance_mod.BinanceAdapter.timeframes
    assert "30s" in _okx_mod.OkxAdapter.timeframes
    assert "1s" in _gate_mod.GateAdapter.timeframes


def test_service_rejects_unsupported_with_honest_reason():
    """kraken/1s, binance/30s, gate/30s, coinbase/1s: available:false con
    motivo que dice qué TF no es nativo y cuáles sí (sin inventar)."""
    _cache.clear()
    try:
        for prov, tf in (("kraken", "1s"), ("kraken", "30s"),
                         ("binance", "30s"), ("gate", "30s"),
                         ("coinbase", "1s"), ("bybit", "1s"),
                         ("mexc", "30s"), ("kucoin", "1s"),
                         ("bitget", "30s")):
            out = svc.candles("BTC/USDT", provider=prov, timeframe=tf, limit=5)
            assert out["available"] is False, (prov, tf)
            assert "no nativo" in out["reason"], (prov, tf, out["reason"])
            assert tf in out["reason"], (prov, tf, out["reason"])
    finally:
        _cache.clear()


def test_adapters_request_real_1s_30s_params():
    """Con http_get mockeado: binance pide interval=1s, okx bar=1s/30s y
    gate interval=1s (los parámetros que las APIs aceptan de verdad)."""
    seen = {}

    def fake_binance(url, timeout=5.0):
        seen["binance"] = url
        return [[1700000000000, "1", "2", "0.5", "1.5", "10"]]

    def fake_okx(url, timeout=5.0):
        seen["okx"] = url
        return {"data": [["1700000000000", "1", "2", "0.5", "1.5", "10"]]}

    def fake_gate(url, timeout=5.0):
        seen["gate"] = url
        return [[1700000000, "10", "1.5", "2", "0.5", "1"]]

    b = _binance_mod.BinanceAdapter(http_get=fake_binance)
    rows = b.get_candles("BTC/USDT", timeframe="1s", limit=10)
    assert rows and rows[0]["t"] == 1700000000
    assert "interval=1s" in seen["binance"], seen["binance"]

    o = _okx_mod.OkxAdapter(http_get=fake_okx)
    rows = o.get_candles("BTC/USDT", timeframe="30s", limit=10)
    assert rows and rows[0]["t"] == 1700000000
    assert "bar=30s" in seen["okx"], seen["okx"]
    o1 = _okx_mod.OkxAdapter(http_get=fake_okx)
    o1.get_candles("BTC/USDT", timeframe="1s", limit=10)
    assert "bar=1s" in seen["okx"], seen["okx"]

    g = _gate_mod.GateAdapter(http_get=fake_gate)
    rows = g.get_candles("BTC/USDT", timeframe="1s", limit=10)
    assert rows and rows[0]["t"] == 1700000000
    assert "interval=1s" in seen["gate"], seen["gate"]


def test_frontend_derives_availability_from_observed_backend_reason():
    """EQUIVALENCIA DOCUMENTADA (M5): sustituye a
    ``test_frontend_table_matches_backend_adapters`` (la tabla estática
    ``TF_PROVIDER_UNSUPPORTED`` desapareció por diseño). La verdad sigue
    estando en el backend —los adapters declaran 1s/30s donde existen
    (``test_adapters_declare_1s_30s_per_probe_table``) y el servicio
    rechaza lo demás con motivo honesto
    (``test_service_rejects_unsupported_with_honest_reason``)— y el
    frontend la DERIVA: el reason observado para un par proveedor/TF se
    cachea íntegro y lo no probado sigue habilitado.

    Con los helpers REALES en Node: el motivo observado de kraken/1s se
    devuelve exacto; binance/1s (no probado) no bloquea nada."""
    res = _node_run_observed(r"""
const reason = "timeframe 1s no nativo en kraken (nativos: 1m, 5m). No se inventan datos.";
if (tfUnavailableReason("kraken", "1s") !== "") throw new Error("sin probar debe estar habilitado");
markTfUnavailable("kraken", "1s", reason);
if (tfUnavailableReason("kraken", "1s") !== reason) throw new Error("el reason observado debe devolverse exacto");
if (tfUnavailableReason("binance", "1s") !== "") throw new Error("lo no probado sigue habilitado");
if (tfUnavailableReason("kraken", "30s") !== "") throw new Error("otro TF del mismo proveedor sigue habilitado");
markTfAvailable("kraken", "1s");
if (tfUnavailableReason("kraken", "1s") !== "") throw new Error("tras el éxito vuelve a habilitarse");
console.log("FASE4-JSON:" + JSON.stringify({ ok: true }));
""")
    assert res["ok"] is True


# -- 4.2 regla única de refresco (comportamiento en Node) ------------------


def test_auto_refresh_single_rule_values():
    """Cuarta parte de la vela en segundos enteros, 1 s–60 s: 1s→1s,
    30s→8s (7,5 redondeado), 1m→15s, 5m y mayores→60s; TF rara→30s."""
    res = _node_run(r"""
const got = { "1s": autoRefreshFor("1s"), "30s": autoRefreshFor("30s"), "1m": autoRefreshFor("1m"), "5m": autoRefreshFor("5m"), "15m": autoRefreshFor("15m"), "30m": autoRefreshFor("30m"), "1h": autoRefreshFor("1h"), "4h": autoRefreshFor("4h"), "1d": autoRefreshFor("1d"), "1w": autoRefreshFor("1w"), "2s": autoRefreshFor("2s"), "12h": autoRefreshFor("12h"), "x": autoRefreshFor("x") };
const want = { "1s": 1000, "30s": 8000, "1m": 15000, "5m": 60000, "15m": 60000, "30m": 60000, "1h": 60000, "4h": 60000, "1d": 60000, "1w": 60000, "2s": 1000, "12h": 60000, "x": 30000 };
for (const k of Object.keys(want)) {
  if (got[k] !== want[k]) throw new Error(k + ": " + got[k] + " !== " + want[k]);
}
if (Math.min.apply(null, ["1s","30s","1m","5m","15m","30m","1h","4h","1d","1w"].map(autoRefreshFor)) < 1000) throw new Error("más de 1 pet/s");
console.log("FASE4-JSON:" + JSON.stringify({ ok: true }));
""")
    assert res["ok"] is True


def test_merge_candle_rows_updates_in_place_and_appends_sorted():
    """mergeCandleRows real: la vela abierta se actualiza sin crecer; las
    velas nuevas se añaden ordenadas; filas rotas se ignoran."""
    res = _node_run(r"""
let ds = [{ time: 1, close: "10" }, { time: 2, close: "11" }];
// 1) Solo cambia la última: no crece.
let m = mergeCandleRows(ds, [mapCandleRow({ t: 2, o: "9", h: "12", l: "9", c: "11.5", v: "3" })]);
if (m.grew !== false) throw new Error("no debería crecer");
if (ds.length !== 2 || String(ds[1].close) !== "11.5") throw new Error("in situ no aplicado: " + JSON.stringify(ds));
// 2) Velas nuevas desordenadas: crecen y quedan ordenadas.
m = mergeCandleRows(ds, [mapCandleRow({ t: 4, o: "1", h: "1", l: "1", c: "1", v: "1" }), mapCandleRow({ t: 3, o: "1", h: "1", l: "1", c: "1", v: "1" })]);
if (m.grew !== true) throw new Error("debería crecer");
if (ds.map(function (c) { return c.time; }).join(",") !== "1,2,3,4") throw new Error("orden roto: " + JSON.stringify(ds.map(function (c) { return c.time; })));
// 3) Filas rotas: se ignoran sin crecer ni romper.
m = mergeCandleRows(ds, [null, {}, { time: NaN }, { time: "x" }]);
if (m.grew !== false || ds.length !== 4) throw new Error("rotas tocaron el dataset");
console.log("FASE4-JSON:" + JSON.stringify({ ok: true }));
""")
    assert res["ok"] is True


def test_ind_cache_key_invalidates_on_tf_provider_data():
    """La clave cambia con TF, mercado, datos o parámetros; con todo igual
    se reutiliza (tick barato). Cambio de proveedor recarga (loadAll vacía
    las velas), así que los datos —y la clave— cambian con él."""
    res = _node_run(r"""
const p = '{"ema":{"on":true}}';
const k1 = indCacheKey(100, 1, 99, "10", "BTC/EUR", "1m", p);
if (indCacheKey(100, 1, 99, "10", "BTC/EUR", "1m", p) !== k1) throw new Error("misma entrada debe dar misma clave");
if (indCacheKey(100, 1, 99, "10", "BTC/EUR", "5m", p) === k1) throw new Error("cambio de TF debe invalidar");
if (indCacheKey(100, 1, 99, "10", "ETH/EUR", "1m", p) === k1) throw new Error("cambio de mercado debe invalidar");
if (indCacheKey(100, 1, 99, "10.5", "BTC/EUR", "1m", p) === k1) throw new Error("cambio de cierre debe invalidar");
if (indCacheKey(101, 1, 100, "10", "BTC/EUR", "1m", p) === k1) throw new Error("vela nueva debe invalidar");
if (indCacheKey(100, 1, 99, "10", "BTC/EUR", "1m", '{"ema":{"on":false}}') === k1) throw new Error("cambio de params debe invalidar");
console.log("FASE4-JSON:" + JSON.stringify({ ok: true }));
""")
    assert res["ok"] is True


def test_timer_tick_refreshes_candles_not_only_ticker():
    """El timer llama al tick combinado (ticker + vela); el refresco de vela
    usa la fusión incremental, repinta indicadores y respeta loading /
    solapes / dataset vacío. El WS repinta indicadores en cada tick y el
    fallback sin WS refresca lo mismo que el timer."""
    m = re.search(r"function restartEstudioTimer\(\)\s*\{(.*?)\n    \}", ESTUDIO, re.S)
    assert m is not None
    assert "refreshEstudioTick()" in m.group(1)
    m = re.search(r"function refreshEstudioTick\(\)\s*\{(.*?)\n    \}", ESTUDIO, re.S)
    assert m is not None
    assert "loadSnapshot();" in m.group(1) and "refreshOpenCandle();" in m.group(1)
    m = re.search(r"function refreshOpenCandle\(\)\s*\{(.*?)\n    \}", ESTUDIO, re.S)
    assert m is not None
    body = m.group(1)
    assert "if (loading || refreshing" in body
    assert "!candles.length" in body
    assert "mergeCandleRows(candles, rows)" in body
    assert "paintIndicators();" in body
    assert "paintAll(true)" in body  # velas nuevas: vista conservada
    assert '"&limit=5"' in body  # incremental (5 últimas, sin since: el
    # since no significa lo mismo en cada exchange y OKX pagina hacia atrás)
    assert "since=" not in body
    assert "no-store" in body  # sin esto el navegador congela la vela con
    # su caché heurística (URL estable + API sin Cache-Control)
    assert "refreshLastClose" in body  # sin cambios de cierre no se repinta
    assert "setVisibleLogicalRange({ from: vr.from + k, to: vr.to + k })" in body  # seguir en vivo solo en el borde
    m = re.search(r"ws\.onmessage = function \(ev\) \{(.*?)\n        \};", ESTUDIO, re.S)
    assert m is not None and "paintIndicators();" in m.group(1)
    m = re.search(r"function fallbackPoll\(\) \{(.*?)\n      \}", ESTUDIO, re.S)
    assert m is not None and "refreshEstudioTick()" in m.group(1)


def test_tf_and_provider_change_reload_clean():
    """Cambiar TF o proveedor recarga velas desde cero (sin restos de la TF
    anterior) y recalcula el Auto; la clave de caché invalida por TF."""
    m = re.search(r'\$\("st-tf"\)\.addEventListener\("change", function \(\) \{(.*?)\n    \}\);', ESTUDIO, re.S)
    assert m is not None
    assert "paintRefreshNote();" in m.group(1) and "loadAll();" in m.group(1)
    handlers = re.findall(r'\$\("st-prov"\)\.addEventListener\("change", function \(\) \{(.*?)\n    \}\);', ESTUDIO, re.S)
    assert any("updateTfAvailability();" in handler and "loadAll();" in handler
               for handler in handlers)
    m = re.search(r"function loadAll\(\)\s*\{(.*?)\n    \}", ESTUDIO, re.S)
    assert m is not None
    assert "candles = [];" in m.group(1)  # sin datos de la TF anterior
    assert "loadCandles(false);" in m.group(1)
    # Tras cargar con éxito, la nota de TF no soportados se mantiene (el
    # selector deshabilita con motivo lo que no existe).
    assert "updateTfAvailability();" in ESTUDIO.split("var rp = ")[1].split("loadSnapshot();")[0]


# -- 4.0 gestos a medias + Delete (comportamiento en Node) -----------------


def test_context_switch_clears_editdrag_and_sets_nav():
    """loadAll/applyLayout/cleanup abortan gestos vía la función común: con
    un editDrag activo a mitad de arrastre, el estado queda a null y la
    navegación se restaura según haya herramienta o no."""
    for name, pat in (("loadAll", r"function loadAll\(\)\s*\{(.*?)\n    \}"),
                      ("applyLayout", r"function applyLayout\(.*?\)\s*\{(.*?)\n    \}"),
                      ("cleanup", r"var cleanup = function \(\) \{(.*?)\n    \};")):
        m = re.search(pat, ESTUDIO, re.S)
        assert m is not None, name
        assert "cancelDrawingInteractions();" in m.group(1), name
    res = _node_run(r"""
// editDrag a mitad de arrastre (ya movido, con undo diferido guardado).
if (dragState === null || editDrag === null) throw new Error("el stub debe partir con gestos activos");
// Sin herramienta: anula estados y reactiva la navegación nativa.
cancelDrawingInteractions();
if (dragState !== null || editDrag !== null) throw new Error("estados no anulados");
let nav = navLog[navLog.length - 1];
if (!nav.handleScroll || nav.handleScroll.mouseWheel !== true) throw new Error("nav no restaurada sin herramienta");
// Con herramienta: anula estados pero la navegación sigue off.
dragState = { tool: "trend" };
editDrag = { id: "d1", part: -1, moved: true };
activeTool = "trend";
cancelDrawingInteractions();
if (dragState !== null || editDrag !== null) throw new Error("con herramienta también se anulan");
nav = navLog[navLog.length - 1];
if (nav.handleScroll !== false || nav.handleScale !== false) throw new Error("con herramienta la nav sigue off");
console.log("FASE4-JSON:" + JSON.stringify({ ok: true }));
""", extra="\n".join([
        "var dragState = { tool: 'trend', moved: true };",
        "var editDrag = { id: 'd1', part: -1, moved: true, orig: [], redoBackup: [] };",
        "var activeTool = null;",
        "var navLog = [];",
        "var chart = { applyOptions: function (o) { navLog.push(o); } };",
    ]))
    assert res["ok"] is True


def test_delete_cancels_editdrag_leaving_single_undo():
    """Supr con editDrag movido: primero cancela el arrastre (retira su
    paso de undo) y luego borra; el borrado deja exactamente UN paso nuevo
    en el undo en vez de dos."""
    m = re.search(r"keyHandler = function \(ev\) \{(.*?)\n    \};", ESTUDIO, re.S)
    assert m is not None
    branch = m.group(1)
    i_del = branch.index('"Delete"')
    i_rem = branch.index("removeDrawing(drawings, selectedId)")
    assert "cancelEditDragApply(drawings, editDrag, undoStack, redoStack)" in branch[i_del:i_rem]
    assert "editDrag = null" in branch[i_del:i_rem]
    res = _node_run(r"""
// Arrastre movido: undo = [previa, gesto], redo guardado aparte.
const orig = [{ t: 100, p: 10 }, { t: 200, p: 20 }];
const drawings = [{ id: "d1", type: "trend", pts: [{ t: 999, p: 99 }, { t: 1999, p: 199 }] }];
const undoStack = ['{"snap":"previa"}', '{"snap":"gesto"}'];
const redoStack = [];
const ed = { id: "d1", part: -1, orig: JSON.parse(JSON.stringify(orig)), moved: true, redoBackup: ["R1"] };
// 1) El Delete cancela el arrastre: vuelve al original y retira SU paso.
if (cancelEditDragApply(drawings, ed, undoStack, redoStack) !== true) throw new Error("cancel falso");
if (JSON.stringify(drawings[0].pts) !== JSON.stringify(orig)) throw new Error("pts no restaurados");
if (undoStack.length !== 1) throw new Error("el paso del gesto debía retirarse: " + JSON.stringify(undoStack));
// 2) El borrado guarda su paso (como hace pushUndo): queda [previa, borrado].
undoStack.push(JSON.stringify(drawings));
if (undoStack.length !== 2) throw new Error("el borrado debe dejar un solo paso nuevo");
console.log("FASE4-JSON:" + JSON.stringify({ ok: true }));
""")
    assert res["ok"] is True


def test_unavailable_reason_reaches_ui():
    """El backend rechaza con HTTP 200 + available:false y motivo: el
    Estudio lo muestra (tf-note + meta con Reintentar/Cambiar proveedor) en
    vez de un 'Sin datos' genérico."""
    m = re.search(r"function loadCandles\(backfill\)\s*\{(.*?)\n    \}", ESTUDIO, re.S)
    assert m is not None
    body = m.group(1)
    assert "res.data && res.data.available === false" in body
    assert "res.data.reason" in body
    assert body.count('id="st-retry"') >= 2  # error HTTP y available:false
