"""Smoke de transporte real: ejecutar con el servidor local ya arrancado.

ORBIS_TEST_WS_URL permite usar un puerto de pruebas y una base temporal.
No usa claves ni crea operaciones.
"""

import asyncio
import json
import os

from websockets.asyncio.client import connect


async def main() -> None:
    url = os.environ.get(
        "ORBIS_TEST_WS_URL",
        "ws://127.0.0.1:8799/market/stream?symbol=BTC%2FUSD&provider=bitstamp",
    )
    async with connect(url, open_timeout=10) as socket:
        first = json.loads(await asyncio.wait_for(socket.recv(), timeout=15))
        assert first["symbol"] == "BTC/USD"
        assert first["resolved_provider"] == "bitstamp" and first["available"] is True
        assert float(first["price"]) > 0
        await socket.send("ping")
        second = json.loads(await asyncio.wait_for(socket.recv(), timeout=15))
        assert second["symbol"] == "BTC/USD" and second["available"] is True
    print("WebSocket real: conexión, snapshot, segundo mensaje y cierre correctos")


if __name__ == "__main__":
    asyncio.run(main())
