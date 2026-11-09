"""Servidor aislado del smoke de navegador LAB con histórico PERP controlado."""

import app.lab.api as lab_api
from app.main import app


class Source:
    known_pairs = ("BTC/USDT:PERP",)

    def get_historical_candles(self, symbol, timeframe, start, end, *, now):
        assert symbol == "BTC/USDT:PERP" and timeframe == "1h" and end - start == 3 * 3600
        return [{"t": start + i * 3600, "o": str(100 + i), "h": str(101 + i),
                 "l": str(99 + i), "c": str(100 + i), "v": "10"} for i in range(3)]


lab_api.GrvtAdapter = Source
