"""Estudio no dibuja una señal MACD antes del calentamiento completo."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_estudio_ema import _extract_fn


SOURCE = (Path(__file__).resolve().parent.parent / "app/web/static/estudio.js").read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("node") is None, reason="node no disponible")
def test_macd_signal_warmup_matches_lab_and_zero_padding_is_not_used():
    code = "\n".join(_extract_fn(SOURCE, name) for name in ("num", "ema", "macd"))
    prices = [10, 10, 10, 10, 10, 12, 14, 16, 18, 17, 15, 13, 11, 9, 11, 13, 15, 17]
    code += "\nconsole.log(JSON.stringify(macd(" + json.dumps(prices) + ", 2, 3, 2)));"
    result = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    values = json.loads(result.stdout)
    assert values["line"][:2] == [None, None]
    assert values["line"][2] == 0
    assert values["signal"][:3] == [None, None, None]
    assert values["signal"][3] == 0
    assert values["line"][5] > values["signal"][5]
    assert values["line"][9] < values["signal"][9]


@pytest.mark.skipif(shutil.which("node") is None, reason="node no disponible")
def test_standard_macd_12_26_9_first_signal_is_at_bar_34():
    code = "\n".join(_extract_fn(SOURCE, name) for name in ("num", "ema", "macd"))
    code += "\nconsole.log(JSON.stringify(macd(Array.from({length:40}, (_,i)=>100+i), 12, 26, 9)));"
    result = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    values = json.loads(result.stdout)
    assert values["line"][:25] == [None] * 25
    assert values["signal"][:33] == [None] * 33
    assert values["line"][25] is not None
    assert values["signal"][33] is not None
