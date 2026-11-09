"""Copias consistentes de LAB para recuperación offline; nunca incluyen REAL."""

from __future__ import annotations

from app.lab.schema import DATASET_READ_VERSIONS

from collections import deque
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
from uuid import uuid4

import sqlcipher3

from app.core.local_sqlite import sqlite_file_digest
try:
    import fcntl
except ImportError:  # pragma: no cover - Windows no admite aún restauración LAB
    fcntl = None

from app.lab.engine import LabError
from app.lab.store import LabStore, content_hash, validate_dataset_coverage
from app.lab.paper_gap import analyze_paper_gap


_NAME = re.compile(r"lab-[0-9]{8}-[0-9]{6}-[0-9a-f]{12}\.db\Z")
_RECOVERY_NAME = re.compile(r"pre-restore-lab-[0-9]{8}-[0-9]{6}-[0-9a-f]{12}\.db\Z")
_TABLES = ("datasets", "strategies", "strategy_versions", "runs", "detected_events",
           "simulated_trades", "research_runs", "research_events", "validation_runs",
           "optimization_runs", "strategy_imports")


def verify_lab_backup(path: Path) -> dict:
    """Verifica estructura, claves ajenas, huellas de velas y runs fijados."""
    if path.is_symlink() or not path.is_file():
        raise LabError("La copia LAB no es un archivo regular")
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            schema_version = db.execute("PRAGMA user_version").fetchone()[0]
            if schema_version not in DATASET_READ_VERSIONS:
                raise LabError("La copia LAB tiene una versión no compatible")
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise LabError("La copia LAB no supera la integridad SQLite")
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise LabError("La copia LAB contiene referencias rotas")
            names = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            tables = _TABLES + (("dataset_refreshes",) if schema_version >= 7 else ()) + (
                ("paper_gap_reports",) if schema_version >= 8 else ()) + (
                ("ai_analyses",) if schema_version >= 9 else ()) + (
                ("perp_datasets",) if schema_version >= 10 else ()) + (
                ("perp_profiles","perp_runs") if schema_version >= 11 else ()) + (
                ("perp_validations",) if schema_version >= 12 else ()) + (
                ("perp_rule_observations",) if schema_version >= 13 else ()) + (
                ("perp_optimizations",) if schema_version >= 14 else ()) + (
                ("perp_optimization_variants",) if schema_version >= 15 else ())
            if not set(tables).issubset(names):
                raise LabError("La copia LAB no contiene todas las tablas")
            counts = {name: db.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                      for name in tables}
            for row in db.execute("""SELECT symbol,timeframe,source,candles_json,gaps_json,
                                           start_t,end_t,verified_public,hash FROM datasets"""):
                candles = json.loads(row["candles_json"])
                validate_dataset_coverage(row["timeframe"], candles,
                                          json.loads(row["gaps_json"]),
                                          row["start_t"], row["end_t"])
                material = {"symbol": row["symbol"], "timeframe": row["timeframe"],
                            "source": row["source"], "candles": candles}
                if row["verified_public"]:
                    material["verified_public"] = True
                if content_hash(material) != row["hash"]:
                    raise LabError("La copia LAB tiene un dataset con huella incorrecta")
            for row in db.execute("SELECT definition_json FROM strategy_versions"):
                json.loads(row["definition_json"])
            if schema_version >= 7:
                children: dict[str, list[str]] = {}
                indegree: dict[str, int] = {}
                for row in db.execute("""SELECT r.parent_hash,r.child_hash,
                                               r.parent_id,r.child_id,
                                               p.hash AS current_parent_hash,c.hash AS current_child_hash
                                        FROM dataset_refreshes r
                                        JOIN datasets p ON p.id=r.parent_id
                                        JOIN datasets c ON c.id=r.child_id"""):
                    if (row["parent_hash"] != row["current_parent_hash"] or
                            row["child_hash"] != row["current_child_hash"]):
                        raise LabError("La copia LAB tiene una procedencia de históricos alterada")
                    children.setdefault(row["parent_id"], []).append(row["child_id"])
                    indegree.setdefault(row["parent_id"], 0)
                    indegree[row["child_id"]] = indegree.get(row["child_id"], 0) + 1
                pending = deque(node for node, degree in indegree.items() if degree == 0)
                visited = 0
                while pending:
                    node = pending.popleft()
                    visited += 1
                    for child in children.get(node, ()):
                        indegree[child] -= 1
                        if indegree[child] == 0:
                            pending.append(child)
                if visited != len(indegree):
                    raise LabError("La copia LAB contiene un ciclo de procedencia")
            if schema_version >= 8:
                for row in db.execute("SELECT * FROM paper_gap_reports"):
                    snapshot = json.loads(row["snapshot_json"])
                    downloaded = json.loads(row["downloaded_json"])
                    result = json.loads(row["result_json"])
                    material = {"venue": row["venue"], "snapshot": snapshot,
                                "downloaded": downloaded, "result": result}
                    dataset = db.execute("SELECT hash,symbol,timeframe,candles_json FROM datasets WHERE id=?",
                                         (row["dataset_id"],)).fetchone()
                    version = db.execute("SELECT definition_json FROM strategy_versions WHERE id=?",
                                         (row["version_id"],)).fetchone()
                    source = {"MEXC_SPOT": "mexc-spot-public",
                              "GRVT_PERP": "grvt-perp-trade-public"}.get(row["venue"])
                    if (source is None or dataset is None or version is None
                            or content_hash(material) != row["hash"]
                            or snapshot["id"] != row["session_id"]
                            or snapshot["dataset_id"] != row["dataset_id"]
                            or snapshot["dataset_hash"] != row["dataset_hash"]
                            or snapshot["version_id"] != row["version_id"]
                            or snapshot["engine_version"] != row["engine_version"]
                            or dataset["hash"] != row["dataset_hash"]
                            or dataset["symbol"] != snapshot["symbol"]
                            or dataset["timeframe"] != snapshot["timeframe"]
                            or snapshot["history"][:len(json.loads(dataset["candles_json"]))]
                            != json.loads(dataset["candles_json"])
                            or json.loads(version[0]) != snapshot["definition"]
                            or analyze_paper_gap(snapshot, snapshot["definition"], snapshot["history"],
                                                 downloaded, now=snapshot["reconstruction_now"],
                                                 source=source) != result):
                        raise LabError("La copia LAB tiene una reconstrucción PAPER alterada")
            if schema_version >= 9:
                for row in db.execute("SELECT * FROM ai_analyses"):
                    material = {"kind": row["kind"], "run_id": row["run_id"],
                                "evidence": json.loads(row["evidence_json"]),
                                "question": row["question"], "answer": row["answer"],
                                "model": row["model"], "usage": json.loads(row["usage_json"])}
                    run_table = "research_runs" if row["kind"] == "research" else "runs"
                    if (row["kind"] not in ("research", "backtest")
                            or db.execute(f"SELECT id FROM {run_table} WHERE id=?",
                                          (row["run_id"],)).fetchone() is None
                            or material["evidence"].get("source_run") != row["run_id"]
                            or content_hash(material) != row["hash"]):
                        raise LabError("La copia LAB tiene un análisis de IA alterado")
            if schema_version >= 10:
                from app.lab.perp_store import verified_record
                for row in db.execute("SELECT * FROM perp_datasets"):
                    verified_record(row)
            if schema_version >= 11:
                from app.lab.perp_experiments import read_profile_row,read_run_row
                for row in db.execute("SELECT * FROM perp_profiles"):
                    read_profile_row(db,row)
                for row in db.execute("SELECT * FROM perp_runs"):
                    read_run_row(db,row,replay=True)
            if schema_version >= 12:
                from app.lab.perp_validation_store import read_validation_row
                for row in db.execute("SELECT * FROM perp_validations"):
                    read_validation_row(db,row,replay=True)
            if schema_version >= 13:
                from app.lab.perp_rules import read_observation
                for row in db.execute("SELECT * FROM perp_rule_observations"):
                    read_observation(row)
            elif 'perp_rule_observations' in names and db.execute('SELECT 1 FROM perp_rule_observations LIMIT 1').fetchone():
                raise LabError('La versión de la copia LAB no admite observaciones de reglas')
            if schema_version >= 14:
                from app.lab.perp_optimization_store import read_optimization_row, read_optimization_variant
                for row in db.execute("SELECT * FROM perp_optimizations"):
                    saved = read_optimization_row(db, row, replay=True)
                    if schema_version >= 15:
                        read_optimization_variant(db, saved)
            elif 'perp_optimizations' in names and db.execute('SELECT 1 FROM perp_optimizations LIMIT 1').fetchone():
                raise LabError('La versión de la copia LAB no admite optimizaciones de derivados')
            if (schema_version < 15 and 'perp_optimization_variants' in names
                    and db.execute('SELECT 1 FROM perp_optimization_variants LIMIT 1').fetchone()):
                raise LabError('La versión de la copia LAB no admite variantes de optimización')
            legacy_runs = 0
            for row in db.execute("SELECT id,dataset_id,version_id,result_json FROM runs"):
                saved = json.loads(row["result_json"])
                dataset = db.execute("SELECT hash,source FROM datasets WHERE id=?",
                                     (row["dataset_id"],)).fetchone()
                version = db.execute("SELECT definition_json FROM strategy_versions WHERE id=?",
                                     (row["version_id"],)).fetchone()
                if (dataset is None or version is None
                        or (saved.get("dataset_hash") is not None
                            and saved["dataset_hash"] != dataset["hash"])
                        or (saved.get("dataset_source") is not None
                            and saved["dataset_source"] != dataset["source"])
                        or (saved.get("strategy_hash") is not None
                            and saved["strategy_hash"] != content_hash(json.loads(version[0])))):
                    raise LabError("La copia LAB tiene un backtest con entradas alteradas")
                events = [json.loads(item[0]) for item in db.execute(
                    "SELECT event_json FROM detected_events WHERE run_id=? ORDER BY index_no", (row["id"],))]
                trades = [json.loads(item[0]) for item in db.execute(
                    "SELECT trade_json FROM simulated_trades WHERE run_id=? ORDER BY index_no", (row["id"],))]
                digest = saved.pop("result_hash", None)
                if digest is None or any(saved.get(field) is None for field in (
                        "dataset_hash", "dataset_source", "strategy_hash")):
                    legacy_runs += 1
                for field in ("dataset_hash", "dataset_source", "strategy_hash"):
                    saved.pop(field, None)
                if digest is not None and content_hash({**saved, "events": events,
                                                        "trades": trades}) != digest:
                    raise LabError("La copia LAB tiene un backtest con huella incorrecta")
            for table, field in (("research_runs", "result_json"),
                                 ("validation_runs", "result_json"),
                                 ("optimization_runs", "result_json"),
                                 ("research_events", "event_json")):
                for row in db.execute(f"SELECT {field} FROM {table}"):
                    json.loads(row[0])
            return {"schema_version": schema_version, "counts": counts,
                    "verified_datasets": counts["datasets"],
                    "verified_runs": counts["runs"] - legacy_runs,
                    "legacy_runs_without_hash": legacy_runs}
    except LabError:
        raise
    except (sqlite3.DatabaseError, OSError, ValueError, TypeError, KeyError) as exc:
        raise LabError("La copia LAB no se pudo verificar") from exc


def create_lab_backup(store: LabStore) -> dict:
    """SQLite backup API → fichero privado → verificación → nombre definitivo."""
    if store.encrypted:
        raise LabError("Una base LAB cifrada no admite copias sin cifrar")
    if store.path.is_symlink() or not store.path.is_file():
        raise LabError("No existe una base LAB que copiar")
    directory = store.path.parent / "lab-backups"
    if directory.is_symlink():
        raise LabError("La carpeta de copias LAB no puede ser un enlace")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    nonce = uuid4().hex[:12]
    destination = directory / f"lab-{stamp}-{nonce}.db"
    pending = directory / f".lab-pending-{nonce}.db"
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    created = False
    try:
        descriptor = os.open(pending, flags, 0o600)
        created = True
        os.close(descriptor)
        with store.connect() as source, closing(sqlcipher3.connect(pending)) as target:
            source.backup(target, pages=256, sleep=0.05)
        verified = verify_lab_backup(pending)
        # Enlazar falla si el nombre final ya existe: nunca sobrescribe otra copia.
        os.link(pending, destination)
        destination.chmod(0o600)
        return {"name": destination.name, "path": str(destination),
                "size_bytes": destination.stat().st_size, "encrypted": False,
                "verified": verified}
    except (OSError, sqlite3.DatabaseError) as exc:
        raise LabError("No se pudo crear la copia LAB") from exc
    finally:
        # Sólo este fichero temporal generado por esta llamada; el original y
        # las copias verificadas nunca se eliminan en la limpieza.
        if created:
            pending.unlink(missing_ok=True)


def list_lab_backups(store: LabStore) -> list[dict]:
    from app.lab.secure_backup import SECURE_NAME, SECURE_RECOVERY_NAME

    directory = store.path.parent / "lab-backups"
    if directory.is_symlink() or not directory.is_dir():
        return []
    rows = []
    for path in directory.iterdir():
        secure = bool(SECURE_NAME.fullmatch(path.name) or
                      SECURE_RECOVERY_NAME.fullmatch(path.name))
        if (not secure and not _NAME.fullmatch(path.name)
                and not _RECOVERY_NAME.fullmatch(path.name)) or path.is_symlink() or not path.is_file():
            continue
        info = path.stat()
        rows.append({"name": path.name, "path": str(path), "size_bytes": info.st_size,
                     "modified_at": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
                     "encrypted": secure})
    return sorted(rows, key=lambda item: (item["modified_at"], item["name"]), reverse=True)


@contextmanager
def _lab_lease(path: Path, *, exclusive: bool):
    """El servidor mantiene lease compartida; restaurar exige exclusividad."""
    if fcntl is None:
        if exclusive:
            raise LabError("Restauración LAB offline no disponible en esta plataforma")
        yield
        return
    lock_path = path.with_name(path.name + ".app-lock")
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise LabError("No se pudo abrir el cerrojo LAB") from exc
    try:
        if lock_path.is_symlink() or not lock_path.is_file():
            raise LabError("Cerrojo LAB inválido")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LabError("Orbis sigue usando LAB; ciérralo antes de restaurar") from exc
        yield
    finally:
        os.close(descriptor)


@contextmanager
def lab_runtime_lease(path: Path):
    """Mantiene la restauración offline bloqueada mientras vive el servidor."""
    with _lab_lease(Path(path).expanduser().absolute(), exclusive=False):
        yield


def _copy_exclusive(source: Path, destination: Path) -> None:
    """Copia bytes a un nombre nuevo privado y sincroniza antes del reemplazo."""
    read_flags = os.O_RDONLY
    write_flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        read_flags |= os.O_NOFOLLOW
        write_flags |= os.O_NOFOLLOW
    source_fd = os.open(source, read_flags)
    created = False
    try:
        with os.fdopen(source_fd, "rb") as reader:
            source_fd = -1
            if not stat.S_ISREG(os.fstat(reader.fileno()).st_mode) or source.is_symlink():
                raise LabError("Origen LAB no es un archivo regular")
            destination_fd = os.open(destination, write_flags, 0o600)
            created = True
            with os.fdopen(destination_fd, "wb") as writer:
                shutil.copyfileobj(reader, writer)
                writer.flush()
                os.fsync(writer.fileno())
    except Exception:
        if created:
            destination.unlink(missing_ok=True)
        raise
    finally:
        if source_fd >= 0:
            os.close(source_fd)


def restore_lab_backup(store: LabStore, name: str, confirmation: str,
                       passphrase: str | None = None) -> dict:
    """Restaura sólo una copia gestionada, con preimagen. Uso offline por CLI."""
    from app.lab.secure_backup import SECURE_NAME, SECURE_RECOVERY_NAME, restore_secure_lab_backup

    if SECURE_NAME.fullmatch(name) or SECURE_RECOVERY_NAME.fullmatch(name):
        return restore_secure_lab_backup(store, name, confirmation, passphrase)
    if store.encrypted:
        raise LabError("Una copia sin cifrar no puede sustituir un LAB activo cifrado")
    if confirmation != "RESTAURAR":
        raise LabError("Debes confirmar exactamente RESTAURAR")
    if not (_NAME.fullmatch(name) or _RECOVERY_NAME.fullmatch(name)):
        raise LabError("Selecciona una copia LAB gestionada por nombre")
    directory = store.path.parent / "lab-backups"
    if directory.is_symlink() or not directory.is_dir():
        raise LabError("No existe la carpeta de copias LAB")
    source = directory / name
    with _lab_lease(store.path, exclusive=True):
        # No abrir ni reemplazar una base con ficheros de journal pendientes.
        if store.path.is_symlink() or (store.path.exists() and not store.path.is_file()):
            raise LabError("La ruta LAB actual no es una base regular")
        if store.path.exists():
            with store.path.open("rb") as active:
                if active.read(16) != b"SQLite format 3\x00":
                    raise LabError("LAB activo no está en formato SQLite plano; no se sustituirá")
        if any(Path(str(store.path) + suffix).exists()
               for suffix in ("-wal", "-shm", "-journal")):
            raise LabError("LAB conserva archivos de journal; revisa y cierra Orbis antes de restaurar")
        verified = verify_lab_backup(source)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        nonce = uuid4().hex[:12]
        staged = store.path.with_name(f".{store.path.name}.restore-{nonce}")
        recovery = directory / f"pre-restore-lab-{stamp}-{nonce}.db"
        before = sqlite_file_digest(store.path) if store.path.exists() else None
        try:
            _copy_exclusive(source, staged)
            if verify_lab_backup(staged) != verified:
                raise LabError("La copia LAB cambió durante la restauración")
            if before is not None:
                _copy_exclusive(store.path, recovery)
                try:
                    with closing(sqlite3.connect(store.path.as_uri() + "?mode=rw", uri=True,
                                                 timeout=0, isolation_level=None)) as guard:
                        guard.execute("BEGIN EXCLUSIVE")
                        if any(Path(str(store.path) + suffix).exists()
                               for suffix in ("-wal", "-shm", "-journal")):
                            raise LabError("LAB conserva journals; no se sustituyó")
                        if sqlite_file_digest(store.path) != before:
                            raise LabError("LAB cambió durante la restauración; no se sustituyó")
                        os.replace(staged, store.path)
                        store.path.chmod(0o600)
                except sqlite3.DatabaseError:
                    raise LabError("Otro proceso usa LAB; no se sustituyó") from None
            else:
                os.replace(staged, store.path)
                store.path.chmod(0o600)
            parent_fd = os.open(store.path.parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            except OSError as exc:
                raise LabError("LAB se sustituyó, pero la sincronización final falló; revisa la base") from exc
            finally:
                os.close(parent_fd)
            return {"restored": True, "backup": name, "previous_lab_backup": recovery.name if before else None,
                    "verified": verified}
        except (OSError, sqlite3.DatabaseError) as exc:
            raise LabError("No se pudo restaurar LAB; revisa la copia previa") from exc
        finally:
            staged.unlink(missing_ok=True)
