"""M1: marcadores PAPER legibles + tooltip propio (funciones reales en Node).

paperMarks se construye desde paperOps (datos crudos de /paper/operations)
con buildPaperMarks REAL extraído de estudio.js (brace-matching, sin copiar
su cuerpo); paperTipHtml REAL genera la tarjeta con los mismos formateadores
que Operativa (fecha, cantidad, precio, importe, comisión). Sin paperOps no
hay marcadores. Con showPAPER=false no se pide (/paper/operations va después
del guard) ni se pinta (paperOps/paperMarks se vacían + tooltip oculto).

Equivalencias (no-debilitamiento): solo añade comprobaciones; no toca ningún
test existente. El `title` desaparece de los marcadores porque LWC v5.0.9 lo
ignora (barrido hover exhaustivo: 0 tooltips); la tarjeta es el portador.
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
APP_CSS = (STATIC_DIR / "app.css").read_text(encoding="utf-8")


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


def _node_lib():
    return "\n".join([
        _extract_fn(ESTUDIO, "num"),
        _extract_fn(ESTUDIO, "opEpoch"),
        _extract_fn(ESTUDIO, "candleEndTime"),
        _extract_fn(ESTUDIO, "paperBarTime"),
        _extract_fn(ESTUDIO, "paperQtyText"),
        _extract_fn(ESTUDIO, "paperFee"),
        _extract_fn(ESTUDIO, "groupPaperOps"),
        _extract_fn(ESTUDIO, "buildPaperMarks"),
        _extract_fn(ESTUDIO, "paperTipHtml"),
        _extract_fn(ESTUDIO, "paperTipGroupHtml"),
        _extract_fn(ESTUDIO, "paperOpNear"),
    ])


# U simulado con la MISMA lógica de formato que app.js (Intl es-ES):
# fmtMoney EUR -> "74.195,10 €", fmtQty hasta 8 decimales + sufijo,
# fmtDateTime naive UTC + "Z", esc de HTML.
U_STUB = r"""
var __eurFmt = new Intl.NumberFormat("es-ES", { style: "currency", currency: "EUR", minimumFractionDigits: 2, maximumFractionDigits: 2 });
var __qtyFmt = new Intl.NumberFormat("es-ES", { maximumFractionDigits: 8, minimumFractionDigits: 0 });
var U = {
  fmtMoney: function (x, ccy) {
    if (x === null || x === undefined || x === "") return "—";
    var n = Number(x);
    if (!isFinite(n)) return "—";
    return __eurFmt.format(n);
  },
  fmtQty: function (x, suffix) {
    if (x === null || x === undefined || x === "") return "—";
    var n = Number(x);
    if (!isFinite(n)) return "—";
    var t = __qtyFmt.format(n);
    return suffix ? t + " " + suffix : t;
  },
  fmtDateTime: function (s) {
    if (!s) return "—";
    var iso = /Z|[+-]\d{2}:?\d{2}$/.test(s) ? s : s + "Z";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return "—";
    return d.toLocaleString("es-ES");
  },
  esc: function (s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
};
"""

OPS = [
    {"operation_type": "BUY", "asset": "BTC", "quote_currency": "EUR",
     "amount_spent": "74195.10", "asset_received": "0.01", "price": "7419510",
     "trading_fee": "1.5", "withdrawal_fee": "0", "network_fee": "0.25",
     "timestamp": "2026-10-05 12:00:00"},
    {"operation_type": "SELL", "asset": "BTC", "quote_currency": "EUR",
     "amount_spent": "0.005", "asset_received": "37097.55", "price": "7419510",
     "trading_fee": "1.0", "withdrawal_fee": "0", "network_fee": "0",
     "timestamp": "2026-10-06 12:00:00"},
    {"operation_type": "BUY", "asset": "ETH", "quote_currency": "EUR",
     "amount_spent": "100", "asset_received": "0.05", "price": "2000",
     "trading_fee": "0", "withdrawal_fee": "0", "network_fee": "0",
     "timestamp": "2026-10-05 13:00:00"},
]

BODY_TMPL = r"""
var paperOps = [];
const OPS = __OPS__;
paperOps = OPS.filter(function (o) { return (o.asset + "/" + o.quote_currency) === "BTC/EUR"; });
const marksBtc = buildPaperMarks(paperOps, "BTC/EUR");
const marksEmpty = buildPaperMarks([], "BTC/EUR");
const marksAll = buildPaperMarks(OPS, "BTC/EUR");
const tipBuy = paperTipHtml(OPS[0]);
const tipSell = paperTipHtml(OPS[1]);
// paperOpNear lee paperOps (var libre, como en el closure de render).
paperOps = OPS.filter(function (o) { return (o.asset + "/" + o.quote_currency) === "BTC/EUR"; });
const nearBuy = paperOpNear(1790000000);
const nearSell = paperOpNear(1890000000);
console.log("PAPER-M1-JSON:" + JSON.stringify({
  marksBtc: marksBtc, marksEmpty: marksEmpty, marksAll: marksAll,
  tipBuy: tipBuy, tipSell: tipSell,
  nearBuyTs: nearBuy && nearBuy.timestamp, nearSellTs: nearSell && nearSell.timestamp,
  epochBuy: opEpoch(OPS[0]), feeBuy: paperFee(OPS[0])
}));
"""


def _node_run():
    if shutil.which("node") is None:
        pytest.skip("node no disponible")
    body = (_node_lib() + "\n" + U_STUB + "\nconst __OPS__ = "
            + json.dumps(OPS) + ";\n" + BODY_TMPL)
    tmpdir = Path("/tmp/opencode")
    tmpdir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=str(tmpdir), prefix="paper-m1-",
                                     suffix=".js", delete=False, mode="w",
                                     encoding="utf-8") as fh:
        fh.write(body)
        path = fh.name
    proc = subprocess.run(["node", path], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("PAPER-M1-JSON:")]
    assert line, proc.stdout
    return json.loads(line[0][len("PAPER-M1-JSON:"):])



def test_build_paper_marks_desde_paper_ops():
    """paperMarks nace de paperOps: filtra por mercado, círculo/compra y
    cuadrado/venta, size visible, texto con cantidad, tiempos época, y
    NUNCA emite `title` (LWC lo ignora)."""
    res = _node_run()
    marks = res["marksBtc"]
    assert len(marks) == 2
    buy, sell = marks
    assert buy["shape"] == "circle" and buy["position"] == "belowBar"
    assert buy["color"] == "#3fb950" and buy["size"] == 2
    assert buy["text"] == "P · 0,01 BTC"
    assert sell["shape"] == "square" and sell["position"] == "aboveBar"
    assert sell["color"] == "#f85149" and sell["size"] == 2
    assert sell["text"] == "S"
    for m in marks:
        assert "title" not in m
        assert set(m.keys()) == {"time", "position", "shape", "color", "size", "text"}
    assert buy["time"] == res["epochBuy"] == 1791201600
    assert sell["time"] == 1791288000
    # Las de otro mercado no entran aunque vengan en paperOps.
    assert res["marksAll"] == marks


def test_sin_paper_ops_no_hay_marcadores():
    """Sin paperOps (respuesta vacía o mercado sin operaciones) no hay
    marcadores que pintar."""
    assert _node_run()["marksEmpty"] == []


def test_tarjeta_con_importe_y_comision_formateados():
    """La tarjeta muestra fecha, lado, cantidad, precio, importe y comisión
    con los formateadores de Operativa (p. ej. '74.195,10 €')."""
    res = _node_run()
    tip = res["tipBuy"]
    # Intl es-ES separa con NBSP (U+00A0): se normaliza antes de comparar.
    norm = tip.replace(" ", " ").replace(" ", " ")
    assert "Compra PAPER" in tip
    assert "0,01 BTC" in norm  # cantidad vía fmtQty
    assert "74.195,10 €" in norm  # importe vía fmtMoney
    assert "1,75 €" in norm  # comisión 1.5+0+0.25
    assert res["feeBuy"] == 1.75
    assert "Venta PAPER" in res["tipSell"]
    assert "0,005 BTC" in res["tipSell"]
    assert "37.097,55 BTC" not in res["tipSell"]
    assert "37.097,55" in res["tipSell"]
    assert "Importe 0,01" not in res["tipSell"]
    # HTML escapado: sin inyección desde los datos.
    assert "<script" not in tip


def test_operacion_mas_cercana_por_tiempo():
    """El tooltip elige la operación PAPER más cercana por tiempo a la vela
    bajo el cursor."""
    res = _node_run()
    assert res["nearBuyTs"] == "2026-10-05 12:00:00"
    assert res["nearSellTs"] == "2026-10-06 12:00:00"


def test_paper_marks_and_tooltip_match_loaded_candles_without_crossing_gaps():
    if shutil.which("node") is None:
        pytest.skip("node no disponible")
    body = _node_lib() + "\n" + U_STUB + r"""
var timeframe = '1d';
function tfSeconds() { return 86400; }
var candles = [{time:Date.parse('2026-10-05T00:00:00Z')/1000},
  {time:Date.parse('2026-10-06T00:00:00Z')/1000},
  {time:Date.parse('2026-10-08T00:00:00Z')/1000}];
var paperOps = [
  {timestamp:'2026-10-05T22:00:00+02:00',operation_type:'BUY',asset:'BTC',quote_currency:'EUR',asset_received:'0.01'},
  {timestamp:'2026-10-07 12:00:00',operation_type:'SELL',asset:'BTC',quote_currency:'EUR',asset_received:'10'}
];
const marks = buildPaperMarks(paperOps,'BTC/EUR',candles,86400,false);
const onFirst = paperOpNear(candles[0].time,86400);
const onGap = paperOpNear(candles[1].time,86400);
const jan = Date.parse('2026-01-01T00:00:00Z')/1000;
const lateJan = Date.parse('2026-01-31T20:00:00Z')/1000;
const month = paperBarTime(lateJan,[{time:jan},{time:Date.parse('2026-02-01T00:00:00Z')/1000}],2592000,true);
console.log('PAPER-M1-JSON:' + JSON.stringify({marks,onFirst:onFirst&&onFirst.timestamp,onGap,month,jan,first:candles[0].time}));
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "markers.js"
        path.write_text(body, encoding="utf-8")
        proc = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    out = json.loads(proc.stdout.split("PAPER-M1-JSON:", 1)[1])
    assert [mark["time"] for mark in out["marks"]] == [out["first"]]
    assert out["onFirst"] == "2026-10-05T22:00:00+02:00"
    assert out["onGap"] is None
    assert out["month"] == out["jan"]


def test_multiple_paper_operations_in_one_candle_have_counts_and_full_context():
    if shutil.which("node") is None:
        pytest.skip("node no disponible")
    body = _node_lib() + "\n" + U_STUB + r"""
var timeframe = '1h';
function tfSeconds() { return 3600; }
var candles = [{time:Date.parse('2026-10-10T00:00:00Z')/1000},
  {time:Date.parse('2026-10-10T01:00:00Z')/1000}];
var paperOps = [
  {id:2,timestamp:'2026-10-10T00:20:00Z',asset:'BTC',quote_currency:'EUR',operation_type:'BUY',asset_received:'0.02',amount_spent:'200',price:'10000',trading_fee:'0',withdrawal_fee:'0',network_fee:'0'},
  {id:1,timestamp:'2026-10-10T00:10:00Z',asset:'BTC',quote_currency:'EUR',operation_type:'BUY',asset_received:'0.01',amount_spent:'100',price:'10000',trading_fee:'0',withdrawal_fee:'0',network_fee:'0'},
  {id:3,timestamp:'2026-10-10T00:30:00Z',asset:'BTC',quote_currency:'EUR',operation_type:'SELL',asset_received:'100',amount_spent:'0.01',price:'10000',trading_fee:'0',withdrawal_fee:'0',network_fee:'0'},
  {id:4,timestamp:'2026-10-10T00:40:00Z',asset:'ETH',quote_currency:'EUR',operation_type:'BUY'},
  {id:5,timestamp:'2026-10-10T03:00:00Z',asset:'BTC',quote_currency:'EUR',operation_type:'BUY'}
];
const grouped = groupPaperOps(paperOps,'BTC/EUR',candles,3600,false);
const marks = buildPaperMarks(paperOps,'BTC/EUR',candles,3600,false,grouped);
const tip = paperTipGroupHtml(grouped[candles[0].time]);
console.log('PAPER-GROUP-JSON:' + JSON.stringify({marks,tip,keys:Object.keys(grouped)}));
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "grouped.js"
        path.write_text(body, encoding="utf-8")
        proc = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    out = json.loads(proc.stdout.split("PAPER-GROUP-JSON:", 1)[1])
    assert len(out["marks"]) == 2
    assert [mark["text"] for mark in out["marks"]] == ["P ×2", "S"]
    assert all(mark["time"] == 1791590400 for mark in out["marks"])
    assert out["keys"] == ["1791590400"]
    assert "3 operaciones PAPER en esta vela" in out["tip"]
    assert out["tip"].count('class="mt-operation"') == 2
    assert "1 más en Movimientos" in out["tip"]
    assert out["tip"].index("0,01 BTC") < out["tip"].index("0,02 BTC")


def test_paper_chart_query_bounds_cover_last_bar_and_calendar_month():
    if shutil.which("node") is None:
        pytest.skip("node no disponible")
    body = _extract_fn(ESTUDIO, "candleEndTime") + "\n" + _extract_fn(ESTUDIO, "paperChartBounds") + r"""
const hourly = paperChartBounds([{time:1700000000},{time:1700003600}],3600,false);
const monthly = paperChartBounds([{time:Date.parse('2026-01-01T00:00:00Z')/1000},
  {time:Date.parse('2026-02-01T00:00:00Z')/1000}],2592000,true);
const empty = paperChartBounds([],3600,false);
console.log(JSON.stringify({hourly,monthly,empty}));
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "bounds.js"
        path.write_text(body, encoding="utf-8")
        proc = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    out = json.loads(proc.stdout)
    assert out["hourly"]["start"] == "2023-11-14T22:13:20.000Z"
    assert out["hourly"]["end"] == "2023-11-15T00:13:20.000Z"
    assert out["monthly"]["end"] == "2026-03-01T00:00:00.000Z"
    assert out["empty"] is None
    assert '"&start_at="' in ESTUDIO and '"&end_before="' in ESTUDIO
    assert '"/paper/operations?limit=200&order=asc"' not in ESTUDIO


def test_showpaper_false_ni_pide_ni_pinta():
    """Con showPAPER=false paintMarkers NO pide /paper/operations (el fetch
    va después del guard) y vacía paperOps/paperMarks ocultando el tooltip."""
    m = ESTUDIO[ESTUDIO.index("function paintMarkers()"):]
    m = m[:m.index("\n    }\n")]
    assert "if (!showPAPER)" in m
    assert m.index("if (!showPAPER)") < m.index("/paper/operations")
    for frag in ("paperOps = []", "paperMarks = []", "hideMarkerTip()"):
        assert frag in m, frag


def test_capa_tooltip_contrato_y_resets():
    """La capa #st-marker-tip existe junto a #st-ohlc con pointer-events
    none y clamp, y se resetea donde priceMarkers se pone a null."""
    assert 'id="st-marker-tip"' in ESTUDIO
    assert ESTUDIO.index('id="st-marker-tip"') - ESTUDIO.index('id="st-ohlc"') < 2000
    assert "#st-marker-tip" in APP_CSS and "pointer-events: none" in APP_CSS
    assert "getBoundingClientRect" in ESTUDIO
    assert ESTUDIO.count("paperOps = []") >= 4  # declaración + paintMarkers + 3 resets
    assert ESTUDIO.count("hideMarkerTip()") >= 4  # crosshair, leave, resets, paintMarkers
    for frag in ("st-marker-tip", "st-depth", "st-tf", "st-mkpaper"):
        assert frag in ESTUDIO, frag  # ids st-* son contrato: no renombrados
