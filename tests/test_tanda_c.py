"""Tanda C: 7 pulidos frontend (solo app/web/static + tests + docs).

C1 paperOpNear con umbral (medio ancho de vela del TF actual) · C2 helper
stMetaHtml usado en loadCandles + ws.onmessage + refreshOpenCandle · C3
rollCandleIfNeeded puro + enganche en ws.onmessage · C4 paintCompare
empareja por c.time (diagnóstico + arreglo mínimo) · C5 favicon data-URI
inline · C6 pnlLabel es-ES con coma · C7 mini LWC en tema oscuro.

Equivalencias (no-debilitamiento):
* paperOpNear(t) sin maxDt conserva el comportamiento M1 (la más próxima):
  test_estudio_paper_marks.py llama sin umbral y sigue en verde; el umbral
  solo actúa cuando updateMarkerTip pasa tfSeconds(timeframe)/2.
* pnlLabel: mismos casos y signos que M3 (test_m3_entry_pnl.py actualizado
  solo en el separador decimal: "+2.50%" -> "+2,50%"); el cero conserva "+"
  (signDisplay "always", ver C6).
* stMetaHtml genera byte a byte el HTML que loadCandles construía inline.
* rollCandleIfNeeded muta la última vela con las mismas 3 asignaciones con
  String() que hacía ws.onmessage (vía mutateLast).
* paintCompare: solo cambia la clave de emparejamiento c.t -> c.time; el
  resto (base0/c0, % normalizada, color, early-returns) intacto.
"""

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "web" / "static"
ESTUDIO = (STATIC_DIR / "estudio.js").read_text(encoding="utf-8")
APP_JS = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
INDEX = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def _extract_fn(src, name):
    """Extrae `function name(...) {...}` con balanceo de llaves."""
    m = re.search(r"function " + re.escape(name) + r"\s*\(", src)
    assert m is not None, name
    start = src.index("{", m.end())
    depth, i = 0, start
    in_str = None
    while i < len(src):
        c = src[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == in_str:
                in_str = None
        elif c in ("'", '"', "`"):
            in_str = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():i + 1]
        i += 1
    raise AssertionError("sin cierre: " + name)


def _node_json(lib, payload_js):
    if shutil.which("node") is None:
        pytest.skip("node no disponible")
    tmpdir = Path("/tmp/opencode")
    tmpdir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=str(tmpdir), prefix="tandac-",
                                     suffix=".js", delete=False, mode="w",
                                     encoding="utf-8") as fh:
        fh.write(lib + "\n" + payload_js)
        path = fh.name
    proc = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    m = re.search(r"TANDAC-JSON:(.*)", proc.stdout, re.S)
    assert m, proc.stdout
    return json.loads(m.group(1))


U_STUB = r"""
var U = {
  esc: function (s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
};
"""

# ------------------------------------------------------------------ C1


def _c1_lib():
    m = re.search(r"var TF_SECONDS = \{[^}]*\};", ESTUDIO)
    assert m, "TF_SECONDS"
    return "\n".join([
        m.group(0),
        _extract_fn(ESTUDIO, "num"),
        _extract_fn(ESTUDIO, "tfSeconds"),
        _extract_fn(ESTUDIO, "opEpoch"),
        _extract_fn(ESTUDIO, "paperOpNear"),
    ])


C1_OPS = [
    {"operation_type": "BUY", "asset": "BTC", "quote_currency": "EUR",
     "amount_spent": "100", "asset_received": "0.01", "price": "10000",
     "trading_fee": "0", "withdrawal_fee": "0", "network_fee": "0",
     "timestamp": "2026-10-05 12:00:00"},
]

# 2026-10-05 12:00:00Z == 1791201600 (época ya fijada en test M1).
C1_EPOCH = 1791201600
# TF 1h -> medio ancho de vela = 1800 s.
C1_HALF_1H = 1800


def test_c1_umbral_cerca_lejos_y_borde_exacto():
    """Sin velas cargadas, la compatibilidad del umbral temporal permanece:
    a 100 s devuelve la operación y a 1801 s la descarta."""
    body = (_c1_lib() + "\nvar paperOps = " + json.dumps(C1_OPS) + ";\n" + r"""
console.log("TANDAC-JSON:" + JSON.stringify({
  near: (paperOpNear(%d, %d) || {}).timestamp || null,
  edge: (paperOpNear(%d, %d) || {}).timestamp || null,
  far: paperOpNear(%d, %d),
  tf1h: tfSeconds("1h"), tf1M: tfSeconds("1M"), tf1w: tfSeconds("1w")
}));
""" % (C1_EPOCH + 100, C1_HALF_1H, C1_EPOCH + 1800, C1_HALF_1H,
       C1_EPOCH + 1801, C1_HALF_1H))
    out = _node_json(body, "")
    assert out["near"] == "2026-10-05 12:00:00"
    assert out["edge"] == "2026-10-05 12:00:00"
    assert out["far"] is None
    assert out["tf1h"] == 3600 and out["tf1M"] == 2592000 and out["tf1w"] == 604800


def test_c1_sin_umbral_conserva_m1_y_hook_usa_tf():
    """Sin maxDt, paperOpNear conserva M1 (la más próxima aunque esté a
    días: equivalencia con test_estudio_paper_marks). La UI ya no usa esa
    compatibilidad: consulta sólo el grupo de la vela cargada."""
    body = (_c1_lib() + "\nvar paperOps = " + json.dumps(C1_OPS) + ";\n" + r"""
console.log("TANDAC-JSON:" + JSON.stringify({
  legacy: (paperOpNear(%d) || {}).timestamp || null
}));
""" % (C1_EPOCH + 999999,))
    out = _node_json(body, "")
    assert out["legacy"] == "2026-10-05 12:00:00"
    hook = _extract_fn(ESTUDIO, "updateMarkerTip")
    assert "paperBarGroups[t]" in hook
    assert "paperTipGroupHtml(group)" in hook
    assert "paperOpNear(t," not in hook
    painter = _extract_fn(ESTUDIO, "paintMarkers")
    assert 'groupPaperOps(paperOps, mk, candles, tfSeconds(timeframe), timeframe === "1M")' in painter


# ------------------------------------------------------------------ C2


def _c2_lib():
    return "\n".join([
        _extract_fn(ESTUDIO, "num"),
        _extract_fn(ESTUDIO, "canon"),
        _extract_fn(ESTUDIO, "canon2"),
        _extract_fn(ESTUDIO, "precision"),
        _extract_fn(ESTUDIO, "fmtP"),
        _extract_fn(ESTUDIO, "stMetaHtml"),
    ])


def test_c2_tick_pinta_igual_que_vela_y_tres_caminos():
    """stMetaHtml REAL: con precio de tick pinta lo mismo que con el close
    de la vela; loadCandles + ws.onmessage + refreshOpenCandle lo usan (un
    solo punto de formateo, sin bloques copiados)."""
    body = (_c2_lib() + "\n" + U_STUB + r"""
var fromTick = stMetaHtml("BTC/EUR", "BINANCE", 74040.6, 100, false);
var fromCandle = stMetaHtml("BTC/EUR", "BINANCE", 74040.6, 100, false);
var withStart = stMetaHtml("BTC/EUR", "BINANCE", 74040.6, 100, true);
var noData = stMetaHtml("BTC/EUR", "BINANCE", null, 0, false);
console.log("TANDAC-JSON:" + JSON.stringify({
  fromTick: fromTick, same: fromTick === fromCandle,
  withStart: withStart, noData: noData
}));
""")
    out = _node_json(body, "")
    assert out["same"] is True
    norm = out["fromTick"].replace(" ", " ").replace(" ", " ")
    assert "BTC/EUR" in norm and "BINANCE" in norm
    assert "74.040,60" in norm  # fmtP es-ES, 2 decimales
    assert "Inicio del historial" in out["withStart"]
    assert "Sin datos" in out["noData"]
    # Un solo constructor del HTML: el bloque inline desapareció.
    assert ESTUDIO.count("stMetaHtml(") >= 4  # def + 3 caminos
    assert ESTUDIO.count('<p class="muted"><strong>\' + U.esc(market)') == 0
    assert "stMetaHtml(" in _extract_fn(ESTUDIO, "loadCandles")
    assert "stMetaHtml(" in _extract_fn(ESTUDIO, "refreshOpenCandle")
    # La etiqueta del gráfico sigue la fuente OHLC, no la del ticker AUTO.
    assert ESTUDIO.count("stMetaHtml(market, String(chartResolvedProvider || provider)") == 2


# ------------------------------------------------------------------ C3


def _c3_lib():
    return "\n".join([
        _extract_fn(ESTUDIO, "num"),
        _extract_fn(ESTUDIO, "nextCandleStart"),
        _extract_fn(ESTUDIO, "rollCandleIfNeeded"),
    ])


def test_c3_dentro_muta_frontera_rola():
    """rollCandleIfNeeded REAL (TF 1m=60 s): tick dentro muta la última
    (false, misma longitud, close/high/low como el WS); tick en la frontera
    exacta abre vela nueva (true, o=h=l=c=precio, v 0)."""
    body = (_c3_lib() + r"""
var cs = [{ time: 60000, open: 100, high: 101, low: 99, close: 100.5, v: 7 }];
var rIn = rollCandleIfNeeded(cs, { t: 60030, price: 102 }, 60);
var afterIn = JSON.parse(JSON.stringify(cs));
var rEdge = rollCandleIfNeeded(cs, { t: 60060, price: 103 }, 60);
console.log("TANDAC-JSON:" + JSON.stringify({
  rIn: rIn, afterIn: afterIn, rEdge: rEdge, cs: cs
}));
""")
    out = _node_json(body, "")
    assert out["rIn"] is False
    assert len(out["afterIn"]) == 1
    assert out["afterIn"][0]["close"] == "102"
    assert out["afterIn"][0]["high"] == "102"
    assert out["afterIn"][0]["low"] == "99"
    assert out["rEdge"] is True
    assert len(out["cs"]) == 2
    new = out["cs"][1]
    assert new["time"] == 60060
    assert new["open"] == 103 and new["high"] == 103
    assert new["low"] == 103 and new["close"] == 103
    assert new["v"] == 0


def test_c3_hueco_grande_una_sola_vela_del_tick():
    """En un hueco grande NO se inventan velas intermedias: solo la del
    tick, en su bucket (documentado en la función)."""
    body = (_c3_lib() + r"""
var cs = [{ time: 60000, open: 100, high: 101, low: 99, close: 100.5, v: 7 }];
var r = rollCandleIfNeeded(cs, { t: 60000 + 3600 * 5, price: 110 }, 60);
console.log("TANDAC-JSON:" + JSON.stringify({ r: r, cs: cs }));
""")
    out = _node_json(body, "")
    assert out["r"] is True
    assert len(out["cs"]) == 2
    assert out["cs"][1]["time"] == 60000 + 3600 * 5
    assert out["cs"][1]["v"] == 0


def test_c3_tf_semanal_y_mensual_coherentes_y_hook_ws():
    """Sin TF de calendario explícito, la función conserva la rejilla fija;
    el WebSocket le pasa el TF para usar la frontera real del proveedor."""
    body = (_c3_lib() + r"""
function bucket(t, s) { return Math.floor(t / s) * s; }
var baseW = bucket(1700000000, 604800), baseM = bucket(1700000000, 2592000);
var cw = [{ time: baseW, open: 1, high: 1, low: 1, close: 1, v: 1 }];
var cm = [{ time: baseM, open: 1, high: 1, low: 1, close: 1, v: 1 }];
var r = {
  wIn: rollCandleIfNeeded(cw, { t: baseW + 100, price: 2 }, 604800),
  mIn: rollCandleIfNeeded(cm, { t: baseM + 100, price: 2 }, 2592000)
};
r.wEdge = rollCandleIfNeeded(cw, { t: baseW + 604800, price: 3 }, 604800);
r.mEdge = rollCandleIfNeeded(cm, { t: baseM + 2592000, price: 3 }, 2592000);
console.log("TANDAC-JSON:" + JSON.stringify({
  r: r, lenW: cw.length, lenM: cm.length,
  tW: cw[1].time, tM: cm[1].time
}));
""")
    out = _node_json(body, "")
    assert out["r"] == {"wIn": False, "mIn": False, "wEdge": True, "mEdge": True}
    assert out["lenW"] == 2 and out["lenM"] == 2
    assert out["tW"] % 604800 == 0 and out["tM"] % 2592000 == 0
    assert "rollCandleIfNeeded(candles, { t: t, price: d.price }, tfSeconds(timeframe), timeframe)" in ESTUDIO
    ws_block = ESTUDIO[ESTUDIO.index("ws.onmessage = function"):]
    ws_block = ws_block[:ws_block.index("ws.onclose = function")]
    assert ws_block.index("rollCandleIfNeeded(") < ws_block.index("paintIndicators();")
    assert "last.close = String(d.price)" not in ws_block  # sin lógica duplicada


def test_c3_calendar_month_leap_year_and_provider_anchored_week():
    body = (_c3_lib() + r"""
function utc(s) { return Date.parse(s) / 1000; }
var cm = [{time:utc('2024-01-01T00:00:00Z'),open:1,high:1,low:1,close:1,v:1}];
var cw = [{time:utc('2024-01-07T00:00:00Z'),open:1,high:1,low:1,close:1,v:1}];
var january = rollCandleIfNeeded(cm,{t:utc('2024-01-31T23:59:59Z'),price:2},2592000,'1M');
var february = rollCandleIfNeeded(cm,{t:utc('2024-02-01T00:00:00Z'),price:3},2592000,'1M');
var leapDay = rollCandleIfNeeded(cm,{t:utc('2024-02-29T23:59:59Z'),price:4},2592000,'1M');
var march = rollCandleIfNeeded(cm,{t:utc('2024-03-01T00:00:00Z'),price:5},2592000,'1M');
var stale = rollCandleIfNeeded(cm,{t:utc('2024-02-28T00:00:00Z'),price:999},2592000,'1M');
var sunday = rollCandleIfNeeded(cw,{t:utc('2024-01-13T23:59:59Z'),price:2},604800,'1w');
var nextSunday = rollCandleIfNeeded(cw,{t:utc('2024-01-14T00:00:00Z'),price:3},604800,'1w');
console.log('TANDAC-JSON:'+JSON.stringify({january,february,leapDay,march,stale,
  sunday,nextSunday,monthTimes:cm.map(x=>x.time),monthClose:cm.at(-1).close,
  weekTimes:cw.map(x=>x.time)}));
""")
    out = _node_json(body, "")
    assert [out[key] for key in ("january", "february", "leapDay", "march", "stale")] == [
        False, True, False, True, False,
    ]
    assert out["monthTimes"] == [1704067200, 1706745600, 1709251200]
    assert out["monthClose"] == 5
    assert out["sunday"] is False and out["nextSunday"] is True
    assert out["weekTimes"] == [1704585600, 1705190400]


# ------------------------------------------------------------------ C4


def test_c4_compare_empareja_por_time_mapeado():
    """Diagnóstico (evidencia en docstring del test): la comparada viaja
    CRUDA ({t,...}) pero las velas del Estudio viajan MAPEADAS por
    mapCandleRow a {time,open,high,low,close,v} SIN `.t`; `byT[c.t]` era
    siempre undefined -> rows vacío -> return -> nunca pintaba. Arreglo
    mínimo: emparejar por c.time (mismo formato, épocas en segundos);
    base/c0, % normalizada y color intactos."""
    assert "time: num(c.t)" in ESTUDIO  # mapCandleRow: la mapeada pierde .t
    paint = _extract_fn(ESTUDIO, "paintCompare")
    assert "byT[r.t]" in paint  # la comparada sigue cruda: r.t correcto
    assert "byT[c.t]" not in paint  # el bug: ya no existe en el fichero
    assert ESTUDIO.count("byT[c.t]") == 0
    assert "byT[c.time]" in paint
    assert "rows.push({ time: c.time, value: (byT[c.time] / c0 - 1) * 100 });" in paint


# ------------------------------------------------------------------ C5


def test_c5_favicon_inline_sin_backend():
    """index.html declara favicon data-URI inline (sin ficheros ni backend):
    el navegador ya no pide /favicon.ico."""
    m = re.search(r'<link rel="icon" href="([^"]+)"', INDEX)
    assert m, "sin <link rel=icon>"
    assert m.group(1).startswith("data:image/svg+xml,")
    assert "favicon" not in (STATIC_DIR / "app.js").read_text(encoding="utf-8").lower()


# ------------------------------------------------------------------ C6


def test_c6_etiqueta_eje_en_es_es():
    """pnlLabel REAL: la etiqueta de la price line viva viaja YA formateada
    en es-ES ("-0,00%", coma) porque el canvas no localiza; no-finito -> em
    dash; paintEntryLines la usa como title sin re-formatear."""
    body = (_extract_fn(ESTUDIO, "pnlLabel") + r"""
console.log("TANDAC-JSON:" + JSON.stringify({
  neg: pnlLabel(-0.001), pos: pnlLabel(2.5),
  zero: pnlLabel(0), nan: pnlLabel(NaN), inf: pnlLabel("xx")
}));
""")
    out = _node_json(body, "")
    assert out["neg"] == "-0,00%"
    assert out["pos"] == "+2,50%"
    assert out["zero"] == "+0,00%"
    assert out["nan"] == "—" and out["inf"] == "—"
    paint = _extract_fn(ESTUDIO, "paintEntryLines")
    assert "title = pnlLabel(pct)" in paint or "title: title" in paint
    assert 'toFixed(2) + "%"' not in paint  # sin formateo con punto propio


# ------------------------------------------------------------------ C7


def test_c7_mini_oscuro_y_estudio_intacto():
    """paintLwcMini (app.js) crea el chart con fondo/grid/texto oscuros como
    la UI (#08100f/#172521/#9caaa4, es-ES) y series explícitas; el Estudio
    conserva su único createChart con sus opciones (sin tocar)."""
    paint = _extract_fn(APP_JS, "paintLwcMini")
    assert '"#08100f"' in paint and '"#9caaa4"' in paint and '"#172521"' in paint
    assert '"es-ES"' in paint
    assert '"#26a69a"' in paint and '"#ef5350"' in paint  # velas explícitas
    assert '"#4da3ff"' in paint  # línea como el Estudio
    assert ESTUDIO.count("LW.createChart(chartBox,") == 1
    assert 'background: { color: "#08100f" }' in ESTUDIO
