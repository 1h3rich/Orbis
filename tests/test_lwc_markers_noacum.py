"""A2: los primitivos de marcadores no se acumulan (motor real v5.0.9).

Verificación honesta en dos niveles:

1. Motor real (Node + DOM falso, ver tests/lwc_harness_helper.py): crear un
   chart, adjuntar marcadores con createSeriesMarkers + setMarkers,
   chart.remove(), y repetir 10 veces. La API v5 no expone un contador de
   primitivos adjuntos, así que se comprueba de forma indirecta pero real:
   a) tras cada destrucción no queda trabajo de render pendiente
      (global.__rafQ vacía tras bombear: sin frames fugados);
   b) el handler del chart destruido queda desvinculado de verdad:
      setMarkers sobre él lanza "Object is disposed" (un primitivo fugado
      y aún vivo no lanzaría "disposed");
   c) un chart nuevo parte limpio: acepta serie + marcadores vacíos y pinta
      sin errores (con A1, cualquier error RAF haría fallar el harness).
2. Código de estudio.js: instancia única por serie — una sola llamada real a
   createSeriesMarkers (en priceMarkersApi) y el repintado actualiza con
   setMarkers (refreshMarkers), nunca creando primitivos en bucle.
"""

from pathlib import Path

from lwc_harness_helper import run_lwc_body

STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "web" / "static"
ESTUDIO = (STATIC_DIR / "estudio.js").read_text(encoding="utf-8")

MARKERS_BODY = r"""
const stale = [];
for (let i = 0; i < 10; i++) {
  const chart = mkchart();
  const s = chart.addSeries(LW.CandlestickSeries, {});
  s.setData(candles100());
  pump();
  const h = LW.createSeriesMarkers(s, [{ time: 1700000000 + 50 * 60, position: "belowBar", shape: "arrowUp", color: "rgb(63, 185, 80)", text: "P" + i }]);
  pump();
  h.setMarkers([{ time: 1700000000 + 51 * 60, position: "belowBar", shape: "arrowUp", color: "rgb(63, 185, 80)", text: "Q" + i }]);
  pump();
  chart.remove();
  pump();
  if (global.__rafQ.length !== 0) throw new Error("raf pendiente tras remove en iter " + i);
  stale.push(h);
}
// El primitivo del chart destruido está desmontado de verdad.
let disposed = false;
try { stale[0].setMarkers([]); pump(); }
catch (e) { if (/disposed/i.test(e.message)) disposed = true; else throw e; }
if (!disposed) throw new Error("el handler viejo no se desvinculo del chart destruido");
// Un chart nuevo no hereda marcadores: parte limpio y pinta sin errores.
const c2 = mkchart();
const s2 = c2.addSeries(LW.CandlestickSeries, {});
s2.setData(candles100());
pump();
const h2 = LW.createSeriesMarkers(s2, []);
h2.setMarkers([]);
pump();
if (global.__rafQ.length !== 0) throw new Error("raf pendiente en chart nuevo limpio");
c2.remove();
pump();
console.log("MARKERS-NOACUM-OK");
"""


def test_markers_no_acumulan_tras_diez_ciclos():
    """Motor real: 10 ciclos crear/adjuntar/destruir no dejan ni frames
    pendientes ni primitivos vivos (el handler viejo lanza 'disposed'), y
    un chart nuevo arranca limpio sin heredar marcadores."""
    proc = run_lwc_body(MARKERS_BODY)
    assert "MARKERS-NOACUM-OK" in proc.stdout, proc.stderr or proc.stdout


def test_estudio_usa_instancia_unica_de_marcadores():
    """estudio.js crea UNA sola instancia de marcadores por serie
    (priceMarkersApi) y la actualiza con setMarkers (refreshMarkers):
    el repintado nunca adjunta primitivos nuevos en bucle."""
    code_uses = [ln for ln in ESTUDIO.splitlines()
                 if "createSeriesMarkers(" in ln and "window.LightweightCharts" in ln]
    assert len(code_uses) == 1, code_uses
    assert "priceMarkersApi()" in ESTUDIO
    assert "api.setMarkers(paperMarks.concat(planMarks))" in ESTUDIO
    # paintMarkers (llamado en cada pintada) delega en refreshMarkers, no crea.
    m = ESTUDIO[ESTUDIO.index("function paintMarkers()"):]
    m = m[:m.index("\n    }\n")]
    assert "createSeriesMarkers" not in m, "paintMarkers no debe crear primitivos"


M1_BODY = r"""
const chart = mkchart();
const s = chart.addSeries(LW.CandlestickSeries, {});
s.setData(candles100());
pump();
// Marcadores M1: circulo/cuadrado + size visible + texto largo con cantidad
// (legibles sin zoom); sin `title` porque LWC v5.0.9 lo ignora.
const h = LW.createSeriesMarkers(s, []);
pump();
h.setMarkers([
  { time: 1700000000 + 10 * 60, position: "belowBar", shape: "circle", color: "rgb(63, 185, 80)", size: 2, text: "P · 0,01 BTC" },
  { time: 1700000000 + 20 * 60, position: "aboveBar", shape: "square", color: "rgb(248, 81, 73)", size: 2, text: "S" },
]);
pump();
if (global.__rafQ.length !== 0) throw new Error("raf pendiente tras setMarkers M1");
// setMarkers REEMPLAZA: repintar sustituye, no acumula primitivos.
h.setMarkers([
  { time: 1700000000 + 11 * 60, position: "belowBar", shape: "circle", color: "rgb(63, 185, 80)", size: 2, text: "P · 0,02 BTC" },
]);
pump();
if (global.__rafQ.length !== 0) throw new Error("raf pendiente tras re-set M1");
chart.remove();
pump();
if (global.__rafQ.length !== 0) throw new Error("raf pendiente tras remove M1");
let disposed = false;
try { h.setMarkers([]); pump(); }
catch (e) { if (/disposed/i.test(e.message)) disposed = true; else throw e; }
if (!disposed) throw new Error("el handler M1 no se desvinculo del chart destruido");
console.log("MARKERS-M1-OK");
"""


def test_markers_m1_size_y_texto_largo_sin_acumular():
    """Motor real: setMarkers con size + texto largo ('P · 0,01 BTC',
    círculo/cuadrado) no lanza, reemplaza sin acumular y se desmonta con
    el chart (el handler viejo lanza 'disposed')."""
    proc = run_lwc_body(M1_BODY)
    assert "MARKERS-M1-OK" in proc.stdout, proc.stderr or proc.stdout


def test_estudio_marcadores_m1_legibles_sin_title():
    """M1 en fuente: paperMarks se construye desde paperOps con
    círculo/cuadrado + size visible y texto con cantidad; `title` deja de
    usarse (LWC lo ignora); con showPAPER=false no se pide ni se pinta."""
    m = ESTUDIO[ESTUDIO.index("function paintMarkers()"):]
    m = m[:m.index("\n    }\n")]
    assert "createSeriesMarkers" not in m, "paintMarkers no debe crear primitivos"
    assert "buildPaperMarks(paperOps, mk, candles" in m
    assert "title" not in m, "title es ignorado por LWC: no debe portar info"
    assert "if (!showPAPER)" in m
    assert m.index("if (!showPAPER)") < m.index("/paper/operations")
    assert "paperOps = []" in m and "hideMarkerTip()" in m
    assert "buildPaperMarks" in ESTUDIO and "paperOpNear" in ESTUDIO
    assert "updateMarkerTip" in ESTUDIO and 'id="st-marker-tip"' in ESTUDIO
