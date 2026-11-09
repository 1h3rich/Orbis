"""Fase 4b: frescura real de la caché backend + guarda WS↔REST.

Bloqueante del juez técnico: la caché servía velas 60 s mientras la UI
prometía «se actualiza cada 1 s» (1s) u 8 s (30s). Estos tests FALLABAN con
el TTL fijo de 60 s y pasan con el TTL proporcional al timeframe:

* TTL de velas proporcional (1s→1, 30s→8, 1m→15, 5m+→60) y acotado [1, 60].
* TTL de ticker declarado (5 s).
* Comportamiento con reloj falso (sin `sleep`): dentro del TTL sirve caché
  (protección contra picos intacta); pasado el TTL sirve dato nuevo.
* Coherencia: el TTL efectivo de velas nunca supera el refresco Auto que la
  UI anuncia (regla leída del propio `estudio.js` vía Node, sin duplicarla).
* Guarda WS↔REST: `pruneRefreshRows` real en Node (tick + refresh solapados
  no retroceden) + cableado (`wsTickSeq`) verificado por regex.
"""

import json
import re
import shutil
import subprocess
import tempfile
import types
from pathlib import Path

import pytest

from app.marketdata import cache as _cache
from app.marketdata import service as svc

STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "web" / "static"
ESTUDIO = (STATIC_DIR / "estudio.js").read_text(encoding="utf-8")


def _tf_list_from_estudio():
    """`TF_LIST` REAL de estudio.js (sin duplicarla: si la UI añade un TF,
    este test lo sigue)."""
    m = re.search(r"var TF_LIST\s*=\s*(\[.*?\]);", ESTUDIO, re.S)
    assert m is not None, "TF_LIST no encontrado en estudio.js"
    return json.loads(m.group(1))


# -- TTL proporcional ------------------------------------------------------


def test_candle_ttl_proportional_and_bounded():
    """1s→1 s, 30s→8 s (7,5 redondeado como la UI), 1m→15 s, 5m/1d→60 s
    (tope); toda la lista de TF del Estudio queda en [1, 60]."""
    assert _cache.candle_ttl_for("1s") == 1.0
    assert _cache.candle_ttl_for("30s") == 8.0
    assert _cache.candle_ttl_for("1m") == 15.0
    assert _cache.candle_ttl_for("5m") == 60.0
    assert _cache.candle_ttl_for("1d") == 60.0
    assert _cache.candle_ttl_for("1M") == 60.0
    for tf in _tf_list_from_estudio():
        ttl = _cache.candle_ttl_for(tf)
        assert 1.0 <= ttl <= 60.0, (tf, ttl)
    # Ilegible: cadencia media acotada, nunca el congelado de 60 s.
    assert _cache.candle_ttl_for("¿qué?") == 15.0


def test_ticker_ttl_is_declared_5s():
    """El último precio puede retrasarse 5 s como máximo, no 60 s."""
    assert _cache.TTL["ticker"] == 5.0
    assert _cache.ttl_for("ticker", ("binance", "BTC/USDT")) == 5.0


def test_ttl_for_reads_timeframe_from_cache_key():
    """La clave de velas ya trae el timeframe en `key[2]`: el TTL efectivo
    lo usa sin cambiar `service.py`."""
    assert _cache.ttl_for("candles", ("binance", "BTC/USDT", "1s", 5, None)) == 1.0
    assert _cache.ttl_for("candles", ("okx", "BTC/USDT", "30s", 5, None)) == 8.0
    assert _cache.ttl_for("candles", ("kraken", "BTC/EUR", "1h", 200, None)) == 60.0
    assert _cache.ttl_for("symbols", ("kraken",)) == 900.0
    assert _cache.ttl_for("health", ("kraken",)) == 15.0
    assert _cache.ttl_for("rareza", ("x",)) == 60.0


# -- comportamiento con reloj falso ----------------------------------------


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


@pytest.fixture()
def _frozen_time(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(_cache, "time", types.SimpleNamespace(monotonic=clock.monotonic))
    return clock


class _FakeProv:
    """Proveedor doble: cada llamada avanza la última vela (dato nuevo real)."""

    name = "fake"
    timeframes = ("1s", "30s", "1m", "5m", "1h")
    n_calls = 0
    t = 1791342313

    def get_candles(self, symbol, timeframe="1h", limit=200, since=None):
        type(self).n_calls += 1
        type(self).t += 1
        base = type(self).t - 4
        return [
            {"t": base + i, "o": "1", "h": "2", "l": "0.5",
             "c": "1.%d" % (type(self).n_calls), "v": "10"}
            for i in range(5)
        ]

    def get_ticker(self, symbol):
        type(self).n_calls += 1
        return {"symbol": symbol, "price": "100.%d" % type(self).n_calls,
                "source": "fake"}


@pytest.fixture()
def _fake_provider(monkeypatch):
    _FakeProv.n_calls = 0
    _FakeProv.t = 1791342313
    fake = _FakeProv()
    monkeypatch.setattr(
        svc, "resolve_provider",
        lambda provider, symbol=None, http_get=None: (fake, "fake"),
    )
    return fake


def test_candles_1s_serves_cached_within_ttl_and_fresh_after(
    _frozen_time, _fake_provider
):
    """Con TF 1s (TTL 1 s): a 0,5 s sirve caché (misma llamada, sin tocar el
    proveedor: la protección contra picos sigue); a 1,5 s sirve dato nuevo.
    Con el TTL fijo de 60 s la segunda llamada devolvía lo cacheado: fallaba.
    """
    first = svc.candles("BTC/USDT", provider="fake", timeframe="1s", limit=5)
    assert first["available"] is True
    assert _FakeProv.n_calls == 1
    last_t = first["candles"][-1]["t"]

    _frozen_time.now += 0.5
    second = svc.candles("BTC/USDT", provider="fake", timeframe="1s", limit=5)
    assert _FakeProv.n_calls == 1  # pico absorbido por la caché
    assert second["candles"][-1]["t"] == last_t

    _frozen_time.now += 1.0  # 1,5 s desde la primera > TTL de 1 s
    third = svc.candles("BTC/USDT", provider="fake", timeframe="1s", limit=5)
    assert _FakeProv.n_calls == 2
    assert third["candles"][-1]["t"] > last_t  # dato nuevo, no congelado


def test_candles_30s_keeps_longer_window(_frozen_time, _fake_provider):
    """Con TF 30s (TTL 8 s): a 2,5 s sigue en caché; a 9 s refresca."""
    first = svc.candles("BTC/USDT", provider="fake", timeframe="30s", limit=5)
    assert _FakeProv.n_calls == 1
    _frozen_time.now += 2.5
    svc.candles("BTC/USDT", provider="fake", timeframe="30s", limit=5)
    assert _FakeProv.n_calls == 1
    _frozen_time.now += 6.5
    out = svc.candles("BTC/USDT", provider="fake", timeframe="30s", limit=5)
    assert _FakeProv.n_calls == 2
    assert out["candles"][-1]["t"] > first["candles"][-1]["t"]


def test_snapshot_ticker_cached_within_5s_and_fresh_after(
    _frozen_time, _fake_provider
):
    """El ticker aguanta picos 5 s y luego refresca (antes: 60 s viejo)."""
    first = svc.snapshot("BTC/USDT", provider="fake")
    assert first["available"] is True and first["cached"] is False
    _frozen_time.now += 4.0
    second = svc.snapshot("BTC/USDT", provider="fake")
    assert second["cached"] is True
    assert second["price"] == first["price"]
    _frozen_time.now += 2.0
    third = svc.snapshot("BTC/USDT", provider="fake")
    assert third["cached"] is False
    assert third["price"] != first["price"]


# -- coherencia con la regla real del frontend ------------------------------


def _node_auto_refresh():
    """`autoRefreshFor` REAL de estudio.js evaluado en Node (sin copiar la
    tabla: si la UI cambia la regla, este test la sigue)."""
    if shutil.which("node") is None:
        pytest.skip("node no disponible")
    m = re.search(r"var TF_SECONDS\s*=\s*\{.*?\};", ESTUDIO, re.S)
    assert m is not None
    fn = re.search(r"function tfSeconds\(tf\)\s*\{", ESTUDIO)
    assert fn is not None
    start = ESTUDIO.index("{", fn.start())
    depth, i = 0, start
    instr = None
    while i < len(ESTUDIO):
        c = ESTUDIO[i]
        if instr:
            if c == "\\":
                i += 2
                continue
            if c == instr:
                instr = None
        elif c in ("'", '"', "`"):
            instr = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    tf_fn = ESTUDIO[fn.start():i + 1]
    fn2 = re.search(r"function autoRefreshFor\(tf\)\s*\{", ESTUDIO)
    assert fn2 is not None
    start2 = ESTUDIO.index("{", fn2.start())
    depth, i = 0, start2
    instr = None
    while i < len(ESTUDIO):
        c = ESTUDIO[i]
        if instr:
            if c == "\\":
                i += 2
                continue
            if c == instr:
                instr = None
        elif c in ("'", '"', "`"):
            instr = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    auto_fn = ESTUDIO[fn2.start():i + 1]
    tfs = _tf_list_from_estudio()
    body = (m.group(0) + "\n" + tf_fn + "\n" + auto_fn
            + "\nconst tfs = " + json.dumps(tfs) + ";"
            + "\nconsole.log('FRESCURA-JSON:' + JSON.stringify("
              "Object.fromEntries(tfs.map(function (t) { return [t, autoRefreshFor(t)]; }))));")
    tmpdir = Path("/tmp/opencode")
    tmpdir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=str(tmpdir), prefix="frescura-",
                                     suffix=".js", delete=False, mode="w",
                                     encoding="utf-8") as fh:
        fh.write(body)
        path = fh.name
    proc = subprocess.run(["node", path], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("FRESCURA-JSON:")]
    assert line, proc.stdout
    return json.loads(line[0][len("FRESCURA-JSON:"):])


def test_candle_ttl_never_exceeds_ui_refresh():
    """El TTL efectivo de cada TF es ≤ el refresco Auto que la UI anuncia
    para ese TF: nunca se promete frescura que el backend no da."""
    ui_ms = _node_auto_refresh()
    for tf, ms in ui_ms.items():
        assert _cache.candle_ttl_for(tf) * 1000 <= ms, (tf, _cache.candle_ttl_for(tf), ms)


# -- guarda WS↔REST ----------------------------------------------------------


def _node_run_frescura(lib_extra, body):
    if shutil.which("node") is None:
        pytest.skip("node no disponible")
    full = lib_extra + "\n" + body
    tmpdir = Path("/tmp/opencode")
    tmpdir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=str(tmpdir), prefix="frescura-ws-",
                                     suffix=".js", delete=False, mode="w",
                                     encoding="utf-8") as fh:
        fh.write(full)
        path = fh.name
    proc = subprocess.run(["node", path], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("FRESCURA-JSON:")]
    assert line, proc.stdout
    return json.loads(line[0][len("FRESCURA-JSON:"):])


def _extract_fn_frescura(src, name):
    m = re.search(r"function " + re.escape(name) + r"\s*\(", src)
    assert m is not None, name
    start = src.index("{", m.end())
    depth, i = 0, start
    instr = None
    while i < len(src):
        c = src[i]
        if instr:
            if c == "\\":
                i += 2
                continue
            if c == instr:
                instr = None
        elif c in ("'", '"', "`"):
            instr = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():i + 1]
        i += 1
    raise AssertionError("sin cierre: " + name)


def test_prune_refresh_rows_overlapped_tick_does_not_regress():
    """Tick WS + refresh REST solapados con las funciones REALES: la vela que
    el tick avanzó a 12 no retrocede al 10 viejo del REST; la vela nueva (101)
    sí se añade. Sin tick en vuelo, el refresco funde normal."""
    lib = "\n".join([
        _extract_fn_frescura(ESTUDIO, "mergeCandleRows"),
        _extract_fn_frescura(ESTUDIO, "pruneRefreshRows"),
    ])
    res = _node_run_frescura(lib, r"""
let ds = [{ time: 100, close: "12" }]; // el tick WS ya avanzó el cierre a 12
let rest = [{ time: 100, close: "10" }, { time: 101, close: "11" }]; // REST pedido antes, más viejo
let pruned = pruneRefreshRows(100, rest, true); // hubo tick en vuelo
if (pruned.length !== 1 || pruned[0].time !== 101) throw new Error("poda mala: " + JSON.stringify(pruned));
let m = mergeCandleRows(ds, pruned);
if (String(ds[0].close) !== "12") throw new Error("retroceso al dato viejo: " + JSON.stringify(ds));
if (ds.length !== 2 || ds[1].time !== 101) throw new Error("la vela nueva debe añadirse: " + JSON.stringify(ds));
// Sin tick en vuelo: fusión completa (comportamiento intacto).
let ds2 = [{ time: 100, close: "10" }];
let full = pruneRefreshRows(100, [{ time: 100, close: "10.5" }], false);
if (full.length !== 1) throw new Error("sin tick no se poda");
mergeCandleRows(ds2, full);
if (String(ds2[0].close) !== "10.5") throw new Error("sin tick debe actualizar in situ");
// Filas rotas con tick en vuelo: se ignoran sin romper.
if (pruneRefreshRows(100, [null, {}, { time: NaN }], true).length !== 0) throw new Error("rotas deben podarse");
console.log("FRESCURA-JSON:" + JSON.stringify({ ok: true }));
""")
    assert res["ok"] is True


def test_ws_rest_guard_is_wired():
    """La guarda existe y está conectada: secuencia compartida `wsTickSeq`
    (mismo patrón que `generation`/`loading`/`refreshing`, sin sistema
    nuevo): el tick la avanza y el refresco la comprueba antes de fundir."""
    assert "wsTickSeq = 0" in ESTUDIO
    m = re.search(r"ws\.onmessage = function \(ev\) \{(.*?)\n        \};", ESTUDIO, re.S)
    assert m is not None and "wsTickSeq++" in m.group(1)
    m = re.search(r"function refreshOpenCandle\(\)\s*\{(.*?)\n    \}", ESTUDIO, re.S)
    assert m is not None
    body = m.group(1)
    assert "var wsMark = wsTickSeq;" in body
    assert "wsTickSeq !== wsMark" in body
    assert "pruneRefreshRows(lastT, rows, true)" in body
