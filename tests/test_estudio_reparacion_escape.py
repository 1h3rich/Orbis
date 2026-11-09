"""Reparación bloqueantes Fase 3: Escape cancela el arrastre de edición y los
cambios de contexto (loadAll/applyLayout/cleanup) no dejan gestos a medias.

Cubre con COMPORTAMIENTO REAL (funciones extraídas de
app/web/static/estudio.js y ejecutadas en Node, mismo patrón que
tests/test_estudio_fase3.py: brace-matching, sin copiar cuerpos):
cancelEditDragApply (restaura pts/fx-fy, retira el undo del gesto y devuelve
el redo), cancelDrawingInteractions (anula dragState+editDrag y reactiva la
navegación), hitTest real de rect sensible al grosor y de text con radio fijo,
más el cableado (Escape/loadAll/applyLayout/cleanup llaman a esas funciones y
el pushUndo del arrastre es diferido al primer movimiento, no al agarrar).

Este fichero solo añade; no toca ningún test existente.
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
    """Índice de la `}` que cierra la `{` en `start` (strings, comentarios
    y literales regex ignorados; misma técnica que test_estudio_fase3)."""
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


def _extract_hit_test(src):
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
    parts = [
        _extract_fn(ESTUDIO, "num"),
        _extract_fn(ESTUDIO, "effWidth"),
        _extract_fn(ESTUDIO, "distToSegment"),
        _extract_fn(ESTUDIO, "movePts"),
        _extract_fn(ESTUDIO, "moveEndpoint"),
        _extract_fn(ESTUDIO, "cancelEditDragApply"),
        _extract_fn(ESTUDIO, "DrawingPrimitive"),
        _extract_hit_test(ESTUDIO),
        fib_ext, fib_default,
        'var fibLevels = FIB_DEFAULT.slice();',
        'var market = "BTC/EUR";',
        'var chartBox = { clientHeight: 380, getBoundingClientRect: function () { return { width: 640, height: 380 }; } };',
    ]
    return "\n".join(parts)


def _node_run(body, extra=""):
    if shutil.which("node") is None:
        import pytest
        pytest.skip("node no disponible")
    full = _node_lib() + "\n" + extra + "\n" + body
    tmpdir = Path("/tmp/opencode")
    tmpdir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=str(tmpdir), prefix="repesc-",
                                     suffix=".js", delete=False, mode="w",
                                     encoding="utf-8") as fh:
        fh.write(full)
        path = fh.name
    proc = subprocess.run(["node", path], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("REPESC-JSON:")]
    assert line, proc.stdout
    return json.loads(line[0][len("REPESC-JSON:"):])


def _cancel_lib_stubs():
    """setNavEnabled + cancelDrawingInteractions reales con chart simulado.

    Fase 4: se declara activeTool (la invariante herramienta activa ⇒
    navegación desactivada lo lee); sin herramienta el resultado es el de
    siempre (navegación reactivada)."""
    return "\n".join([
        "var dragState = { tool: 'trend', moved: true };",
        "var editDrag = { id: 'd1', part: -1, moved: true };",
        "var activeTool = null;",
        "var navLog = [];",
        "var chart = { applyOptions: function (o) { navLog.push(o); } };",
        _extract_fn(ESTUDIO, "setNavEnabled"),
        _extract_fn(ESTUDIO, "cancelDrawingInteractions"),
    ])


def test_escape_cancel_body_restores_all_pts_and_pops_undo():
    """Cancelar un editDrag de cuerpo devuelve TODOS los pts al original,
    deja el undo como estaba (pop del paso del gesto) y devuelve el redo."""
    res = _node_run(r"""
const orig = [{ t: 100, p: 10 }, { t: 200, p: 20 }];
const drawings = [{ id: "d1", type: "trend", pts: JSON.parse(JSON.stringify(orig)) }];
const undoStack = ['{"snap":"previa"}', '{"snap":"gesto"}'];
const redoStack = [];
const ed = { id: "d1", part: -1, orig: JSON.parse(JSON.stringify(orig)), moved: true, redoBackup: ["R1"] };
// El gesto mueve el cuerpo (como hace onChartMouseMove con movePts).
drawings[0].pts = [{ t: 999, p: 99 }, { t: 1999, p: 199 }];
const ok = cancelEditDragApply(drawings, ed, undoStack, redoStack);
if (ok !== true) throw new Error("debería devolver true");
if (JSON.stringify(drawings[0].pts) !== JSON.stringify(orig)) throw new Error("pts no restaurados: " + JSON.stringify(drawings[0].pts));
if (undoStack.length !== 1 || undoStack[0] !== '{"snap":"previa"}') throw new Error("undo no retirado: " + JSON.stringify(undoStack));
if (redoStack.length !== 1 || redoStack[0] !== "R1") throw new Error("redo no devuelto: " + JSON.stringify(redoStack));
console.log("REPESC-JSON:" + JSON.stringify({ ok: true }));
""")
    assert res["ok"] is True


def test_escape_cancel_endpoint_restores_only_part():
    """Cancelar un editDrag de extremo restaura orig[part] y deja el resto;
    también retira su paso de undo."""
    res = _node_run(r"""
const orig = [{ t: 100, p: 10 }, { t: 200, p: 20 }];
const drawings = [{ id: "d1", type: "trend", pts: JSON.parse(JSON.stringify(orig)) }];
const undoStack = ['{"snap":"previa"}', '{"snap":"gesto"}'];
const redoStack = [];
const ed = { id: "d1", part: 1, orig: JSON.parse(JSON.stringify(orig)), moved: true, redoBackup: [] };
// El gesto mueve solo el extremo 1.
drawings[0].pts = [{ t: 100, p: 10 }, { t: 777, p: 77 }];
const ok = cancelEditDragApply(drawings, ed, undoStack, redoStack);
if (ok !== true) throw new Error("debería devolver true");
if (drawings[0].pts[1].t !== 200 || drawings[0].pts[1].p !== 20) throw new Error("extremo no restaurado");
if (drawings[0].pts[0].t !== 100 || drawings[0].pts[0].p !== 10) throw new Error("origen tocado");
if (undoStack.length !== 1) throw new Error("undo no retirado");
console.log("REPESC-JSON:" + JSON.stringify({ ok: true }));
""")
    assert res["ok"] is True


def test_escape_cancel_unmoved_keeps_undo_and_float_text():
    """Sin movimiento no hay pop de undo; el texto flotante restaura fx/fy."""
    res = _node_run(r"""
// 1) Agarrado pero sin mover: el undo queda intacto (push diferido).
let drawings = [{ id: "d1", type: "trend", pts: [{ t: 1, p: 1 }, { t: 2, p: 2 }] }];
let undoStack = ['{"snap":"previa"}'];
let ed = { id: "d1", part: -1, orig: [{ t: 1, p: 1 }, { t: 2, p: 2 }], moved: false, redoBackup: null };
if (cancelEditDragApply(drawings, ed, undoStack, []) !== true) throw new Error("falso negativo");
if (undoStack.length !== 1) throw new Error("undo tocado sin movimiento");
// 2) Texto flotante movido: fx/fy vuelven a fx0/fy0 y el undo se retira.
drawings = [{ id: "t1", type: "text", pinned: false, fx: 0.9, fy: 0.9, pts: [{ t: 5, p: 5 }] }];
undoStack = ['{"snap":"previa"}', '{"snap":"gesto"}'];
ed = { id: "t1", part: 0, orig: [{ t: 5, p: 5 }], fx0: 0.5, fy0: 0.3, moved: true, redoBackup: [] };
if (cancelEditDragApply(drawings, ed, undoStack, []) !== true) throw new Error("texto no cancelado");
if (drawings[0].fx !== 0.5 || drawings[0].fy !== 0.3) throw new Error("fx/fy no restaurados");
if (undoStack.length !== 1) throw new Error("undo del texto no retirado");
// 3) Sin arrastre: false y no toca nada.
if (cancelEditDragApply(drawings, null, undoStack, []) !== false) throw new Error("null debería ser false");
if (undoStack.length !== 1) throw new Error("null tocó el undo");
console.log("REPESC-JSON:" + JSON.stringify({ ok: true }));
""")
    assert res["ok"] is True


def test_cancel_interactions_nulls_states_and_restores_nav():
    """cancelDrawingInteractions real: dragState y editDrag a null y la
    navegación del motor reactivada (handleScroll/Scale nativos).

    Fase 4, equivalencia documentada: sin herramienta activa el resultado es
    el de siempre (navegación on); con herramienta pulsada la navegación se
    MANTIENE desactivada (invariante herramienta activa ⇒ navegación
    desactivada: loadAll/applyLayout no desarman la herramienta, así que
    deben seguir obedeciendo su botón)."""
    res = _node_run(r"""
cancelDrawingInteractions();
const out = { dragNull: dragState === null, editNull: editDrag === null, nav: navLog[navLog.length - 1] };
if (!out.dragNull || !out.editNull) throw new Error("estados no anulados");
if (!out.nav || !out.nav.handleScroll || out.nav.handleScroll.mouseWheel !== true) throw new Error("nav no restaurada: " + JSON.stringify(out.nav));
if (!out.nav.handleScale || out.nav.handleScale.pinch !== true) throw new Error("escala no restaurada");
// Con herramienta activa: estados a null pero navegación sigue off.
activeTool = "trend";
dragState = { tool: "trend" };
editDrag = { id: "d1", part: -1, moved: true };
cancelDrawingInteractions();
if (dragState !== null || editDrag !== null) throw new Error("con herramienta también se anulan los gestos");
const off = navLog[navLog.length - 1];
if (!off || off.handleScroll !== false || off.handleScale !== false) throw new Error("con herramienta la nav debe seguir off: " + JSON.stringify(off));
console.log("REPESC-JSON:" + JSON.stringify({ ok: true }));
""", extra=_cancel_lib_stubs())
    assert res["ok"] is True


def test_rect_hittest_thickness_sensitive():
    """rect usa tol = 6 + grosor/2 como el resto: a 7 px del borde, con
    width 4 (tol 8) selecciona y con width 1 (tol 6,5) no."""
    res = _node_run(r"""
const conv = { tx: function (t) { return t; }, py: function (p) { return p; } };
function H(o, x, y) { return new DrawingPrimitive(o).hitTest(x, y, conv); }
// Zona de (0,0) a (100,100): punto a 7 px fuera del borde derecho.
const thin = { id: "r1", type: "rect", width: 1, pts: [{ t: 0, p: 0 }, { t: 100, p: 100 }] };
const thick = { id: "r2", type: "rect", width: 4, pts: [{ t: 0, p: 0 }, { t: 100, p: 100 }] };
if (H(thin, 107, 50) !== null) throw new Error("fino no debería llegar a 7 px");
const h = H(thick, 107, 50);
if (!h || h.part !== -1) throw new Error("grueso debería llegar a 7 px: " + JSON.stringify(h));
// Cerca (5 px) llegan los dos; el interior siempre selecciona.
if (!H(thin, 105, 50) || !H(thick, 105, 50)) throw new Error("a 5 px deberían llegar ambos");
if (!H(thin, 50, 50) || !H(thick, 50, 50)) throw new Error("interior debería seleccionar");
if (H(thin, 200, 200) !== null || H(thick, 200, 200) !== null) throw new Error("lejos debería ser null");
console.log("REPESC-JSON:" + JSON.stringify({ ok: true }));
""")
    assert res["ok"] is True


def test_text_hittest_fixed_radius():
    """text mantiene radio fijo 12 px aunque el grosor cambie (no es trazo)."""
    res = _node_run(r"""
const conv = { tx: function (t) { return t; }, py: function (p) { return p; } };
function H(o, x, y) { return new DrawingPrimitive(o).hitTest(x, y, conv); }
const plain = { id: "x1", type: "text", pinned: true, pts: [{ t: 50, p: 50 }] };
const wide = { id: "x2", type: "text", pinned: true, width: 4, pts: [{ t: 50, p: 50 }] };
for (const o of [plain, wide]) {
  const near = H(o, 50 + 11, 50);
  if (!near || near.part !== 0) throw new Error("a 11 px debería agarrar: " + JSON.stringify(near));
  if (H(o, 50 + 13, 50) !== null) throw new Error("a 13 px no debería agarrar (radio fijo 12)");
}
console.log("REPESC-JSON:" + JSON.stringify({ ok: true }));
""")
    assert res["ok"] is True


def test_wiring_escape_loadall_layout_cleanup_and_deferred_push():
    """Cableado: Escape restaura vía cancelEditDragApply y limpia vía
    cancelDrawingInteractions; loadAll/applyLayout/cleanup abortan gestos vía
    la función común (sin duplicar); el pushUndo del arrastre es diferido al
    mousemove, no al agarrar en mousedown."""
    assert "function cancelDrawingInteractions()" in ESTUDIO
    assert "function cancelEditDragApply(" in ESTUDIO
    m = re.search(r"function loadAll\(\)\s*\{(.*?)\n    \}", ESTUDIO, re.S)
    assert m is not None
    assert "cancelDrawingInteractions();" in m.group(1)
    m = re.search(r"function applyLayout\(.*?\)\s*\{(.*?)\n    \}", ESTUDIO, re.S)
    assert m is not None
    assert "cancelDrawingInteractions();" in m.group(1)
    m = re.search(r"var cleanup = function \(\) \{(.*?)\n    \};", ESTUDIO, re.S)
    assert m is not None
    assert "cancelDrawingInteractions();" in m.group(1)
    assert "editDrag = null" in m.group(1)
    m = re.search(r"keyHandler = function \(ev\) \{(.*?)\n    \};", ESTUDIO, re.S)
    assert m is not None
    esc = m.group(1)
    assert "cancelEditDragApply(drawings, editDrag, undoStack, redoStack)" in esc
    assert "cancelDrawingInteractions();" in esc
    # Push diferido: mousedown ya no guarda undo; mousemove sí al moverse.
    m = re.search(r"function onChartMouseDown\(ev\)\s*\{(.*?)\n    \}", ESTUDIO, re.S)
    assert m is not None
    assert "pushUndo()" not in m.group(1)
    m = re.search(r"function onChartMouseMove\(ev\)\s*\{(.*?)\n    \}", ESTUDIO, re.S)
    assert m is not None
    assert "pushUndo();" in m.group(1)
    assert "redoBackup" in m.group(1)
