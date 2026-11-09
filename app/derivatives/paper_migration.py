"""Cifrado y restauración offline de GRVT PAPER, con verificación lógica."""

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
from app.lab.engine import paper_precision_for_engine
from app.lab.paper_indicator_state import paper_indicator_state_matches
from app.derivatives.paper_access import SQLITE_HEADER, paper_perp_access,paper_perp_format
from app.derivatives.paper_lease import paper_perp_lease
from app.derivatives.paper_math import PaperPerpError
from app.derivatives.paper_store import PaperPerpStore
from app.derivatives.paper_schema import READABLE_PAPER_SCHEMAS
from app.lab.backup import _copy_exclusive
from app.lab.engine import LabError
from app.lab.secure_backup import _export


TABLES = ("paper_perp_accounts", "paper_perp_positions", "paper_perp_events",
          "paper_perp_requests", "paper_signal_sessions", "paper_signal_observations",
          "paper_perp_funding_events", "paper_signal_actions")
ANCHOR_TABLES = ("paper_perp_funding_anchors", "paper_perp_funding_legacy")
JSON_COLUMNS = {
    "paper_perp_positions": ("margin_rules_json", "instrument_spec_json"),
    "paper_perp_events": ("details_json",),
    "paper_perp_requests": ("response_json",),
    "paper_signal_sessions": ("definition_json", "candles_json", "indicator_state_json"),
    "paper_signal_observations": ("evidence_json",),
    "paper_signal_candles": ("candle_json",),
    "paper_signal_all_candles": ("candle_json",),
}
BACKUP_NAME = re.compile(
    r"(?:paper-perp-secure|pre-restore-paper-perp)-[0-9]{8}-[0-9]{6}-[0-9a-f]{12}\.db\Z")


def verify_paper_perp_database(path: Path, passphrase: str | None = None) -> dict:
    path = Path(path).expanduser().absolute()
    if path.is_symlink() or not path.is_file():
        raise PaperPerpError("La base GRVT PAPER no es un archivo regular")
    try:
        with closing(sqlcipher3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlcipher3.Row
            if passphrase is not None:
                db.execute("PRAGMA key = '" + passphrase.replace("'", "''") + "'")
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN')
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in READABLE_PAPER_SCHEMAS:
                raise PaperPerpError("Versión GRVT PAPER no compatible")
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise PaperPerpError("Integridad SQLite GRVT PAPER inválida")
            db.execute("PRAGMA foreign_keys=ON")
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise PaperPerpError("GRVT PAPER contiene referencias rotas")
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            tables = (TABLES + (ANCHOR_TABLES if version >= 7 else ())
                      + (("paper_signal_candles",) if version >= 9 else ())
                      + (("paper_signal_all_candles",) if version >= 11 else ())
                      + (("paper_signal_archive_chain",) if version >= 12 else ()))
            if version >= 13:
                from app.derivatives.perp_observed_archive_format import TABLES as observed_tables
                tables += observed_tables
            if version >= 14:
                tables += ('paper_observed_gap_closes',)
            if not set(tables).issubset(names):
                raise PaperPerpError("Faltan tablas GRVT PAPER")
            counts, digests = {}, {}
            for table in tables:
                if table.startswith('paper_observed_'):
                    order={'paper_observed_sessions':'id','paper_observed_events':'session_id,sequence',
                           'paper_observed_candles':'session_id,candle_t','paper_observed_trades':'session_id,trade_index',
                           'paper_observed_gap_closes':'session_id'}[table]
                    digest=hashlib.sha256(b'ORBIS_OBSERVED_ROWS_V1\n');count=0
                    for row in db.execute(f'SELECT * FROM {table} ORDER BY {order}'):
                        cells=[{'blob_sha256':hashlib.sha256(cell).hexdigest(),'bytes':len(cell)}
                               if type(cell) is bytes else cell for cell in row]
                        digest.update(json.dumps(cells,ensure_ascii=False,separators=(',',':')).encode('utf-8'))
                        digest.update(b'\n');count+=1
                    counts[table]=count;digests[table]=digest.hexdigest()
                    continue
                source_rows = db.execute(f"SELECT * FROM {table}").fetchall()
                for row in source_rows:
                    for field in JSON_COLUMNS.get(table, ()):
                        if field == "indicator_state_json" and version < 10:
                            continue
                        if row[field] is not None:
                            json.loads(row[field])
                rows = [[{'blob_sha256':hashlib.sha256(cell).hexdigest(),'bytes':len(cell)}
                         if type(cell) is bytes else cell for cell in row] for row in source_rows]
                counts[table] = len(rows)
                encoded = json.dumps(sorted(rows, key=lambda row: json.dumps(row, ensure_ascii=False)),
                                     ensure_ascii=False, separators=(",", ":"))
                digests[table] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            if version >= 9:
                for session in db.execute("SELECT id,candles_json FROM paper_signal_sessions"):
                    if not observed_archive_matches(db, session["id"], session["candles_json"], "grvt"):
                        raise PaperPerpError("GRVT PAPER contiene velas observadas divergentes")
                    if version >= 11 and not full_archive_matches(
                            db, session["id"], session["candles_json"], "grvt"):
                        raise PaperPerpError("GRVT PAPER contiene archivo completo divergente")
                    if version >= 12 and not chained_archive_matches(db, session["id"], "grvt"):
                        raise PaperPerpError("GRVT PAPER contiene cadena de velas divergente")
            if version >= 10:
                for session in db.execute("""SELECT definition_json,candles_json,indicator_state_json,
                                                    engine_version FROM paper_signal_sessions"""):
                    if not paper_indicator_state_matches(
                            json.loads(session["indicator_state_json"]),
                            json.loads(session["definition_json"]),
                            json.loads(session["candles_json"]),
                            paper_precision_for_engine(session["engine_version"])):
                        raise PaperPerpError("GRVT PAPER contiene indicadores incrementales divergentes")
            if version >= 13:
                from app.derivatives.perp_observed_archive_format import replay_observed_session
                for header in db.execute('SELECT * FROM paper_observed_sessions'):
                    replay_observed_session(db,header)
            if version >= 14:
                from app.derivatives.perp_gap_close_format import replay_gap_close
                for row in db.execute('SELECT * FROM paper_observed_gap_closes'):
                    replay_gap_close(db,row)
            return {"schema_version": version, "counts": counts, "table_digests": digests}
    except PaperPerpError:
        raise
    except (sqlcipher3.DatabaseError, sqlite3.DatabaseError, OSError, ValueError, TypeError):
        raise PaperPerpError("GRVT PAPER no se pudo verificar") from None


def _password(passphrase: str) -> None:
    if not isinstance(passphrase, str) or not 12 <= len(passphrase) <= 1024:
        raise PaperPerpError("La contraseña GRVT PAPER debe tener entre 12 y 1024 caracteres")


def _identity(path: Path) -> tuple[int, int, int, int]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise PaperPerpError("La base GRVT PAPER no es un archivo regular")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _no_journals(path: Path) -> None:
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise PaperPerpError("GRVT PAPER conserva journals; cierra Orbis antes de migrar")


def _sync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _backup_dir(path: Path) -> Path:
    directory = path.parent / "paper-perp-backups"
    if directory.is_symlink():
        raise PaperPerpError("La carpeta de copias GRVT PAPER no puede ser un enlace")
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not directory.is_dir():
            raise PaperPerpError("La carpeta de copias GRVT PAPER no está disponible")
        directory.chmod(0o700)
    except OSError:
        raise PaperPerpError("La carpeta de copias GRVT PAPER no está disponible") from None
    return directory


def _copy_private(source: Path, destination: Path) -> None:
    try:
        _copy_exclusive(source, destination)
    except LabError:
        raise PaperPerpError("No se pudo copiar GRVT PAPER de forma privada") from None


def create_secure_paper_perp_backup(store: PaperPerpStore,passphrase: str) -> dict:
    """Online cipher-to-cipher snapshot with its own key; never a plaintext file.

    SQLCipher >=4.3 supports backup between encrypted databases with matching
    cipher settings. Both connections use this installed version's defaults;
    this is not a cipher-version migration. Verify the copied snapshot, not a
    later, possibly changed, source balance.
    """
    _password(passphrase)
    path=store.path;key=store.current_passphrase();epoch=paper_perp_access.epoch(path)
    if key is None or paper_perp_format(path)!='encrypted_or_invalid':
        raise PaperPerpError('La copia requiere GRVT PAPER cifrado y desbloqueado')
    identity=_identity(path)[:2];directory=_backup_dir(path)
    name='paper-perp-secure-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')+'-'+uuid4().hex[:12]+'.db'
    target=directory/name;staged=directory/('.pending-'+name)
    flags=os.O_CREAT|os.O_EXCL|os.O_RDWR
    if hasattr(os,'O_NOFOLLOW'):flags|=os.O_NOFOLLOW
    descriptor=os.open(staged,flags,0o600);os.close(descriptor)
    published=False
    try:
        with store.connect() as source,closing(sqlcipher3.connect(staged)) as destination:
            destination.execute("PRAGMA key='"+passphrase.replace("'","''")+"'")
            source.backup(destination,pages=256,sleep=.01)
        verified=verify_paper_perp_database(staged,passphrase)
        with staged.open('rb') as source:
            if source.read(16)==SQLITE_HEADER:
                raise PaperPerpError('La copia GRVT PAPER no está cifrada')
        with staged.open('rb') as source:os.fsync(source.fileno())
        with paper_perp_access.guard(path,epoch):
            if (store.current_passphrase()!=key or _identity(path)[:2]!=identity
                    or paper_perp_format(path)!='encrypted_or_invalid'):
                raise PaperPerpError('GRVT PAPER cambió durante la copia')
            os.link(staged,target)
            published=True
            _sync_dir(directory)
        return {'name':name,'encrypted':True,'verified':verified,'scope':'CONSISTENT_SNAPSHOT_NOT_CURRENT_LIVE_BALANCE'}
    except PaperPerpError:
        raise
    except (sqlcipher3.DatabaseError,sqlite3.DatabaseError,OSError,ValueError):
        raise PaperPerpError(('La copia está guardada, pero falló su sincronización; revisa '+name)
                            if published else 'No se pudo crear o verificar la copia cifrada GRVT PAPER') from None
    finally:
        staged.unlink(missing_ok=True)
        for suffix in ('-wal','-shm','-journal'):Path(str(staged)+suffix).unlink(missing_ok=True)


def encrypt_active_paper_perp(store: PaperPerpStore, confirmation: str,
                              passphrase: str) -> dict:
    if confirmation != "CIFRAR GRVT PAPER":
        raise PaperPerpError("Debes confirmar exactamente CIFRAR GRVT PAPER")
    _password(passphrase)
    if store.current_passphrase() is not None:
        raise PaperPerpError("La migración requiere GRVT PAPER plano sin clave activa")
    path = store.path
    with paper_perp_lease(path, exclusive=True):
        if paper_perp_format(path) != "plain":
            raise PaperPerpError("No existe una base GRVT PAPER plana que cifrar")
        _no_journals(path)
        original = _identity(path)
        original_file = sqlite_file_digest(path)
        if original_file[:4] != original:
            raise PaperPerpError("GRVT PAPER cambió antes del respaldo")
        verified = verify_paper_perp_database(path)
        directory = _backup_dir(path)
        nonce = uuid4().hex[:12]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        staged = path.with_name(f".{path.name}.encrypt-{nonce}")
        backup = directory / f"paper-perp-secure-{stamp}-{nonce}.db"
        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(staged, flags, 0o600)
        os.close(descriptor)
        replaced = False
        try:
            _export(path, staged, source_password=None, destination_password=passphrase)
            if verify_paper_perp_database(staged, passphrase) != verified:
                raise PaperPerpError("La copia cifrada GRVT PAPER difiere del origen")
            with staged.open("rb") as encrypted:
                if encrypted.read(16) == SQLITE_HEADER:
                    raise PaperPerpError("GRVT PAPER no quedó cifrado")
            _copy_private(staged, backup)
            with backup.open("rb") as safe_file:
                os.fsync(safe_file.fileno())
            _sync_dir(directory)
            with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True,
                                         timeout=0, isolation_level=None)) as guard:
                try:
                    guard.execute("BEGIN EXCLUSIVE")
                except sqlite3.Error:
                    raise PaperPerpError("Otro proceso usa GRVT PAPER; no se sustituyó") from None
                _no_journals(path)
                if _identity(path) != original:
                    raise PaperPerpError("GRVT PAPER cambió durante la migración")
                if sqlite_file_digest(path) != original_file:
                    raise PaperPerpError("GRVT PAPER cambió durante la migración")
                os.replace(staged, path)
                replaced = True
                path.chmod(0o600)
                _sync_dir(path.parent)
            return {"encrypted": True, "backup": backup.name,
                    "verified": verified, "paper_path": str(path)}
        except PaperPerpError:
            raise
        except (sqlcipher3.DatabaseError, sqlite3.DatabaseError, OSError, ValueError):
            raise PaperPerpError("GRVT PAPER se cifró, pero falló la sincronización; revisa la copia"
                                 if replaced else "No se pudo cifrar GRVT PAPER; revisa la copia") from None
        finally:
            staged.unlink(missing_ok=True)


def restore_secure_paper_perp(store: PaperPerpStore, name: str,
                              confirmation: str, passphrase: str) -> dict:
    if confirmation != "RESTAURAR GRVT PAPER":
        raise PaperPerpError("Debes confirmar exactamente RESTAURAR GRVT PAPER")
    _password(passphrase)
    if store.current_passphrase() is not None:
        raise PaperPerpError("Bloquea GRVT PAPER antes de restaurar una copia")
    if not isinstance(name, str) or not BACKUP_NAME.fullmatch(name):
        raise PaperPerpError("Selecciona una copia cifrada GRVT PAPER gestionada")
    path = store.path
    directory = path.parent / "paper-perp-backups"
    if directory.is_symlink() or not directory.is_dir():
        raise PaperPerpError("No existe la carpeta de copias GRVT PAPER")
    backup = directory / name
    with paper_perp_lease(path, exclusive=True):
        kind = paper_perp_format(path)
        if kind not in ("missing", "encrypted_or_invalid"):
            raise PaperPerpError("La base GRVT PAPER activa no está cifrada")
        _no_journals(path)
        expected = verify_paper_perp_database(backup, passphrase)
        nonce = uuid4().hex[:12]
        staged = path.with_name(f".{path.name}.restore-{nonce}")
        previous = None
        guard = None
        replaced = False
        try:
            _copy_private(backup, staged)
            if verify_paper_perp_database(staged, passphrase) != expected:
                raise PaperPerpError("La copia GRVT PAPER cambió durante la restauración")
            original = _identity(path) if kind != "missing" else None
            if original is not None:
                original_file = sqlite_file_digest(path)
                if original_file[:4] != original:
                    raise PaperPerpError("GRVT PAPER cambió antes de la preimagen")
                current = verify_paper_perp_database(path, passphrase)
                stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                previous = directory / f"pre-restore-paper-perp-{stamp}-{nonce}.db"
                _copy_private(path, previous)
                if verify_paper_perp_database(previous, passphrase) != current:
                    raise PaperPerpError("La preimagen GRVT PAPER difiere del activo")
                _sync_dir(directory)
                try:
                    guard = sqlcipher3.connect(path.as_uri() + "?mode=rw", uri=True,
                                               timeout=0, isolation_level=None)
                    guard.execute("PRAGMA key = '" + passphrase.replace("'", "''") + "'")
                    guard.execute("BEGIN EXCLUSIVE")
                except sqlcipher3.DatabaseError:
                    raise PaperPerpError("Otro proceso usa GRVT PAPER o la clave difiere") from None
                _no_journals(path)
                if _identity(path) != original:
                    raise PaperPerpError("GRVT PAPER cambió durante la restauración")
                if sqlite_file_digest(path) != original_file:
                    raise PaperPerpError("GRVT PAPER cambió durante la restauración")
            os.replace(staged, path)
            replaced = True
            path.chmod(0o600)
            _sync_dir(path.parent)
            return {"restored": True, "backup": name,
                    "previous_backup": previous.name if previous else None,
                    "verified": expected}
        except PaperPerpError:
            raise
        except (sqlcipher3.DatabaseError, sqlite3.DatabaseError, OSError, ValueError):
            raise PaperPerpError("GRVT PAPER se restauró; revisa la preimagen"
                                 if replaced else "No se pudo restaurar GRVT PAPER; se conservó el activo") from None
        finally:
            if guard is not None:
                guard.close()
            staged.unlink(missing_ok=True)
