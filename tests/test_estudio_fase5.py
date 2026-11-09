"""Fase 5 (5.1 rendimiento + 5.3 persistencia): reutilización de series de
indicadores y verificación de que la personalización sobrevive.

5.1 ANTES (medido en stub Node sobre el código previo, todo activo
SMA20+EMA20/50+BB×3+RSI14+MACD×2 = 9 series): repintado 1 → 9 add/0 rm;
repintados 2..10 → 9 add + 9 rm cada uno (90 recreaciones en 10
repintados: se rompía y se recreaba el indicador entero en cada tick).
DESPUÉS (este fichero, mismo harness): repintado 1 → 9 add/0 rm;
repintados 2..10 → 0 add + 0 rm + 9 setData cada uno. La serie se crea
solo la primera vez o cuando cambia su configuración; se elimina solo
al apagarse el indicador o destruirse el chart. Valores, colores y
leyenda idénticos (misma caché indCacheKey, mismos rows).

Qué exige recrear en LWC v5.0.9 (verificado en harness sobre el motor
real, lwc_harness_helper): NADA de lo usado aquí —applyOptions cambia
color/título/grosor e incluso priceScaleId sin recrear, setData
actualiza valores, y series.moveToPane() mueve de pane en vivo—. El
pane se fija en addSeries pero moveToPane lo cambia sin recrear, así
que ni un cambio de pane obligaría a recrear (camino defensivo).
"""

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

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


def _paint_lib():
    return "\n".join([
        _extract_fn(ESTUDIO, "num"),
        _extract_fn(ESTUDIO, "closes"),
        _extract_fn(ESTUDIO, "parsePeriodList"),
        _extract_fn(ESTUDIO, "emaColorFor"),
        "var EMA_PALETTE = " + ESTUDIO.split("var EMA_PALETTE = ")[1].split(";")[0] + ";",
        "var EMA_FIXED_COLORS = " + ESTUDIO.split("var EMA_FIXED_COLORS = ")[1].split(";")[0] + ";",
        _extract_fn(ESTUDIO, "sma"),
        _extract_fn(ESTUDIO, "ema"),
        _extract_fn(ESTUDIO, "bollinger"),
        _extract_fn(ESTUDIO, "rsi"),
        _extract_fn(ESTUDIO, "macd"),
        _extract_fn(ESTUDIO, "indCacheKey"),
        _extract_fn(ESTUDIO, "paintIndicators"),
    ])


def _node_run(body, extra=""):
    if shutil.which("node") is None:
        import pytest
        pytest.skip("node no disponible")
    lib = _paint_lib()
    full = lib + "\n" + extra + "\n" + body
    tmpdir = Path("/tmp/opencode")
    tmpdir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=str(tmpdir), prefix="fase5-",
                                     suffix=".js", delete=False, mode="w",
                                     encoding="utf-8") as fh:
        fh.write(full)
        path = fh.name
    proc = subprocess.run(["node", path], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("FASE5-JSON:")]
    assert line, proc.stdout
    return json.loads(line[0][len("FASE5-JSON:"):])


_PRELUDE = r"""
var __counts = { add: 0, rm: 0, set: 0, apply: 0, move: 0 };
var __seriesByKey = {};
var __lastOpts = {}, __lastRows = {};
function __mkSeries(key) {
  return {
    __key: key,
    setData: function (rows) { __counts.set++; __lastRows[this.__key] = rows; },
    applyOptions: function (o) { __counts.apply++; __lastOpts[this.__key] = o; },
    moveToPane: function () { __counts.move++; },
  };
}
var chart = {
  addSeries: function (kind, opts, pane) {
    __counts.add++;
    var s = __mkSeries("s" + __counts.add);
    if (opts && opts.title) { __lastOpts[s.__key] = opts; }
    return s;
  },
  removeSeries: function () { __counts.rm++; },
};
var LW0 = { LineSeries: {} };
var window = { LightweightCharts: LW0 };
var priceSeries = {};
var candles = [];
for (var i = 0; i < 200; i++) candles.push({ time: 1700000000 + i * 60, open: 100 + i, high: 101 + i, low: 99 + i, close: 100.5 + i, v: 10 });
var indCfg = { sma: { on: true, p: 20 }, ema: { on: true, p: "20,50" }, bb: { on: true, p: 20, m: 2 }, rsi: { on: true, p: 14 }, macd: { on: true, f: 12, s: 26, sig: 9 } };
var indCache = { key: null, vals: null };
var indSeries = {};
var indSeriesMeta = {};
var market = "BTC/EUR", timeframe = "1h";
"""


def test_reutiliza_series_en_repintados_idénticos():
    """N repintados seguidos NO recrean las series N veces: 9 add una sola
    vez y 0 removeSeries; cada repintado solo hace 9 setData."""
    res = _node_run(r"""
var per = [];
for (var k = 0; k < 10; k++) {
  __counts = { add: 0, rm: 0, set: 0, apply: 0, move: 0 };
  paintIndicators();
  per.push([__counts.add, __counts.rm, __counts.set]);
}
console.log("FASE5-JSON:" + JSON.stringify({ per: per, nseries: Object.keys(indSeries).length }));
""", extra=_PRELUDE)
    per = res["per"]
    assert res["nseries"] == 9, res  # sma20 ema20 ema50 bb×3 rsi14 macd×2
    assert per[0][0] == 9 and per[0][1] == 0, per  # primera vez: se crean
    for k in range(1, 10):
        assert per[k][0] == 0 and per[k][1] == 0, (k, per)  # después: 0 add + 0 rm
        assert per[k][2] == 9, (k, per)  # pero los datos sí se actualizan


def test_tick_actualiza_valores_sin_recrear():
    """Con el cierre movido (tick de WS) no hay add/rm y la última fila de
    la EMA refleja el recálculo (mismos valores que ema() directo)."""
    res = _node_run(r"""
paintIndicators();
var before = JSON.stringify(__lastRows);
var adds0 = __counts.add, rms0 = __counts.rm;
candles[candles.length - 1].close = 999.5;
__counts = { add: 0, rm: 0, set: 0, apply: 0, move: 0 };
paintIndicators();
var ref = ema(closes(candles), 20);
var tail = null;
for (var k in __lastRows) {
  var rows = __lastRows[k];
  if (rows.length && Math.abs(rows[rows.length - 1].value - ref[ref.length - 1]) < 1e-9) tail = rows[rows.length - 1].value;
}
console.log("FASE5-JSON:" + JSON.stringify({ add: __counts.add, rm: __counts.rm, set: __counts.set, changed: JSON.stringify(__lastRows) !== before, tail: tail, ref: ref[ref.length - 1] }));
""", extra=_PRELUDE)
    assert res["add"] == 0 and res["rm"] == 0, res
    assert res["set"] == 9 and res["changed"] is True, res
    assert res["tail"] is not None and abs(res["tail"] - res["ref"]) < 1e-9, res


def test_apagar_indicador_elimina_solo_sus_series():
    """Apagar RSI elimina exactamente su serie (1 rm, 0 add en el resto);
    al encenderlo se crea de nuevo (1 add) con sus filas."""
    res = _node_run(r"""
paintIndicators();
__counts = { add: 0, rm: 0, set: 0, apply: 0, move: 0 };
indCfg.rsi.on = false;
paintIndicators();
var afterOff = { add: __counts.add, rm: __counts.rm, keys: Object.keys(indSeries).sort() };
__counts = { add: 0, rm: 0, set: 0, apply: 0, move: 0 };
indCfg.rsi.on = true;
paintIndicators();
console.log("FASE5-JSON:" + JSON.stringify({ off: afterOff, onAdd: __counts.add, onRm: __counts.rm, keys: Object.keys(indSeries).sort() }));
""", extra=_PRELUDE)
    assert res["off"]["rm"] == 1 and res["off"]["add"] == 0, res
    assert [k for k in res["off"]["keys"] if k.startswith("rsi")] == [], res
    assert res["onAdd"] == 1 and res["onRm"] == 0, res
    assert "rsi14" in res["keys"], res


def test_cambiar_periodo_retira_clave_vieja_y_reusa_la_que_sigue():
    """Pasar EMA de "20,50" a "50": ema20 se elimina (1 rm), ema50 se
    reutiliza (0 add para ella); el total de add es 0."""
    res = _node_run(r"""
paintIndicators();
__counts = { add: 0, rm: 0, set: 0, apply: 0, move: 0 };
indCfg.ema.p = "50";
paintIndicators();
console.log("FASE5-JSON:" + JSON.stringify({ add: __counts.add, rm: __counts.rm, keys: Object.keys(indSeries).sort() }));
""", extra=_PRELUDE)
    assert res["add"] == 0 and res["rm"] == 1, res
    assert "ema20" not in res["keys"] and "ema50" in res["keys"], res


def test_leyenda_y_colores_identicos_al_reutilizar():
    """Títulos "EMA <p>"/"SMA 20"/"BB …"/"RSI 14"/"MACD…" y colores por
    periodo (emaColorFor) llegan al motor también en la vía de
    reutilización (applyOptions), no solo al crear."""
    res = _node_run(r"""
paintIndicators();
__counts = { add: 0, rm: 0, set: 0, apply: 0, move: 0 };
paintIndicators();
var titles = {}, colors = {};
for (var k in indSeries) {
  var o = __lastOpts[indSeries[k].__key] || {};
  if (o.title) titles[o.title] = (titles[o.title] || 0) + 1;
  colors[k] = o.color;
}
console.log("FASE5-JSON:" + JSON.stringify({ titles: titles, colors: colors, nApply: __counts.apply, c20: emaColorFor(20) }));
""", extra=_PRELUDE)
    for t in ("EMA 20", "EMA 50", "SMA 20", "BB sup", "BB med", "BB inf",
              "RSI 14", "MACD", "MACD señal"):
        assert t in res["titles"], (t, res["titles"])
    assert res["nApply"] == 9, res  # cada serie recibe applyOptions al reutilizar
    assert res["colors"]["ema20"] == res["c20"] == "#7c5cff", res


# -- 5.3 persistencia: qué se guarda, dónde y sin mezclar contextos --------

def _persist_lib():
    return "\n".join([
        _extract_fn(ESTUDIO, "persistenceKey"),
        _extract_fn(ESTUDIO, "loadLocal"),
        _extract_fn(ESTUDIO, "saveLocal"),
        _extract_fn(ESTUDIO, "isValidHexColor"),
        _extract_fn(ESTUDIO, "sanitizeCandles"),
    ])


def _persist_run(body, extra=""):
    if shutil.which("node") is None:
        import pytest
        pytest.skip("node no disponible")
    store = "var __store = {};\nvar localStorage = { getItem: function (k) { return (__store[k] === undefined ? null : __store[k]); }, setItem: function (k, v) { __store[k] = String(v); }, removeItem: function (k) { delete __store[k]; } };\n"
    pal = "var CANDLE_PRESETS = " + ESTUDIO.split("var CANDLE_PRESETS = ")[1].split("};")[0] + "};"
    full = _persist_lib() + "\n" + pal + "\n" + store + "\n" + extra + "\n" + body
    tmpdir = Path("/tmp/opencode")
    tmpdir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=str(tmpdir), prefix="fase5p-",
                                     suffix=".js", delete=False, mode="w",
                                     encoding="utf-8") as fh:
        fh.write(full)
        path = fh.name
    proc = subprocess.run(["node", path], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("FASE5P-JSON:")]
    assert line, proc.stdout
    return json.loads(line[0][len("FASE5P-JSON:"):])


def test_claves_no_mezclan_contextos_y_globales_son_globales():
    """Dibujos: clave por mercado|proveedor|TF (cambiar de contexto no
    mezcla); colores/refresco/iman/marcadores: cfg|all|x (globales, la
    misma clave en cualquier contexto)."""
    res = _persist_run(r"""
var d1 = persistenceKey("BTC/EUR", "kraken", "1h", "drawings");
var d2 = persistenceKey("ETH/EUR", "kraken", "1h", "drawings");
var d3 = persistenceKey("BTC/EUR", "kraken", "5m", "drawings");
var d4 = persistenceKey("BTC/EUR", "binance", "1h", "drawings");
var g1 = persistenceKey("cfg", "all", "x", "candles");
var g2 = persistenceKey("cfg", "all", "x", "refresh");
var g3 = persistenceKey("cfg", "all", "x", "magnet");
console.log("FASE5P-JSON:" + JSON.stringify({ d1: d1, d2: d2, d3: d3, d4: d4, g1: g1, g2: g2, g3: g3 }));
""")
    assert len({res["d1"], res["d2"], res["d3"], res["d4"]}) == 4, res
    assert res["g1"] == "orbis.estudio.cfg|all|x|candles", res
    assert res["g2"] == "orbis.estudio.cfg|all|x|refresh", res
    assert res["g3"] == "orbis.estudio.cfg|all|x|magnet", res


def test_dibujos_roundtrip_con_propiedades_nuevas():
    """El mismo mecanismo guarda/restaura dibujos con width/style/opacity
    (props Fase 3): round-trip JSON identico, sin perder ni inventar, y sin
    fugas al contexto de otro mercado."""
    res = _persist_run(r"""
var drawings = [
  { id: "a1", type: "trend", pts: [{ t: 1, p: 2 }, { t: 3, p: 4 }], color: "#e3b341", width: 2.5, style: "dashed", opacity: 0.5 },
  { id: "a2", type: "freehand", pts: [{ t: 5, p: 6 }], color: "#4da3ff" },
];
saveLocal("BTC/EUR", "kraken", "1h", "drawings", drawings);
var back = loadLocal("BTC/EUR", "kraken", "1h", "drawings", []);
var other = loadLocal("ETH/EUR", "kraken", "1h", "drawings", "vacio");
console.log("FASE5P-JSON:" + JSON.stringify({ same: JSON.stringify(back) === JSON.stringify(drawings), other: other }));
""")
    assert res["same"] is True, res
    assert res["other"] == "vacio", res


def test_guardado_y_carga_usan_la_misma_clave():
    """Paridad save/load por caso (misma triple mercado|proveedor|TF y
    mismo kind): dibujos, indicadores, velas, refresco, iman, marcadores,
    layouts, dock, fiblevels, alertas y watchlist. Si alguna difiriese, lo
    guardado no se restauraria."""
    pairs = [
        ('saveLocal(market, provider, timeframe, "drawings", drawings);',
         'loadLocal(market, provider, timeframe, "drawings", [])'),
        ('saveLocal("__ind", provider, timeframe, "indicators", indCfg);',
         'loadLocal("__ind", provider, timeframe, "indicators"'),
        ('saveLocal("cfg", "all", "x", "candles", candleCfg);',
         'loadLocal("cfg", "all", "x", "candles", null)'),
        ('saveLocal("cfg", "all", "x", "refresh", refreshMode);',
         'loadLocal("cfg", "all", "x", "refresh", "auto")'),
        ('saveLocal("cfg", "all", "x", "magnet", magnet);',
         'loadLocal("cfg", "all", "x", "magnet", true)'),
        ('saveLocal("cfg", "all", "x", "showpaper", showPAPER);',
         'loadLocal("cfg", "all", "x", "showpaper", true)'),
        ('saveLocal("cfg", "all", "x", "showdca", showDCA);',
         'loadLocal("cfg", "all", "x", "showdca", true)'),
        ('localStorage.setItem(LAYOUTS_KEY, JSON.stringify(layouts));',
         'localStorage.getItem(LAYOUTS_KEY)'),
        ('localStorage.setItem(DOCK_KEY', 'DOCK_KEY'),
        ('kind: "drawings"', 'kind=drawings'),
        # indicators: PUT al backend (kind: "indicators") + GET de respaldo en
        # loadPersisted con la misma prioridad que dibujos (el local manda;
        # el backend solo rellena si el local esta vacio) y guarda de
        # contexto (ver test_indicadores_get_respaldo_*).
        ('kind: "indicators"', 'kind=indicators'),
        ('kind: "layouts"', 'kind=layouts'),
        ('kind: "toolsdock"', 'kind=toolsdock'),
        ('kind: "fiblevels"', 'kind=fiblevels'),
        ('kind: "candles"', 'kind=candles'),
    ]
    for save, load in pairs:
        assert save in ESTUDIO, save
        assert load in ESTUDIO, load


def test_interruptores_nacen_activos_sin_preferencia():
    """Sin nada guardado el iman y los marcadores nacen activos (mismo
    comportamiento que antes del cambio: solo se apaga lo que el usuario
    apago de verdad; un valor corrupto no desactiva nada)."""
    res = _persist_run(r"""
function eff(v) { return v !== false; }
console.log("FASE5P-JSON:" + JSON.stringify({
  empty: eff(loadLocal("cfg", "all", "x", "magnet", true)),
  off: (function () { saveLocal("cfg", "all", "x", "magnet", false); return eff(loadLocal("cfg", "all", "x", "magnet", true)); })(),
}));
""")
    assert res["empty"] is True and res["off"] is False, res


def test_velas_sanitiza_sin_perder_personalizacion():
    """sanitizeCandles conserva el preset manual completo y cae al
    verde/rojo del motor ante valores rotos (persistir basura no rompe
    el pintado tras recargar)."""
    res = _persist_run(r"""
var manual = { up: "#112233", down: "#445566", borderUp: "#112233", borderDown: "#445566", wick: "#778899", wickAuto: false };
var keep = sanitizeCandles(JSON.parse(JSON.stringify(manual)));
var broken = sanitizeCandles({ up: "no-color", down: null });
console.log("FASE5P-JSON:" + JSON.stringify({ keep: keep, broken: broken }));
""")
    assert res["keep"] == {"up": "#112233", "down": "#445566", "borderUp": "#112233",
                           "borderDown": "#445566", "wick": "#778899", "wickAuto": False}, res
    assert res["broken"]["up"] == "#26a69a" and res["broken"]["down"] == "#ef5350", res


def test_ux_state_roundtrip_por_kind_con_payloads_reales(client):
    """El backend conserva cada kind del Estudio con payloads como los
    que envia el frontal (dibujo con width/style/opacity, EMA en lista,
    layout con current, preset de velas, niveles fib, dock) y no mezcla
    contextos (otro mercado/TF no ve lo guardado)."""
    cases = [
        ("drawings", {"market": "BTC/EUR", "provider": "kraken", "timeframe": "1h"},
         {"items": [{"id": "a1", "type": "trend", "pts": [{"t": 1, "p": 2}],
                     "color": "#e3b341", "width": 2.5, "style": "dashed", "opacity": 0.5}]}),
        ("indicators", {"market": "__ind", "provider": "kraken", "timeframe": "1h"},
         {"ema": {"on": True, "p": "9,20,50"}, "rsi": {"on": True, "p": 14}}),
        ("layouts", {"market": "__layouts", "provider": "all", "timeframe": "x"},
         {"layouts": {"A": {"market": "BTC/EUR"}}, "current": "A"}),
        ("candles", {"market": "__candles", "provider": "all", "timeframe": "x"},
         {"up": "#ffffff", "down": "#000000", "wickAuto": False}),
        ("fiblevels", {"market": "BTC/EUR", "provider": "kraken", "timeframe": "x"},
         {"levels": [0, 0.5, 1, 1.272]}),
        ("toolsdock", {"market": "__dock", "provider": "all", "timeframe": "x"},
         {"pos": "izquierda"}),
    ]
    for kind, ctx, payload in cases:
        body = dict(ctx, kind=kind, payload=payload)
        put = client.put("/ux/state", json=body)
        assert put.status_code == 200, (kind, put.text)
        got = client.get("/ux/state?market=%s&provider=%s&timeframe=%s&kind=%s" % (
            ctx["market"], ctx["provider"], ctx["timeframe"], kind))
        assert got.status_code == 200, kind
        assert got.json()["payload"] == payload, kind
    for mk, tf in (("ETH/EUR", "1h"), ("BTC/EUR", "5m")):
        got = client.get("/ux/state?market=%s&provider=kraken&timeframe=%s&kind=drawings" % (mk, tf))
        assert got.json()["payload"] != cases[0][2], (mk, tf)


def test_orden_visual_reglas_52_y_refactor_54():
    """Fija las reglas de la Fase 5.2 (y su fusion 5.4): iman en una linea
    a 43px, filas de indicadores con ritmo uniforme, casillas del flotante
    en linea, y el selector compuesto unico (5.4, sin cambio visual).

    Las dos reglas de casilla son CONTEXTUALES (no existe ninguna regla
    global `input[type="checkbox"] { ... }`): se fija el selector completo
    y sus declaraciones, de modo que quitar cualquiera de las dos reglas
    hace fallar este test."""
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")

    def _block(selector):
        m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
        assert m is not None, "falta la regla: " + selector
        return m.group(1)

    assert ".magnetlbl" in css and "white-space: nowrap" in css
    magnet_cb = _block('.drawtools .magnetlbl input[type="checkbox"]')
    assert "width: 13px" in magnet_cb, magnet_cb
    assert "min-height: 0" in magnet_cb, magnet_cb
    assert "#st-inds { display: grid; gap: 8px; }" in css
    assert "#st-inds label.kbd, #stf-panel-marks label.kbd" in css
    float_lbl = _block(".float-config .fc-row label.kbd")
    assert "display: inline-flex" in float_lbl, float_lbl
    float_cb = _block('.float-config .fc-row label.kbd input[type="checkbox"]')
    assert "width: auto" in float_cb, float_cb
    assert "min-height: 0" in float_cb, float_cb


def test_indicadores_get_respaldo_con_guarda_y_sin_put():
    """El GET de respaldo de indicadores existe en loadPersisted con el
    mismo patron que dibujos: guarda de contexto (pedido vs actual),
    prioridad al local (solo pide y solo aplica si el local esta vacio)
    y sin PUT al aplicar (sin bucles); repinta panel y series."""
    src = _extract_fn(ESTUDIO, "loadPersisted")
    assert "kind=indicators" in src
    assert "persistInd()" not in src, "aplicar el respaldo no debe persistir"
    assert "reqProv" in src and "reqTf" in src and "reqMarket" in src
    assert 'persistenceKey("__ind", provider, timeframe, "indicators")' in src
    assert "sanitizeIndCfg" in src
    assert "paintInds()" in src and "paintIndicators()" in src


def test_sanitize_ind_cfg_conserva_y_defectos():
    """sanitizeIndCfg conserva una config valida tal cual (incluida la
    lista "9,20,50" de la EMA) y cae a los defectos (todo apagado) ante
    basura, sin romper el pintado tras restaurar del backend."""
    res = _persist_run(r"""
var good = { sma: { on: true, p: 20 }, ema: { on: true, p: "9,20,50" },
  bb: { on: true, p: 20, m: 2 }, rsi: { on: true, p: 14 },
  macd: { on: true, f: 12, s: 26, sig: 9 } };
var keep = sanitizeIndCfg(JSON.parse(JSON.stringify(good)));
var broken = sanitizeIndCfg({ sma: { on: "yes", p: "xx" }, ema: { on: 1, p: "" },
  bb: null, rsi: [1, 2], macd: { on: true, f: -5, s: null, sig: 0 } });
var nul = sanitizeIndCfg(null);
console.log("FASE5P-JSON:" + JSON.stringify({ keep: keep, broken: broken, nul: nul }));
""", extra="\n".join([
        "var DEFAULT_IND = " + ESTUDIO.split("var DEFAULT_IND = ")[1].split(";")[0] + ";",
        _extract_fn(ESTUDIO, "sanitizeIndCfg"),
    ]))
    assert res["keep"] == {"sma": {"on": True, "p": 20}, "ema": {"on": True, "p": "9,20,50"},
                           "bb": {"on": True, "p": 20, "m": 2}, "rsi": {"on": True, "p": 14},
                           "macd": {"on": True, "f": 12, "s": 26, "sig": 9}}, res
    assert res["broken"]["sma"] == {"on": False, "p": 20}, res
    assert res["broken"]["ema"] == {"on": False, "p": 20}, res
    assert res["broken"]["bb"] == {"on": False, "p": 20, "m": 2}, res
    assert res["broken"]["rsi"] == {"on": False, "p": 14}, res
    assert res["broken"]["macd"] == {"on": True, "f": 12, "s": 26, "sig": 9}, res
    assert all(v["on"] is False for v in res["nul"].values()), res


def test_bb_macd_clave_lleva_parametros_y_recarga_datos():
    """Cambiar p/m de BB o f/s/sig de MACD retira las series viejas y crea
    las nuevas con datos recalculados (defensa en profundidad junto a
    indCacheKey); en repintados identicos no hay add/rm ni claves
    huerfanas."""
    res = _node_run(r"""
function rowsOf(sk) {
  var s = indSeries[sk];
  if (!s) return null;
  return (__lastRows[s.__key] || []).map(function (r) { return r.value; });
}
function close(a, b) {
  if (a === null || b === null || a.length !== b.length) return false;
  for (var i = 0; i < a.length; i++) if (Math.abs(a[i] - b[i]) > 1e-9) return false;
  return true;
}
paintIndicators();
var keys0 = Object.keys(indSeries).sort();
__counts = { add: 0, rm: 0, set: 0, apply: 0, move: 0 };
indCfg.bb.p = 30;
paintIndicators();
var afterBB = { add: __counts.add, rm: __counts.rm, set: __counts.set, keys: Object.keys(indSeries).sort() };
var refBB = bollinger(closes(candles), 30, 2);
var okBB = close(rowsOf("bbup30m2"), refBB.up.filter(function (v) { return v !== null; })) &&
  close(rowsOf("bbmid30m2"), refBB.mid.filter(function (v) { return v !== null; })) &&
  close(rowsOf("bblo30m2"), refBB.lo.filter(function (v) { return v !== null; }));
__counts = { add: 0, rm: 0, set: 0, apply: 0, move: 0 };
paintIndicators();
var sameBB = { add: __counts.add, rm: __counts.rm };
__counts = { add: 0, rm: 0, set: 0, apply: 0, move: 0 };
indCfg.macd.f = 8;
paintIndicators();
var afterMACD = { add: __counts.add, rm: __counts.rm, keys: Object.keys(indSeries).sort() };
var refM = macd(closes(candles), 8, 26, 9);
var okM = close(rowsOf("macdline8_26_9"), refM.line.filter(function (v) { return v !== null; })) &&
  close(rowsOf("macdsig8_26_9"), refM.signal.filter(function (v) { return v !== null; }));
console.log("FASE5-JSON:" + JSON.stringify({ keys0: keys0, afterBB: afterBB, okBB: okBB, sameBB: sameBB, afterMACD: afterMACD, okM: okM }));
""", extra=_PRELUDE)
    assert len(res["keys0"]) == 9 and "bbup20m2" in res["keys0"], res
    assert "macdline12_26_9" in res["keys0"] and "macdsig12_26_9" in res["keys0"], res
    assert res["afterBB"]["rm"] == 3 and res["afterBB"]["add"] == 3, res
    assert "bbup20m2" not in res["afterBB"]["keys"] and "bbup30m2" in res["afterBB"]["keys"], res
    assert len(res["afterBB"]["keys"]) == 9, res  # sin huerfanas
    assert res["okBB"] is True, res
    assert res["sameBB"] == {"add": 0, "rm": 0}, res
    assert res["afterMACD"]["rm"] == 2 and res["afterMACD"]["add"] == 2, res
    assert "macdline8_26_9" in res["afterMACD"]["keys"], res
    assert "macdline12_26_9" not in res["afterMACD"]["keys"], res
    assert res["okM"] is True, res


def test_movetopane_defensivo_mueve_sin_recrear():
    """El camino defensivo de lineOf (pane cambiado en meta) mueve la serie
    en vivo con moveToPane: 0 add + 0 rm, y la meta queda actualizada."""
    res = _node_run(r"""
paintIndicators();
__counts = { add: 0, rm: 0, set: 0, apply: 0, move: 0 };
indSeriesMeta["ema20"] = { pane: 2 };
paintIndicators();
console.log("FASE5-JSON:" + JSON.stringify({ add: __counts.add, rm: __counts.rm, move: __counts.move, meta: indSeriesMeta["ema20"], nseries: Object.keys(indSeries).length }));
""", extra=_PRELUDE)
    assert res["add"] == 0 and res["rm"] == 0 and res["move"] == 1, res
    assert res["meta"] == {"pane": 0} and res["nseries"] == 9, res


def _loadpersisted_run(body, extra=""):
    if shutil.which("node") is None:
        import pytest
        pytest.skip("node no disponible")
    lib = "\n".join([
        _extract_fn(ESTUDIO, "persistenceKey"),
        _extract_fn(ESTUDIO, "loadLocal"),
        _extract_fn(ESTUDIO, "stateKind"),
        "var DEFAULT_IND = " + ESTUDIO.split("var DEFAULT_IND = ")[1].split(";")[0] + ";",
        _extract_fn(ESTUDIO, "sanitizeIndCfg"),
        _extract_fn(ESTUDIO, "shouldApplyIndPayload"),
        _extract_fn(ESTUDIO, "loadPersisted"),
    ])
    store = ("var __store = {};\nvar localStorage = { getItem: function (k) { return (__store[k] === undefined ? null : __store[k]); },"
             " setItem: function (k, v) { __store[k] = String(v); }, removeItem: function (k) { delete __store[k]; } };\n")
    stubs = """
var market = "BTC/EUR", provider = "kraken", timeframe = "1h";
var drawings = [];
var indCfg = sanitizeIndCfg(loadLocal("__ind", provider, timeframe, "indicators", null));
var candleCfg = {};
var paintCalls = { inds: 0, series: 0, refill: 0 };
var puts = [], requested = [], __indDeferred = null;
var U = { api: function (url, opts) {
  if (opts && opts.method === "PUT") { puts.push(url); return Promise.resolve({ ok: true }); }
  requested.push(url);
  if (url.indexOf("kind=indicators") >= 0) { return new Promise(function (res) { __indDeferred = res; }); }
  return Promise.resolve({ ok: false });
}, esc: function (s) { return String(s); } };
function redrawDrawings() {}
function paintObjects() {}
function loadFibLevels() {}
function sanitizeCandles(v) { return v || {}; }
function applyCandleColors() {}
function paintCandles() {}
function paintInds() { paintCalls.inds++; }
function paintIndicators() { paintCalls.series++; }
function maybeRefillForIndicators() { paintCalls.refill++; }
function __tick() { return new Promise(function (r) { setTimeout(r, 30); }); }
"""
    full = lib + "\n" + store + "\n" + stubs + "\n" + extra + "\n" + body
    tmpdir = Path("/tmp/opencode")
    tmpdir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=str(tmpdir), prefix="fase5i-",
                                     suffix=".js", delete=False, mode="w",
                                     encoding="utf-8") as fh:
        fh.write(full)
        path = fh.name
    proc = subprocess.run(["node", path], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("FASE5P-JSON:")]
    assert line, proc.stdout
    return json.loads(line[0][len("FASE5P-JSON:"):])



def test_loadpersisted_indicadores_restaura_respaldo_y_guarda():
    """Comportamiento del GET de respaldo, en orden: (A) local vacio +
    backend con indicadores -> se restauran (saneados), se repinta y no
    hay PUT; (B) local presente + backend distinto -> se conserva el local
    y ni se pide; (C) cambio de contexto en vuelo -> no se aplica;
    (D) backend vacio -> se conserva lo que habia."""
    res = _loadpersisted_run(r"""
(async function () {
  var out = {};
  // A: local vacio + backend con valores
  loadPersisted();
  out.aAsked = requested.some(function (u) { return u.indexOf("kind=indicators") >= 0; });
  __indDeferred({ ok: true, data: { payload: { ema: { on: true, p: "9,20" }, rsi: { on: true, p: 14 } } } });
  await __tick();
  out.aCfg = JSON.parse(JSON.stringify(indCfg));
  out.aPaint = { inds: paintCalls.inds, series: paintCalls.series };
  out.aPuts = puts.length;
  // B: local presente + backend distinto -> manda el local, sin pedir
  paintCalls = { inds: 0, series: 0, refill: 0 };
  requested = []; puts = []; __indDeferred = null;
  var mine = { sma: { on: true, p: 10 }, ema: { on: false, p: 20 }, bb: { on: false, p: 20, m: 2 }, rsi: { on: false, p: 14 }, macd: { on: false, f: 12, s: 26, sig: 9 } };
  try { localStorage.setItem(persistenceKey("__ind", provider, timeframe, "indicators"), JSON.stringify(mine)); } catch (e) {}
  indCfg = sanitizeIndCfg(mine);
  loadPersisted();
  await __tick();
  out.bAsked = requested.some(function (u) { return u.indexOf("kind=indicators") >= 0; });
  out.bKept = JSON.stringify(indCfg) === JSON.stringify(mine);
  out.bPaint = { inds: paintCalls.inds, series: paintCalls.series };
  // C: cambio de contexto mientras vuela -> no se aplica
  paintCalls = { inds: 0, series: 0, refill: 0 };
  requested = []; puts = []; __indDeferred = null;
  try { localStorage.removeItem(persistenceKey("__ind", provider, timeframe, "indicators")); } catch (e) {}
  indCfg = sanitizeIndCfg(null);
  loadPersisted();
  out.cAsked = requested.some(function (u) { return u.indexOf("kind=indicators") >= 0; });
  provider = "binance"; timeframe = "5m";
  __indDeferred({ ok: true, data: { payload: { sma: { on: true, p: 99 } } } });
  await __tick();
  out.cApplied = indCfg.sma.on === true && indCfg.sma.p === 99;
  out.cPaint = { inds: paintCalls.inds, series: paintCalls.series };
  provider = "kraken"; timeframe = "1h";
  // D: backend vacio -> se conserva
  paintCalls = { inds: 0, series: 0, refill: 0 };
  requested = []; puts = []; __indDeferred = null;
  indCfg = sanitizeIndCfg(null);
  loadPersisted();
  __indDeferred({ ok: false });
  await __tick();
  out.dCfg = JSON.parse(JSON.stringify(indCfg));
  out.dPaint = { inds: paintCalls.inds, series: paintCalls.series };
  out.dPuts = puts.length;
  // D2 (R1): backend sin fila = ok:true + payload {} -> TAMPOCO se aplica:
  // antes borraba los indicadores activos al cambiar de mercado.
  paintCalls = { inds: 0, series: 0, refill: 0 };
  requested = []; puts = []; __indDeferred = null;
  indCfg = sanitizeIndCfg({ ema: { on: true, p: 20 }, rsi: { on: true, p: 14 } });
  loadPersisted();
  __indDeferred({ ok: true, data: { payload: {} } });
  await __tick();
  out.d2Cfg = JSON.parse(JSON.stringify(indCfg));
  out.d2Paint = { inds: paintCalls.inds, series: paintCalls.series };
  out.d2Puts = puts.length;
  console.log("FASE5P-JSON:" + JSON.stringify(out));
})();
""")
    assert res["aAsked"] is True, res
    assert res["aCfg"]["ema"] == {"on": True, "p": "9,20"} and res["aCfg"]["rsi"] == {"on": True, "p": 14}, res
    assert res["aCfg"]["sma"]["on"] is False, res
    assert res["aPaint"] == {"inds": 1, "series": 1}, res
    assert res["aPuts"] == 0, res
    assert res["bAsked"] is False and res["bKept"] is True, res
    assert res["bPaint"] == {"inds": 0, "series": 0}, res
    assert res["cAsked"] is True and res["cApplied"] is False, res
    assert res["cPaint"] == {"inds": 0, "series": 0}, res
    assert all(v["on"] is False for v in res["dCfg"].values()), res
    assert res["dPaint"] == {"inds": 0, "series": 0} and res["dPuts"] == 0, res
    assert res["d2Cfg"]["ema"]["on"] is True and res["d2Cfg"]["rsi"]["on"] is True, res
    assert res["d2Paint"] == {"inds": 0, "series": 0} and res["d2Puts"] == 0, res
