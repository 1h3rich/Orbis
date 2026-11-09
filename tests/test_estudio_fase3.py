"""Fase 3: dibujos editables + mano alzada.

Cubre con COMPORTAMIENTO REAL (funciones extraídas de
app/web/static/estudio.js y ejecutadas en Node, mismo patrón que
tests/test_estudio_ema.py: brace-matching, sin copiar cuerpos):
creación (makeDrawing con pts correctos), mover un extremo de un trendline
(cambia SOLO ese punto), mover el objeto completo (desplaza TODOS los
puntos manteniendo la forma), duplicar (copia independiente), borrar
(removeDrawing), bloqueado no arrastra (canDragDrawing), freehand (pts en
tiempo/precio + muestreo), hitTest real (cuerpo/extremos/polilínea y
tolerancia con grosor), drawOne real (grosor/estilo/opacidad aplicados y
retrocompatibilidad: sin campos nuevos el marcado es el de antes), y
persistencia por round-trip JSON. Lo que solo es cableado DOM (posición del
panel, listeners) se comprueba con aserciones de presencia acotadas.

Equivalencias documentadas: este fichero solo añade; no toca ningún test
existente. La rama `measure` duplicada al final del hitTest se eliminó por
estar cubierta por la rama `trend||measure` (mismo check); ningún test
anterior la fijaba.
"""

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "web" / "static"
ESTUDIO = (STATIC_DIR / "estudio.js").read_text(encoding="utf-8")
CSS = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
VENDOR = (STATIC_DIR / "vendor" / "lightweight-charts.standalone.production.js").read_text(encoding="utf-8")


def _balanced_end(src, start):
    """Devuelve el índice de la `}` que cierra la `{` en `start`,
    ignorando strings, comentarios y literales regex."""
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
    """Extrae `function name(...) {...}` con balanceo de llaves.

    Robusto ante llaves anidadas, strings, comentarios y literales regex
    (p. ej. el `/'/g` de escA, que desincroniza a un contador ingenuo: la
    comilla del regex no abre un string).
    """
    m = re.search(r"function " + re.escape(name) + r"\s*\(", src)
    assert m is not None, name
    start = src.index("{", m.end())
    return src[m.start():_balanced_end(src, start) + 1]


def _extract_hit_test(src):
    """Extrae `DrawingPrimitive.prototype.hitTest = function...};`."""
    m = re.search(r"DrawingPrimitive\.prototype\.hitTest\s*=\s*function\s*\(", src)
    assert m is not None, "hitTest"
    start = src.index("{", m.end())
    end = _balanced_end(src, start) + 1
    if end < len(src) and src[end] == ";":
        end += 1
    return src[m.start():end]


def _node_lib():
    m = re.search(r"var FIB_EXT = \[.*?\];", ESTUDIO)
    assert m is not None
    fib_ext = m.group(0)
    m = re.search(r"var FIB_DEFAULT = \[.*?\];", ESTUDIO)
    assert m is not None
    fib_default = m.group(0)
    m = re.search(r"var DRAW_WIDTHS = \[.*?\];", ESTUDIO)
    assert m is not None
    widths = m.group(0)
    parts = [
        _extract_fn(ESTUDIO, "num"),
        _extract_fn(ESTUDIO, "canon"),
        _extract_fn(ESTUDIO, "canon2"),
        _extract_fn(ESTUDIO, "precision"),
        _extract_fn(ESTUDIO, "fmtP"),
        _extract_fn(ESTUDIO, "escA"),
        _extract_fn(ESTUDIO, "distToSegment"),
        _extract_fn(ESTUDIO, "effWidth"),
        _extract_fn(ESTUDIO, "effDash"),
        _extract_fn(ESTUDIO, "effOpacity"),
        _extract_fn(ESTUDIO, "canDragDrawing"),
        _extract_fn(ESTUDIO, "movePts"),
        _extract_fn(ESTUDIO, "moveEndpoint"),
        _extract_fn(ESTUDIO, "removeDrawing"),
        _extract_fn(ESTUDIO, "dupDrawing"),
        _extract_fn(ESTUDIO, "strokeToTimePrice"),
        _extract_fn(ESTUDIO, "decimateStroke"),
        _extract_fn(ESTUDIO, "line"),
        _extract_fn(ESTUDIO, "dot"),
        _extract_fn(ESTUDIO, "label"),
        _extract_fn(ESTUDIO, "drawOne"),
        _extract_fn(ESTUDIO, "makeDrawing"),
        _extract_fn(ESTUDIO, "DrawingPrimitive"),
        _extract_hit_test(ESTUDIO),
        fib_ext, fib_default, widths,
        'var FREEHAND_MIN_PX = 5;',
        'var fibLevels = FIB_DEFAULT.slice();',
        'var market = "BTC/EUR";',
        'var selectedId = null;',
        'var chartBox = { clientHeight: 380, getBoundingClientRect: function () { return { width: 640, height: 380 }; } };',
        'var window = { prompt: function () { throw new Error("prompt no debe usarse en tests"); } };',
    ]
    return "\n".join(parts)


def _node_run(body):
    """Ejecuta la librería real + body en Node; el body debe imprimir
    FASE3-JSON:{...} y lanzar ante cualquier desviación."""
    if shutil.which("node") is None:
        import pytest
        pytest.skip("node no disponible")
    full = _node_lib() + "\n" + body
    tmpdir = Path("/tmp/opencode")
    tmpdir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=str(tmpdir), prefix="fase3-",
                                     suffix=".js", delete=False, mode="w",
                                     encoding="utf-8") as fh:
        fh.write(full)
        path = fh.name
    proc = subprocess.run(["node", path], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("FASE3-JSON:")]
    assert line, proc.stdout
    return json.loads(line[0][len("FASE3-JSON:"):])


def test_pure_defaults_retrocompat():
    """effWidth/effDash/effOpacity devuelven los valores de siempre cuando
    el dibujo no trae los campos nuevos (sin migración de datos)."""
    res = _node_run(r"""
const out = {};
out.w_unset = effWidth({}, 1.5);
out.w_set = effWidth({ width: 4 }, 1.5);
out.w_bad = effWidth({ width: "xx" }, 1.5);
out.d_legacy_empty = effDash({}, "");
out.d_measure = effDash({}, "4 3");
out.d_solid_over = effDash({ style: "solid" }, "4 3");
out.d_dashed = effDash({ style: "dashed" }, "");
out.o_unset = effOpacity({});
out.o_half = effOpacity({ opacity: 0.5 });
out.o_bad = effOpacity({ opacity: 9 });
out.can_lock = canDragDrawing({ locked: true });
out.can_free = canDragDrawing({ locked: false });
if (out.w_unset !== 1.5) throw new Error("width defecto");
if (out.w_set !== 4) throw new Error("width explícito");
if (out.w_bad !== 1.5) throw new Error("width inválido");
if (out.d_legacy_empty !== "") throw new Error("dash legacy vacío");
if (out.d_measure !== "4 3") throw new Error("dash medida");
if (out.d_solid_over !== "") throw new Error("solid pisa defecto");
if (out.d_dashed !== "5 4") throw new Error("dashed por defecto");
if (out.o_unset !== 1) throw new Error("opacidad defecto");
if (out.o_half !== 0.5) throw new Error("opacidad explícita");
if (out.o_bad !== 1) throw new Error("opacidad inválida");
if (out.can_lock !== false || out.can_free !== true) throw new Error("canDrag");
console.log("FASE3-JSON:" + JSON.stringify(out));
""")
    assert res["w_unset"] == 1.5 and res["o_unset"] == 1
    assert res["can_lock"] is False and res["can_free"] is True


def test_move_endpoint_keeps_rest():
    """Mover el extremo final de un trendline cambia SOLO ese punto."""
    res = _node_run(r"""
const pts = [{ t: 100, p: 10 }, { t: 200, p: 20 }];
const moved = moveEndpoint(pts, 1, 300, 30);
if (moved.length !== 2) throw new Error("longitud");
if (moved[0].t !== 100 || moved[0].p !== 10) throw new Error("origen tocado: " + JSON.stringify(moved[0]));
if (moved[1].t !== 300 || moved[1].p !== 30) throw new Error("destino mal: " + JSON.stringify(moved[1]));
if (pts[1].t !== 200) throw new Error("muta el original");
const whole = movePts(pts, 50, 5);
if (whole[0].t !== 150 || whole[0].p !== 15) throw new Error("todo mal A");
if (whole[1].t !== 250 || whole[1].p !== 25) throw new Error("todo mal B");
if ((whole[1].t - whole[0].t) !== 100 || (whole[1].p - whole[0].p) !== 10) throw new Error("forma no conservada");
console.log("FASE3-JSON:" + JSON.stringify({ ok: true }));
""")
    assert res["ok"] is True


def test_duplicate_is_independent_and_remove_deletes():
    """Duplicar crea copia profunda (tocar la copia no afecta al original)
    y borrar la elimina de la lista."""
    res = _node_run(r"""
const d = { id: "d1", type: "trend", pts: [{ t: 1, p: 2 }, { t: 3, p: 4 }], color: "#e3b341" };
const copy = dupDrawing(d, 60, 0.5, "d2");
if (copy.id !== "d2") throw new Error("id");
if (copy.pts[0].t !== 61 || copy.pts[0].p !== 2.5) throw new Error("offset: " + JSON.stringify(copy.pts[0]));
copy.pts[0].t = 999;
if (d.pts[0].t !== 1) throw new Error("aliasing con el original");
let list = [d, copy];
list = removeDrawing(list, "d1");
if (list.length !== 1 || list[0].id !== "d2") throw new Error("borrado: " + JSON.stringify(list.map(function (o) { return o.id; })));
console.log("FASE3-JSON:" + JSON.stringify({ ok: true }));
""")
    assert res["ok"] is True


def test_make_drawing_creation():
    """makeDrawing crea con pts correctos, defaults y niveles fib por valor."""
    res = _node_run(r"""
const t = makeDrawing("trend", [{ t: 10, p: 1.5 }, { t: 20, p: 2.5 }]);
if (t.type !== "trend" || t.pts.length !== 2) throw new Error("trend");
if (t.pts[0].t !== 10 || t.pts[1].p !== 2.5) throw new Error("pts");
if (t.color !== "#e3b341" || t.locked !== false || t.hidden !== false) throw new Error("defaults");
if (!t.id || t.id[0] !== "d") throw new Error("id");
const tx = makeDrawing("text", [{ t: 1, p: 1 }], { text: "hola" });
if (tx.text !== "hola" || tx.pinned !== true) throw new Error("texto");
const f = makeDrawing("fib", [{ t: 1, p: 1 }, { t: 2, p: 2 }]);
if (!Array.isArray(f.levels) || f.levels.length !== 7) throw new Error("niveles fib");
fibLevels.push(9.99);
if (f.levels.indexOf(9.99) >= 0) throw new Error("niveles por referencia");
const fh = makeDrawing("freehand", [{ t: 1, p: 1 }, { t: 2, p: 2 }, { t: 3, p: 3 }]);
if (fh.pts.length !== 3 || fh.pts[2].t !== 3) throw new Error("freehand");
console.log("FASE3-JSON:" + JSON.stringify({ ok: true }));
""")
    assert res["ok"] is True


def test_freehand_sampling_and_timeprice_mapping():
    """El gesto se muestrea por distancia mínima (no miles de puntos) y los
    puntos guardados van en tiempo/precio, nunca en píxeles."""
    res = _node_run(r"""
const raw = [];
for (let i = 0; i < 300; i++) raw.push({ x: i, y: 100 + Math.sin(i / 10) * 20 });
const kept = decimateStroke(raw, 5);
if (!(kept.length < 150 && kept.length > 10)) throw new Error("muestreo raro: " + kept.length);
if (kept[0].x !== 0 || kept[kept.length - 1].x !== 299) throw new Error("extremos del gesto");
const conv = { tAt: function (x) { return 1700000000 + Math.round(x) * 60; }, pAt: function (y) { return 50000 - y; } };
const tp = strokeToTimePrice(kept, conv);
if (tp.length !== kept.length) throw new Error("longitud mapeo");
for (const q of tp) {
  if (typeof q.t !== "number" || typeof q.p !== "number") throw new Error("no numérico");
  if (q.t < 1700000000 || q.p <= 0) throw new Error("fuera de rango: " + JSON.stringify(q));
}
if (tp[0].t !== 1700000000 || tp[tp.length - 1].t !== 1700000000 + 299 * 60) throw new Error("anclaje");
console.log("FASE3-JSON:" + JSON.stringify({ kept: kept.length }));
""")
    assert 10 < res["kept"] < 150


def test_hittest_body_endpoints_polyline_and_thickness():
    """hitTest real: cuerpo (part -1), extremos (part i), polilínea
    freehand (interior = cuerpo, no vértice) y tolerancia con grosor."""
    res = _node_run(r"""
const conv = { tx: function (t) { return t; }, py: function (p) { return p; } };
function H(o, x, y) { return new DrawingPrimitive(o).hitTest(x, y, conv); }
const trend = { id: "t", type: "trend", pts: [{ t: 0, p: 50 }, { t: 100, p: 50 }] };
let h = H(trend, 2, 50);
if (!h || h.part !== 0) throw new Error("extremo A: " + JSON.stringify(h));
h = H(trend, 98, 50);
if (!h || h.part !== 1) throw new Error("extremo B: " + JSON.stringify(h));
h = H(trend, 50, 53);
if (!h || h.part !== -1) throw new Error("cuerpo: " + JSON.stringify(h));
h = H(trend, 50, 200);
if (h !== null) throw new Error("lejos debería ser null");
// Grosor: a 7 px del cuerpo, con width 1.5 (tol 6.75) falla; con width 4 (tol 8) acierta.
h = H(trend, 50, 57);
if (h !== null) throw new Error("fino no debería llegar: " + JSON.stringify(h));
const thick = { id: "t2", type: "trend", width: 4, pts: [{ t: 0, p: 50 }, { t: 100, p: 50 }] };
h = H(thick, 50, 57);
if (!h || h.part !== -1) throw new Error("grueso debería llegar: " + JSON.stringify(h));
// Freehand: interior del trazo = cuerpo aunque caiga sobre un vértice.
const fh = { id: "f", type: "freehand", pts: [{ t: 0, p: 50 }, { t: 50, p: 55 }, { t: 100, p: 50 }] };
h = H(fh, 50, 55);
if (!h || h.part !== -1) throw new Error("interior freehand: " + JSON.stringify(h));
h = H(fh, 52, 53);
if (!h || h.part !== -1) throw new Error("segmento freehand: " + JSON.stringify(h));
h = H(fh, 1, 51);
if (!h || h.part !== 0) throw new Error("inicio freehand: " + JSON.stringify(h));
h = H(fh, 99, 51);
if (!h || h.part !== 2) throw new Error("fin freehand: " + JSON.stringify(h));
// Horizontal: cuerpo con tolerancia.
const hl = { id: "h", type: "h", pts: [{ t: 5, p: 50 }] };
h = H(hl, 300, 52);
if (!h || h.part !== -1) throw new Error("h line: " + JSON.stringify(h));
console.log("FASE3-JSON:" + JSON.stringify({ ok: true }));
""")
    assert res["ok"] is True


def test_drawone_applies_props_and_legacy_identical():
    """drawOne real: aplica grosor/estilo/opacidad; sin campos nuevos el
    marcado es el de antes (mismo stroke-width/dash, sin grupo opacidad) y
    los puntos de control quedan fuera del grupo."""
    res = _node_run(r"""
const conv = { tx: function (t) { return t * 2; }, py: function (p) { return 400 - p; } };
const legacy = { id: "a", type: "trend", pts: [{ t: 0, p: 50 }, { t: 100, p: 50 }] };
const sLeg = drawOne(legacy, conv);
if (sLeg.indexOf('stroke-width="1.5"') < 0) throw new Error("ancho legacy: " + sLeg);
if (sLeg.indexOf("dasharray") >= 0) throw new Error("dash legacy inesperado");
if (sLeg.indexOf("<g opacity") >= 0) throw new Error("grupo legacy inesperado");
if ((sLeg.match(/<circle/g) || []).length !== 2) throw new Error("controles legacy");
const styled = { id: "b", type: "trend", width: 4, style: "dashed", opacity: 0.5, pts: [{ t: 0, p: 50 }, { t: 100, p: 50 }] };
const sSt = drawOne(styled, conv);
if (sSt.indexOf('stroke-width="4"') < 0) throw new Error("ancho nuevo: " + sSt);
if (sSt.indexOf('stroke-dasharray="5 4"') < 0) throw new Error("estilo nuevo: " + sSt);
if (sSt.indexOf('<g opacity="0.5">') < 0) throw new Error("opacidad nueva: " + sSt);
if (!(sSt.indexOf("</g>") < sSt.indexOf("<circle"))) throw new Error("controles dentro del grupo");
const meas = { id: "m", type: "measure", pts: [{ t: 0, p: 50 }, { t: 100, p: 60 }] };
if (drawOne(meas, conv).indexOf('stroke-dasharray="4 3"') < 0) throw new Error("medidor legacy");
const rLeg = drawOne({ id: "r", type: "rect", pts: [{ t: 0, p: 10 }, { t: 50, p: 20 }] }, conv);
if (rLeg.indexOf("stroke-width") >= 0) throw new Error("rect legacy con ancho: " + rLeg);
const rW = drawOne({ id: "r2", type: "rect", width: 2.5, pts: [{ t: 0, p: 10 }, { t: 50, p: 20 }] }, conv);
if (rW.indexOf('stroke-width="2.5"') < 0) throw new Error("rect ancho: " + rW);
// Freehand + round-trip de persistencia: recargado se ve idéntico.
const fh = { id: "f", type: "freehand", color: "#e3b341", pts: [{ t: 1, p: 1 }, { t: 2, p: 2 }, { t: 3, p: 1 }] };
const sFh = drawOne(fh, conv);
if (sFh.indexOf("<polyline") < 0 || sFh.indexOf('stroke-width="2"') < 0) throw new Error("freehand: " + sFh);
if (sFh.indexOf('stroke-linecap="round"') < 0) throw new Error("remates freehand");
const sFh2 = drawOne(JSON.parse(JSON.stringify(fh)), conv);
if (sFh !== sFh2) throw new Error("round-trip distinto");
console.log("FASE3-JSON:" + JSON.stringify({ ok: true }));
""")
    assert res["ok"] is True


def test_stacking_order_draw_above_vendor_below_overlays():
    """#st-draw (z-index 5) por encima de los canvas del vendor (1/2) y su
    logo (3), por debajo de overlays de arrastre (49/50), menú (60), panel
    flotante (100) y toasts (150). Valores leídos de las fuentes reales."""
    assert re.search(r"#st-draw\s*\{[^}]*z-index:\s*5", CSS) is not None
    assert "z-index: 100" in CSS and ".float-config" in CSS
    assert "z-index: 60" in CSS and ".ctxmenu" in CSS
    assert "z-index: 150" in CSS and "#toasts" in CSS
    # Vendor real: canvas zIndex 1/2, logo CSS z-index:3, overlays 49/50.
    assert 'zIndex="1"' in VENDOR and 'zIndex="2"' in VENDOR
    assert "z-index:3" in VENDOR
    assert 'zIndex="49"' in VENDOR and 'zIndex="50"' in VENDOR
    order = [1, 2, 3, 5, 49, 50, 60, 100, 150]
    assert order == sorted(order)
    # El SVG inline del Estudio declara el mismo 5.
    assert "z-index:5" in ESTUDIO.replace(" ", "")


def test_unlock_price_auto_resets_leftovers():
    """unlockPriceAuto resetea lastInDataPriceRange y navDown (defensivo)."""
    m = re.search(r"function unlockPriceAuto\(\)\s*\{(.*?)\n    \}", ESTUDIO, re.S)
    assert m is not None
    body = m.group(1)
    assert "lastInDataPriceRange = null" in body
    assert "navDown = null" in body


def test_edit_wiring_and_single_paint_route():
    """Cableado Fase 3: arrastre (cuerpo/extremos), freehand y panel; una
    sola ruta de pintado (sin DrawingPrimitive.prototype.draw duplicado)."""
    assert "DrawingPrimitive.prototype.draw" not in ESTUDIO
    for token in ("editDrag", "setNavEnabled", "moveEndpoint", "movePts",
                  "canDragDrawing", "suppressChartClick = true",
                  "decimateStroke", "strokeToTimePrice", "paintFreehandPreview",
                  "FREEHAND_MIN_PX", "freehand:"):
        assert token in ESTUDIO, token
    # Borrados unificados en removeDrawing (panel, menú, Supr, lista).
    assert ESTUDIO.count("removeDrawing(drawings,") >= 4
    # Panel: grosor, estilo, opacidad, duplicar (sin romper color/bloqueo).
    for pid in ('id="fc-width"', 'id="fc-style"', 'id="fc-op"', 'id="fc-dup"',
                'id="fc-col"', 'id="fc-lock"', 'id="fc-hide"', 'id="fc-del"'):
        assert pid in ESTUDIO, pid
    assert "dupDrawing(obj," in ESTUDIO
    # Toolbar: herramienta nueva con el patrón de las demás.
    assert 'data-tool="freehand"' in ESTUDIO
    assert ">Trazo</span>" in ESTUDIO
    assert "aria-pressed" in ESTUDIO
    # Ayuda: trazo libre + edición documentadas, sin perder lo anterior.
    assert "Trazo libre" in ESTUDIO and "puntos de control" in ESTUDIO
    assert "duplicarlo" in ESTUDIO and "grosor" in ESTUDIO
    for kw in ("arrastra de A a B", "un toque", "Proyección",
               "niveles", "bloquear", "ocultar", "eliminar"):
        assert kw in ESTUDIO, kw


def test_docs_no_longer_list_drawings_pending():
    """docs/ESTADO_Y_FALLOS_PENDIENTES.md ya no lista drawings como
    pendiente y recoge la Fase 3 + la nota del z-index."""
    docs = (STATIC_DIR.parent.parent.parent / "docs" / "ESTADO_Y_FALLOS_PENDIENTES.md").read_text(encoding="utf-8")
    assert "Fase 3" in docs
    assert "z-index" in docs or "zIndex" in docs
    # §6 decía "Lo que sigue pendiente...: EMA/drawings/timeframes...": ahora
    # es un cierre (Fases 1-3) y drawings figura como cerrado, no pendiente.
    m = re.search(r"\*\*Cierre \(Fases 1-3.*?(?=\n## |\Z)", docs, re.S)
    assert m is not None
    cierre = m.group(0).lower()
    assert "drawings" in cierre or "dibujos" in cierre
    assert "pendiente" not in cierre or "cerrad" in cierre
