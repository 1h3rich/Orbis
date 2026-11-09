"""Servidor temporal PAPER SQLCipher para el flujo de copia en navegador."""

import os
from pathlib import Path
import tempfile

import uvicorn

from app.database.legacy_runtime import create_new_encrypted_legacy_database


PASSWORD = "browser paper active encrypted key"

with tempfile.TemporaryDirectory(prefix="orbis-paper-cipher-browser-") as temporary:
    root = Path(temporary)
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
    try:
        os.write(write_fd, PASSWORD.encode("utf-8"))
    finally:
        os.close(write_fd)
    os.environ["ORBIS_DATABASE_KEY_FD"] = str(read_fd)
    uvicorn.run("app.main:app", host="127.0.0.1", port=8778, log_level="warning")
