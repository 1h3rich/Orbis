"""M4: operar PAPER desde el gráfico + marcadores en Operativa (reutiliza M1).

Contrato verificado aquí:

* ``postPaperTrade`` conserva el contrato manual heredado. Las vistas usan
  ``postMarketPaperTrade``: no envían precio, generan un ID por intención y
  lo conservan en un reintento tras fallo de red. El servidor obtiene la
  cotización pública. Las frases Guard siguen compartidas.
* ``paintLwcMini`` repinta los marcadores PAPER del par visible tras
  ``setData`` en cada creación (el chart se recrea cada tick): instancia
  única por serie + ``setMarkers`` que reemplaza, con la MISMA forma que M1
  (círculo/square, size 2, texto con cantidad, sin ``title``).
* Formulario mínimo del Estudio (ids ``st-*`` como contrato): par visible,
  cantidad + cotización actual no editable. El cursor histórico no puede
  fijar el precio de una operación PAPER; tras 201/200 refresca ``paintMarkers()`` +
  ``loadSnapshot()`` sin recargar; los bloqueos Guard se muestran honestos
  sin marca nueva; sin precio publicado no se opera; respeta el mercado
  visible y el modo PAPER.

No toca ningún test existente (la idempotencia por ``request_id`` sigue en
test_web_ui.py). Nota deliberada: NO se usa el literal ``randomUUID`` (el
helper nativo falla en http no seguro; ver
test_js_uses_safe_uuid_and_same_origin_base): el "id nuevo por clic" es
``newRequestId()`` por llamada al helper.
"""

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "web" / "static"
APP_JS = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
ESTUDIO = (STATIC_DIR / "estudio.js").read_text(encoding="utf-8")


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


def _strip_comments(code):
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    return re.sub(r"(^|\s)//[^\n]*", r"\1", code)


# ---------------------------------------------------------------- estático

def test_helper_compartido_existe_y_se_usa_en_ambos_sitios():
    """postMarketPaperTrade vive en app.js, se expone en OrbisUI y lo usan
    bindTrade (Operativa) y submitTradeForm (Estudio); las frases Guard
    también se comparten en vez de duplicarse."""
    assert "function postPaperTrade(" in APP_JS
    assert "postPaperTrade: postPaperTrade" in APP_JS
    assert "function postMarketPaperTrade(" in APP_JS
    assert "postMarketPaperTrade: postMarketPaperTrade" in APP_JS
    assert "guardPhrase: guardPhrase" in APP_JS
    assert "tradeResultBox: tradeResultBox" in APP_JS
    bind = _extract_fn(APP_JS, "bindTrade")
    assert "postMarketPaperTrade(" in bind, "bindTrade debe delegar en el helper"
    submit = _extract_fn(ESTUDIO, "submitTradeForm")
    assert "U.postMarketPaperTrade" in submit
    assert "U.guardPhrase" in submit


def test_quote_failure_message_does_not_claim_a_trade_cannot_be_in_flight():
    box = _extract_fn(APP_JS, "httpErrorBox")
    assert "Reintenta con los mismos datos para confirmar esta orden." in box
    assert "No se ha registrado ninguna operación." not in box


def test_rounding_guard_has_an_actionable_short_message():
    phrase = _extract_fn(APP_JS, "guardPhrase")
    assert 'case "Order rounding loss exceeds limit":' in phrase
    assert "Se perdería demasiado por redondeo. Aumenta el importe." in phrase


def test_request_id_nuevo_por_clic_nunca_reutilizado():
    """El intento conserva un ID entre reintentos; no aparece randomUUID."""
    assert "pendingRequest = { key: requestKey, id: newRequestId() }" in APP_JS
    assert "pendingTradeRequest = { key: intentKey, id: U.newRequestId() }" in ESTUDIO
    helper = _extract_fn(APP_JS, "postPaperTrade")
    assert "newRequestId()" in helper
    market_helper = _extract_fn(APP_JS, "postMarketPaperTrade")
    assert "f.request_id || newRequestId()" in market_helper
    assert 'body.price' not in market_helper
    assert "randomUUID" not in APP_JS
    assert "randomUUID" not in ESTUDIO
    assert "strategy_id" not in helper, "el esquema prohíbe strategy_id"


def test_paintLwcMini_repinta_markers_tras_setData():
    """El mini repinta marcas PAPER del par visible en cada creación
    (tras setData, no solo al cambiar de par) con instancia única."""
    mini = _extract_fn(APP_JS, "paintLwcMini")
    assert "setData" in mini
    assert "refreshMiniMarks" in mini
    assert mini.index("setData") < mini.index("refreshMiniMarks")
    for src, tag in ((APP_JS, "app.js"), (ESTUDIO, "estudio.js")):
        uses = [ln for ln in src.splitlines()
                if "createSeriesMarkers(" in ln and "window.LightweightCharts" in ln]
        assert len(uses) == 1, (tag, uses)
    refresh = _extract_fn(APP_JS, "refreshMiniMarks")
    assert "setMarkers(" in refresh
    assert "miniMarketCtx" in mini


def test_mini_misma_forma_que_m1_sin_title():
    """miniPaperMarks copia la forma M1: círculo/square, size 2, texto con
    cantidad, colores de compra/venta, filtrado por mercado, sin title."""
    fn = _extract_fn(APP_JS, "miniPaperMarks")
    for frag in ('"circle"', '"square"', "size: 2", '"P · "', '"belowBar"',
                 '"aboveBar"', '"#3fb950"', '"#f85149"'):
        assert frag in fn, frag
    assert "title" not in fn, "LWC ignora title: no debe portar info"


def test_estudio_formulario_contrato_y_sin_recarga():
    """Panel Compra y venta: ids st-*, cotización actual, refresco sin
    recargar tras éxito, Guard honesto sin marca nueva, gates PAPER/sin
    precio, anti doble-clic y guarda de cambio de mercado."""
    for ident in ("st-trade-pair", "st-buy-open", "st-sell-open",
                  "st-trade-form", "st-trade-side", "st-trade-qty",
                  "st-trade-price", "st-trade-send", "st-trade-cancel",
                  "st-trade-msg"):
        assert 'id="%s"' % ident in ESTUDIO, ident
    assert "lastCrossPrice = num(row.close)" in ESTUDIO
    assert "function tradeSnapshotPrice(" in ESTUDIO
    assert 'id="st-trade-price" inputmode="decimal" readonly' in ESTUDIO
    assert "tradePrefillPrice" not in ESTUDIO
    assert "openTradeForm" in ESTUDIO and "closeTradeForm" in ESTUDIO
    submit = _extract_fn(ESTUDIO, "submitTradeForm")
    assert "U.postMarketPaperTrade" in submit
    assert "paintMarkers()" in submit and "loadSnapshot()" in submit
    assert "location.reload" not in submit
    assert "btn.disabled" in submit
    assert "mkAtSend !== market" in submit
    assert "orbisMode" in submit
    assert "Sin cotización actual" in submit
    assert "fields.request_id = pendingTradeRequest.id" in submit
    assert "fields.price" not in submit
    assert "lastAvailQty" in ESTUDIO, "el Guard de venta muestra lo disponible"
    # El éxito refresca; el bloqueo Guard NO llama a paintMarkers.
    ok_branch = submit[submit.index("res.data.executed"):]
    assert "paintMarkers()" in ok_branch
    guard_branch = submit[submit.index("U.guardPhrase"):]
    assert "paintMarkers()" not in guard_branch


# ------------------------------------------------------------------ Node

OPS = [
    {"operation_type": "BUY", "asset": "BTC", "quote_currency": "EUR",
     "amount_spent": "100", "asset_received": "0.01", "price": "10000",
     "trading_fee": "0", "withdrawal_fee": "0", "network_fee": "0",
     "timestamp": "2026-10-05 12:00:00"},
    {"operation_type": "SELL", "asset": "BTC", "quote_currency": "EUR",
     "amount_spent": "0.005", "asset_received": "50", "price": "10000",
     "trading_fee": "0", "withdrawal_fee": "0", "network_fee": "0",
     "timestamp": "2026-10-06 12:00:00"},
    {"operation_type": "BUY", "asset": "ETH", "quote_currency": "EUR",
     "amount_spent": "100", "asset_received": "0.05", "price": "2000",
     "trading_fee": "0", "withdrawal_fee": "0", "network_fee": "0",
     "timestamp": "2026-10-05 13:00:00"},
]

FMT_STUB = r"""
function fmtQty(x, suffix) {
  var n = Number(x);
  if (!isFinite(n)) return "—";
  var t = String(n);
  return suffix ? t + " " + suffix : t;
}
"""


def _node_eval(body):
    if shutil.which("node") is None:
        pytest.skip("node no disponible")
    tmpdir = Path("/tmp/opencode")
    tmpdir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=str(tmpdir), prefix="m4-",
                                     suffix=".js", delete=False, mode="w",
                                     encoding="utf-8") as fh:
        fh.write(body)
        path = fh.name
    proc = subprocess.run(["node", path], capture_output=True, text=True,
                          timeout=30)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    m = re.search(r"M4-JSON:(.*)", proc.stdout, re.S)
    assert m, proc.stdout
    return json.loads(m.group(1))


def test_miniPaperMarks_node_misma_forma_que_m1():
    """Motor extraído REAL de app.js: filtra por mercado, círculo/square +
    size 2 + texto con cantidad, vacío sin ops, nunca title."""
    lib = "\n".join([_extract_fn(APP_JS, "miniOpEpoch"),
                     FMT_STUB,
                     _extract_fn(APP_JS, "miniPaperMarks")])
    body = (lib + "\nconst OPS = " + json.dumps(OPS) + ";\n" + r"""
const btc = miniPaperMarks(OPS.filter(function (o) { return (o.asset + "/" + o.quote_currency) === "BTC/EUR"; }), "BTC/EUR");
const empty = miniPaperMarks([], "BTC/EUR");
console.log("M4-JSON:" + JSON.stringify({ btc: btc, empty: empty }));
""")
    out = _node_eval(body)
    assert out["empty"] == []
    assert len(out["btc"]) == 2, out["btc"]
    buy, sell = out["btc"]
    assert buy["shape"] == "circle" and buy["position"] == "belowBar"
    assert buy["size"] == 2 and buy["text"].startswith("P · ")
    assert sell["shape"] == "square" and sell["position"] == "aboveBar"
    assert sell["size"] == 2 and sell["text"] == "S"
    for mk in out["btc"]:
        assert "title" not in mk
        assert mk["color"] in ("#3fb950", "#f85149")


def test_miniPaperMarks_only_marks_the_candle_containing_the_operation():
    lib = "\n".join([_extract_fn(APP_JS, "miniOpEpoch"), FMT_STUB,
                     _extract_fn(APP_JS, "miniPaperMarks")])
    body = lib + r"""
const base = Date.parse('2026-10-05T00:00:00Z') / 1000;
const bars = [{t:base}, {t:base + 86400}, {t:base + 3 * 86400}];
function op(timestamp) { return {timestamp, operation_type:'BUY', asset:'BTC',
  quote_currency:'EUR', asset_received:'0.01'}; }
const marks = miniPaperMarks([
  op('2026-10-05 12:00:00'),
  op('2026-10-06T15:00:00Z'),
  op('2026-10-07 12:00:00'), // no bar for this day
  op('2026-10-09 12:00:00')  // beyond observed coverage
], 'BTC/EUR', bars);
const offset = miniOpEpoch(op('2026-10-05T14:00:00+02:00'));
console.log('M4-JSON:' + JSON.stringify({marks, offset, base}));
"""
    out = _node_eval(body)
    assert [mark["time"] for mark in out["marks"]] == [out["base"], out["base"] + 86400]
    assert out["offset"] == out["base"] + 12 * 3600


def test_trade_snapshot_price_rechaza_mercado_ajeno_y_dato_caducado():
    """El precio PAPER sólo viene de un snapshot vigente del mercado visible."""
    body = (_extract_fn(ESTUDIO, "tradeSnapshotPrice") + r"""
const now = Date.parse("2026-10-23T12:00:00Z");
const quote = {available:true, symbol:"BTC/EUR", price:"74000",
  received_at:"2026-10-23T11:59:59Z", market_time_status:"unknown"};
console.log("M4-JSON:" + JSON.stringify({
  fresh: tradeSnapshotPrice(quote, "BTC/EUR", now, 1000),
  other: tradeSnapshotPrice(quote, "ETH/EUR", now, 1000),
  oldServer: tradeSnapshotPrice(quote, "BTC/EUR", now + 61000, 1000),
  oldBrowser: tradeSnapshotPrice(quote, "BTC/EUR", now, 61000),
  stale: tradeSnapshotPrice({...quote, market_time_status:"stale"}, "BTC/EUR", now, 1000),
  future: tradeSnapshotPrice({...quote, market_time_status:"future"}, "BTC/EUR", now, 1000),
  missingTime: tradeSnapshotPrice({...quote, received_at:null}, "BTC/EUR", now, 1000)
}));
""")
    out = _node_eval(body)
    assert out["fresh"] == 74000
    for key in ("other", "oldServer", "oldBrowser", "stale", "future", "missingTime"):
        assert out[key] is None, key


def test_postPaperTrade_node_id_nuevo_por_envio_y_rutas():
    """Helper REAL de app.js con api/stubs: dos envíos = dos ids distintos,
    buy->/paper/buy con order_amount, sell->/paper/sell con quantity, sin
    strategy_id, exchange por provenance."""
    lib = _extract_fn(APP_JS, "postPaperTrade")
    body = (r"""
var __calls = [];
var __seq = 0;
function newRequestId() { __seq += 1; return "id-" + __seq; }
var __provider = "auto";
function orbisProvider() { return __provider; }
function api(url, opts) {
  __calls.push({ url: url, body: JSON.parse(opts.body) });
  return Promise.resolve({ ok: true, data: { executed: true } });
}
""" + lib + r"""
(async function () {
  await postPaperTrade("buy", { asset: "BTC", quote_currency: "EUR", price: "74000", order_amount: "100", estimated_fee: "0" });
  await postPaperTrade("buy", { asset: "BTC", quote_currency: "EUR", price: "74000", order_amount: "100", estimated_fee: "0" });
  await postPaperTrade("sell", { asset: "BTC", quote_currency: "EUR", price: "74000", quantity: "0.001", fee: "0" });
  __provider = "binance";
  await postPaperTrade("buy", { asset: "BTC", quote_currency: "EUR", price: "74000", order_amount: "50", estimated_fee: "0" });
  console.log("M4-JSON:" + JSON.stringify({ calls: __calls }));
})();
""")
    out = _node_eval(body)
    calls = out["calls"]
    assert len(calls) == 4, calls
    assert calls[0]["url"] == "/paper/buy"
    assert calls[2]["url"] == "/paper/sell"
    ids = [c["body"]["request_id"] for c in calls]
    assert len(set(ids)) == 4, "cada envío con id nuevo"
    assert calls[0]["body"]["order_amount"] == "100"
    assert calls[2]["body"]["quantity"] == "0.001"
    for c in calls:
        assert "strategy_id" not in c["body"]
        assert c["body"]["asset"] == "BTC" and c["body"]["quote_currency"] == "EUR"
    assert calls[0]["body"]["exchange"] == "kraken", "auto sin dato = kraken"
    assert calls[3]["body"]["exchange"] == "binance"


def test_post_market_paper_trade_omits_client_price_and_keeps_retry_id():
    lib = _extract_fn(APP_JS, "postMarketPaperTrade")
    body = (r"""
var calls = [];
function newRequestId() { return "new-id"; }
function api(url, opts) {
  calls.push({url, body: JSON.parse(opts.body)});
  return Promise.resolve({ok:true});
}
""" + lib + r"""
(async function () {
  const fields = {request_id:"same-id", asset:"BTC", quote_currency:"EUR",
    provider:"kraken", price:"1", order_amount:"100"};
  await postMarketPaperTrade("buy", fields);
  await postMarketPaperTrade("buy", fields);
  console.log("M4-JSON:" + JSON.stringify({calls}));
})();
""")
    calls = _node_eval(body)["calls"]
    assert len(calls) == 2
    assert all(call["url"] == "/paper/market/buy" for call in calls)
    assert all(call["body"]["request_id"] == "same-id" for call in calls)
    assert all("price" not in call["body"] for call in calls)
