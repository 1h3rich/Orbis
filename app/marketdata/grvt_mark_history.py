"""Closed MARK history, deliberately separate from GRVT TRADE/cache identities."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.lab.engine import INTERVALS, LabError, number
from app.marketdata.grvt_candle_time import GrvtCandleNotClosed, read_candle_close


def closed_mark_history(post: Callable[[str, dict], Any], *, instrument: str,
                        timeframe: str, start: int, end: int, now: int) -> list[dict]:
    step = INTERVALS.get(timeframe)
    if (step is None or any(type(v) is not int for v in (start, end, now))
            or start <= 0 or end <= start or end > now or start % step or end % step
            or not 3 <= (end-start)//step <= 5000):
        raise LabError("Rango MARK inválido, excesivo o con vela abierta")
    count = (end-start)//step
    expected = set(range(start, end, step))
    rows: dict[int, dict] = {}
    cursor = ""
    seen: set[str] = set()
    for _ in range(50):
        response = post("https://market-data.grvt.io/full/v1/kline", {
            "instrument": instrument, "interval": {"1m":"CI_1_M", "5m":"CI_5_M",
                "15m":"CI_15_M", "1h":"CI_1_H", "4h":"CI_4_H", "1d":"CI_1_D"}[timeframe],
            "type": "MARK", "start_time": str(start*10**9), "end_time": str(end*10**9-1),
            "limit": min(count, 1000), "cursor": cursor})
        if not isinstance(response, dict):
            raise LabError("Respuesta MARK inválida")
        page = response.get("result")
        if not isinstance(page, list) or not 1 <= len(page) <= min(count, 1000):
            raise LabError("Página MARK vacía o malformada")
        for raw in page:
            if not isinstance(raw, dict) or raw.get("instrument") != instrument:
                raise LabError("Vela MARK de otro instrumento")
            stamp, confirmed = read_candle_close(raw.get("open_time"), raw.get("close_time"),
                                                 start=start, end=end, step=step)
            if not confirmed:
                continue
            values = {short: raw.get(long) for short, long in
                      (("o","open"),("h","high"),("l","low"),("c","close"))}
            numeric = {key: number(value, positive=True) for key,value in values.items()}
            if (numeric['h'] < max(numeric.values()) or numeric['l'] > min(numeric.values())):
                raise LabError("OHLC MARK inconsistente")
            candle = {"t": stamp, **values}
            if stamp in rows and rows[stamp] != candle:
                raise LabError("Vela MARK cambió entre páginas")
            rows[stamp] = candle
        if len(rows) == count:
            return [rows[stamp] for stamp in sorted(expected)]
        next_cursor = response.get("next")
        if next_cursor == "" and rows.keys() == expected - {end - step}:
            raise GrvtCandleNotClosed(end * 10**9)
        if not isinstance(next_cursor, str) or not 1 <= len(next_cursor) <= 2048 or next_cursor in seen:
            raise LabError("Histórico MARK incompleto o cursor repetido")
        seen.add(next_cursor)
        cursor = next_cursor
    raise LabError("Demasiadas páginas de histórico MARK")
