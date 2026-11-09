"""Estabilidad vertical del gráfico de Estudio (anti-crecimiento infinito).

El bug original (stub): fitChartSize() medía el alto TOTAL de #st-chart y se
lo pasaba a chart.resize(w, h), que lo asignaba SOLO al panel 0. El total
resultante (h + volumen) era siempre mayor que h y el ResizeObserver volvía
a disparar: la altura crecía sin parar hacia abajo.

Fase 0 (motor real): chart.resize(w, h) fija el alto TOTAL del chart y los
paneles se reparten por stretch; con autoSize:false el resize NO es no-op y
repetir resize(w, h) con el mismo h devuelve alturas idénticas (punto fijo),
así que el circuito ResizeObserver -> fitChartSize -> resize converge en vez
de sumar. El contrato que este archivo fija:
* resize(w, h) repetido con el mismo h es idempotente (sin crecimiento ni
  encogido): panel principal + volumen 110 px estables;
* fitChartSize() ignora tamaños que no cambian (< 1 px) y la reentrada;
* el panel de volumen queda en 110 px tras setHeight(110) y separado del
  principal (incluido un tercer panel RSI de 140 px);
* priceToCoordinate/coordinateToPrice siguen alineadas (roundtrip exacto).
"""

import json
from pathlib import Path

from lwc_harness_helper import run_lwc_body

STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "web" / "static"
ESTUDIO = (STATIC_DIR / "estudio.js").read_text(encoding="utf-8")
VENDOR = (STATIC_DIR / "vendor" / "lightweight-charts.standalone.production.js").read_text(encoding="utf-8")

# Con el motor real, repetir chart.resize(w, h) con el MISMO h debe devolver
# las mismas alturas de panel (idempotencia = punto fijo del circuito del
# ResizeObserver). El volumen se fija con panes()[1].setHeight(110) y el
# principal ocupa el resto; la conversión precio<->coordenada roundtripea.
NODE_LOOP_HARNESS = r"""
const chart = mkchart();
const px = chart.addSeries(LW.CandlestickSeries, {});
const rows = candles100();
px.setData(rows);
pump();
const vol = chart.addSeries(LW.HistogramSeries, {}, 1);
vol.setData(rows.map(r => ({ time: r.time, value: 10 })));
pump();
chart.panes()[1].setHeight(110);
pump();
// Bucle de realimentación: resize con el MISMO h 10 vueltas (lo que haría
// fitChartSize si el contenedor midiera estable).
const heights = [];
for (let i = 0; i < 10; i++) { chart.resize(640, 400); pump(50); heights.push(chart.panes()[0].getHeight() + chart.panes()[1].getHeight()); }
const mainH = chart.panes()[0].getHeight(), volH = chart.panes()[1].getHeight();
// Conversión precio<->coordenada alineada.
const mid = (100 + 199) / 2;
const y = px.priceToCoordinate(mid);
const back = px.coordinateToPrice(y);
// Tercer panel (p. ej. RSI): también estable y conserva su altura.
const rsi = chart.addSeries(LW.LineSeries, {}, 2);
rsi.setData(rows.map(r => ({ time: r.time, value: 50 })));
pump();
chart.panes()[2].setHeight(140);
pump(50);
const heights3 = [];
for (let i = 0; i < 10; i++) { chart.resize(640, 400); pump(50); heights3.push(chart.panes().reduce((a, p) => a + p.getHeight(), 0)); }
const rsiH = chart.panes()[2].getHeight();
console.log(JSON.stringify({ heights, mainH, volH, mid, y, back, heights3, rsiH }));
const growth = heights[heights.length - 1] - heights[0];
if (!(Math.abs(growth) < 1)) throw new Error("TOTAL CRECE/ENCOGE: " + JSON.stringify(heights));
if (volH !== 110) throw new Error("volumen alterado: " + volH);
if (Math.abs(mainH - (heights[0] - 110)) > 0.001) throw new Error("panel principal no ocupa el resto");
const growth3 = heights3[heights3.length - 1] - heights3[0];
if (!(Math.abs(growth3) < 1)) throw new Error("TOTAL inestable con 3 paneles: " + JSON.stringify(heights3));
// El motor reparte píxeles enteros por stretch al redimensionar: el tercer
// panel puede quedar a 1 px del setHeight(140) nominal, pero estable en el
// tiempo (eso es lo que este test fija: sin deriva, no el entero exacto).
if (Math.abs(rsiH - 140) > 1) throw new Error("tercer panel alterado: " + rsiH);
if (Math.abs(back - mid) > 1e-9) throw new Error("conversion desalineada");
console.log("LOOP-ESTABLE-OK");
"""


def _run_node_harness():
    return run_lwc_body(NODE_LOOP_HARNESS)


def test_resize_loop_converge_en_vez_de_crecer():
    """El test que faltaba: 10 vueltas resize con el mismo h (Fase 0: sobre
    el motor real; la idempotencia sustituye al "h - others" del stub como
    prueba de punto fijo)."""
    proc = _run_node_harness()
    assert "LOOP-ESTABLE-OK" in proc.stdout, proc.stderr or proc.stdout
    first = json.loads(proc.stdout.strip().splitlines()[0])
    assert len(first["heights"]) == 10
    assert max(first["heights"]) - min(first["heights"]) < 1


def test_volumen_visible_y_conversion_alineada():
    """Volumen en 110 px + conversión precio roundtrip exacto (Fase 0: con
    priceToCoordinate/coordinateToPrice del motor real). El tercer panel
    (RSI, nominal 140) se exige con tolerancia de 1 px: el motor reparte
    píxeles enteros por stretch y puede redondear, pero sin deriva entre
    vueltas (ver aserción growth3 del harness)."""
    proc = _run_node_harness()
    assert "LOOP-ESTABLE-OK" in proc.stdout, proc.stderr or proc.stdout
    first = json.loads(proc.stdout.strip().splitlines()[0])
    assert first["volH"] == 110  # volumen visible y separado
    assert first["mainH"] > 80  # el principal no colapsa
    assert abs(first["back"] - first["mid"]) < 1e-9  # hit-test alineado
    assert abs(first["rsiH"] - 140) <= 1  # tercer panel, redondeo entero


def test_vendor_resize_es_total_con_paneles_fijos():
    """Fase 0: sustituye a test_vendor_resize_resta_otros_paneles (el stub
    restaba "h - others" con mínimo Math.max(80 y canvas en block; nada de
    eso existe en el motor real). Equivalente honesto: el motor expone
    resize(w,h) total + panes()[i].setHeight/getHeight + paneSize + el guard
    autoSizeActive (resize es no-op con autoSize:true, verificado en fuente),
    y Estudio crea el chart con autoSize:false + setHeight(110) al volumen,
    conservando las guardas anti-realimentación de fitChartSize."""
    assert "h - others" not in VENDOR
    assert "autoSizeActive" in VENDOR  # el no-op con autoSize:true
    assert "paneSize" in VENDOR
    assert "setHeight" in VENDOR
    assert "autoSize: false" in ESTUDIO  # sin esto, chart.resize sería no-op
    assert "autoSize: true" not in ESTUDIO
    assert "panes()[1].setHeight(110)" in ESTUDIO  # volumen fijo


def test_fit_chart_size_con_guarda_anti_realimentacion():
    """Intacto (lee Estudio, no el vendor): las guardas siguen ahí."""
    assert "lastFitW" in ESTUDIO and "lastFitH" in ESTUDIO
    assert "stFitting" in ESTUDIO  # bandera de reentrada
    assert "Math.abs(w - lastFitW) < 1" in ESTUDIO
    assert "chart.resize" in ESTUDIO
