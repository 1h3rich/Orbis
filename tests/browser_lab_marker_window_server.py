"""Servidor LAB aislado para probar ventanas de marcas con la API real."""

from pathlib import Path
from tempfile import TemporaryDirectory
import os

from fastapi import FastAPI
import uvicorn

from app.lab.api import ENGINE_VERSION, create_lab_router
from app.lab.engine import run_backtest, validate_candles, validate_strategy
from app.lab.store import LabStore
from app.web.mount import register_ui


ROOT = Path(__file__).resolve().parents[1]
START = 1_514_764_800  # 2018-01-01 UTC
STEP = 86_400
COUNT = 2_200
PASSWORD = "Fixture LAB range 22"


def app_with_history(path: Path, *, full: bool = False, encrypted: bool = False) -> FastAPI:
    if full:
        count, start, step, timeframe = 20_000, 1_640_995_200, 3600, "1h"
        prices = [100 + (i // 2000 if i % 2000 < 1950 else 2000 - i % 2000) / 10 for i in range(count)]
        raw = [{"t":start+i*step,"o":f"{value:.2f}","h":f"{value+1:.2f}","l":f"{value-1:.2f}",
                "c":f"{value+(0.3 if i%2000<1950 else -0.3):.2f}","v":"10"} for i,value in enumerate(prices)]
        entry = {"kind":"BEARISH_STREAK","period":2}
    else:
        count, start, step, timeframe = COUNT, START, STEP, "1d"
        raw = [{"t":start+i*step,"o":str(100+i),"h":str(102+i),"l":str(99+i),"c":str(101+i),"v":"10"}
               for i in range(count)]
        entry = {"kind":"CLOSE_ABOVE_EMA","period":10}
    bars, gaps = validate_candles(raw, timeframe, now=start + (count + 2) * step)
    store = LabStore(path, passphrase=PASSWORD if encrypted else None)
    dataset = store.save_dataset("BTC/USDT", timeframe, "fixture-aislada", bars, gaps)
    definition = validate_strategy({"direction": "LONG", "initial_capital": "1000",
        "allocation": "0.2", "fee_rate": "0.001", "slippage_rate": "0.001",
        "stop_loss": "0.03", "take_profit": "0.001",
        "entry": entry})
    version = store.create_strategy("Ventanas de marcas", definition)
    result = run_backtest(bars, definition, timeframe)
    store.save_run(dataset["id"], version["version_id"], result, ENGINE_VERSION)
    app = FastAPI()
    app.include_router(create_lab_router(path))
    register_ui(app, static_dir=ROOT / "app" / "web" / "static")
    return app


if os.environ.get("ORBIS_TEST_LAB_WINDOW"):
    app = app_with_history(Path(os.environ["ORBIS_LAB_PATH"]),
                           full=os.environ["ORBIS_TEST_LAB_WINDOW"] == "lab_full_window", encrypted=True)


if __name__ == "__main__":
    with TemporaryDirectory(prefix="orbis-lab-markers-") as directory:
        uvicorn.run(app_with_history(Path(directory) / "lab.db"),
                    host="127.0.0.1", port=8798, log_level="warning")
