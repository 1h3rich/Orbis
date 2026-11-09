"""Las bases locales planas no deben quedar legibles por otros usuarios."""

from app.lab.schema import CURRENT_SCHEMA_VERSION
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from app.core.local_sqlite import ensure_private_sqlite
from app.lab.store import LabStore


def test_private_sqlite_creation_and_existing_content(tmp_path):
    path = tmp_path / "private" / "lab.db"
    ensure_private_sqlite(path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE proof (value TEXT NOT NULL)")
        db.execute("INSERT INTO proof VALUES ('preserved')")
    path.chmod(0o640)
    ensure_private_sqlite(path)
    assert path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value FROM proof").fetchone()[0] == "preserved"


def test_private_sqlite_rejects_symlink_without_touching_target(tmp_path):
    target = tmp_path / "target.db"
    target.write_bytes(b"keep")
    link = tmp_path / "link.db"
    link.symlink_to(target)
    with pytest.raises(OSError, match="not a regular file"):
        ensure_private_sqlite(link)
    assert target.read_bytes() == b"keep"


def test_lab_store_uses_private_creation(tmp_path):
    store = LabStore(tmp_path / "nested" / "lab.db")
    with store.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.path.parent.stat().st_mode & 0o777 == 0o700


def test_legacy_sqlalchemy_database_is_tightened_without_data_loss(tmp_path):
    path = tmp_path / "orbis.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE proof (value TEXT NOT NULL)")
        db.execute("INSERT INTO proof VALUES ('preserved')")
    path.chmod(0o640)
    environment = os.environ.copy()
    environment["ORBIS_DATABASE_URL"] = f"sqlite:///{path}"
    result = subprocess.run([sys.executable, "-c", "import app.database.database"],
                            cwd=Path(__file__).resolve().parents[1], env=environment,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value FROM proof").fetchone()[0] == "preserved"
