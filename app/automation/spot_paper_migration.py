"""Migración offline de Spot PAPER a SQLCipher con copia verificable."""

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
from app.core.paper_archive import full_archive_matches, observed_archive_matches
from app.core.paper_archive_chain import chained_archive_matches
from app.lab.paper_indicator_state import paper_indicator_state_matches
from app.lab.engine import paper_precision_for_engine
from app.automation.spot_paper_access import SQLITE_HEADER, SpotPaperAccessError, spot_paper_format
from app.automation.spot_paper_lease import spot_paper_lease
from app.automation.spot_strategy_paper import SpotStrategyPaper
from app.lab.backup import _copy_exclusive
from app.lab.engine import LabError
from app.lab.secure_backup import _export


BACKUP_NAME = re.compile(r"(?:spot-paper-secure|pre-restore-spot-paper)-[0-9]{8}-[0-9]{6}-[0-9a-f]{12}\.db\Z")


def verify_spot_paper_database(path: Path, passphrase: str | None = None) -> dict:
    path = Path(path).expanduser().absolute()
    if path.is_symlink() or not path.is_file():
        raise SpotPaperAccessError("La base Spot PAPER no es un archivo regular")
    try:
        with closing(sqlcipher3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlcipher3.Row
            if passphrase is not None:
                db.execute("PRAGMA key = '" + passphrase.replace("'", "''") + "'")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (1, 2, 3, 4, 5, 6):
                raise SpotPaperAccessError("Versión Spot PAPER no compatible")
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise SpotPaperAccessError("Integridad SQLite Spot PAPER inválida")
            db.execute("PRAGMA foreign_keys=ON")
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise SpotPaperAccessError("Spot PAPER contiene referencias rotas")
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required = ({"sessions", "events"}
                        | ({"observed_candles"} if version >= 3 else set())
                        | ({"session_candles"} if version >= 5 else set())
                        | ({"session_archive_chain"} if version >= 6 else set()))
            if not required.issubset(names):
                raise SpotPaperAccessError("Faltan tablas Spot PAPER")
            counts, digests = {}, {}
            session_fields = ("definition_json", "history_json", "state_json", "last_quote_json")
            if version >= 4:
                session_fields += ("indicator_state_json",)
            tables = (("sessions", session_fields),
                      ("events", ("payload_json",)))
            if version >= 3:
                tables += (("observed_candles", ("candle_json",)),)
            if version >= 5:
                tables += (("session_candles", ("candle_json",)),)
            if version >= 6:
                tables += (("session_archive_chain", ()),)
            for table, fields in tables:
                source_rows = db.execute(f"SELECT * FROM {table}").fetchall()
                for row in source_rows:
                    for field in fields:
                        if row[field] is not None:
                            json.loads(row[field])
                rows = [list(row) for row in source_rows]
                counts[table] = len(rows)
                data = json.dumps(sorted(rows, key=lambda row: json.dumps(row, ensure_ascii=False)),
                                  ensure_ascii=False, separators=(",", ":"))
                digests[table] = hashlib.sha256(data.encode("utf-8")).hexdigest()
            if version >= 3:
                for session in db.execute("SELECT id,history_json FROM sessions"):
                    if not observed_archive_matches(db, session["id"], session["history_json"], "spot"):
                        raise SpotPaperAccessError("Spot PAPER contiene velas observadas divergentes")
            if version >= 4:
                for session in db.execute("""SELECT definition_json,history_json,indicator_state_json,
                                                    engine_version FROM sessions"""):
                    if not paper_indicator_state_matches(
                            json.loads(session["indicator_state_json"]),
                            json.loads(session["definition_json"]),
                            json.loads(session["history_json"]),
                            paper_precision_for_engine(session["engine_version"])):
                        raise SpotPaperAccessError("Spot PAPER contiene indicadores incrementales divergentes")
            if version >= 5:
                for session in db.execute("SELECT id,history_json FROM sessions"):
                    if not full_archive_matches(db, session["id"], session["history_json"], "spot"):
                        raise SpotPaperAccessError("Spot PAPER contiene archivo completo divergente")
            if version >= 6:
                for session in db.execute("SELECT id FROM sessions"):
                    if not chained_archive_matches(db, session["id"]):
                        raise SpotPaperAccessError("Spot PAPER contiene cadena de velas divergente")
            return {"schema_version": version, "counts": counts, "table_digests": digests}
    except SpotPaperAccessError:
        raise
    except (sqlcipher3.DatabaseError, sqlite3.DatabaseError, OSError, ValueError, TypeError):
        raise SpotPaperAccessError("Spot PAPER no se pudo verificar") from None


def _password(passphrase: str) -> None:
    if not isinstance(passphrase, str) or not 12 <= len(passphrase) <= 1024:
        raise SpotPaperAccessError("La contraseña Spot PAPER debe tener entre 12 y 1024 caracteres")


def _identity(path: Path) -> tuple[int, int, int, int]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise SpotPaperAccessError("La base Spot PAPER no es un archivo regular")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _no_journals(path: Path) -> None:
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise SpotPaperAccessError("Spot PAPER conserva journals; cierra Orbis antes de migrar")


def _sync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _backup_dir(path: Path) -> Path:
    directory = path.parent / "spot-paper-backups"
    if directory.is_symlink():
        raise SpotPaperAccessError("La carpeta de copias Spot PAPER no puede ser un enlace")
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not directory.is_dir():
            raise SpotPaperAccessError("La carpeta de copias Spot PAPER no está disponible")
        directory.chmod(0o700)
    except OSError:
        raise SpotPaperAccessError("La carpeta de copias Spot PAPER no está disponible") from None
    return directory


def _copy_private(source: Path, destination: Path) -> None:
    try:
        _copy_exclusive(source, destination)
    except LabError:
        raise SpotPaperAccessError("No se pudo copiar Spot PAPER de forma privada") from None


def encrypt_active_spot_paper(paper: SpotStrategyPaper, confirmation: str,
                              passphrase: str) -> dict:
    if confirmation != "CIFRAR SPOT PAPER":
        raise SpotPaperAccessError("Debes confirmar exactamente CIFRAR SPOT PAPER")
    _password(passphrase)
    if paper.current_passphrase() is not None:
        raise SpotPaperAccessError("La migración requiere Spot PAPER plano sin clave activa")
    path = paper.path
    with spot_paper_lease(path, exclusive=True):
        if spot_paper_format(path) != "plain":
            raise SpotPaperAccessError("No existe una base Spot PAPER plana que cifrar")
        _no_journals(path)
        original = _identity(path)
        original_file = sqlite_file_digest(path)
        if original_file[:4] != original:
            raise SpotPaperAccessError("Spot PAPER cambió antes del respaldo")
        verified = verify_spot_paper_database(path)
        directory = _backup_dir(path)
        nonce = uuid4().hex[:12]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        staged = path.with_name(f".{path.name}.encrypt-{nonce}")
        backup = directory / f"spot-paper-secure-{stamp}-{nonce}.db"
        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(staged, flags, 0o600)
        os.close(descriptor)
        replaced = False
        try:
            _export(path, staged, source_password=None, destination_password=passphrase)
            if verify_spot_paper_database(staged, passphrase) != verified:
                raise SpotPaperAccessError("La copia cifrada Spot PAPER difiere del origen")
            with staged.open("rb") as encrypted:
                if encrypted.read(16) == SQLITE_HEADER:
                    raise SpotPaperAccessError("Spot PAPER no quedó cifrado")
            _copy_private(staged, backup)
            with backup.open("rb") as safe_file:
                os.fsync(safe_file.fileno())
            _sync_dir(directory)
            with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True,
                                         timeout=0, isolation_level=None)) as guard:
                try:
                    guard.execute("BEGIN EXCLUSIVE")
                except sqlite3.Error:
                    raise SpotPaperAccessError("Otro proceso usa Spot PAPER; no se sustituyó") from None
                _no_journals(path)
                if _identity(path) != original:
                    raise SpotPaperAccessError("Spot PAPER cambió durante la migración")
                if sqlite_file_digest(path) != original_file:
                    raise SpotPaperAccessError("Spot PAPER cambió durante la migración")
                os.replace(staged, path)
                replaced = True
                path.chmod(0o600)
                _sync_dir(path.parent)
            return {"encrypted": True, "backup": backup.name,
                    "verified": verified, "paper_path": str(path)}
        except SpotPaperAccessError:
            raise
        except (sqlcipher3.DatabaseError, sqlite3.DatabaseError, OSError, ValueError):
            raise SpotPaperAccessError("Spot PAPER se cifró, pero falló la sincronización; revisa la copia"
                                       if replaced else "No se pudo cifrar Spot PAPER; revisa la copia") from None
        finally:
            staged.unlink(missing_ok=True)


def restore_secure_spot_paper(paper: SpotStrategyPaper, name: str,
                              confirmation: str, passphrase: str) -> dict:
    if confirmation != "RESTAURAR SPOT PAPER":
        raise SpotPaperAccessError("Debes confirmar exactamente RESTAURAR SPOT PAPER")
    _password(passphrase)
    if paper.current_passphrase() is not None:
        raise SpotPaperAccessError("Bloquea Spot PAPER antes de restaurar una copia")
    if not isinstance(name, str) or not BACKUP_NAME.fullmatch(name):
        raise SpotPaperAccessError("Selecciona una copia cifrada Spot PAPER gestionada")
    path = paper.path
    directory = path.parent / "spot-paper-backups"
    if directory.is_symlink() or not directory.is_dir():
        raise SpotPaperAccessError("No existe la carpeta de copias Spot PAPER")
    backup = directory / name
    with spot_paper_lease(path, exclusive=True):
        kind = spot_paper_format(path)
        if kind not in ("missing", "encrypted_or_invalid"):
            raise SpotPaperAccessError("La base Spot PAPER activa no está cifrada")
        _no_journals(path)
        expected = verify_spot_paper_database(backup, passphrase)
        nonce = uuid4().hex[:12]
        staged = path.with_name(f".{path.name}.restore-{nonce}")
        previous = None
        guard = None
        replaced = False
        try:
            _copy_private(backup, staged)
            if verify_spot_paper_database(staged, passphrase) != expected:
                raise SpotPaperAccessError("La copia Spot PAPER cambió durante la restauración")
            original = _identity(path) if kind != "missing" else None
            if original is not None:
                original_file = sqlite_file_digest(path)
                if original_file[:4] != original:
                    raise SpotPaperAccessError("Spot PAPER cambió antes de la preimagen")
                current = verify_spot_paper_database(path, passphrase)
                stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                previous = directory / f"pre-restore-spot-paper-{stamp}-{nonce}.db"
                _copy_private(path, previous)
                if verify_spot_paper_database(previous, passphrase) != current:
                    raise SpotPaperAccessError("La preimagen Spot PAPER difiere del activo")
                _sync_dir(directory)
                try:
                    guard = sqlcipher3.connect(path.as_uri() + "?mode=rw", uri=True,
                                               timeout=0, isolation_level=None)
                    guard.execute("PRAGMA key = '" + passphrase.replace("'", "''") + "'")
                    guard.execute("BEGIN EXCLUSIVE")
                except sqlcipher3.DatabaseError:
                    raise SpotPaperAccessError("Otro proceso usa Spot PAPER o la clave difiere") from None
                _no_journals(path)
                if _identity(path) != original:
                    raise SpotPaperAccessError("Spot PAPER cambió durante la restauración")
                if sqlite_file_digest(path) != original_file:
                    raise SpotPaperAccessError("Spot PAPER cambió durante la restauración")
            os.replace(staged, path)
            replaced = True
            path.chmod(0o600)
            _sync_dir(path.parent)
            return {"restored": True, "backup": name,
                    "previous_backup": previous.name if previous else None,
                    "verified": expected}
        except SpotPaperAccessError:
            raise
        except (sqlcipher3.DatabaseError, sqlite3.DatabaseError, OSError, ValueError):
            raise SpotPaperAccessError("Spot PAPER se restauró; revisa la preimagen"
                                       if replaced else "No se pudo restaurar Spot PAPER; se conservó el activo") from None
        finally:
            if guard is not None:
                guard.close()
            staged.unlink(missing_ok=True)
