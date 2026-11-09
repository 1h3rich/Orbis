from contextlib import closing
import os
from pathlib import Path
import select
import shutil
import sqlite3
import subprocess
import sys
from urllib.request import urlopen

import pytest
import sqlcipher3
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.main as main_module
from app.database.legacy_backup import create_legacy_encrypted_backup
from app.database.legacy_backup import verify_legacy_encrypted_backup
from app.database.legacy_runtime import (
    LegacyDatabaseError, LegacyPaperAccess,
    create_encrypted_legacy_engine,
    create_new_encrypted_legacy_database,
    inspect_legacy_sibling_copies,
    legacy_database_format,
)
from app.main import app


PASSWORD = "clave-segura-paper-local"


def test_memory_legacy_status_cannot_be_locked_as_cipher():
    with TestClient(app) as client:
        assert client.get("/paper/legacy/status").json()["format"] == "memory"
        assert client.post("/paper/legacy/lock").status_code == 409
        assert client.post("/paper/legacy/unlock", json={"passphrase": PASSWORD}).status_code == 409


def test_plaintext_legacy_sibling_inventory_and_git_exclusion(tmp_path, monkeypatch):
    source = tmp_path / "orbis.db"
    with closing(sqlite3.connect(source)) as db:
        db.execute("CREATE TABLE operation (note TEXT)")
        db.execute("INSERT INTO operation VALUES ('private fixture')")
        db.commit()
    for name in ("orbis.db.bak", "orbis.db.bak-older", "orbis.db.orig"):
        shutil.copy2(source, tmp_path / name)
    (tmp_path / "orbis.db.bak-encrypted").write_bytes(b"encrypted fixture")
    (tmp_path / "orbis.db.orig-link").symlink_to(source)
    (tmp_path / "other.db.bak").write_bytes(source.read_bytes())
    inventory = inspect_legacy_sibling_copies(source)
    assert inventory == {
        "plaintext_sibling_copies": ["orbis.db.bak", "orbis.db.bak-older", "orbis.db.orig"],
        "unverified_sibling_copies": ["orbis.db.bak-encrypted", "orbis.db.orig-link"],
        "sibling_scan_complete": True,
    }
    monkeypatch.setattr(main_module, "DATABASE_URL", f"sqlite:///{source}")
    with TestClient(app) as client:
        status = client.get("/paper/legacy/status")
    assert status.status_code == 200
    assert status.headers["cache-control"] == "no-store"
    assert status.json()["plaintext_sibling_copies"] == inventory["plaintext_sibling_copies"]
    assert "private fixture" not in status.text
    project_root = Path(__file__).resolve().parents[1]
    # Source archives and isolated development copies need not contain .git.
    # Exercise the actual project rules in an owned repository, without using
    # the developer's global excludes or initializing the working checkout.
    ignore_root = tmp_path / "ignore-rules"
    ignore_root.mkdir()
    shutil.copyfile(project_root / ".gitignore", ignore_root / ".gitignore")
    git_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    git_env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    subprocess.run(["git", "init", "--quiet"], cwd=ignore_root, env=git_env, check=True)
    for name in ("orbis.db.bak", "orbis.db.bak-2026", "orbis.db.orig", "orbis.db.lock",
                 "paper.sqlite.backup", "paper.sqlite3.bak"):
        ignored = subprocess.run(["git", "-c", "core.excludesFile=" + os.devnull,
                                  "check-ignore", "--no-index", "--quiet", "--", name],
                                 cwd=ignore_root, env=git_env, check=False)
        assert ignored.returncode == 0, name
    visible = subprocess.run(["git", "-c", "core.excludesFile=" + os.devnull,
                              "check-ignore", "--no-index", "--quiet", "--", "README.md"],
                             cwd=ignore_root, env=git_env, check=False)
    assert visible.returncode == 1, "Backup rules must not hide normal project sources"


def test_legacy_paper_api_creates_verified_encrypted_copy_without_migration(tmp_path, monkeypatch):
    source = tmp_path / "orbis.db"
    with closing(sqlite3.connect(source)) as db:
        db.execute("CREATE TABLE operations (id INTEGER PRIMARY KEY, note TEXT)")
        db.execute("INSERT INTO operations (note) VALUES ('PAPER previo')")
        db.commit()
    before = source.read_bytes()
    monkeypatch.setattr(main_module, "DATABASE_URL", f"sqlite:///{source}")
    with TestClient(app) as client:
        endpoint = "/paper/legacy/backup/encrypted"
        body = {"passphrase": PASSWORD, "confirmation": "COPIA PAPER"}
        assert client.post(endpoint, json={**body, "confirmation": "COPIA"}).status_code == 422
        assert client.post(endpoint, json={**body, "passphrase": "corta"}).status_code == 422
        assert client.post(endpoint, json=body,
                           headers={"Origin": "https://externo.invalid"}).status_code == 403
        result = client.post(endpoint, json=body)
        assert result.status_code == 200, result.text
        assert result.headers["cache-control"] == "no-store"
        assert result.json()["encrypted_backup"] is True
        assert result.json()["active_database_unchanged"] is True
        assert result.json()["tables"] == {"operations": 1}
        assert PASSWORD not in result.text
    assert source.read_bytes() == before
    assert legacy_database_format(source) == "plain"
    backup = Path(result.json()["path"])
    assert backup.is_file() and backup.stat().st_mode & 0o777 == 0o600
    assert backup.read_bytes()[:16] != b"SQLite format 3\x00"
    assert verify_legacy_encrypted_backup(backup, PASSWORD)["fingerprint"] == result.json()["fingerprint"]


def test_legacy_paper_api_rejects_memory_database(monkeypatch):
    monkeypatch.setattr(main_module, "DATABASE_URL", "sqlite://")
    with TestClient(app) as client:
        assert client.post("/paper/legacy/backup/encrypted", json={
            "passphrase": PASSWORD, "confirmation": "COPIA PAPER",
        }).status_code == 409


def test_encrypted_paper_backup_requires_unlock_and_preserves_active_database(tmp_path, monkeypatch):
    source = tmp_path / "orbis.db"
    backup_key = "clave-de-copia-independiente"
    create_new_encrypted_legacy_database(source, PASSWORD)
    with closing(sqlcipher3.connect(source)) as db:
        db.execute("PRAGMA key = 'clave-segura-paper-local'")
        db.execute("CREATE TABLE operations (id INTEGER PRIMARY KEY, note TEXT)")
        db.execute("INSERT INTO operations (note) VALUES ('PAPER protegido')")
        db.commit()
    original = source.read_bytes()
    access = LegacyPaperAccess(source)
    monkeypatch.setattr(main_module, "DATABASE_URL", f"sqlite:///{source}")
    monkeypatch.setattr(main_module, "legacy_paper_access", access)
    body = {"passphrase": backup_key, "confirmation": "COPIA PAPER"}
    with TestClient(app) as client:
        endpoint = "/paper/legacy/backup/encrypted"
        assert client.post(endpoint, json=body).status_code == 423
        access.unlock(PASSWORD)
        assert client.post(endpoint, json=body,
                           headers={"Origin": "https://outside.invalid"}).status_code == 403
        saved = client.post(endpoint, json=body)
        assert saved.status_code == 200, saved.text
        assert saved.headers["cache-control"] == "no-store"
        assert saved.json()["active_database_unchanged"] is True
        assert saved.json()["active_database_encrypted"] is True
        assert saved.json()["tables"] == {"operations": 1}
        assert PASSWORD not in saved.text and backup_key not in saved.text
        access.lock()
        assert client.post(endpoint, json=body).status_code == 423
    assert source.read_bytes() == original
    backup = Path(saved.json()["path"])
    assert backup.read_bytes()[:16] != b"SQLite format 3\x00"
    assert verify_legacy_encrypted_backup(backup, backup_key)["fingerprint"] == saved.json()["fingerprint"]


def test_new_paper_is_encrypted_before_first_sqlalchemy_write(tmp_path):
    path = tmp_path / "orbis.db"
    create_new_encrypted_legacy_database(path, PASSWORD)
    assert legacy_database_format(path) == "encrypted_or_invalid"
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(LegacyDatabaseError, match="no se sobrescribirá"):
        create_new_encrypted_legacy_database(path, PASSWORD)
    engine = create_encrypted_legacy_engine(f"sqlite:///{path}", path, PASSWORD)
    try:
        with engine.begin() as db:
            db.execute(text("CREATE TABLE note (value TEXT)"))
            db.execute(text("INSERT INTO note (value) VALUES ('guardado')"))
    finally:
        engine.dispose()
    assert path.read_bytes()[:16] != b"SQLite format 3\x00"
    assert not list(tmp_path.glob(".legacy-init-*"))


def _encrypted_database(tmp_path: Path) -> Path:
    source = tmp_path / "source.db"
    with closing(sqlite3.connect(source)) as db:
        db.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY, note TEXT)")
        db.execute("INSERT INTO entries (note) VALUES ('persistente')")
        db.commit()
    result = create_legacy_encrypted_backup(source, PASSWORD)
    return Path(result["path"])


def test_sqlalchemy_reads_and_writes_encrypted_legacy_database(tmp_path):
    path = _encrypted_database(tmp_path)
    assert legacy_database_format(path) == "encrypted_or_invalid"
    engine = create_encrypted_legacy_engine(f"sqlite:///{path}", path, PASSWORD)
    try:
        with engine.begin() as db:
            assert db.execute(text("SELECT note FROM entries")).scalar_one() == "persistente"
            db.execute(text("INSERT INTO entries (note) VALUES ('segunda')"))
        engine.dispose()
        with engine.connect() as db:
            assert db.execute(text("SELECT count(*) FROM entries")).scalar_one() == 2
    finally:
        engine.dispose()
    assert path.read_bytes()[:16] != b"SQLite format 3\x00"


def test_wrong_password_rejected_without_modifying_database(tmp_path):
    path = _encrypted_database(tmp_path)
    original = path.read_bytes()
    with pytest.raises(LegacyDatabaseError, match="desbloquear"):
        create_encrypted_legacy_engine(f"sqlite:///{path}", path, "contraseña-equivocada")
    assert path.read_bytes() == original


def test_encrypted_paper_key_expires_from_memory(tmp_path):
    path = _encrypted_database(tmp_path)
    now = [100.0]
    access = LegacyPaperAccess(path, timeout_seconds=900, clock=lambda: now[0])
    assert access.unlock(PASSWORD)["unlocked"] is True
    assert access.get() == PASSWORD
    now[0] = 1000.0
    assert access.get() is None
    assert access.status()["unlocked"] is False


def test_application_import_consumes_key_from_fd(tmp_path):
    path = _encrypted_database(tmp_path)
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, PASSWORD.encode())
        os.close(write_fd)
        write_fd = -1
        environment = os.environ.copy()
        environment["ORBIS_DATABASE_URL"] = f"sqlite:///{path}"
        environment["ORBIS_DATABASE_KEY_FD"] = str(read_fd)
        script = (
            "from app.database.database import engine; "
            "from sqlalchemy import text; "
            "db=engine.connect(); "
            "assert db.execute(text('SELECT count(*) FROM entries')).scalar_one() == 1; "
            "assert db.exec_driver_sql('PRAGMA foreign_keys').scalar_one() == 1; "
            "db.close(); engine.dispose()"
        )
        result = subprocess.run([sys.executable, "-c", script], env=environment,
                                pass_fds=(read_fd,), capture_output=True, text=True,
                                timeout=15, check=False)
        assert result.returncode == 0, result.stderr
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)


def test_full_api_starts_with_encrypted_legacy_database(tmp_path):
    path = _encrypted_database(tmp_path)
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, PASSWORD.encode())
        os.close(write_fd)
        write_fd = -1
        environment = os.environ.copy()
        environment.update({
            "ORBIS_DATABASE_URL": f"sqlite:///{path}",
            "ORBIS_DATABASE_KEY_FD": str(read_fd),
            "ORBIS_LAB_PATH": str(tmp_path / "lab.db"),
            "ORBIS_AUTOMATION_PATH": str(tmp_path / "automation.db"),
            "ORBIS_SPOT_STRATEGY_PATH": str(tmp_path / "spot.db"),
            "ORBIS_PAPER_PERP_PATH": str(tmp_path / "perp.db"),
            "ORBIS_REAL_VAULT_PATH": str(tmp_path / "real.db"),
        })
        script = (
            "from app.main import app; "
            "from fastapi.testclient import TestClient; "
            "client=TestClient(app); "
            "assert client.get('/paper/legacy/status').json()['unlocked']; "
            "assert client.get('/strategies').status_code == 200; "
            "assert client.post('/paper/legacy/lock').json()['unlocked'] is False; "
            "assert client.get('/strategies').status_code == 423; "
            "assert client.post('/paper/legacy/unlock', json={'passphrase':'wrong-password-123'}).status_code == 400; "
            f"assert client.post('/paper/legacy/unlock', json={{'passphrase':'{PASSWORD}'}}).json()['unlocked']; "
            "assert client.get('/strategies').status_code == 200"
        )
        result = subprocess.run([sys.executable, "-c", script], env=environment,
                                pass_fds=(read_fd,), capture_output=True, text=True,
                                timeout=30, check=False)
        assert result.returncode == 0, result.stderr
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)
    assert path.read_bytes()[:16] != b"SQLite format 3\x00"


def test_required_encryption_rejects_missing_path_without_creating_sqlite(tmp_path):
    missing = tmp_path / "wrong-name.db"
    environment = os.environ.copy()
    environment["ORBIS_DATABASE_URL"] = f"sqlite:///{missing}"
    environment["ORBIS_DATABASE_REQUIRE_ENCRYPTED"] = "1"
    environment.pop("ORBIS_DATABASE_KEY_FD", None)
    result = subprocess.run(
        [sys.executable, "-c", "import app.database.database"],
        env=environment, capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode != 0
    assert "Se exige PAPER cifrado" in result.stderr
    assert not missing.exists()


def test_direct_default_start_does_not_create_plain_legacy_database(tmp_path):
    environment = os.environ.copy()
    environment.pop("ORBIS_DATABASE_URL", None)
    environment.pop("ORBIS_DATABASE_KEY_FD", None)
    environment.pop("ORBIS_DATABASE_REQUIRE_ENCRYPTED", None)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    result = subprocess.run(
        [sys.executable, "-c", "import app.database.database"],
        cwd=tmp_path, env=environment, capture_output=True, text=True,
        timeout=15, check=False)
    assert result.returncode != 0
    assert "arranque directo no creará SQLite en claro" in result.stderr
    assert not (tmp_path / "orbis.db").exists()


def test_direct_custom_path_does_not_create_plain_paper(tmp_path):
    missing = tmp_path / "custom-paper.db"
    environment = os.environ.copy()
    environment["ORBIS_DATABASE_URL"] = f"sqlite:///{missing}"
    environment.pop("ORBIS_DATABASE_KEY_FD", None)
    environment.pop("ORBIS_DATABASE_REQUIRE_ENCRYPTED", None)
    result = subprocess.run(
        [sys.executable, "-c", "import app.database.database"],
        env=environment, capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode != 0
    assert "arranque directo no creará SQLite en claro" in result.stderr
    assert not missing.exists()


def test_local_launcher_required_encryption_rejects_plain_file(tmp_path):
    source = tmp_path / "orbis.db"
    with closing(sqlite3.connect(source)) as db:
        db.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY)")
        db.commit()
    original = source.read_bytes()
    environment = os.environ.copy()
    environment["ORBIS_DATABASE_URL"] = f"sqlite:///{source}"
    environment.pop("ORBIS_DATABASE_KEY_FD", None)
    result = subprocess.run(
        [sys.executable, "-m", "scripts.run_orbis_local", "--require-encrypted-paper",
         "--terminal-paper-unlock", "--no-browser", "--port", "0"],
        env=environment, capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode != 0
    assert "PAPER cifrado requerido" in result.stderr
    assert source.read_bytes() == original


def test_local_launcher_serves_encrypted_paper_with_password_prompt(tmp_path):
    path = _encrypted_database(tmp_path)
    environment = os.environ.copy()
    environment.update({
        "ORBIS_DATABASE_URL": f"sqlite:///{path}",
        "ORBIS_LAB_PATH": str(tmp_path / "lab.db"),
        "ORBIS_AUTOMATION_PATH": str(tmp_path / "automation.db"),
        "ORBIS_SPOT_STRATEGY_PATH": str(tmp_path / "spot.db"),
        "ORBIS_PAPER_PERP_PATH": str(tmp_path / "perp.db"),
        "ORBIS_REAL_VAULT_PATH": str(tmp_path / "real.db"),
    })
    environment.pop("ORBIS_DATABASE_KEY_FD", None)
    process = subprocess.Popen(
        [sys.executable, "-m", "scripts.run_orbis_local", "--require-encrypted-paper",
         "--terminal-paper-unlock", "--no-browser", "--port", "0"],
        env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(PASSWORD + "\n")
        process.stdin.flush()
        process.stdin.close()
        process.stdin = None
        ready, _, _ = select.select([process.stdout], [], [], 10)
        assert ready, "El lanzador cifrado no anunció la URL local"
        announced = process.stdout.readline().strip()
        assert announced.startswith("ORBIS local: http://127.0.0.1:")
        url = announced.removeprefix("ORBIS local: ").replace("/ui/", "/strategies")
        with urlopen(url, timeout=10) as response:
            assert response.status == 200
            assert response.read().startswith(b"[")
    finally:
        process.terminate()
        try:
            _, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            _, stderr = process.communicate(timeout=5)
    assert PASSWORD not in stderr
    assert path.read_bytes()[:16] != b"SQLite format 3\x00"


@pytest.mark.parametrize("require_flag", [False, True])
def test_local_launcher_creates_encrypted_paper_on_first_use(tmp_path, require_flag):
    path = tmp_path / "new-paper.db"
    environment = os.environ.copy()
    environment["ORBIS_DATABASE_URL"] = f"sqlite:///{path}"
    environment.pop("ORBIS_DATABASE_KEY_FD", None)
    environment.update({
        "ORBIS_LAB_PATH": str(tmp_path / "lab.db"),
        "ORBIS_AUTOMATION_PATH": str(tmp_path / "automation.db"),
        "ORBIS_SPOT_STRATEGY_PATH": str(tmp_path / "spot.db"),
        "ORBIS_PAPER_PERP_PATH": str(tmp_path / "perp.db"),
        "ORBIS_REAL_VAULT_PATH": str(tmp_path / "real.db"),
    })
    command = [sys.executable, "-m", "scripts.run_orbis_local", "--terminal-paper-unlock"]
    if require_flag:
        command.append("--require-encrypted-paper")
    command.extend(["--no-browser", "--port", "0"])
    process = subprocess.Popen(
        command,
        env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(PASSWORD + "\n" + PASSWORD + "\n")
        process.stdin.flush()
        process.stdin.close()
        process.stdin = None
        ready, _, _ = select.select([process.stdout], [], [], 10)
        assert ready, "El primer arranque cifrado no anunció la URL local"
        announced = process.stdout.readline().strip()
        assert announced.startswith("ORBIS local: http://127.0.0.1:")
        url = announced.removeprefix("ORBIS local: ").replace("/ui/", "/strategies")
        with urlopen(url, timeout=10) as response:
            assert response.status == 200
    finally:
        process.terminate()
        try:
            _, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            _, stderr = process.communicate(timeout=5)
    assert PASSWORD not in stderr
    assert legacy_database_format(path) == "encrypted_or_invalid"
