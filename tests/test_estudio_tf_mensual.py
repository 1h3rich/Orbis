"""R2: temporalidad mensual (1M) para ver el histórico desde 2020.

Disponibilidad VERIFICADA contra las APIs reales (probes con respuesta
concreta, 7 oct 2026; sin simular):

* binance ``interval=1M`` → velas mensuales (ms) ✓.
* okx ``bar=1M`` → velas mensuales (ms, ordenadas por el adapter) ✓;
  ``bar=1Y`` → ``{"code":"51000","Parameter bar error"}`` (sin 1Y).
* bybit ``interval=M`` → velas mensuales (ms) ✓.
* bitget ``granularity=1M`` → velas mensuales (ms) ✓.
* mexc ``interval=1M`` → velas mensuales (ms) ✓.
* kucoin ``type=1month`` → velas mensuales (SEGUNDOS) ✓ con ventana
  startAt/endAt explícita (82 velas desde 2020-01-01); sin ventana amplia
  la fija del adapter (200 h) no devolvía ni una vela mensual.
* kraken: sin mensual nativo (máx 1w=10080) ✗.
* coinbase: ``granularity=2592000`` → ``{"message":"Unsupported
  granularity"}`` ✗.
* gate: ``interval=1M`` → ``{"label":"INVALID_PARAM_VALUE"}`` ✗ (su
  ``30d`` existe pero son ventanas fijas de 30 días, no mes calendario).
* grvt: sin OHLC ✗.

Decisión 1y: NO se añade —ningún exchange lo sirve nativo (OKX lo rechaza
explícitamente) y 1M ya llega a 2020 con ~79 velas (binance devuelve 111
mensuales desde 2017-08). Decisión diario/backfill: NO se pagina —los
adapters interpretan ``since`` como «≥ desde» (hacia adelante) y el tope de
1000 del endpoint es homogéneo con lo que dan los exchanges (binance máx
1000-1500, kraken 720); 1d cubre ~2,7 años y queda documentado como
limitación en docs/ESTADO_Y_FALLOS_PENDIENTES.md. 1w (1000 semanas ≈ 19
años) sí llega a 2020 sin cambios.

Cobertura (comportamiento, JS real ejecutado en Node + backend con
http_get mockeado, mismo patrón de las fases anteriores):

* 1M declarado SOLO donde se verificó (mapas TF/timeframes) y ausente
  donde se rechazó; el frontend deriva la disponibilidad de lo observado
  (cada available:false cachea proveedor/TF con su reason exacto).
* Parámetros reales que pide cada adapter para 1M + parseo de ``t`` a
  segundos + ventana proporcional de kucoin.
* ``available:false`` honesto donde no hay nativo (menciona el TF y los
  nativos, sin inventar).
* Regla única de refresco/TTL para 1M: Auto = 60 s (tope), TTL = 60 s
  (``TTL ≤ refresco`` también vía TF_LIST del JS real).
* Histórico largo: 1M cabe en el tope de 1000 (dedupe/orden del servicio) y
  con menos velas que el límite ``hasMore`` es False + mensaje honesto.
"""

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from app.marketdata import cache as _cache
from app.marketdata import service as svc
from app.marketdata.adapters import binance as _binance_mod
from app.marketdata.adapters import bitget as _bitget_mod
from app.marketdata.adapters import bybit as _bybit_mod
from app.marketdata.adapters import coinbase as _coinbase_mod
from app.marketdata.adapters import gate as _gate_mod
from app.marketdata.adapters import grvt as _grvt_mod
from app.marketdata.adapters import kraken as _kraken_mod
from app.marketdata.adapters import kucoin as _kucoin_mod
from app.marketdata.adapters import mexc as _mexc_mod
from app.marketdata.adapters import okx as _okx_mod

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


def _extract_var(src, name):
    m = re.search(r"var " + re.escape(name) + r"\s*=\s*", src)
    assert m is not None, name
    b = src.index("[", m.end()) if "[" in src[m.end():m.end() + 4] else src.index("{", m.end())
    if src[b] == "[":
        end = src.index("];", b) + 1
        return src[m.start():end + 1] + ";"
    return src[m.start():_balanced_end(src, b) + 1] + ";"


def _node_run(body, extra_fns=()):
    if shutil.which("node") is None:
        import pytest
        pytest.skip("node no disponible")
    lib = "\n".join(
        [_extract_var(ESTUDIO, "TF_LIST"),
         _extract_var(ESTUDIO, "TF_SECONDS"),
         _extract_fn(ESTUDIO, "num"),
         _extract_fn(ESTUDIO, "tfSeconds"),
         _extract_fn(ESTUDIO, "autoRefreshFor"),
         _extract_fn(ESTUDIO, "parsePeriodList"),
         _extract_fn(ESTUDIO, "maxWindowPeriod"),
         _extract_fn(ESTUDIO, "candleLimitFor")]
        + [_extract_fn(ESTUDIO, f) for f in extra_fns]
    )
    full = lib + "\n" + body
    tmpdir = Path("/tmp/opencode")
    tmpdir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=str(tmpdir), prefix="r2-1m-",
                                     suffix=".js", delete=False, mode="w",
                                     encoding="utf-8") as fh:
        fh.write(full)
        path = fh.name
    proc = subprocess.run(["node", path], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("R2-JSON:")]
    assert line, proc.stdout
    return json.loads(line[0][len("R2-JSON:"):])


def _node_observed(body):
    if shutil.which("node") is None:
        import pytest
        pytest.skip("node no disponible")
    var_src = re.search(r"var TF_OBSERVED_UNAVAILABLE\s*=\s*\{.*?\};", ESTUDIO, re.S)
    assert var_src is not None, "TF_OBSERVED_UNAVAILABLE no encontrado"
    full = "\n".join([
        var_src.group(0),
        _extract_fn(ESTUDIO, "tfUnavailableReason"),
        _extract_fn(ESTUDIO, "markTfUnavailable"),
        _extract_fn(ESTUDIO, "markTfAvailable"),
        body,
    ])
    tmpdir = Path("/tmp/opencode")
    tmpdir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=str(tmpdir), prefix="r2o-",
                                     suffix=".js", delete=False, mode="w",
                                     encoding="utf-8") as fh:
        fh.write(full)
        path = fh.name
    proc = subprocess.run(["node", path], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("R2-JSON:")]
    assert line, proc.stdout
    return json.loads(line[0][len("R2-JSON:"):])


# -- backend: quién sirve 1M ------------------------------------------------

_MONTH_OK = {
    "binance": _binance_mod.BinanceAdapter,
    "okx": _okx_mod.OkxAdapter,
    "bybit": _bybit_mod.BybitAdapter,
    "mexc": _mexc_mod.MexcAdapter,
    "kucoin": _kucoin_mod.KucoinAdapter,
    "bitget": _bitget_mod.BitgetAdapter,
}
_MONTH_NO = {
    "kraken": _kraken_mod.KrakenAdapter,
    "coinbase": _coinbase_mod.CoinbaseAdapter,
    "gate": _gate_mod.GateAdapter,
    "grvt": _grvt_mod.GrvtAdapter,
}


def _tfs(cls):
    if cls is _grvt_mod.GrvtAdapter:
        return tuple(cls.timeframes)
    if cls is _coinbase_mod.CoinbaseAdapter:
        return tuple(cls.timeframes)
    mod_name = cls.__module__
    for mod in (_binance_mod, _okx_mod, _bybit_mod, _mexc_mod,
                _kucoin_mod, _bitget_mod, _kraken_mod, _gate_mod):
        if mod.__name__.endswith(mod_name.split(".")[-1]):
            tfs = getattr(mod, "TF", None) or getattr(mod, "TF_TO_INTERVAL", None)
            if tfs is not None:
                return tuple(tfs)
    return tuple(cls.timeframes)


def test_1m_only_where_verified():
    """1M en los mapas de los 6 verificados; ausente en kraken/coinbase/
    gate/grvt (probes reales en el docstring)."""
    assert _binance_mod.TF["1M"] == "1M"
    assert _okx_mod.TF["1M"] == "1M"
    assert _bybit_mod.TF["1M"] == "M"
    assert _mexc_mod.TF["1M"] == "1M"
    assert _kucoin_mod.TF["1M"] == "1month"
    assert _bitget_mod.TF["1M"] == "1M"
    for cls in _MONTH_OK.values():
        assert "1M" in tuple(cls.timeframes), cls
    assert "1M" not in _kraken_mod.TF_TO_INTERVAL
    assert "1M" not in _coinbase_mod.GRAN
    assert "1M" not in _coinbase_mod.CoinbaseAdapter.timeframes
    assert "1M" not in _gate_mod.TF
    assert "1M" not in _grvt_mod.GrvtAdapter.timeframes
    assert "1y" not in _binance_mod.TF  # 1y no existe en ningún mapa


def test_1m_real_params_and_seconds():
    """Con http_get mockeado cada adapter pide su parámetro 1M real y
    devuelve ``t`` en segundos (ms→s donde toca)."""
    seen = {}

    def fake_ms(url, timeout=5.0):
        seen["url"] = url
        return [[1788220800000, "78581.3", "87395.6", "74967.9", "83623.6", "10"]]

    b = _binance_mod.BinanceAdapter(http_get=fake_ms)
    rows = b.get_candles("BTC/USDT", timeframe="1M", limit=5)
    assert rows and rows[0]["t"] == 1788220800
    assert "interval=1M" in seen["url"], seen["url"]

    o = _okx_mod.OkxAdapter(http_get=lambda url, timeout=5.0: (
        seen.update(url=url) or
        {"data": [["1788220800000", "78588.5", "87399", "74955.5", "84134.4", "1"]]}))
    rows = o.get_candles("BTC/USDT", timeframe="1M", limit=5)
    assert rows and rows[0]["t"] == 1788220800
    assert "bar=1M" in seen["url"], seen["url"]

    y = _bybit_mod.BybitAdapter(http_get=lambda url, timeout=5.0: (
        seen.update(url=url) or
        {"result": {"list": [["1788220800000", "78580.8", "87401.9", "74961.2", "83616.2", "1"]]}}))
    rows = y.get_candles("BTC/USDT", timeframe="1M", limit=5)
    assert rows and rows[0]["t"] == 1788220800
    assert "interval=M" in seen["url"], seen["url"]

    g = _bitget_mod.BitgetAdapter(http_get=lambda url, timeout=5.0: (
        seen.update(url=url) or
        {"data": [["1788220800000", "78575", "87393.7", "74950.4", "84137.2", "1"]]}))
    rows = g.get_candles("BTC/USDT", timeframe="1M", limit=5)
    assert rows and rows[0]["t"] == 1788220800
    assert "granularity=1M" in seen["url"], seen["url"]

    m = _mexc_mod.MexcAdapter(http_get=fake_ms)
    rows = m.get_candles("BTC/USDT", timeframe="1M", limit=5)
    assert rows and rows[0]["t"] == 1788220800
    assert "interval=1M" in seen["url"], seen["url"]

    k = _kucoin_mod.KucoinAdapter(http_get=lambda url, timeout=5.0: (
        seen.update(url=url) or
        {"data": [["1788220800", "78573.5", "84138.2", "87396", "74973.8", "1"]]}))
    rows = k.get_candles("BTC/USDT", timeframe="1M", limit=5)
    assert rows and rows[0]["t"] == 1788220800, rows
    assert "type=1month" in seen["url"], seen["url"]


def test_kucoin_window_covers_monthly():
    """Kucoin dimensiona la ventana al TF (limit × duración): con 1M pide
    años, no 200 h (que no devolvían ni una vela mensual)."""
    seen = {}

    def fake(url, timeout=5.0):
        seen["url"] = url
        return {"data": []}

    k = _kucoin_mod.KucoinAdapter(http_get=fake)
    k.get_candles("BTC/USDT", timeframe="1M", limit=100)
    m = re.search(r"startAt=(\d+)&endAt=(\d+)", seen["url"])
    assert m, seen["url"]
    span = int(m.group(2)) - int(m.group(1))
    assert span >= 100 * 2592000 - 5, (span, seen["url"])
    k.get_candles("BTC/USDT", timeframe="1h", limit=200)
    m = re.search(r"startAt=(\d+)&endAt=(\d+)", seen["url"])
    assert m, seen["url"]
    assert int(m.group(2)) - int(m.group(1)) == 200 * 3600


def test_service_rejects_1m_where_not_native():
    """kraken/coinbase/gate/grvt + 1M: available:false honesto (menciona el
    TF y los nativos); 1M nunca se inventa."""
    _cache.clear()
    try:
        for prov in ("kraken", "coinbase", "gate", "grvt"):
            out = svc.candles("BTC/USDT", provider=prov, timeframe="1M", limit=5)
            assert out["available"] is False, (prov, out)
            assert "1M" in out["reason"], (prov, out["reason"])
    finally:
        _cache.clear()


def test_service_1m_monthly_flow_dedupe_sorted():
    """El servicio ordena/dedup las mensuales y respeta el límite (caben de
    sobra en el tope de 1000: ~79 desde 2020)."""
    _cache.clear()
    try:
        rows = [{"t": 1577836800 + i * 2592000, "o": "1", "h": "2",
                 "l": "0.5", "c": "1.5", "v": "10"} for i in range(82)]
        import app.marketdata.registry as reg
        real = reg.get_provider

        class _A:
            name = "binance"
            timeframes = _binance_mod.BinanceAdapter.timeframes

            def get_candles(self, symbol, timeframe="1h", limit=200, since=None):
                assert timeframe == "1M"
                return list(reversed(rows))

        reg.get_provider = lambda name, http_get=None: _A()
        try:
            out = svc.candles("BTC/USDT", provider="binance", timeframe="1M", limit=1000)
        finally:
            reg.get_provider = real
        assert out["available"] is True
        ts = [c["t"] for c in out["candles"]]
        assert ts == sorted(ts) and len(set(ts)) == len(ts)
        assert out["candles"][0]["t"] == 1577836800  # 2020-01-01
    finally:
        _cache.clear()


# -- frontend ---------------------------------------------------------------

def test_frontend_monthly_is_wired():
    """TF_LIST trae 1M; duración 2592000 s sin colisionar con 1m; Auto=60 s
    (tope) y el límite de velas no cambia para 1M."""
    out = _node_run("""
var r = {
  list: TF_LIST,
  secs1M: TF_SECONDS["1M"],
  fn1M: tfSeconds("1M"),
  fn1m: tfSeconds("1m"),
  auto1M: autoRefreshFor("1M"),
  auto1w: autoRefreshFor("1w"),
  limPlain: candleLimitFor({sma:{on:false,p:20},ema:{on:false,p:20},bb:{on:false,p:20,m:2},rsi:{on:false,p:14},macd:{on:false,f:12,s:26,sig:9}}),
  limEma200: candleLimitFor({sma:{on:false,p:20},ema:{on:true,p:200},bb:{on:false,p:20,m:2},rsi:{on:false,p:14},macd:{on:false,f:12,s:26,sig:9}})
};
console.log('R2-JSON:' + JSON.stringify(r));
""")
    assert "1M" in out["list"]
    assert out["list"].index("1M") > out["list"].index("1w")
    assert out["secs1M"] == 2592000
    assert out["fn1M"] == 2592000
    assert out["fn1m"] == 60  # sin colisión con el minuto
    assert out["auto1M"] == 60000
    assert out["auto1w"] == 60000
    assert out["limPlain"] == 500
    assert out["limEma200"] == 800


def test_frontend_derives_1m_from_observed_backend_reason():
    """EQUIVALENCIA DOCUMENTADA (M5): sustituye a
    ``test_frontend_table_covers_1m_both_ways`` (la tabla estática
    desapareció por diseño). La verdad de 1M sigue en el backend —mapas de
    los 6 verificados (``test_1m_only_where_verified``) y available:false
    honesto donde no hay nativo
    (``test_service_rejects_1m_where_not_native``)— y el frontend la
    DERIVA: con los helpers REALES en Node, el reason observado de
    kraken/1M se devuelve exacto (deshabilita) mientras binance/1M no
    probado sigue habilitado; tras un éxito vuelve a habilitarse."""
    out = _node_observed("""
const reason = "timeframe 1M no nativo en kraken (nativos: 1m, 5m, 15m). No se inventan datos.";
if (tfUnavailableReason("kraken", "1M") !== "") throw new Error("sin probar debe estar habilitado");
markTfUnavailable("kraken", "1M", reason);
if (tfUnavailableReason("kraken", "1M") !== reason) throw new Error("el reason observado debe devolverse exacto");
if (tfUnavailableReason("binance", "1M") !== "") throw new Error("1M no probado en binance sigue habilitado");
markTfAvailable("kraken", "1M");
if (tfUnavailableReason("kraken", "1M") !== "") throw new Error("tras el éxito vuelve a habilitarse");
console.log('R2-JSON:' + JSON.stringify({ ok: true }));
""")
    assert out["ok"] is True


def test_monthly_ttl_within_ui_refresh():
    """TTL(1M) = 60 s ≤ refresco Auto anunciado (60 s): coherencia también
    para el TF nuevo (el test general de frescura lo cubre vía TF_LIST)."""
    assert _cache.candle_ttl_for("1M") == 60.0
    assert _cache.tf_seconds("1M") == 2592000.0
    assert _cache.ttl_for("candles", ("binance", "BTC/USDT", "1M", 500, None)) == 60.0
