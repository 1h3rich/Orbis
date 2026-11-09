"""M5: disponibilidad de TF derivada del backend, sin tabla hardcodeada.

La tabla estática ``TF_PROVIDER_UNSUPPORTED`` del cliente duplicaba una
verdad que ya vive en el backend (adapters + ``service.candles`` con
``available:false`` + ``reason`` honesto). M5 la elimina: cada
``available:false`` observado en la sesión cachea el par proveedor/TF con
su ``reason`` EXACTO y el mismo ``updateTfAvailability`` deshabilita esa
opción con ese texto; lo no probado aparece habilitado hasta que falla (y
el fallo ya es honesto); un éxito posterior rehabilita el par.

Cobertura (JS real extraído de app/web/static/estudio.js y ejecutado en
Node, mismo patrón brace-matching de las fases anteriores):

* Estático: ``TF_PROVIDER_UNSUPPORTED`` no reaparece (falla si vuelve) y
  no se tocó lo prohibido (``TF_LIST``, ``TF_SECONDS``, ``tfSeconds``,
  ``autoRefreshFor`` siguen presentes).
* Cableado: el ``updateTfAvailability`` real usa la caché observada (no la
  tabla) y ``loadCandles`` alimenta/limpia la caché en las ramas
  ``available:false`` / éxito.
* Comportamiento en Node con el ``updateTfAvailability`` REAL y DOM
  mínimo: reason observado → opción deshabilitada con el texto exacto;
  TF no probado → habilitado; tras éxito → habilitado.
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


def _node_run_m5(body):
    if shutil.which("node") is None:
        import pytest
        pytest.skip("node no disponible")
    var_obs = re.search(r"var TF_OBSERVED_UNAVAILABLE\s*=\s*\{.*?\};", ESTUDIO, re.S)
    assert var_obs is not None, "TF_OBSERVED_UNAVAILABLE no encontrado"
    var_list = re.search(r"var TF_LIST\s*=\s*\[.*?\];", ESTUDIO, re.S)
    assert var_list is not None, "TF_LIST no encontrado"
    lib = "\n".join([
        var_obs.group(0),
        var_list.group(0),
        _extract_fn(ESTUDIO, "tfUnavailableReason"),
        _extract_fn(ESTUDIO, "markTfUnavailable"),
        _extract_fn(ESTUDIO, "markTfAvailable"),
        _extract_fn(ESTUDIO, "tfReason"),
        _extract_fn(ESTUDIO, "updateTfAvailability"),
    ])
    harness = """
function __makeOpt(v) { return { value: v, disabled: false, title: "" }; }
var tfSel = { value: "1s", options: TF_LIST.map(__makeOpt) };
var provSel = { value: "kraken" };
var noteEl = { textContent: "" };
function $(id) {
  if (id === "st-prov") return provSel;
  if (id === "st-tf") return tfSel;
  if (id === "st-tf-note") return noteEl;
  throw new Error("id inesperado: " + id);
}
var timeframe = "1s";
var declaredTimeframes = null;
function __opt(v) {
  for (var i = 0; i < tfSel.options.length; i++) {
    if (tfSel.options[i].value === v) return tfSel.options[i];
  }
  throw new Error("opción ausente: " + v);
}
Object.defineProperty(tfSel, "selectedOptions", { get: function () { return [__opt(tfSel.value)]; } });
"""
    full = lib + "\n" + harness + "\n" + body
    tmpdir = Path("/tmp/opencode")
    tmpdir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=str(tmpdir), prefix="m5-",
                                     suffix=".js", delete=False, mode="w",
                                     encoding="utf-8") as fh:
        fh.write(full)
        path = fh.name
    proc = subprocess.run(["node", path], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("M5-JSON:")]
    assert line, proc.stdout
    return json.loads(line[0][len("M5-JSON:"):])


# -- estático ------------------------------------------------------------


def test_no_static_tf_provider_table():
    """Anti-reaparición (M5): ``TF_PROVIDER_UNSUPPORTED`` no existe en
    estudio.js. Si vuelve, la disponibilidad dejaría de derivarse del
    backend y este test lo caza."""
    assert "TF_PROVIDER_UNSUPPORTED" not in ESTUDIO


def test_untouchables_still_present():
    """M5 solo elimina la tabla por proveedor: ``TF_LIST``,
    ``TF_SECONDS``, ``tfSeconds``, ``autoRefreshFor`` y el propio
    ``updateTfAvailability`` siguen (los leen test_frescura_cache.py y
    test_estudio_tf_mensual.py por regex; romperlos es regresión)."""
    assert re.search(r"var TF_LIST\s*=\s*\[", ESTUDIO) is not None
    assert re.search(r"var TF_SECONDS\s*=\s*\{", ESTUDIO) is not None
    assert re.search(r"function tfSeconds\s*\(", ESTUDIO) is not None
    assert re.search(r"function autoRefreshFor\s*\(", ESTUDIO) is not None
    assert re.search(r"function updateTfAvailability\s*\(", ESTUDIO) is not None
    assert re.search(r"var TF_OBSERVED_UNAVAILABLE\s*=", ESTUDIO) is not None
    assert re.search(r"function tfUnavailableReason\s*\(", ESTUDIO) is not None
    assert re.search(r"function markTfUnavailable\s*\(", ESTUDIO) is not None
    assert re.search(r"function markTfAvailable\s*\(", ESTUDIO) is not None


def test_update_tf_availability_reuses_observed_cache():
    """El ``updateTfAvailability`` REAL lee la caché observada (no una
    tabla): nombra ``tfUnavailableReason`` y no la tabla eliminada. Y
    ``loadCandles`` alimenta la caché con el reason exacto en la rama
    ``available:false`` y la limpia tras el éxito (mismo mecanismo, sin
    crear otro)."""
    body = _extract_fn(ESTUDIO, "updateTfAvailability")
    assert "tfUnavailableReason" in body
    assert "tfReason" in body
    assert "TF_PROVIDER_UNSUPPORTED" not in body
    assert "opt.disabled" in body
    m = re.search(r"function loadCandles\(backfill\)\s*\{(.*?)\n    \}", ESTUDIO, re.S)
    assert m is not None
    load = m.group(1)
    i_un = load.index("res.data && res.data.available === false")
    assert "markTfUnavailable(" in load[i_un:], "la rama available:false debe cachear"
    assert "res.data.reason" in load[i_un:]
    i_ok = load.index("markTfAvailable(")
    assert i_ok > i_un, "el éxito posterior rehabilita el par"


# -- comportamiento en Node (updateTfAvailability REAL + DOM mínimo) -----


def test_observed_reason_disables_option_with_exact_text():
    """Reason observado (kraken/1s) → opción 1s deshabilitada con el texto
    EXACTO en el title y la nota lo contiene; el resto sigue habilitado."""
    out = _node_run_m5("""
var REASON = "No disponible para este proveedor: timeframe 1s no nativo en kraken (nativos: 1m, 5m). No se inventan datos.";
updateTfAvailability();
if (__opt("1s").disabled !== false) throw new Error("sin probar debe estar habilitado");
markTfUnavailable("kraken", "1s", REASON);
updateTfAvailability();
var o = __opt("1s");
if (o.disabled !== true) throw new Error("lo observado debe deshabilitarse");
if (o.title !== REASON) throw new Error("el texto debe ser el reason exacto: " + o.title);
if (__opt("30s").disabled !== false) throw new Error("otro TF sigue habilitado");
if (__opt("1m").disabled !== false) throw new Error("1m sigue habilitado");
if (noteEl.textContent.indexOf(REASON) < 0) throw new Error("la nota debe contener el motivo: " + noteEl.textContent);
console.log("M5-JSON:" + JSON.stringify({ ok: true }));
""")
    assert out["ok"] is True


def test_unobserved_provider_tf_stays_enabled():
    """Lo no probado aparece habilitado: con otro proveedor (binance) nada
    se deshabilita aunque kraken/1s esté observado; y en kraken los TF no
    observados siguen habilitados."""
    out = _node_run_m5("""
markTfUnavailable("kraken", "1s", "timeframe 1s no nativo en kraken. No se inventan datos.");
provSel.value = "binance";
updateTfAvailability();
for (var i = 0; i < tfSel.options.length; i++) {
  if (tfSel.options[i].disabled !== false) throw new Error("binance sin probar: todo habilitado (" + tfSel.options[i].value + ")");
}
if (noteEl.textContent !== "") throw new Error("sin observados la nota queda vacía");
provSel.value = "kraken";
tfSel.value = "1m";
timeframe = "1m";
updateTfAvailability();
if (__opt("1s").disabled !== true) throw new Error("kraken/1s observado sigue deshabilitado");
if (__opt("1M").disabled !== false) throw new Error("kraken/1M no probado sigue habilitado");
console.log("M5-JSON:" + JSON.stringify({ ok: true }));
""")
    assert out["ok"] is True


def test_success_reenables_pair():
    """Tras éxito, habilitado: limpiar lo observado (markTfAvailable, lo que
    hace loadCandles al recibir available:true) + updateTfAvailability deja
    la opción habilitada y sin texto."""
    out = _node_run_m5("""
var REASON = "timeframe 1M no nativo en kraken. No se inventan datos.";
markTfUnavailable("kraken", "1M", REASON);
updateTfAvailability();
if (__opt("1M").disabled !== true) throw new Error("precondición: observado deshabilita");
markTfAvailable("kraken", "1M");
updateTfAvailability();
var o = __opt("1M");
if (o.disabled !== false) throw new Error("tras el éxito vuelve a habilitarse");
if (o.title !== "") throw new Error("sin texto tras rehabilitar");
if (noteEl.textContent !== "") throw new Error("sin observados la nota queda vacía");
console.log("M5-JSON:" + JSON.stringify({ ok: true }));
""")
    assert out["ok"] is True


def test_auto_provider_never_pre_disables():
    """Con provider "auto" nada se deshabilita por adelantado: la caché se
    indexa por resolved_provider real (kraken/1s observado) y
    ``updateTfAvailability`` consulta la clave "auto", siempre vacía. El
    control final demuestra que el test muerde: si la caché se indexara por
    "auto", la misma aserción deshabilitaría 1s y la primera fase fallaría."""
    out = _node_run_m5("""
markTfUnavailable("kraken", "1s", "timeframe 1s no nativo en kraken. No se inventan datos.");
provSel.value = "auto";
updateTfAvailability();
for (var i = 0; i < tfSel.options.length; i++) {
  if (tfSel.options[i].disabled !== false) throw new Error("auto no deshabilita por adelantado (" + tfSel.options[i].value + ")");
}
if (__opt("1s").title !== "") throw new Error("auto sin texto previo");
if (noteEl.textContent !== "") throw new Error("auto sin observados: nota vacía");
// Control de mordida (solo en el test, sin tocar producto): si la caché se
// indexara por "auto", esta marca SÍ deshabilita, luego la fase anterior es
// sensible a esa regresión.
markTfUnavailable("auto", "1s", "timeframe 1s no nativo. No se inventan datos.");
updateTfAvailability();
if (__opt("1s").disabled !== true) throw new Error("control: indexar por auto debe deshabilitar");
console.log("M5-JSON:" + JSON.stringify({ ok: true }));
""")
    assert out["ok"] is True


def test_declared_capability_disables_before_observation_and_selects_valid_tf():
    out = _node_run_m5("""
declaredTimeframes = { coinbase: ["1m", "5m", "15m", "1h", "1d"], bitstamp: ["1d"] };
provSel.value = "coinbase";
tfSel.value = "4h";
updateTfAvailability();
if (!__opt("4h").disabled || __opt("15m").disabled) throw new Error("capacidad Coinbase");
if (tfSel.value !== "1m" || timeframe !== "1m") throw new Error("fallback Coinbase");
provSel.value = "bitstamp";
updateTfAvailability();
if (tfSel.value !== "1d" || timeframe !== "1d") throw new Error("fallback Bitstamp");
if (!__opt("1m").disabled || __opt("1d").disabled) throw new Error("capacidad Bitstamp");
console.log("M5-JSON:" + JSON.stringify({ ok: true }));
""")
    assert out["ok"] is True
