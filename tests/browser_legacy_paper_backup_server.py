"""Servidor PAPER en claro aislado para ensayar una copia cifrada desde Ajustes."""

import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

import uvicorn


root = Path(tempfile.mkdtemp(prefix="orbis-legacy-backup-browser-"))
paper = root / "orbis.db"
with sqlite3.connect(paper) as db:
    db.execute("CREATE TABLE fixture (id INTEGER PRIMARY KEY, note TEXT)")
    db.execute("INSERT INTO fixture (note) VALUES ('PAPER aislado')")
    db.commit()
shutil.copy2(paper, root / "orbis.db.bak")
os.environ["ORBIS_DATABASE_URL"] = f"sqlite:///{paper}"
for name, file in (("ORBIS_LAB_PATH", "lab.db"),
                   ("ORBIS_AUTOMATION_PATH", "automation.db"),
                   ("ORBIS_SPOT_STRATEGY_PATH", "spot.db"),
                   ("ORBIS_PAPER_PERP_PATH", "perp.db"),
                   ("ORBIS_REAL_VAULT_PATH", "real.db")):
    os.environ[name] = str(root / file)
uvicorn.run("app.main:app", host="127.0.0.1", port=8777, log_level="warning")
