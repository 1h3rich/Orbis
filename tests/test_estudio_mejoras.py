"""Mejoras Estudio (5 cambios): zoom real, submenú único, fullscreen, ayuda, auto-refresh.

Contractuales sin browser (texto + harness node sobre el motor REAL):
* el vendor es Lightweight Charts v5.0.9 real (Apache-2.0), no el stub propio;
* el vendor pinta usando el rango visible y el zoom es perceptible;
* zoomBy cambia el rango con los botones +/−;
* el submenú abre un solo panel (ARIA + teclado, sin <details> anidados);
* app.css trae :fullscreen (+ fallback .studio-expanded) y el JS recalcula;
* Ayuda en 6 secciones ordenadas y plegables;
* refresco Auto por defecto con cálculo por temporalidad; Off apaga el timer;
* assets locales existentes, sin duplicados y con versión de cache válida.

Fase 0: el vendor pasó de stub propio a Lightweight Charts v5.0.9 real
vendorizado (mismo nombre de fichero). Los harnesses Node ejecutan el motor
real con un DOM falso (ver tests/lwc_harness_helper.py) en vez del harness
mínimo que bastaba para el stub.
"""

import re
from pathlib import Path

from lwc_harness_helper import run_lwc_body

STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "web" / "static"
ESTUDIO = (STATIC_DIR / "estudio.js").read_text(encoding="utf-8")
VENDOR = (STATIC_DIR / "vendor" / "lightweight-charts.standalone.production.js").read_text(encoding="utf-8")
CSS = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
APP_JS = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

NODE_HARNESS = r"""
const chart = mkchart();
const s = chart.addSeries(LW.CandlestickSeries, {});
const rows = candles100();
s.setData(rows);
pump();
const ts = chart.timeScale();
function eq(a, b, msg) { if (a !== b) throw new Error(msg + " got=" + a + " want=" + b); }
function ok(c, msg) { if (!c) throw new Error(msg); }
// Rango por defecto del motor real: incluye desplazamiento a la derecha
// (rightOffset), así que no es 0-99 exactos como en el stub; se exige
// cobertura coherente de las 100 velas en vez de igualdad literal.
const full = ts.getVisibleLogicalRange();
ok(isFinite(full.from) && isFinite(full.to), "default finito");
ok(full.from < full.to, "default ordenado");
ok(full.from <= 0 && full.to >= 99, "default cubre los datos");
ok((full.to - full.from) >= 99, "default abarca 100 velas");
const gapFull = ts.timeToCoordinate(rows[51].time) - ts.timeToCoordinate(rows[50].time);
ok(gapFull > 0, "gapFull positivo");
ts.setVisibleLogicalRange({ from: 40, to: 60 });
pump();
const zoomed = ts.getVisibleLogicalRange();
eq(zoomed.from, 40, "zoom from");
eq(zoomed.to, 60, "zoom to");
const gapZoom = ts.timeToCoordinate(rows[51].time) - ts.timeToCoordinate(rows[50].time);
ok(gapZoom > gapFull * 3, "zoom perceptible x3");
const back = ts.coordinateToTime(ts.timeToCoordinate(rows[50].time));
eq(back, rows[50].time, "roundtrip");
// Reset: el botón Centrar usa fitContent(), que encuadra 0-99 exactos
// (resetTimeScale vuelve al defecto con rightOffset, no a 0-99).
ts.fitContent();
pump();
const reset = ts.getVisibleLogicalRange();
eq(reset.from, 0, "fit from");
eq(reset.to, 99, "fit to");
console.log(JSON.stringify({ full, gapFull, zoomed, gapZoom, back, reset }));
console.log("HARNESS-OK");
"""


def test_vendor_is_real_lightweight_charts():
    """Fase 0: equivale a test_vendor_paint_reads_visible_range sobre el stub.

    El vendor es Lightweight Charts v5.0.9 real (Apache-2.0), no el stub
    propio: conserva la cabecera @license, expone createChart/addSeries/
    createSeriesMarkers/panes y el rango lógico get/set/reset/fit, y ya no
    contiene ningún interno del stub (_paint, chart._range, "h - others").
    El comportamiento (zoom perceptible + roundtrip) lo verifica el harness
    test_vendor_zoom_is_perceptible_with_roundtrip sobre el motor real.
    """
    assert "@license" in VENDOR and "Apache License 2.0" in VENDOR
    assert "Lightweight Charts" in VENDOR and "5.0.9" in VENDOR
    for api in ("createChart", "createSeriesMarkers", "CandlestickSeries",
                "LineSeries", "AreaSeries", "BarSeries", "HistogramSeries",
                "getVisibleLogicalRange", "setVisibleLogicalRange",
                "resetTimeScale", "fitContent", "timeToCoordinate",
                "coordinateToTime", "priceToCoordinate", "coordinateToPrice",
                "subscribeCrosshairMove", "applyOptions"):
        assert api in VENDOR, api
    for stub_only in ("_paint: function (pane)", "chart._range", "h - others",
                       "visibleRange(chart, n)"):
        assert stub_only not in VENDOR, stub_only


def test_vendor_zoom_is_perceptible_with_roundtrip():
    """Mismo contrato que con el stub (zoom perceptible x3 + roundtrip),
    ejecutado sobre el motor real con DOM falso (ver lwc_harness_helper):
    setVisibleLogicalRange({from:40,to:60}) roundtripea exacto, la separación
    entre velas crece >3x al hacer zoom, coordinateToTime invierte
    timeToCoordinate, y fitContent() (botón Centrar) encuadra 0-99."""
    proc = run_lwc_body(NODE_HARNESS)
    assert "HARNESS-OK" in proc.stdout, proc.stderr or proc.stdout


def test_zoom_by_changes_range_perceptibly():
    assert "zoomBy" in ESTUDIO and "setVisibleLogicalRange" in ESTUDIO
    assert '"st-zin"' in ESTUDIO and '"st-zout"' in ESTUDIO
    assert "zoomBy(0.75)" in ESTUDIO and "zoomBy(1.33)" in ESTUDIO
    # zoomBy recalcula el span con el factor (perceptible) y lo aplica
    m = re.search(r"function zoomBy\(factor\)\s*\{(.*?)\n    \}", ESTUDIO, re.S)
    assert m is not None
    body = m.group(1)
    assert "setVisibleLogicalRange" in body
    assert "* factor" in body


def test_functions_single_open_submenu():
    # Cinco entradas como botones con ARIA, sin <details> anidados.
    assert '<div class="st-func-menu"' in ESTUDIO
    for key in ("layouts", "inds", "cmp", "alerts", "marks"):
        assert 'data-stf="' + key + '"' in ESTUDIO, key
    assert "aria-expanded" in ESTUDIO and "aria-controls" in ESTUDIO
    assert "stf-panel-layouts" in ESTUDIO and "stf-panel-alerts" in ESTUDIO
    # Apertura única: cerrar todo y abrir solo el pulsado; repulsar cierra.
    assert "closeAll()" in ESTUDIO
    assert "p.hidden = false" in ESTUDIO and "p.hidden = true" in ESTUDIO
    assert "studio-functions-grid" not in ESTUDIO
    assert 'studio-functions"><summary>' not in ESTUDIO
    # Teclado: botones nativos + flechas + Escape.
    assert "ArrowRight" in ESTUDIO and "ArrowLeft" in ESTUDIO
    # Los paneles conservan los IDs que usa el resto del cableado.
    for keep in ("st-layoutsel", "st-inds", "st-cmp", "st-alerts", "st-mkpaper", "st-mkdca"):
        assert keep in ESTUDIO, keep


def test_fullscreen_styles_and_resize():
    assert ".studio:fullscreen" in CSS
    assert "#st-chart" in CSS and ":fullscreen" in CSS
    assert ".studio-expanded" in CSS  # fallback honesto sin Fullscreen API
    assert "studio-expanded" in ESTUDIO
    assert "fullscreenchange" in ESTUDIO
    assert "ResizeObserver" in ESTUDIO
    assert "chart.resize" in ESTUDIO  # fitChartSize recalcula al entrar/salir
    assert "exitFullscreen" in ESTUDIO and "requestFullscreen" in ESTUDIO


def test_help_ordered_sections():
    titles = ["Navegar el gráfico", "Herramientas de dibujo",
              "Seleccionar y configurar", "Preferencias de herramientas",
              "Atajos de teclado", "Alertas de precio"]
    help_block = ESTUDIO[ESTUDIO.index("st-help-body"):]
    pos = [help_block.index(t) for t in titles]
    assert pos == sorted(pos), "las 6 secciones deben ir en orden"
    assert "st-help-sec" in ESTUDIO  # plegables, sin muro de texto
    for kw in ("rueda", "Acercar", "Alejar", "Centrar",
               "arrastra de A a B", "un toque", "Proyección", "Alcista", "Bajista",
               "niveles", "texto", "posición", "color",
               "bloquear", "ocultar", "eliminar",
               "ubicación de la barra", "Ajustar a velas",
               "Esc", "Supr", "Ctrl+Z", "Ctrl+Mayús+Z",
               "campana", "sonido"):
        assert kw in help_block, kw


def test_refresh_auto_default_and_mapping():
    # Regla única Fase 4 (sustituye a la tabla AUTO_TF_MS + la fórmula tf/2
    # con topes, que eran dos verdades distintas): cuarta parte de la vela
    # en segundos enteros, entre 1 s y 60 s (TF_SECONDS + autoRefreshFor como
    # única fuente). Equivalencia documentada con la tabla anterior:
    # 1s→1s sin cambios; 30s 5s→8s, 1m 5s→15s, 5m 15s→60s, 1h 30s→60s
    # (refresco más acompasado a la vela); 15m/30m/4h/1d/1w→60s sin cambios.
    # Nunca más de 1 pet/s por ventana.
    assert "AUTO_TF_MS" not in ESTUDIO  # una sola fuente, sin tabla paralela
    assert "TF_SECONDS" in ESTUDIO and "tfSeconds" in ESTUDIO
    assert "autoRefreshFor" in ESTUDIO and "effectiveRefreshMs" in ESTUDIO
    assert 'value="auto"' in ESTUDIO
    assert "Automática (" in ESTUDIO  # el selector muestra el intervalo efectivo
    assert "se actualiza cada" in ESTUDIO  # lenguaje llano + vela abierta
    assert "vela abierta" in ESTUDIO  # el refresco trae ticker Y vela
    assert "Actualización apagada" in ESTUDIO  # Off funciona
    # Al cambiar de temporalidad se recalcula y se reinicia el timer solo.
    assert "paintRefreshNote()" in ESTUDIO
    assert "restartEstudioTimer" in ESTUDIO
    m = re.search(r"function restartEstudioTimer\(\)\s*\{(.*?)\n    \}", ESTUDIO, re.S)
    assert m is not None and "effectiveRefreshMs()" in m.group(1)
    # Fase 4: el timer refresca ticker + vela (antes solo ticker).
    assert "refreshEstudioTick()" in m.group(1)
    assert "loadSnapshot()" not in m.group(1)
    # Timers independientes: Operativa 15 s y Portfolio 24 h viven en app.js.
    assert "OPERATIVA_REFRESH_MS = 15000" in APP_JS
    assert "PORTFOLIO_REFRESH_MS = 86400000" in APP_JS


def test_cachebust_versions_consistent():
    # Version bumps should not require copying every version number here.
    # Verify the browser's actual local imports and retain the required modules.
    imports=re.findall(r'(?:src|href)="/ui/([^\"]+\.(?:css|js)(?:\?[^\"]*)?)"',HTML)
    assert imports
    paths=[]
    for asset in imports:
        match=re.fullmatch(r'([^?]+)\?v=([1-9][0-9]*)',asset)
        assert match is not None,asset
        path=(STATIC_DIR/match[1]).resolve()
        assert path.is_relative_to(STATIC_DIR.resolve()) and path.is_file(),asset
        paths.append(match[1])
    assert len(paths)==len(set(paths)),"Duplicate styles/scripts"
    assert {'app.css','app.js','real.js','estudio.js','lab.js','dca.js','decimal.js',
        'wallet.js','wallet-dex-runs.js','wallet-dex-bot.js','search.js',
        'vendor/lightweight-charts.standalone.production.js'}<=set(paths)
