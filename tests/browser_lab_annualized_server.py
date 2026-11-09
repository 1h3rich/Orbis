"""LAB aislado con un año de velas para probar la métrica anualizada."""

from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import FastAPI
import uvicorn

from app.lab.api import ENGINE_VERSION, create_lab_router
from app.lab.engine import run_backtest, validate_candles, validate_strategy
from app.lab.store import LabStore
from app.web.mount import register_ui


ROOT = Path(__file__).resolve().parents[1]
START = 1_514_764_800
DAY = 86_400


def build(path: Path) -> FastAPI:
    raw = [{"t": START + i * DAY, "o": str(100 + i), "h": str(102 + i),
            "l": str(99 + i), "c": str(101 + i), "v": "10"}
           for i in range(367)]
    bars, gaps = validate_candles(raw, "1d", now=START + 369 * DAY)
    store = LabStore(path)
    dataset = store.save_dataset("BTC/USDT", "1d", "fixture-aislada", bars, gaps)
    definition = validate_strategy({"direction": "LONG", "initial_capital": "1000",
        "allocation": "0.2", "fee_rate": "0.001", "slippage_rate": "0.001",
        "stop_loss": "0.03", "take_profit": "0.08",
        "entry": {"kind": "CLOSE_ABOVE_EMA", "period": 10}})
    version = store.create_strategy("Métrica anual", definition)
    store.save_run(dataset["id"], version["version_id"],
                   run_backtest(bars, definition, "1d"), ENGINE_VERSION)
    short = bars[:100]
    short_dataset = store.save_dataset("BTC/USDT", "1d", "fixture-corta", short, [])
    store.save_run(short_dataset["id"], version["version_id"],
                   run_backtest(short, definition, "1d"), ENGINE_VERSION)
    app = FastAPI()
    app.include_router(create_lab_router(path))
    register_ui(app, static_dir=ROOT / "app" / "web" / "static")
    return app


if __name__ == "__main__":
    with TemporaryDirectory(prefix="orbis-lab-annualized-") as directory:
        uvicorn.run(build(Path(directory) / "lab.db"),
                    host="127.0.0.1", port=8797, log_level="warning")
