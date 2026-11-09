"""Fase 1: EMA correcta — verificación numérica contra referencia en Python.

Causa (verificada, no re-investigada): `ema` en app/web/static/estudio.js se
sembraba con `vals[0]` en lugar de con la SMA de los primeros `p` valores.
El cambio (misma orientación cronológica ascendente):
  EMA_t = P_t·k + EMA_(t-1)·(1−k), k = 2/(p+1), semilla = SMA(p) en índice p−1.

Estrategia: se extrae la función `ema` REAL del fuente (brace-matching, sin
copiar su cuerpo) junto a sus dependencias (`num`, `parsePeriodList`,
`maxWindowPeriod`, `candleLimitFor`) y se ejecuta en Node; los resultados se
comparan contra una referencia independiente implementada en Python en este
fichero desde la especificación. Periodos exigidos: 9, 20, 50, 100 y 200.

Equivalencias documentadas (política de no-debilitamiento): no se toca
ningún test existente; este fichero solo añade comprobaciones nuevas. El
cambio de `limit` fijo 200 → `candleLimitFor(indCfg)` y de `indCfg.ema.p`
número → número o lista no colisiona con aserciones previas (ningún test
anterior fijaba esos literales).
"""

import json
import random
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "web" / "static"
ESTUDIO = (STATIC_DIR / "estudio.js").read_text(encoding="utf-8")

PERIODS = (9, 20, 50, 100, 200)
TOL = 1e-9


def _extract_fn(src, name):
    """Extrae `function name(...) {...}` con balanceo de llaves (robusto
    ante llaves anidadas, a diferencia de un regex no-greedy)."""
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


def _node_lib():
    parts = [_extract_fn(ESTUDIO, "num"), _extract_fn(ESTUDIO, "closes"),
             _extract_fn(ESTUDIO, "ema"),
             _extract_fn(ESTUDIO, "parsePeriodList"),
             _extract_fn(ESTUDIO, "maxWindowPeriod"),
             _extract_fn(ESTUDIO, "candleLimitFor")]
    m = re.search(r"var EMA_PALETTE = \[.*?\];", ESTUDIO)
    assert m is not None
    parts.append(m.group(0))
    m = re.search(r"var EMA_FIXED_COLORS = \{.*?\};", ESTUDIO)
    assert m is not None
    parts.append(m.group(0))
    parts.append(_extract_fn(ESTUDIO, "emaColorFor"))
    return "\n".join(parts)


def _node_run(cases):
    """cases: lista de dicts {key, fn, args}. Devuelve {key: resultado}."""
    if shutil.which("node") is None:
        pytest.skip("node no disponible")
    payload = json.dumps(cases)
    body = (_node_lib() + "\nconst CASES = " + payload + ";\n" + r"""
const out = {};
for (const c of CASES) {
  let r;
  if (c.fn === "ema") r = ema(c.args[0], c.args[1]);
  else if (c.fn === "closes") r = closes(c.args[0]);
  else if (c.fn === "parsePeriodList") r = parsePeriodList(c.args[0], c.args[1]);
  else if (c.fn === "candleLimitFor") r = candleLimitFor(c.args[0]);
  else if (c.fn === "emaColorFor") r = emaColorFor(c.args[0]);
  else throw new Error("fn desconocida " + c.fn);
  out[c.key] = r;
}
console.log("EMA-JSON:" + JSON.stringify(out));
""")
    tmpdir = Path("/tmp/opencode")
    tmpdir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=str(tmpdir), prefix="ema-fase1-",
                                     suffix=".js", delete=False, mode="w",
                                     encoding="utf-8") as fh:
        fh.write(body)
        path = fh.name
    proc = subprocess.run(["node", path], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("EMA-JSON:")]
    assert line, proc.stdout
    return json.loads(line[0][len("EMA-JSON:"):])


# ---------------- referencia independiente (Python, desde la espec) --------
def py_ema(vals, p):
    p = max(1, int(p))
    out = [None] * len(vals)
    if len(vals) < p:
        return out
    k = 2.0 / (p + 1)
    seed = sum(vals[:p]) / p
    e = None
    for i in range(len(vals)):
        if i < p - 1:
            continue
        e = seed if i == p - 1 else vals[i] * k + e * (1.0 - k)
        out[i] = e
    return out


def trend_series(n=300):
    return [100.0 + i * 0.5 + (i % 7) * 0.3 for i in range(n)]


def seeded_random_series(n=300, seed=7):
    r = random.Random(seed)
    return [100.0 + i * 0.1 + r.uniform(-2.0, 2.0) for i in range(n)]


def assert_close(got, want, where):
    if want is None:
        assert got is None, where
    else:
        assert got is not None, where
        assert abs(got - want) <= TOL, "%s: %r vs %r" % (where, got, want)


# ---------------- tests -----------------------------------------------------
@pytest.mark.parametrize("p", PERIODS)
def test_ema_coincide_referencia_en_tendencia(p):
    """Serie sintética determinista con tendencia (300 puntos)."""
    vals = trend_series()
    res = _node_run([{"key": "e", "fn": "ema", "args": [vals, p]}])
    want = py_ema(vals, p)
    got = res["e"]
    assert len(got) == len(want)
    for i, (g, w) in enumerate(zip(got, want)):
        assert_close(g, w, "p=%d i=%d" % (p, i))


@pytest.mark.parametrize("p", PERIODS)
def test_ema_coincide_referencia_en_aleatoria_con_semilla(p):
    """Serie aleatoria con semilla fija (300 puntos, seed=7)."""
    vals = seeded_random_series()
    res = _node_run([{"key": "e", "fn": "ema", "args": [vals, p]}])
    want = py_ema(vals, p)
    for i, (g, w) in enumerate(zip(got := res["e"], want)):
        assert_close(g, w, "p=%d i=%d" % (p, i))


@pytest.mark.parametrize("p", PERIODS)
def test_ema_primer_valor_es_sma(p):
    """El primer valor válido (índice p−1) es exactamente la SMA de los
    primeros p precios; antes, todo null."""
    vals = trend_series()
    res = _node_run([{"key": "e", "fn": "ema", "args": [vals, p]}])
    got = res["e"]
    assert all(g is None for g in got[:p - 1])
    assert abs(got[p - 1] - sum(vals[:p]) / p) <= 1e-12


def test_ema_bordes():
    """len < p → todo null; p = 1 → identidad; constante → constante;
    salto grande → converge sin nulls tras el índice p−1."""
    short = [1.0, 2.0, 3.0, 4.0, 5.0]
    const = [42.0] * 300
    jump = [10.0] * 100 + [1000.0] * 100
    res = _node_run([
        {"key": "short", "fn": "ema", "args": [short, 20]},
        {"key": "p1", "fn": "ema", "args": [trend_series(50), 1]},
        {"key": "const", "fn": "ema", "args": [const, 50]},
        {"key": "jump", "fn": "ema", "args": [jump, 20]},
    ])
    assert res["short"] == [None] * 5
    assert res["short"] == py_ema(short, 20)
    for g, w in zip(res["p1"], py_ema(trend_series(50), 1)):
        assert_close(g, w, "p=1")
    assert res["p1"] == trend_series(50)
    for g in res["const"][49:]:
        assert abs(g - 42.0) <= TOL
    for i, (g, w) in enumerate(zip(res["jump"], py_ema(jump, 20))):
        assert_close(g, w, "jump i=%d" % i)
    # Tras el salto, la EMA sube monótonamente hacia 1000 sin rebotes.
    tail = res["jump"][100:]
    assert all(b >= a for a, b in zip(tail, tail[1:]))
    assert tail[-1] > 900.0


def test_closes_lee_velas_mapeadas_y_crudad():
    """Bug real hallado en la verificación con navegador (Fase 1): `closes`
    leía `r.c` (fila cruda del API) pero recibe velas ya mapeadas por
    loadCandles (`{..., close, ...}`), así que `num(undefined)` devolvía 0
    y TODOS los indicadores (SMA/EMA/BB/RSI/MACD) pintaban ceros. Debe leer
    `close` con fallback a `c` para no romper otros llamantes."""
    mapped = [{"time": 1, "open": "10", "high": "11", "low": "9",
               "close": "10.5", "v": "1"},
              {"time": 2, "open": "10.5", "high": "12", "low": "10",
               "close": "11.5", "v": "2"}]
    raw = [{"t": 1, "c": "10.5"}, {"t": 2, "c": "11.5"}]
    res = _node_run([
        {"key": "m", "fn": "closes", "args": [mapped]},
        {"key": "r", "fn": "closes", "args": [raw]},
    ])
    assert res["m"] == [10.5, 11.5]
    assert res["r"] == [10.5, 11.5]
    assert not all(v == 0 for v in res["m"])
    # Fin a fin honesto: EMA sobre velas mapeadas ya no es todo-ceros.
    vals = [{"time": i, "close": 100.0 + i} for i in range(30)]
    res2 = _node_run([{"key": "c", "fn": "closes", "args": [vals]},
                      {"key": "e", "fn": "ema", "args": [[100.0 + i for i in range(30)], 20]}])
    assert res2["c"] == [100.0 + i for i in range(30)]
    tail = [v for v in res2["e"][19:] if v is not None]
    assert len(tail) == 11 and all(v > 100.0 for v in tail)


def test_limit_crece_con_periodo_maximo():
    """Regresión: el limit pedido ya no es 200 fijo; sigue
    clamp(maxPeriodo × 4, 500, 1000) según los indicadores activos."""
    off = {"sma": {"on": False, "p": 20}, "ema": {"on": False, "p": 20},
           "bb": {"on": False, "p": 20, "m": 2}, "rsi": {"on": False, "p": 14},
           "macd": {"on": False, "f": 12, "s": 26, "sig": 9}}

    def on(**kw):
        import copy
        c = copy.deepcopy(off)
        for k, v in kw.items():
            c[k].update(v)
        return c

    res = _node_run([
        {"key": "off", "fn": "candleLimitFor", "args": [off]},
        {"key": "e20", "fn": "candleLimitFor", "args": [on(ema={"on": True, "p": 20})]},
        {"key": "e200", "fn": "candleLimitFor", "args": [on(ema={"on": True, "p": 200})]},
        {"key": "emulti", "fn": "candleLimitFor",
         "args": [on(ema={"on": True, "p": "9,20,50,100,200"})]},
        {"key": "bb150", "fn": "candleLimitFor", "args": [on(bb={"on": True, "p": 150})]},
        {"key": "macd", "fn": "candleLimitFor",
         "args": [on(macd={"on": True, "f": 12, "s": 26, "sig": 9})]},
        {"key": "rsi300", "fn": "candleLimitFor", "args": [on(rsi={"on": True, "p": 300})]},
        {"key": "p500", "fn": "candleLimitFor", "args": [on(ema={"on": True, "p": 500})]},
    ])
    assert res["off"] == 500
    assert res["e20"] == 500  # 20×4=80 → suelo 500
    assert res["e200"] == 800  # EMA-200 recibe ~800 velas y converge
    assert res["emulti"] == 800
    assert res["bb150"] == 600
    assert res["macd"] == 500  # 26×4=104 → suelo 500
    assert res["rsi300"] == 1000  # 300×4=1200 → techo 1000
    assert res["p500"] == 1000  # nunca pasa del tope del endpoint
    # El fuente ya no pide limit=200 en velas (principal ni comparación);
    # el limit=200 de /paper/operations es otro endpoint y se conserva.
    assert '"&limit=200"' not in ESTUDIO
    assert ESTUDIO.count("candleLimitFor(indCfg)") >= 2


def test_ema_multiperiodo_parse_y_colores():
    """indCfg.ema.p admite número (legado) o lista '9,20,50'; cada periodo
    tiene color distinto y estable (el 20 conserva el morado histórico)."""
    res = _node_run([
        {"key": "n", "fn": "parsePeriodList", "args": [20, [20]]},
        {"key": "s", "fn": "parsePeriodList", "args": ["20", [20]]},
        {"key": "l", "fn": "parsePeriodList", "args": ["9,20,50", [20]]},
        {"key": "mix", "fn": "parsePeriodList", "args": ["200; 20,20  9", [20]]},
        {"key": "bad", "fn": "parsePeriodList", "args": ["abc", [20]]},
        {"key": "empty", "fn": "parsePeriodList", "args": ["", [20]]},
        {"key": "c9", "fn": "emaColorFor", "args": [9]},
        {"key": "c20", "fn": "emaColorFor", "args": [20]},
        {"key": "c50", "fn": "emaColorFor", "args": [50]},
        {"key": "c100", "fn": "emaColorFor", "args": [100]},
        {"key": "c200", "fn": "emaColorFor", "args": [200]},
    ])
    assert res["n"] == [20]  # número como hasta ahora (persistencia intacta)
    assert res["s"] == [20]
    assert res["l"] == [9, 20, 50]
    assert res["mix"] == [9, 20, 200]  # ordena y deduplica
    assert res["bad"] == [20] and res["empty"] == [20]
    colors = [res["c9"], res["c20"], res["c50"], res["c100"], res["c200"]]
    assert res["c20"] == "#7c5cff"
    assert len(set(colors)) == 5  # distintos por periodo
    # UI: input amplio con pista + guardado tal cual (sin num()) + títulos
    # de serie "EMA <p>" para identificar cada línea en la leyenda del motor
    # (el Estudio no tiene tooltip propio de indicadores: solo st-ohlc).
    assert 'id="ind-ema-p"' in ESTUDIO and 'size="12"' in ESTUDIO
    assert "9,20,50" in ESTUDIO
    assert "indCfg.ema.p = raw" in ESTUDIO
    assert '"EMA " + p' in ESTUDIO


def test_indicadores_con_cache_y_clave_completa():
    """Repintado sin cambios de datos no recalcula: la clave incluye
    dataset (longitud, primera/última vela, último cierre), mercado,
    temporalidad y parámetros. (Fase 5.1: las series además se REUTILIZAN
    en vez de recrearse —ver test_estudio_fase5.py—; el removeSeries que
    queda es solo el barrido de claves que dejan de estar activas, no la
    recreación sistemática de antes.)"""
    assert "indCache" in ESTUDIO
    for part in ("candles.length", "candles[0].time",
                 "candles[candles.length - 1].time", "market", "timeframe",
                 "paramsJson"):
        assert part in ESTUDIO, part
    assert "indCache.key === ckey" in ESTUDIO
    assert "chart.removeSeries(indSeries[k])" in ESTUDIO
    assert "maybeRefillForIndicators" in ESTUDIO
