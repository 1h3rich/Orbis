"""Servidor aislado SQLCipher para verificar bloqueo PAPER en el navegador."""

import os
from datetime import datetime, timezone
from pathlib import Path
import tempfile

import uvicorn

from app.database.legacy_runtime import create_new_encrypted_legacy_database


PASSWORD = "clave-paper-browser-aislada"
root = Path(tempfile.mkdtemp(prefix="orbis-legacy-browser-"))
paper = root / "orbis.db"
create_new_encrypted_legacy_database(paper, PASSWORD)
os.environ["ORBIS_DATABASE_URL"] = f"sqlite:///{paper}"
os.environ["ORBIS_DATABASE_REQUIRE_ENCRYPTED"] = "1"
for name, file in (("ORBIS_LAB_PATH", "lab.db"),
                   ("ORBIS_AUTOMATION_PATH", "automation.db"),
                   ("ORBIS_SPOT_STRATEGY_PATH", "spot.db"),
                   ("ORBIS_PAPER_PERP_PATH", "perp.db"),
                   ("ORBIS_REAL_VAULT_PATH", "real.db")):
    os.environ[name] = str(root / file)
read_fd, write_fd = os.pipe()
os.write(write_fd, PASSWORD.encode())
os.close(write_fd)
os.environ["ORBIS_DATABASE_KEY_FD"] = str(read_fd)
import app.main as main_module  # noqa: E402


def public_test_quote(symbol: str, provider: str = "auto") -> dict:
    """Sólo este proceso aislado: cotización pública reproducible de prueba."""
    return {
        "available": True, "symbol": symbol, "resolved_provider": "kraken",
        "source": "kraken-test", "price": "100000", "bid": None, "ask": None,
        "received_at": datetime.now(timezone.utc).isoformat(),
        "market_time_at": None, "market_time_status": "unknown",
    }


main_module.market_snapshot = public_test_quote
uvicorn.run("app.main:app", host="127.0.0.1", port=8776, log_level="warning")
