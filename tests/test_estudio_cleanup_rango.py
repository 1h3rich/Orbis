"""Defectos juez técnico: fugas en cleanup(), flags de storage y rango vendor.

Contractuales sin browser (texto + harness node sobre el motor REAL):
* cleanup() elimina el listener de resize y el intervalo de 5 s (referencias
  guardadas, sin anónimos) y es idempotente;
* los flags _orbisAlertSoundArmed/_orbisDockStorageArmed se resetean en
  cleanup() para que al reentrar se re-registre exactamente un handler;
* set/getVisibleLogicalRange son simétricos y coherentes (roundtrip, setData
  conserva el rango, overscroll con span preservado, objetos frescos);
* timeToCoordinate devuelve null fuera del histórico o en tiempos sin vela
  (el fallback lineal idxTx/idxTAt de svgConv cubre esos casos).

Fase 0: el harness ejecuta Lightweight Charts v5.0.9 real con DOM falso
(ver tests/lwc_harness_helper.py). Diferencias honestas frente al stub:
* el rango invertido LANZA (el motor valida from<=to); Estudio nunca lo
  envía gracias a la normalización de visRange (test dedicado intacto);
* el span mínimo lo impone el motor (1 unidad lógica), no 0.5;
* el pan fuera del histórico se recorta pero conserva orden y span.
"""

import re
from pathlib import Path

from lwc_harness_helper import run_lwc_body

STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "web" / "static"
ESTUDIO = (STATIC_DIR / "estudio.js").read_text(encoding="utf-8")
VENDOR = (STATIC_DIR / "vendor" / "lightweight-charts.standalone.production.js").read_text(encoding="utf-8")

CLEANUP = ESTUDIO[ESTUDIO.index("var cleanup = function ()"):ESTUDIO.index("activeCleanup = cleanup")]

NODE_HARNESS2 = r"""
const chart = mkchart();
const s = chart.addSeries(LW.CandlestickSeries, {});
const rows = candles100();
s.setData(rows);
pump();
const ts = chart.timeScale();
function eq(a, b, msg) { if (a !== b) throw new Error(msg + " got=" + a + " want=" + b); }
function ok(c, msg) { if (!c) throw new Error(msg); }

// 1. zoom simétrico con roundtrip (contrato intacto desde el stub)
ts.setVisibleLogicalRange({ from: 40, to: 60 });
pump();
let zoomed = ts.getVisibleLogicalRange();
eq(zoomed.from, 40, "zoom from"); eq(zoomed.to, 60, "zoom to");
eq(ts.coordinateToTime(ts.timeToCoordinate(rows[50].time)), rows[50].time, "roundtrip zoom");

// 2. setData conserva el rango visible (el motor real lo mantiene; paintAll
//    además lo restaura explícitamente con keepRange)
s.setData(rows.slice());
pump();
zoomed = ts.getVisibleLogicalRange();
eq(zoomed.from, 40, "keep from"); eq(zoomed.to, 60, "keep to");

// 3. rango invertido: el motor real LANZA (antes el stub lo ordenaba en
//    silencio). Equivalente honesto: Estudio nunca lo envía porque visRange
//    normaliza el orden (ver test_estudio_visrange_mirrors_vendor_normalization).
let threw = false;
try { ts.setVisibleLogicalRange({ from: 60, to: 40 }); pump(); } catch (e) { threw = true; }
ok(threw, "invertido debe lanzar");

// 4. span mínimo: el motor real impone 1 unidad lógica (antes 0.5 del stub);
//    el 0.5 de visRange en Estudio solo estabiliza la capa SVG.
ts.setVisibleLogicalRange({ from: 50, to: 50.1 });
pump();
let tiny = ts.getVisibleLogicalRange();
ok((tiny.to - tiny.from) >= 1, "min-span motor");
ok(tiny.from <= 50 && tiny.to >= 50.1, "min-span cubre lo pedido");

// 5. overscroll fuera del histórico: el motor recorta pero conserva orden y
//    span (pan legítimo); re-fijar lo devuelto es punto fijo.
ts.setVisibleLogicalRange({ from: -100, to: -50 });
pump();
let out = ts.getVisibleLogicalRange();
ok(isFinite(out.from) && isFinite(out.to), "overscroll finito");
ok(out.from < out.to, "overscroll ordenado");
eq(out.to - out.from, 50, "overscroll conserva span");
ts.setVisibleLogicalRange({ from: out.from, to: out.to });
pump();
let out2 = ts.getVisibleLogicalRange();
eq(out2.from, out.from, "overscroll punto fijo from");
eq(out2.to, out.to, "overscroll punto fijo to");

// 6. get devuelve objetos frescos (mutar lo devuelto no corrompe el interno)
ts.setVisibleLogicalRange({ from: 40, to: 60 });
pump();
let g1 = ts.getVisibleLogicalRange();
ok(g1 !== ts.getVisibleLogicalRange(), "fresh object");
g1.from = 9999;
eq(ts.getVisibleLogicalRange().from, 40, "no alias");

// 7. tiempo exacto de vela roundtripea; tiempo sin vela -> null honesto
//    (el stub interpolaba; el motor no inventa y svgConv usa idxTx entonces)
ts.setVisibleLogicalRange({ from: 40, to: 60 });
pump();
eq(ts.coordinateToTime(ts.timeToCoordinate(rows[50].time)), rows[50].time, "exacta estable");
ok(ts.timeToCoordinate((rows[50].time + rows[51].time) / 2) === null, "sin vela -> null");

// 8. fuera del histórico: null honesto, sin inventar (igual que el stub)
ok(ts.timeToCoordinate(rows[0].time - 99999) === null, "null antes");
ok(ts.timeToCoordinate(rows[99].time + 99999) === null, "null despues");

// 9. fitContent (botón Centrar) vuelve al completo 0-99
ts.fitContent();
pump();
let reset = ts.getVisibleLogicalRange();
eq(reset.from, 0, "reset from"); eq(reset.to, 99, "reset to");

console.log("HARNESS2-OK");
"""


def test_cleanup_removes_resize_and_repaint_timer():
    # Registro con referencia guardada (un solo sitio cada uno).
    assert ESTUDIO.count('window.addEventListener("resize"') == 1
    assert "winResizeHandler = function () { fitChartSize(); };" in ESTUDIO
    assert 'window.addEventListener("resize", winResizeHandler);' in ESTUDIO
    assert ESTUDIO.count("repaintTimer = setInterval(") == 1
    assert ", 5000);" in ESTUDIO
    # cleanup() los elimina y los deja a null (idempotente).
    assert 'window.removeEventListener("resize", winResizeHandler);' in CLEANUP
    assert "winResizeHandler = null;" in CLEANUP
    assert "clearInterval(repaintTimer);" in CLEANUP
    assert "repaintTimer = null;" in CLEANUP
    # Ya no quedan registros anónimos sin referencia.
    assert 'window.addEventListener("resize", function ()' not in ESTUDIO


def test_cleanup_keydown_handler_is_removable():
    # El handler de keydown se guarda (antes era un function-expression
    # nombrado inline cuyo nombre no era visible en cleanup y nunca se
    # eliminaba hasta la primera tecla tras salir).
    assert "keyHandler = function (ev)" in ESTUDIO
    assert "document.addEventListener(\"keydown\", keyHandler);" in ESTUDIO
    assert "document.removeEventListener(\"keydown\", keyHandler);" in CLEANUP
    assert "keyHandler = null;" in CLEANUP
    assert "removeEventListener(\"keydown\", onKey)" not in ESTUDIO


def test_storage_flags_reset_and_single_reregister():
    # Los guards de registro único siguen existiendo (reentrada = 1 handler).
    assert "if (!window._orbisAlertSoundArmed)" in ESTUDIO
    assert "if (!window._orbisDockStorageArmed)" in ESTUDIO
    assert ESTUDIO.count('window.addEventListener("storage"') == 2
    # cleanup() elimina los listeners Y resetea flags+refs: sin el reset, al
    # volver a Estudio el guard creía estar armado y la sync entre pestañas
    # moría en silencio.
    for name in ("AlertSound", "DockStorage"):
        assert ("window._orbis%sArmed = false;" % name) in CLEANUP, name
        assert ("window._orbis%sStorageHandler = null;" % name) in CLEANUP or \
               ("window._orbis%sHandler = null;" % name) in CLEANUP, name


def test_estudio_visrange_mirrors_vendor_normalization():
    """Fase 0: intacto. visRange() en Estudio normaliza orden + span mínimo
    0.5 como punto de paso único de la capa SVG; con el motor real cobra más
    sentido (el motor LANZA ante rangos invertidos en vez de ordenarlos), así
    que esta normalización lado-Estudio es la que impide enviarle rangos
    inválidos tras pan/zoom agresivos."""
    m = re.search(r"function visRange\(\)\s*\{(.*?)\n      \}", ESTUDIO, re.S)
    assert m is not None
    body = m.group(1)
    assert "getVisibleLogicalRange" in body
    assert "if (b < a)" in body  # orden, como exigía el vendor anterior
    assert "0.5" in body and "0.25" in body  # span mínimo lado-Estudio


def test_vendor_range_symmetry_and_interpolation():
    """Fase 0: reescrito sobre el motor real (ver docstring del módulo para
    cada equivalencia: roundtrip/setData-keep intactos; invertido lanza;
    span mínimo 1 del motor; overscroll recortado con span; null honesto en
    tiempos sin vela o fuera del histórico; fitContent a 0-99)."""
    proc = run_lwc_body(NODE_HARNESS2)
    assert "HARNESS2-OK" in proc.stdout, proc.stderr or proc.stdout
