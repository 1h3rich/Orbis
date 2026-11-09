"""M3: línea de entrada + PnL flotante en el gráfico + menor heredado de M4.

Verificaciones previas (hipótesis del diseño, comprobadas en el vendor
v5.0.9 REAL con el harness de tests/lwc_harness_helper.py):

* V1: ``priceSeries.createPriceLine`` + ``removePriceLine`` existen y
  funcionan (crear → ``priceLines()`` 1, retirar → 0, sin frames fugados).
* V2: la etiqueta de la price line acepta texto dinámico: ``applyOptions``
  con ``{ price, title }`` actualiza etiqueta y nivel SIN recrear
  (``priceLines()`` sigue en 1 y ``options()`` refleja el cambio). Por eso
  el % de PnL va en la etiqueta de la price line viva (no hace falta el
  overlay #st-marker-tip).
* V3: ``GET /paper/portfolio`` devuelve por posición ``average_cost``,
  ``unrealized_pnl``, ``invested`` y ``return_percentage`` (verificado en
  ``build_paper_portfolio`` y contra respuesta real). NO existe
  ``unrealized_pnl_pct``: el % sale de ``return_percentage`` y, si falta,
  se calcula en presentación desde ``unrealized_pnl``/``invested`` del
  propio payload (nunca inventado); la etiqueta viva usa
  ``(lastPrice - avg) / avg * 100`` con el precio del gráfico.

Contrato verificado aquí:

* ``entryLinePlan`` (pura, Node): decide qué pintar dado el payload de
  portfolio + mercado + casilla + modo (con posición / sin posición /
  otro mercado / PAPER off / LIVE).
* ``livePnlPct`` / ``pnlLabel`` / ``pnlColor`` (puras, Node): % vivo, % con
  signo y color verde-rojo; la entrada NO se mueve ante ticks (el avg del
  plan es el mismo con precios distintos).
* Instancia única por serie con ``removePriceLine`` antes de recrear
  (mismo patrón anti-acumulación de A2/marcadores); destrucción en los
  mismos puntos donde se libera el chart; sin pelearse con la escala (sin
  ``scaleMargins``, sin tocar guardias de escala); upsert Fase 5 intacto
  (sin ``addSeries`` nuevos).
* Menor heredado de M4 en ``bindTrade`` (app.js): chequeo explícito de
  modo PAPER al enviar + guarda anti-carrera de mercado a la respuesta,
  como ``submitTradeForm`` en Estudio.
"""

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from lwc_harness_helper import run_lwc_body

STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "web" / "static"
ESTUDIO = (STATIC_DIR / "estudio.js").read_text(encoding="utf-8")
APP_JS = (STATIC_DIR / "app.js").read_text(encoding="utf-8")


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


# ---------------------------------------------------------- harness (V1/V2)

ENTRY_BODY = r"""
const chart = mkchart();
const s = chart.addSeries(LW.CandlestickSeries, {});
s.setData(candles100());
pump();
if (typeof s.createPriceLine !== "function") throw new Error("sin createPriceLine");
if (typeof s.removePriceLine !== "function") throw new Error("sin removePriceLine");
// Patron anti-acumulacion M3: 10 recreaciones con remove antes -> 1 viva.
let cur = null;
for (let i = 0; i < 10; i++) {
  if (cur) s.removePriceLine(cur);
  cur = s.createPriceLine({ price: 150 + i, color: "rgb(227, 179, 65)", lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: "Entrada " + (150 + i) });
  pump();
}
if (s.priceLines().length !== 1) throw new Error("deberia haber 1 viva, hay " + s.priceLines().length);
if (global.__rafQ.length !== 0) throw new Error("raf pendiente tras recrear");
// Etiqueta dinamica sin recrear: price + title via applyOptions.
cur.applyOptions({ price: 205, title: "+2.50%" });
pump();
if (s.priceLines().length !== 1) throw new Error("applyOptions recreo: " + s.priceLines().length);
const opts = cur.options();
if (opts.title !== "+2.50%") throw new Error("title no actualizo: " + JSON.stringify(opts.title));
if (opts.price !== 205) throw new Error("price no actualizo: " + JSON.stringify(opts.price));
// PnL roja en perdidas, tambien dinamica.
cur.applyOptions({ price: 140, color: "rgb(247, 82, 95)", title: "-6.67%" });
pump();
const opts2 = cur.options();
if (opts2.title !== "-6.67%" || opts2.price !== 140) throw new Error("segunda etiqueta no actualizo");
s.removePriceLine(cur);
pump();
if (s.priceLines().length !== 0) throw new Error("remove no vacio");
if (global.__rafQ.length !== 0) throw new Error("raf pendiente tras remove");
chart.remove();
pump();
if (global.__rafQ.length !== 0) throw new Error("raf pendiente tras remove chart");
console.log("M3-PRICELINE-OK");
"""


def test_priceline_no_acumula_y_etiqueta_dinamica():
    """Motor real v5.0.9: create/removePriceLine funcionan, recrear con
    remove antes no acumula (1 viva tras 10 ciclos) y la etiqueta % cambia
    con applyOptions sin recrear (priceLines sigue en 1)."""
    proc = run_lwc_body(ENTRY_BODY)
    assert "M3-PRICELINE-OK" in proc.stdout, proc.stderr or proc.stdout


# ------------------------------------------------------------------- Node

POSITIONS = [
    {"asset": "BTC", "quote_currency": "EUR", "quantity": 0.005555555556,
     "average_cost": 89999.9999928, "invested": 500.0,
     "current_price": 74040.6, "value": 411.3366666995736,
     "unrealized_pnl": -88.6633333004264, "realized_pnl": 0,
     "return_percentage": -17.73266666008528, "fees": 0.0,
     "weight_percentage": 4.150163399065998},
    {"asset": "ETH", "quote_currency": "EUR", "quantity": 0.05,
     "average_cost": 2000.0, "invested": 100.0,
     "current_price": 2100.0, "value": 105.0,
     "unrealized_pnl": 5.0, "realized_pnl": 0,
     "return_percentage": 5.0, "fees": 0.0, "weight_percentage": 1.0},
]


def _node_eval(body):
    if shutil.which("node") is None:
        pytest.skip("node no disponible")
    tmpdir = Path("/tmp/opencode")
    tmpdir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=str(tmpdir), prefix="m3-",
                                     suffix=".js", delete=False, mode="w",
                                     encoding="utf-8") as fh:
        fh.write(body)
        path = fh.name
    proc = subprocess.run(["node", path], capture_output=True, text=True,
                          timeout=30)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    m = re.search(r"M3-JSON:(.*)", proc.stdout, re.S)
    assert m, proc.stdout
    return json.loads(m.group(1))


def _m3_lib():
    return "\n".join([
        _extract_fn(ESTUDIO, "entryLinePlan"),
        _extract_fn(ESTUDIO, "livePnlPct"),
        _extract_fn(ESTUDIO, "pnlLabel"),
        _extract_fn(ESTUDIO, "pnlColor"),
    ])


def test_decision_pura_con_posicion_sin_y_otro_mercado():
    """entryLinePlan REAL: con posición devuelve plan (avg + % del
    servidor); sin posición, con otro mercado o lista vacía devuelve
    null (sin línea de entrada)."""
    body = (_m3_lib() + "\nconst POS = " + json.dumps(POSITIONS) + ";\n" + r"""
console.log("M3-JSON:" + JSON.stringify({
  btc: entryLinePlan(POS, "BTC/EUR", true, true),
  eth: entryLinePlan(POS, "ETH/EUR", true, true),
  other: entryLinePlan(POS, "SOL/EUR", true, true),
  empty: entryLinePlan([], "BTC/EUR", true, true),
  nullp: entryLinePlan(null, "BTC/EUR", true, true)
}));
""")
    out = _node_eval(body)
    btc = out["btc"]
    assert btc["market"] == "BTC/EUR"
    assert btc["averageCost"] == pytest.approx(89999.9999928)
    assert btc["pnlPct"] == pytest.approx(-17.73266666008528)
    assert btc["pnlAbs"] == pytest.approx(-88.6633333004264)
    assert out["eth"]["pnlPct"] == pytest.approx(5.0)
    assert out["other"] is None
    assert out["empty"] is None
    assert out["nullp"] is None


def test_decision_pura_gates_paper_off_y_live():
    """entryLinePlan REAL: casilla PAPER apagada o modo LIVE → null (ni se
    pinta ni —vía refreshEntryPlan— se pide el portfolio)."""
    body = (_m3_lib() + "\nconst POS = " + json.dumps(POSITIONS) + ";\n" + r"""
console.log("M3-JSON:" + JSON.stringify({
  off: entryLinePlan(POS, "BTC/EUR", false, true),
  live: entryLinePlan(POS, "BTC/EUR", true, false),
  bothoff: entryLinePlan(POS, "BTC/EUR", false, false)
}));
""")
    out = _node_eval(body)
    assert out["off"] is None
    assert out["live"] is None
    assert out["bothoff"] is None


def test_decision_pura_pct_fallback_sin_return_percentage():
    """Sin `return_percentage` (campo inexistente en algunas versiones) el
    % se calcula en presentación desde unrealized_pnl/invested del propio
    payload; con coste no numérico no hay plan (nunca inventado)."""
    body = (_m3_lib() + r"""
const noPct = [{ asset: "BTC", quote_currency: "EUR", average_cost: "90000",
  invested: "500", unrealized_pnl: "-88.66" }];
const badAvg = [{ asset: "BTC", quote_currency: "EUR", average_cost: "xx",
  invested: "500", unrealized_pnl: "-1", return_percentage: 2 }];
console.log("M3-JSON:" + JSON.stringify({
  fallback: entryLinePlan(noPct, "BTC/EUR", true, true),
  bad: entryLinePlan(badAvg, "BTC/EUR", true, true)
}));
""")
    out = _node_eval(body)
    assert out["fallback"]["pnlPct"] == pytest.approx(-88.66 / 500 * 100)
    assert out["fallback"]["averageCost"] == pytest.approx(90000.0)
    assert out["bad"] is None


def test_pnl_verde_rojo_y_etiqueta_con_signo():
    """pnlColor REAL: verde en ganancia/cero, rojo en pérdida; pnlLabel
    con signo siempre (+/-) y dos decimales, en es-ES (Tanda C6: coma;
    equivalencia documentada —mismos casos y signos que antes, solo cambia
    el separador decimal porque el canvas no localiza)."""
    body = (_m3_lib() + r"""
console.log("M3-JSON:" + JSON.stringify({
  cPos: pnlColor(2.5), cZero: pnlColor(0), cNeg: pnlColor(-0.01),
  lPos: pnlLabel(2.5), lNeg: pnlLabel(-17.73266666), lZero: pnlLabel(0)
}));
""")
    out = _node_eval(body)
    assert out["cPos"] == "#22AB94" and out["cZero"] == "#22AB94"
    assert out["cNeg"] == "#F7525F"
    assert out["lPos"] == "+2,50%"
    assert out["lNeg"] == "-17,73%"
    assert out["lZero"] == "+0,00%"


def test_entrada_inmovil_y_pct_vivo_ante_ticks():
    """La entrada NO se mueve ante ticks (mismo avg con precios
    distintos) mientras el % vivo sí cambia con el precio; sin precio no
    hay % (null honesto, se retira la línea viva)."""
    body = (_m3_lib() + "\nconst POS = " + json.dumps(POSITIONS) + ";\n" + r"""
const plan = entryLinePlan(POS, "BTC/EUR", true, true);
console.log("M3-JSON:" + JSON.stringify({
  avg: plan.averageCost,
  pct1: livePnlPct(plan.averageCost, 74040.6),
  pct2: livePnlPct(plan.averageCost, 95000.0),
  noPrice: livePnlPct(plan.averageCost, null),
  noPrice2: livePnlPct(plan.averageCost, NaN)
}));
""")
    out = _node_eval(body)
    assert out["avg"] == pytest.approx(89999.9999928)
    assert out["pct1"] == pytest.approx((74040.6 - 89999.9999928) / 89999.9999928 * 100)
    assert out["pct2"] == pytest.approx((95000.0 - 89999.9999928) / 89999.9999928 * 100)
    assert out["pct1"] != pytest.approx(out["pct2"])
    assert out["noPrice"] is None and out["noPrice2"] is None


# ---------------------------------------------------------------- estático

def test_instancia_unica_remove_antes_de_recrear():
    """estudio.js mantiene UNA única instancia por línea (entryLine /
    pnlLine): solo paintEntryLines crea price lines (2 llamadas reales),
    clearEntryLines retira con removePriceLine antes de anular, y los
    gates (casilla/LIVE/otro mercado) retiran sin pedir."""
    code_lines = [ln for ln in ESTUDIO.splitlines() if ".createPriceLine({" in ln]
    assert len(code_lines) == 2, code_lines
    paint = _extract_fn(ESTUDIO, "paintEntryLines")
    assert "priceSeries.createPriceLine({" in paint
    clear = _extract_fn(ESTUDIO, "clearEntryLines")
    assert "priceSeries.removePriceLine(entryLine)" in clear
    assert "priceSeries.removePriceLine(pnlLine)" in clear
    assert "entryLine = null" in clear and "pnlLine = null" in clear
    assert "clearEntryLines();" in paint, "el gate retira antes de recrear"
    assert "if (!entryLine)" in paint and "if (!pnlLine)" in paint
    assert "U.orbisMode() !== " in paint and "!showPAPER" in paint
    assert "entryPlan.market !== market" in paint
    # refreshEntryPlan: ni se pide con la casilla apagada ni en LIVE, con
    # guarda anti-carrera como paintMarkers.
    refresh = _extract_fn(ESTUDIO, "refreshEntryPlan")
    assert "if (!showPAPER || U.orbisMode() !== " in refresh
    assert refresh.index("if (!showPAPER") < refresh.index("/paper/portfolio")
    assert "mk !== market" in refresh
    assert "entryLinePlan(" in refresh


def test_entrada_no_se_mueve_y_viva_si():
    """La línea de entrada se crea una vez y nunca se le toca el precio
    (sin applyOptions sobre entryLine); solo la viva se mueve con
    applyOptions({ price, color, title }) en cada tick."""
    paint = _extract_fn(ESTUDIO, "paintEntryLines")
    assert "entryLine.applyOptions" not in paint
    assert "pnlLine.applyOptions({ price:" in paint
    assert "title: " in paint
    assert 'lineStyle: 2' in paint, "entrada discontinua"
    assert "Entrada " in paint, "etiqueta con el coste medio"
    # num(null) es 0: sin precio publicable se retira la viva (nunca -100 %).
    assert "lastPrice === null" in paint
    for fn in ("paintEntryLines", "clearEntryLines", "refreshEntryPlan"):
        body = _extract_fn(ESTUDIO, fn)
        assert "scaleMargins" not in body, fn
        assert "guardPriceOnScroll" not in body, fn
        assert "priceUserFixed" not in body, fn
        assert "addSeries" not in body, fn + " (upsert Fase 5 intacto)"


def test_enganches_tick_refresco_mercado_y_resets():
    """paintEntryLines (barato, sin fetch) corre en cada tick WS, en
    refreshOpenCandle y tras el ticker; refreshEntryPlan (fetch) en
    paintAll, tras operar con éxito y al cambiar la casilla; todo se
    resetea donde se libera el chart."""
    assert ESTUDIO.count("paintEntryLines();") >= 5, ESTUDIO.count("paintEntryLines();")
    assert ESTUDIO.count("refreshEntryPlan();") >= 3, ESTUDIO.count("refreshEntryPlan();")
    assert ESTUDIO.count("entryPlan = null; entryLine = null; pnlLine = null;") == 3
    submit = _extract_fn(ESTUDIO, "submitTradeForm")
    assert "refreshEntryPlan()" in submit[submit.index("res.data.executed"):]
    assert "var entryPlan = null, entryLine = null, pnlLine = null;" in ESTUDIO


def test_menor_heredado_bindtrade_paper_y_anticarrera():
    """bindTrade (app.js) chequea modo PAPER al enviar y guarda el mercado
    para no pintar un resultado ajeno al visible (como submitTradeForm)."""
    bind = _extract_fn(APP_JS, "bindTrade")
    assert 'orbisMode() !== "PAPER"' in bind
    assert "mkAtSend" in bind
    assert "mkAtSend !== " in bind
    assert "Vuelve a PAPER para operar" in bind
    assert "Has cambiado de mercado" in bind
    send_gate = bind.index('orbisMode() !== "PAPER"')
    assert bind.index("mkAtSend = ") < send_gate < bind.index("btn.disabled = true")
    resp_guard = bind[bind.index("mkAtSend !== "):]
    assert "btn.disabled = false" in resp_guard
    assert "tradeResultBox" not in resp_guard.split("return;")[0]
