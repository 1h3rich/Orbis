"""R1: reset único de la memoria de escala al (re)crear el gráfico.

Contrato: ``applyLayout`` recrea el gráfico con ``autoScale:true`` igual que
``loadAll``; ambas rutas (más la creación en ``ensureChart``) deben dejar las
5 banderas de escala en su valor inicial. Si no, ``priceUserFixed`` heredado
en ``true`` hace que ``guardPriceOnScroll`` se salte su lógica con un gráfico
en automático (estado inconsistente).

La lógica vive una sola vez en ``resetScaleMemory()`` (no duplicada): además
de los 4 flags que ya reseteaba ``loadAll``, incluye ``priceProgTag`` (deuda
señalada: tampoco se reseteaba en ``loadAll``). ``unlockPriceAuto`` (Centrar /
doble clic en el eje) sigue reseteando solo sus 2 flags a propósito: es una
transición en caliente sobre el mismo chart, no una vista nueva.
"""

import re
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "web" / "static"
ESTUDIO = (STATIC_DIR / "estudio.js").read_text(encoding="utf-8")


def _body(name):
    i = ESTUDIO.index("function %s" % name)
    # Hasta la siguiente función vecina de nivel render (ancla estable).
    anchors = ["function ensureChart()", "function loadAll()",
               "/* ---------------- eventos ---------------- */"]
    ends = [ESTUDIO.index(a, i + 1) for a in anchors if a in ESTUDIO[i + 1:]]
    return ESTUDIO[i:min(ends)] if ends else ESTUDIO[i:i + 6000]


def test_reset_scale_memory_resets_all_five_flags():
    """resetScaleMemory deja las 5 banderas en su valor inicial."""
    m = re.search(r"function resetScaleMemory\(\) \{(.*?)\n    \}", ESTUDIO, re.S)
    assert m is not None, "resetScaleMemory no existe"
    body = m.group(1)
    for token in ("priceUserFixed = false", "priceFrozenGap = false",
                  "priceProgTag = 0", "lastInDataPriceRange = null",
                  "navDown = null"):
        assert token in body, token


def test_applylayout_and_loadall_use_single_reset():
    """applyLayout y loadAll usan el reset único (sin lógica duplicada)."""
    assert "resetScaleMemory()" in _body("applyLayout(name, silent)")
    assert "resetScaleMemory()" in _body("loadAll()")
    # ensureChart lo llama solo en la ruta de creación (tras el early return
    # `if (chart) return true;`): los repintados de paintAll reutilizan el
    # chart y no deben borrar una escala fijada por el usuario.
    m = re.search(r"if \(chart\) return true;\n(.*?)var LW = window\.LightweightCharts;",
                  ESTUDIO, re.S)
    assert m is not None and "resetScaleMemory()" in m.group(1)
    # Sin duplicados: solo resetScaleMemory y unlockPriceAuto reasignan
    # priceUserFixed (el segundo, transición en caliente, documentada arriba;
    # la declaración `var` inicial no cuenta como reset).
    assert ESTUDIO.count("priceUserFixed = false;") == 2
    # priceProgTag = 0 aparece en la declaración `var` inicial y solo una vez
    # más: dentro de resetScaleMemory (ningún otro reset parcial lo toca).
    assert ESTUDIO.count("priceProgTag = 0") == 2
