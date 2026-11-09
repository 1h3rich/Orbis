"""Helper Fase 0: ejecuta Lightweight Charts v5.0.9 REAL en Node con un DOM falso.

El motor real necesita DOM + canvas + requestAnimationFrame para crear el
chart y bombear frames de render (el rango visible solo se aplica al
procesar el siguiente frame). Este prelude proporciona:

* elementos falsos (createElement/appendChild/style/getBoundingClientRect),
* contexto 2d falso (no-ops + measureText/getImageData/gradientes),
* getComputedStyle que normaliza cualquier color CSS a rgb()/rgba() (el
  parser del motor solo acepta ese formato),
* requestAnimationFrame con cola + __pumpFrames(n) para drenarla de forma
  determinista (sin timers reales),
* helpers: LW (window.LightweightCharts), mkchart(w, h), candles100(), pump().

Uso: run_lwc_body("<js con console.log + throw si algo falla>") devuelve el
CompletedProcess de node. Si node no existe, hace pytest.skip (igual que
los harnesses anteriores).
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "web" / "static"
VENDOR_PATH = STATIC_DIR / "vendor" / "lightweight-charts.standalone.production.js"

PRELUDE = r"""
const fs = require("fs");
function fakeCtx() {
  const grad = { addColorStop(){} };
  return new Proxy({
    measureText: () => ({ width: 10 }),
    getImageData: () => ({ data: [] }),
    createLinearGradient: () => grad,
    canvas: null,
  }, { get: (t,k) => {
    if (k in t) return t[k];
    return () => {};
  }, set: (t,k,v) => { t[k]=v; return true; } });
}
function makeEl(tag) {
  const el = {
    tagName: String(tag||"div").toUpperCase(), style: {}, children: [],
    _w: 640, _h: 320,
    ownerDocument: null, parentElement: null, firstChild: null,
    getClientRects(){ return []; },
    setAttribute(){}, getAttribute(){ return null; }, removeAttribute(){},
    addEventListener(){}, removeEventListener(){},
    appendChild(c){ c.parentElement = el; el.children.push(c); return c; },
    removeChild(c){ const i=el.children.indexOf(c); if(i>=0)el.children.splice(i,1); return c; },
    insertBefore(c){ el.children.unshift(c); return c; },
    getBoundingClientRect(){ const w = 640, h = 320; return { width: w, height: h, left:0, top:0, right:w, bottom:h, x:0, y:0 }; },
    classList: { add(){}, remove(){}, contains(){ return false; }, toggle(){} },
    dataset: {}, tabIndex: 0, innerHTML: "",
    querySelector(){ return null; }, querySelectorAll(){ return []; },
    toggleAttribute(){}, contains(){ return false; }, closest(){ return null; },
    focus(){}, blur(){}, scrollIntoView(){}, innerText: "", textContent: "",
    cloneNode(){ return makeEl(tag); },
    getContext: () => fakeCtx(),
    width: 300, height: 150,
  };
  if (!global.__ownerDoc) global.__ownerDoc = { defaultView: global.window, createElement: function(){ return global.document.createElement.apply(global.document, arguments); }, addEventListener(){}, removeEventListener(){}, documentElement: null };
  el.ownerDocument = global.__ownerDoc;
  el.style = new Proxy({}, { get:(t,k)=> (k==="setProperty"?()=>{}: (t[k] ?? "")), set:(t,k,v)=>(t[k]=v,true) });
  return el;
}
global.window = global; global.self = global;
global.document = {
  createElement: (t) => makeEl(t),
  getElementById: () => null,
  documentElement: makeEl("html"), body: makeEl("body"),
  fonts: { ready: Promise.resolve(), load: async () => [], add: () => {} },
  hidden: false, addEventListener(){}, removeEventListener(){},
};
global.navigator = { language: "es-ES", userAgent: "node" };
global.devicePixelRatio = 1;
global.__rafQ = [];
global.__rafId = 0;
global.requestAnimationFrame = (cb) => { global.__rafQ.push({ id: ++global.__rafId, cb }); return global.__rafId; };
global.cancelAnimationFrame = (id) => { global.__rafQ = global.__rafQ.filter(e => e.id !== id); };
global.__pumpFrames = (max = 50) => {
  let n = 0, t = 0;
  const errs = [];
  while (global.__rafQ.length && n < max) { n++; t += 16; const q = global.__rafQ; global.__rafQ = []; for (const e of q) { try { e.cb(t); } catch (err) { errs.push(err); } } }
  if (errs.length) { const agg = new Error("RAF-ERR x" + errs.length + ": " + errs.map(e => e && e.message).join(" | ")); agg.cause = errs; throw agg; }
  return n;
};
global.ResizeObserver = class { observe(){} unobserve(){} disconnect(){} };
const NAMED_COLORS = { black:[0,0,0], white:[255,255,255], red:[255,0,0], green:[0,128,0], lime:[0,255,0], blue:[0,0,255], transparent:[0,0,0,0] };
function normColor(c) {
  c = String(c || "").trim().toLowerCase();
  if (!c) return "";
  if (c === "transparent") return "rgba(0, 0, 0, 0)";
  let m = c.match(/^#([0-9a-f]{3})$/);
  if (m) { const h = m[1]; return `rgb(${parseInt(h[0]+h[0],16)}, ${parseInt(h[1]+h[1],16)}, ${parseInt(h[2]+h[2],16)})`; }
  m = c.match(/^#([0-9a-f]{6})$/);
  if (m) { const h = m[1]; return `rgb(${parseInt(h.slice(0,2),16)}, ${parseInt(h.slice(2,4),16)}, ${parseInt(h.slice(4,6),16)})`; }
  m = c.match(/^#([0-9a-f]{8})$/);
  if (m) { const h = m[1]; return `rgba(${parseInt(h.slice(0,2),16)}, ${parseInt(h.slice(2,4),16)}, ${parseInt(h.slice(4,6),16)}, ${(parseInt(h.slice(6,8),16)/255).toFixed(3)})`; }
  m = c.match(/^rgba?\s*\(([\d.,\s/%]+)\)$/);
  if (m) return c;
  if (NAMED_COLORS[c]) { const v = NAMED_COLORS[c]; return v.length === 4 ? `rgba(${v[0]}, ${v[1]}, ${v[2]}, ${v[3]})` : `rgb(${v[0]}, ${v[1]}, ${v[2]})`; }
  return c;
}
global.getComputedStyle = (el) => {
  const c = normColor((el && el.style && el.style.color) || "");
  return { getPropertyValue: (p) => (p === "color" ? c : ""), color: c };
};
global.matchMedia = () => ({ matches: false, addEventListener(){}, removeEventListener(){} });
global.location = { href: "http://localhost/" };
eval(fs.readFileSync(process.env.LWC_VENDOR, "utf8"));
const LW = global.LightweightCharts;
function candles100() {
  const rows = [];
  for (let i = 0; i < 100; i++) rows.push({ time: 1700000000 + i * 60, open: 100 + i, high: 101 + i, low: 99 + i, close: 100.5 + i });
  return rows;
}
function mkchart(w, h) {
  return LW.createChart(global.document.createElement("div"), { autoSize: false, width: w || 640, height: h || 400 });
}
function pump(n) { global.__pumpFrames(n || 200); }
"""


def run_lwc_body(body, timeout=120):
    """Ejecuta body (JS) tras el prelude; el body debe hacer console.log con
    un marcador final y throw ante cualquier desviación. Devuelve el
    CompletedProcess. Sin node: pytest.skip."""
    if shutil.which("node") is None:
        pytest.skip("node no disponible")
    tmpdir = Path("/tmp/opencode")
    tmpdir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=str(tmpdir), prefix="lwc-fase0-", suffix=".js", delete=False, mode="w", encoding="utf-8"
    ) as fh:
        fh.write(PRELUDE + body)
        path = fh.name
    import os

    env = dict(os.environ)
    env["LWC_VENDOR"] = str(VENDOR_PATH)
    return subprocess.run(["node", path], capture_output=True, text=True, timeout=timeout, env=env)


# Fase 2: mismo prelude pero con registro de listeners y disparo de eventos
# sintéticos (rueda/ratón a nivel de elemento, document y window). El
# prelude base los declara no-op, así que los gestos nativos del motor no se
# podían ejercitar; este runner sí permite medir zoom de rueda, pan con
# Mayús+rueda y estabilidad de la escala de precios. No toca run_lwc_body.
PRELUDE_EV = (
    "global.__allEls = [];\n"
    + PRELUDE.replace(
        "el.ownerDocument = global.__ownerDoc;",
        "el.ownerDocument = global.__ownerDoc;\n"
        "  global.__allEls.push(el);",
    )
    .replace(
        "global.document = {\n  createElement: (t) => makeEl(t),",
        "global.document = {\n  _handlers: {},\n  createElement: (t) => makeEl(t),",
    )
    .replace(
        "hidden: false, addEventListener(){}, removeEventListener(){},",
        "hidden: false,\n"
        "  addEventListener(t, f) { (global.document._handlers[t] = global.document._handlers[t] || []).push(f); },\n"
        "  removeEventListener(t, f) { const a = global.document._handlers[t];"
        " if (a) { const i = a.indexOf(f); if (i >= 0) a.splice(i, 1); } },",
    )
    + r"""
global.__allEls = global.__allEls || [];
global._winHandlers = {};
global.addEventListener = (t,f)=>{ (global._winHandlers[t]=global._winHandlers[t]||[]).push(f); };
global.removeEventListener = (t,f)=>{ const a=global._winHandlers[t]; if(a){ const i=a.indexOf(f); if(i>=0)a.splice(i,1);} };
(function () {
  // Los elementos del prelude base no guardan handlers: se re-abre el
  // registro envolviendo el createElement original.
  const origCreate = global.document.createElement;
  global.document.createElement = function (t) {
    const el = origCreate(t);
    const oAdd = el.addEventListener, oRem = el.removeEventListener;
    el._handlers = {};
    el.addEventListener = function (type, fn) { (el._handlers[type] = el._handlers[type] || []).push(fn); };
    el.removeEventListener = function (type, fn) { const a = el._handlers[type]; if (a) { const i = a.indexOf(fn); if (i >= 0) a.splice(i, 1); } };
    return el;
  };
})();
function fireAll(type, ev) {
  let n = 0;
  const fire = (hs) => { for (const h of (hs || []).slice()) { n++; try { h(ev); } catch (e) {} } };
  for (const el of global.__allEls) fire(el._handlers && el._handlers[type]);
  fire(global.document._handlers[type]);
  fire(global._winHandlers[type]);
  return n;
}
function wheelEv(dx, dy, x, y) {
  return {
    type: "wheel", deltaX: dx, deltaY: dy, deltaMode: 0, DOM_DELTA_PAGE: 3, DOM_DELTA_LINE: 1,
    clientX: x, clientY: y, cancelable: true, preventDefault() { this._pd = true; },
  };
}
"""
)


def run_lwc_body_ev(body, timeout=120):
    """Como run_lwc_body pero con prelude de eventos (fireAll/wheelEv)."""
    if shutil.which("node") is None:
        pytest.skip("node no disponible")
    tmpdir = Path("/tmp/opencode")
    tmpdir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=str(tmpdir), prefix="lwc-fase2-", suffix=".js", delete=False, mode="w", encoding="utf-8"
    ) as fh:
        fh.write(PRELUDE_EV + body)
        path = fh.name
    import os

    env = dict(os.environ)
    env["LWC_VENDOR"] = str(VENDOR_PATH)
    return subprocess.run(["node", path], capture_output=True, text=True, timeout=timeout, env=env)

