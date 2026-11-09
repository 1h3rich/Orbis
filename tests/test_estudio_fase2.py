"""Fase 2: navegación nativa + colores de velas personalizables.

BLOQUE A (decisión A1: delegar al motor, evidencia en docstring de cada
test): createChart nace con rueda/arrastre/eje nativos; se eliminó el
zoom/pan propios (onChartWheel/onPanMouse*); zoomBy y Centrar se conservan;
la escala fijada por el usuario sobrevive a pan/zoom/update (contrato
verificado en harness) y el paneo fuera del histórico congela el último
rango con datos en vez de colapsar la escala.

BLOQUE B: panel Velas con 3 presets + 5 colores + mecha auto, persistencia
localStorage + /ux/state kind=candles, aplicación al crear la serie y al
cambiar de mercado/TF/proveedor, volumen teñido a juego.
"""

import re
from pathlib import Path

from lwc_harness_helper import run_lwc_body_ev

STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "web" / "static"
ESTUDIO = (STATIC_DIR / "estudio.js").read_text(encoding="utf-8")

NAV_OPTS = r"""
const chart = LW.createChart(global.document.createElement("div"), {
  autoSize: false, width: 640, height: 400,
  handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: true },
  handleScale: { mouseWheel: true, pinch: true, axisPressedMouseMove: true, axisDoubleClickReset: true },
});
"""

WHEEL_HARNESS = (
    NAV_OPTS
    + r"""
const s = chart.addSeries(LW.CandlestickSeries, {});
const rows = candles100();
s.setData(rows);
pump();
const ts = chart.timeScale();
ts.fitContent(); pump();
function R(x) { return Math.round(x * 100) / 100; }
const full = ts.getVisibleLogicalRange();
if (full.from !== 0 || full.to !== 99) throw new Error("fitContent no encuadra 0-99");
// Vela bajo el cursor antes del zoom.
const x70 = ts.timeToCoordinate(rows[70].time);
// 3 gestos de rueda hacia dentro sobre la vela 70.
for (let i = 0; i < 3; i++) { fireAll("wheel", wheelEv(0, -120, x70, 200)); pump(60); }
const z3 = ts.getVisibleLogicalRange();
const span0 = full.to - full.from, span3 = z3.to - z3.from;
// (i) zoom perceptible y progresivo: ~x1.1 por gesto (span 99 -> ~74).
if (!(span3 < span0 * 0.85 && span3 > span0 * 0.6)) throw new Error("zoom poco perceptible span3=" + span3);
// (iii) la vela bajo el cursor no se mueve (±1 vela en px).
const drift = ts.timeToCoordinate(rows[70].time) - x70;
const pxBar = 640 / span3;
if (Math.abs(drift) > pxBar) throw new Error("deriva excesiva drift=" + drift + " pxBar=" + pxBar);
// Simetría: 3 gestos hacia fuera devuelven aprox al inicio.
for (let i = 0; i < 3; i++) { fireAll("wheel", wheelEv(0, 120, x70, 200)); pump(60); }
const zb = ts.getVisibleLogicalRange();
if (Math.abs(zb.from - full.from) > 5 || Math.abs(zb.to - full.to) > 5) throw new Error("zoom asimétrico " + JSON.stringify(zb));
// (A6) Mayús+rueda (deltaX) panea con el span intacto, sin código propio.
fireAll("wheel", wheelEv(120, 0, 320, 200)); pump(60);
const zp = ts.getVisibleLogicalRange();
if (Math.abs((zp.to - zp.from) - (zb.to - zb.from)) > 0.01) throw new Error("shift+rueda cambia el span");
if (zp.from === zb.from) throw new Error("shift+rueda no panea");
console.log(JSON.stringify({ span0: R(span0), span3: R(span3), drift: R(drift), pxBar: R(pxBar), zback: { from: R(zb.from), to: R(zb.to) } }));
console.log("WHEEL-OK");
"""
)

FIXED_SCALE_HARNESS = (
    r"""
const chart = mkchart();
const s = chart.addSeries(LW.CandlestickSeries, {});
const rows = candles100();
s.setData(rows);
pump();
const ts = chart.timeScale(), ps = chart.priceScale("right");
ts.fitContent(); pump();
function eq(a, b, msg) { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(msg + " got=" + JSON.stringify(a) + " want=" + JSON.stringify(b)); }
// Fijar escala como hace Estudio: setVisibleRange apaga autoScale solo.
ps.setVisibleRange({ from: 120, to: 180 }); pump(30);
if (ps.options().autoScale !== false) throw new Error("fijar no apaga autoScale");
const fixed = ps.getVisibleRange();
// (ii) 5 paneos + 3 zooms + 1 update en vivo: el rango no se mueve.
for (let i = 0; i < 5; i++) { const r = ts.getVisibleLogicalRange(); ts.setVisibleLogicalRange({ from: r.from - 7, to: r.to - 7 }); pump(20); }
eq(ps.getVisibleRange(), fixed, "pan mueve escala fijada");
for (let i = 0; i < 3; i++) { const r = ts.getVisibleLogicalRange(); const m = (r.from + r.to) / 2, sp = (r.to - r.from) * 0.85; ts.setVisibleLogicalRange({ from: m - sp / 2, to: m + sp / 2 }); pump(20); }
eq(ps.getVisibleRange(), fixed, "zoom mueve escala fijada");
s.update({ time: rows[99].time, open: 199, high: 201, low: 198, close: 200.5 }); pump(30);
eq(ps.getVisibleRange(), fixed, "update mueve escala fijada");
s.setData(rows.slice()); pump(30);
eq(ps.getVisibleRange(), fixed, "setData mueve escala fijada");
// Overscroll total con escala fijada: tampoco se mueve (A3).
ts.setVisibleLogicalRange({ from: -100, to: -50 }); pump(30);
eq(ps.getVisibleRange(), fixed, "overscroll mueve escala fijada");
// Centrar: re-auto + rango completo (ruta unlockPriceAuto de Estudio).
ps.applyOptions({ autoScale: true }); ts.fitContent(); pump(30);
if (ps.options().autoScale !== true) throw new Error("Centrar no restaura autoScale");
console.log("FIXED-OK");
"""
)

OVERSCROLL_HARNESS = r"""
const chart = mkchart();
const s = chart.addSeries(LW.CandlestickSeries, {});
const rows = candles100();
s.setData(rows);
pump();
const ts = chart.timeScale(), ps = chart.priceScale("right");
ts.fitContent(); pump(30);
const r0 = ps.getVisibleRange();
// Overscroll parcial: el auto reescala a las velas visibles (correcto).
ts.setVisibleLogicalRange({ from: -20, to: 30 }); pump(30);
const parcial = ps.getVisibleRange();
if (!(parcial.to < r0.to)) throw new Error("parcial no reescala a lo visible " + JSON.stringify(parcial));
// Overscroll total con auto: colapsa (motiva guardPriceOnScroll en Estudio).
ts.setVisibleLogicalRange({ from: -100, to: -50 }); pump(30);
const total = ps.getVisibleRange();
if (!((total.to - total.from) < (r0.to - r0.from) / 2)) throw new Error("total no colapsa " + JSON.stringify(total));
console.log(JSON.stringify({ r0, parcial, total }));
console.log("OVER-OK");
"""

COLORS_HARNESS = r"""
const chart = mkchart();
const s = chart.addSeries(LW.CandlestickSeries, {});
s.setData(candles100());
pump();
// El motor acepta los 6 campos (B1) y los devuelve tal cual.
s.applyOptions({ upColor: "#ffffff", downColor: "#000000", borderUpColor: "#ffffff", borderDownColor: "#ffffff", wickUpColor: "#e8e8e8", wickDownColor: "#e8e8e8", borderVisible: true });
pump();
const o = s.options();
function eq(a, b, msg) { if (a !== b) throw new Error(msg + " got=" + a + " want=" + b); }
eq(o.upColor, "#ffffff", "up"); eq(o.downColor, "#000000", "down");
eq(o.borderUpColor, "#ffffff", "bup"); eq(o.borderDownColor, "#ffffff", "bdown");
eq(o.wickUpColor, "#e8e8e8", "wup"); eq(o.wickDownColor, "#e8e8e8", "wdown");
console.log("COLORS-OK");
"""


def test_nav_native_options_and_no_custom_dupes():
    """A1: delegación al motor con menos código propio (sin duplicar lógica).

    Evidencia: la rueda nativa ancla el zoom en el puntero (deriva 0 px en
    test_nav_wheel_zoom_anchored) frente al cálculo fraccional propio
    (1.15/0.87); el motor además trae Mayús+rueda=pan, eje arrastrable,
    doble clic=reset y pinch sin código (ver fuente del vendor: Ym hace
    deltaY->bc(x) zoom y deltaX->Sc scroll). Por eso onChartWheel/onPanMouse*
    se eliminaron en vez de quedar muertos; zoomBy/Centrar se conservan.
    """
    assert "mouseWheel: true" in ESTUDIO and "pressedMouseMove: true" in ESTUDIO
    assert "axisPressedMouseMove: true" in ESTUDIO and "axisDoubleClickReset: true" in ESTUDIO
    # Con herramienta activa se sigue desactivando (contrato test_web_ui intacto).
    assert "handleScroll: false" in ESTUDIO
    # Sin duplicados muertos del cálculo propio.
    for dead in ("function onChartWheel(ev)", "function onPanMouseDown",
                 "function onPanMouseMove", "function onPanMouseUp",
                 "function onPanMouseLeave", "panState", '("wheel", onChartWheel'):
        assert dead not in ESTUDIO, dead
    assert "1.15" not in ESTUDIO and "0.87" not in ESTUDIO
    # zoomBy y Centrar se conservan (mismo mecanismo del motor).
    assert "zoomBy(0.75)" in ESTUDIO and "zoomBy(1.33)" in ESTUDIO
    assert re.search(r"function zoomBy\(factor\)", ESTUDIO) is not None


def test_nav_wheel_zoom_anchored():
    """A2/A6: zoom de rueda centrado en el puntero, simétrico + Shift-pan.

    Números medidos en la sonda (motor real, DOM falso): span 99 -> 74.13
    en 3 gestos (~x1.1 por gesto, progresivo), deriva 0 px, vuelta a
    {-2.16, 99.9} (simétrico ±5) y Mayús+rueda panea con span intacto.
    """
    proc = run_lwc_body_ev(WHEEL_HARNESS)
    assert "WHEEL-OK" in proc.stdout, proc.stderr or proc.stdout


def test_price_fixed_scale_contract():
    """A4: escala fijada no cambia al navegar (contrato motor que usa Estudio).

    Fijar con setVisibleRange apaga autoScale solo; tras 5 paneos + 3 zooms
    + update en vivo + setData + overscroll el rango es idéntico; re-auto
    (ruta Centrar) restaura. Estudio marca priceUserFixed al detectar el
    cambio manual en onNavMouseUp y no toca el rango en ningún repintado.
    """
    for token in ("priceUserFixed", "priceFrozenGap", "priceProgTag",
                  "snapshotPrice", "unlockPriceAuto", "guardPriceOnScroll",
                  "lastInDataPriceRange", "setVisibleRange", "onNavDblClick",
                  "checkBackfill();\n          guardPriceOnScroll();"):
        assert token in ESTUDIO, token
    # Centrar restaura auto (unlock antes de fitContent).
    m = re.search(r'\$\("st-reset"\)\.addEventListener\("click", function \(\) \{(.*?)\n    \}\);', ESTUDIO, re.S)
    assert m is not None and "unlockPriceAuto()" in m.group(1)
    # Con herramienta activa se desactiva la navegación; al salir, nativa.
    assert "handleScroll: false, handleScale: false" in ESTUDIO
    proc = run_lwc_body_ev(FIXED_SCALE_HARNESS)
    assert "FIXED-OK" in proc.stdout, proc.stderr or proc.stdout


def test_price_gap_freeze_motivation():
    """A3: overscroll parcial reescala bien; el total colapsa con auto.

    Por eso guardPriceOnScroll congela el último rango con datos al salir a
    zona sin velas (fijar el rango durante el arrastre, mitigación razonable
    sin romper el rango lógico) y re-auto al volver. Los dibujos usan
    svgConv del motor, intacta (A7).
    """
    assert "svgConv" in ESTUDIO and "timeToCoordinate" in ESTUDIO
    proc = run_lwc_body_ev(OVERSCROLL_HARNESS)
    assert "OVER-OK" in proc.stdout, proc.stderr or proc.stdout


def test_candle_colors_panel_and_persistence():
    """B1-B3: panel Velas con presets + 5 colores, persistencia existente."""
    # Botón y panel dentro de Análisis y organización (mismo estilo/lenguaje).
    assert 'data-stf="candles"' in ESTUDIO and "stf-panel-candles" in ESTUDIO
    assert ">Velas</button>" in ESTUDIO
    # 3 presets con los tonos del motor (verde/rojo = aspecto actual).
    for preset in ('"verde-rojo"', '"azul-rojo"', '"blanco-negro"'):
        assert preset in ESTUDIO, preset
    assert '"#26a69a"' in ESTUDIO and '"#ef5350"' in ESTUDIO
    assert '"#2962ff"' in ESTUDIO and '"#ffffff"' in ESTUDIO
    # 5 valores: alcista, bajista, borde alcista, borde bajista, mecha.
    for label in ("Alcista", "Bajista", "Borde alcista", "Borde bajista", "Mecha"):
        assert label in ESTUDIO, label
    for cid in ('id="cd-', "cd-wickauto", "cd-reset", "data-cpreset"):
        assert cid in ESTUDIO, cid
    assert 'bup: "borderUp"' in ESTUDIO and 'bdown: "borderDown"' in ESTUDIO
    # Persistencia con el mecanismo existente (saveLocal + PUT kind=candles).
    assert 'saveLocal("cfg", "all", "x", "candles"' in ESTUDIO
    assert 'loadLocal("cfg", "all", "x", "candles"' in ESTUDIO
    assert 'kind: "candles"' in ESTUDIO and '"__candles"' in ESTUDIO
    assert "sanitizeCandles" in ESTUDIO and "persistCandleColors" in ESTUDIO
    # Aplicación al crear la serie y al cambiar mercado/TF/proveedor/tipo.
    assert "applyCandleColors()" in ESTUDIO
    m = re.search(r"function ensureChart\(\) \{(.*?)return true;\n    \}", ESTUDIO, re.S)
    assert m is not None and "applyCandleColors()" in m.group(1)
    assert "paintCandles()" in ESTUDIO
    # Los 6 campos del motor (B1) + validación hex estricta (XSS-safe).
    for opt in ("upColor", "downColor", "borderUpColor", "borderDownColor",
                "wickUpColor", "wickDownColor", "borderVisible"):
        assert opt in ESTUDIO, opt
    # La comparada conserva su dorado (línea de % normalizada, B4 no procede).
    assert 'color: "#e3b341"' in ESTUDIO
    # Volumen teñido con los bordes (legible en los 3 presets sobre #08100f).
    assert "volRows()" in ESTUDIO and "hexA(" in ESTUDIO


def test_candle_colors_engine_roundtrip():
    """B4: el motor acepta y devuelve los 6 campos de color."""
    proc = run_lwc_body_ev(COLORS_HARNESS)
    assert "COLORS-OK" in proc.stdout, proc.stderr or proc.stdout


def test_nav_help_text():
    """A6: la ayuda describe rueda, Mayús+rueda, eje y doble clic."""
    assert "Mayús+rueda" in ESTUDIO and "doble clic en el eje" in ESTUDIO
    assert "Rueda: acerca y aleja centrado en el puntero" in ESTUDIO
