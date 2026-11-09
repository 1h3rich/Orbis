"""Migración offline de DCA/PAPER a SQLCipher con copia cifrada verificada."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from uuid import uuid4

import sqlcipher3

from app.core.local_sqlite import sqlite_file_digest
from app.automation.access import SQLITE_HEADER, automation_format
from app.automation.lease import automation_lease
from app.automation.store import AutomationStore
from app.automation.paper_dca_gap import analyze_dca_paper_gap
from app.lab.backup import _copy_exclusive
from app.lab.engine import LabError
from app.lab.store import content_hash
from app.lab.secure_backup import _export


_TABLES = ("dca_rules", "dca_versions", "dca_runs",
           "paper_dca_sessions", "paper_dca_events")
_JSON_COLUMNS = {
    "dca_versions": ("definition_json",),
    "dca_runs": ("result_json",),
    "paper_dca_sessions": ("rule_json", "state_json", "overlap_json", "last_quote_json"),
    "paper_dca_events": ("event_json",),
    "paper_dca_gap_reports": ("evidence_json",),
    "dca_validation_runs": ("result_json",),
}
_BACKUP_NAME = re.compile(r"(?:automation-secure|pre-restore-automation)-[0-9]{8}-[0-9]{6}-[0-9a-f]{12}\.db\Z")


def verify_automation_database(path: Path, passphrase: str | None = None) -> dict:
    """Huella lógica de todas las filas; compara origen y copia cifrada."""
    path = Path(path).expanduser().absolute()
    if path.is_symlink() or not path.is_file():
        raise LabError("La base de automatizaciones no es un archivo regular")
    try:
        with closing(sqlcipher3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlcipher3.Row
            if passphrase is not None:
                db.execute("PRAGMA key = '" + passphrase.replace("'", "''") + "'")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (3, 4, 5, 6):
                raise LabError("Versión de automatizaciones no compatible")
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise LabError("La base de automatizaciones no supera integridad SQLite")
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise LabError("La base de automatizaciones contiene referencias rotas")
            names = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            tables = (_TABLES + (("paper_dca_gap_reports",) if version >= 4 else ())
                      + (("dca_validation_runs",) if version >= 6 else ()))
            if not set(tables).issubset(names):
                raise LabError("Faltan tablas de automatizaciones")
            counts = {}
            digests = {}
            for table in tables:
                source_rows = db.execute(f"SELECT * FROM {table}").fetchall()
                for row in source_rows:
                    for column in _JSON_COLUMNS.get(table, ()):
                        if row[column] is not None:
                            json.loads(row[column])
                    if table == "paper_dca_gap_reports":
                        material = json.loads(row["evidence_json"])
                        snapshot = material["snapshot"]
                        if (content_hash(material) != row["evidence_hash"]
                                or snapshot["id"] != row["session_id"]
                                or analyze_dca_paper_gap(snapshot, material["downloaded"],
                                                         now=snapshot["reconstruction_now"]) != material["result"]):
                            raise LabError("Informe DCA archivado alterado")
                rows = [list(row) for row in source_rows]
                counts[table] = len(rows)
                encoded = json.dumps(sorted(rows, key=lambda row: json.dumps(row, ensure_ascii=False)),
                                     ensure_ascii=False, separators=(",", ":"))
                digests[table] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            return {"schema_version": version, "counts": counts, "table_digests": digests}
    except LabError:
        raise
    except (sqlcipher3.DatabaseError, sqlite3.DatabaseError, OSError, ValueError, TypeError):
        raise LabError("La base de automatizaciones no se pudo verificar") from None


def _identity(path: Path) -> tuple[int, int, int, int]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise LabError("La base de automatizaciones no es un archivo regular")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _no_journals(path: Path) -> None:
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise LabError("Automatizaciones conserva journals; cierra Orbis antes de cifrar")


def _sync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def encrypt_active_automation(store: AutomationStore, confirmation: str,
                              passphrase: str) -> dict:
    """Reemplaza sólo tras copia SQLCipher íntegra, lease y bloqueo exclusivo."""
    if confirmation != "CIFRAR DCA":
        raise LabError("Debes confirmar exactamente CIFRAR DCA")
    if not isinstance(passphrase, str) or not 12 <= len(passphrase) <= 1024:
        raise LabError("La contraseña de automatizaciones debe tener entre 12 y 1024 caracteres")
    if store.current_passphrase() is not None:
        raise LabError("La migración requiere automatizaciones planas sin clave activa")
    path = store.path
    with automation_lease(path, exclusive=True):
        if automation_format(path) != "plain":
            raise LabError("No existe una base de automatizaciones plana que cifrar")
        _no_journals(path)
        original = _identity(path)
        original_file = sqlite_file_digest(path)
        if original_file[:4] != original:
            raise LabError("Automatizaciones cambió antes del respaldo; no se sustituyó")
        verified = verify_automation_database(path)
        directory = path.parent / "automation-backups"
        if directory.is_symlink():
            raise LabError("La carpeta de copias de automatizaciones no puede ser un enlace")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        nonce = uuid4().hex[:12]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        staged = path.with_name(f".{path.name}.encrypt-{nonce}")
        backup = directory / f"automation-secure-{stamp}-{nonce}.db"
        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(staged, flags, 0o600)
        os.close(descriptor)
        replaced = False
        try:
            _export(path, staged, source_password=None, destination_password=passphrase)
            if verify_automation_database(staged, passphrase) != verified:
                raise LabError("La copia cifrada de automatizaciones difiere del origen")
            with staged.open("rb") as encrypted:
                if encrypted.read(16) == SQLITE_HEADER:
                    raise LabError("Automatizaciones no quedó cifrada")
            _copy_exclusive(staged, backup)
            with backup.open("rb") as safe_file:
                os.fsync(safe_file.fileno())
            _sync_dir(directory)
            with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True,
                                         timeout=0, isolation_level=None)) as guard:
                try:
                    guard.execute("BEGIN EXCLUSIVE")
                except sqlite3.Error:
                    raise LabError("Otro proceso usa automatizaciones; no se sustituyó") from None
                _no_journals(path)
                if _identity(path) != original:
                    raise LabError("Automatizaciones cambió durante la migración; no se sustituyó")
                if sqlite_file_digest(path) != original_file:
                    raise LabError("Automatizaciones cambió durante la migración; no se sustituyó")
                os.replace(staged, path)
                replaced = True
                path.chmod(0o600)
                _sync_dir(path.parent)
            return {"encrypted": True, "backup": backup.name,
                    "verified": verified, "automation_path": str(path)}
        except LabError:
            raise
        except (sqlcipher3.DatabaseError, sqlite3.DatabaseError, OSError, ValueError):
            raise LabError("Automatizaciones se cifró, pero falló la sincronización final; revisa la copia"
                           if replaced else
                           "No se pudo cifrar automatizaciones; revisa la copia previa") from None
        finally:
            staged.unlink(missing_ok=True)


def restore_secure_automation(store: AutomationStore, name: str,
                              confirmation: str, passphrase: str) -> dict:
    """Restaura con Orbis cerrado y conserva preimagen cifrada del activo."""
    if confirmation != "RESTAURAR DCA":
        raise LabError("Debes confirmar exactamente RESTAURAR DCA")
    if not isinstance(passphrase, str) or not 12 <= len(passphrase) <= 1024:
        raise LabError("La contraseña de automatizaciones debe tener entre 12 y 1024 caracteres")
    if not isinstance(name, str) or not _BACKUP_NAME.fullmatch(name):
        raise LabError("Selecciona una copia cifrada de automatizaciones gestionada")
    path = store.path
    directory = path.parent / "automation-backups"
    if directory.is_symlink() or not directory.is_dir():
        raise LabError("No existe la carpeta de copias de automatizaciones")
    backup = directory / name
    with automation_lease(path, exclusive=True):
        kind = automation_format(path)
        if kind not in ("missing", "encrypted_or_invalid"):
            raise LabError("La base activa de automatizaciones no está cifrada; no se sustituirá")
        _no_journals(path)
        expected = verify_automation_database(backup, passphrase)
        previous = None
        nonce = uuid4().hex[:12]
        staged = path.with_name(f".{path.name}.restore-{nonce}")
        replaced = False
        try:
            _copy_exclusive(backup, staged)
            if verify_automation_database(staged, passphrase) != expected:
                raise LabError("La copia de automatizaciones cambió durante la restauración")
            original = _identity(path) if kind != "missing" else None
            guard = None
            if original is not None:
                original_file = sqlite_file_digest(path)
                if original_file[:4] != original:
                    raise LabError("Automatizaciones cambió antes de la preimagen")
                original_verified = verify_automation_database(path, passphrase)
                # La copia previa es un archivo distinto, no un enlace duro al activo.
                stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                previous = directory / f"pre-restore-automation-{stamp}-{nonce}.db"
                _copy_exclusive(path, previous)
                previous.chmod(0o600)
                if verify_automation_database(previous, passphrase) != original_verified:
                    raise LabError("La preimagen DCA difiere del activo verificado")
                _sync_dir(directory)
                try:
                    guard = sqlcipher3.connect(path.as_uri() + "?mode=rw", uri=True,
                                               timeout=0, isolation_level=None)
                    guard.execute("PRAGMA key = '" + passphrase.replace("'", "''") + "'")
                    guard.execute("BEGIN EXCLUSIVE")
                except sqlcipher3.DatabaseError:
                    raise LabError("Otro proceso usa automatizaciones o la clave activa difiere") from None
                _no_journals(path)
                if _identity(path) != original:
                    raise LabError("Automatizaciones cambió durante la restauración")
                if sqlite_file_digest(path) != original_file:
                    raise LabError("Automatizaciones cambió durante la restauración")
            os.replace(staged, path)
            replaced = True
            path.chmod(0o600)
            _sync_dir(path.parent)
            return {"restored": True, "backup": name,
                    "previous_backup": previous.name if previous else None,
                    "verified": expected}
        except LabError:
            raise
        except (sqlcipher3.DatabaseError, sqlite3.DatabaseError, OSError, ValueError):
            raise LabError("Automatizaciones se restauró, pero falló la sincronización final; revisa la preimagen"
                           if replaced else
                           "No se pudo restaurar automatizaciones; el activo se conservó") from None
        finally:
            if guard is not None:
                guard.close()
            staged.unlink(missing_ok=True)
