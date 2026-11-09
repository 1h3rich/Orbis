"""Reglas y simulaciones DCA locales, separadas de LAB, PAPER y REAL."""

from __future__ import annotations

from app.lab.schema import DATASET_READ_VERSIONS

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4, uuid5

import sqlcipher3

from app.lab.engine import LabError
from app.lab.store import content_hash
from app.core.local_sqlite import ensure_private_sqlite
from app.automation.access import automation_access, automation_format
from app.automation.paper_dca_gap import analyze_dca_paper_gap


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


DCA_IMPORT_NAMESPACE = UUID("7de95613-eb08-4bb0-b121-52fd10dfbbcf")


class AutomationStore:
    def __init__(self, path: Path, *, passphrase: str | None = None,
                 allow_create: bool = True):
        self.path = Path(path).expanduser().absolute()
        if passphrase is not None and not 12 <= len(passphrase) <= 1024:
            raise LabError("La contraseña de automatizaciones debe tener entre 12 y 1024 caracteres")
        self._passphrase = passphrase
        self._allow_create = allow_create

    def current_passphrase(self) -> str | None:
        return self._passphrase if self._passphrase is not None else automation_access.get(self.path)

    @contextmanager
    def connect(self):
        if not self._allow_create and automation_format(self.path) in ("missing", "empty"):
            raise LabError("Crea primero la base DCA cifrada en Ajustes")
        ensure_private_sqlite(self.path)
        db = sqlcipher3.connect(self.path, timeout=5)
        try:
            db.row_factory = sqlcipher3.Row
            passphrase = self.current_passphrase()
            if passphrase is not None:
                db.execute("PRAGMA key = '" + passphrase.replace("'", "''") + "'")
            db.execute("PRAGMA foreign_keys=ON")
            try:
                version = db.execute("PRAGMA user_version").fetchone()[0]
            except sqlcipher3.DatabaseError:
                raise LabError("Automatizaciones no se pudo abrir; contraseña o formato incompatibles") from None
            if version in (0, 1, 2, 3, 4, 5):
                db.execute("BEGIN IMMEDIATE")
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version == 0:
                    for statement in ("""CREATE TABLE dca_rules (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL
                )""", """CREATE TABLE dca_versions (
                    id TEXT PRIMARY KEY, rule_id TEXT NOT NULL REFERENCES dca_rules(id),
                    version INTEGER NOT NULL, created_at TEXT NOT NULL, definition_json TEXT NOT NULL,
                    UNIQUE(rule_id, version)
                )""", """CREATE TABLE dca_runs (
                    id TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES dca_versions(id),
                    dataset_id TEXT NOT NULL, dataset_hash TEXT NOT NULL, created_at TEXT NOT NULL,
                    engine_version TEXT NOT NULL, result_json TEXT NOT NULL
                )"""):
                        db.execute(statement)
                    db.execute("PRAGMA user_version=1")
                    version = 1
                if version == 1:
                    db.execute("""CREATE TABLE paper_dca_sessions (
                        id TEXT PRIMARY KEY,
                        version_id TEXT NOT NULL REFERENCES dca_versions(id),
                        dataset_id TEXT NOT NULL, dataset_hash TEXT NOT NULL,
                        symbol TEXT NOT NULL, source TEXT NOT NULL,
                        rule_json TEXT NOT NULL, state_json TEXT NOT NULL,
                        overlap_json TEXT NOT NULL, next_exec_t INTEGER NOT NULL,
                        tick_index INTEGER NOT NULL, started_at_ns INTEGER NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('ACTIVE','STOPPED','GAP')),
                        status_reason TEXT, last_quote_json TEXT
                    )""")
                    db.execute("""CREATE TABLE paper_dca_events (
                        session_id TEXT NOT NULL REFERENCES paper_dca_sessions(id),
                        execution_t INTEGER NOT NULL, sequence_no INTEGER NOT NULL,
                        observed_at_ns INTEGER NOT NULL, quote_event_ns INTEGER NOT NULL,
                        event_json TEXT NOT NULL,
                        PRIMARY KEY(session_id,execution_t,sequence_no)
                    )""")
                    db.execute("PRAGMA user_version=2")
                    version = 2
                if version == 2:
                    db.execute("ALTER TABLE paper_dca_sessions ADD COLUMN engine_version TEXT NOT NULL DEFAULT 'UNPINNED'")
                    db.execute("""UPDATE paper_dca_sessions SET status='GAP',
                                  status_reason='Versión de motor no fijada en sesión previa'
                                  WHERE status='ACTIVE'""")
                    db.execute("PRAGMA user_version=3")
                    version = 3
                if version == 3:
                    db.execute("""CREATE TABLE paper_dca_gap_reports (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES paper_dca_sessions(id),
                        created_at TEXT NOT NULL,
                        evidence_json TEXT NOT NULL,
                        evidence_hash TEXT NOT NULL UNIQUE
                    )""")
                    db.execute("CREATE INDEX paper_dca_gap_reports_session ON paper_dca_gap_reports(session_id,created_at)")
                    db.execute("PRAGMA user_version=4")
                    version = 4
                if version == 4:
                    db.execute("ALTER TABLE paper_dca_sessions ADD COLUMN source_run_id TEXT")
                    db.execute("PRAGMA user_version=5")
                    version = 5
                if version == 5:
                    db.execute("""CREATE TABLE dca_validation_runs (
                        id TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES dca_versions(id),
                        dataset_id TEXT NOT NULL, dataset_hash TEXT NOT NULL,
                        created_at TEXT NOT NULL, engine_version TEXT NOT NULL,
                        result_json TEXT NOT NULL
                    )""")
                    db.execute("CREATE INDEX dca_validation_inputs ON dca_validation_runs(version_id,dataset_id)")
                    db.execute("PRAGMA user_version=6")
                    version = 6
                db.commit()
            if version != 6:
                raise LabError(f"Versión de automatizaciones no compatible: {version}")
            self.path.chmod(0o600)
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def create_rule(self, name: str, definition: dict, *, import_id: UUID | None = None) -> dict:
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
            raise LabError("Nombre DCA inválido")
        name = name.strip()
        rule_id = str(uuid5(DCA_IMPORT_NAMESPACE, str(import_id))) if import_id is not None else str(uuid4())
        version_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            if import_id is not None:
                db.execute("BEGIN IMMEDIATE")
                existing = db.execute("""SELECT r.name,v.id,v.definition_json
                    FROM dca_rules r LEFT JOIN dca_versions v ON v.rule_id=r.id AND v.version=1
                    WHERE r.id=?""", (rule_id,)).fetchone()
                if existing is not None:
                    if existing["name"] != name or existing["definition_json"] != _json(definition):
                        raise LabError("La solicitud de importación ya se usó con otro plan DCA")
                    return {"id": rule_id, "name": name, "version": 1,
                            "version_id": existing["id"], "definition": definition}
            db.execute("INSERT INTO dca_rules VALUES (?,?,?)", (rule_id, name, now))
            db.execute("INSERT INTO dca_versions VALUES (?,?,?,?,?)", (version_id, rule_id, 1, now, _json(definition)))
        return {"id": rule_id, "name": name, "version": 1, "version_id": version_id, "definition": definition}

    def add_version(self, rule_id: str, definition: dict) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT name FROM dca_rules WHERE id=?", (rule_id,)).fetchone()
            if row is None:
                raise LabError("Regla DCA no encontrada")
            version = db.execute("SELECT COALESCE(MAX(version), 0)+1 FROM dca_versions WHERE rule_id=?", (rule_id,)).fetchone()[0]
            version_id = str(uuid4())
            db.execute("INSERT INTO dca_versions VALUES (?,?,?,?,?)",
                       (version_id, rule_id, version, datetime.now(timezone.utc).isoformat(), _json(definition)))
        return {"id": rule_id, "name": row["name"], "version": version,
                "version_id": version_id, "definition": definition}

    def rules(self) -> list[dict]:
        if automation_format(self.path) in ("missing", "empty"):
            return []
        with self.connect() as db:
            rows = db.execute("""SELECT r.id, r.name, v.id AS version_id, v.version, v.definition_json
                                 FROM dca_rules r JOIN dca_versions v ON v.rule_id=r.id
                                 WHERE v.version=(SELECT MAX(v2.version) FROM dca_versions v2 WHERE v2.rule_id=r.id)
                                 ORDER BY r.created_at DESC""").fetchall()
            return [{"id": row["id"], "name": row["name"], "version_id": row["version_id"],
                     "version": row["version"], "definition": json.loads(row["definition_json"])} for row in rows]

    def versions(self, rule_id: str) -> list[dict]:
        with self.connect() as db:
            return [{"id": row["id"], "rule_id": row["rule_id"], "version": row["version"],
                     "created_at": row["created_at"], "definition": json.loads(row["definition_json"])}
                    for row in db.execute("SELECT * FROM dca_versions WHERE rule_id=? ORDER BY version", (rule_id,))]

    def version(self, version_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM dca_versions WHERE id=?", (version_id,)).fetchone()
            if row is None:
                raise LabError("Versión DCA no encontrada")
            return {"id": row["id"], "rule_id": row["rule_id"], "version": row["version"],
                    "definition": json.loads(row["definition_json"])}

    def shareable_version(self, rule_id: str, version_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("""SELECT r.name,v.version,v.definition_json
                FROM dca_versions v JOIN dca_rules r ON r.id=v.rule_id
                WHERE r.id=? AND v.id=?""", (rule_id, version_id)).fetchone()
        if row is None:
            raise LabError("Versión DCA no encontrada")
        return {"name": row["name"], "version": row["version"],
                "definition": json.loads(row["definition_json"])}

    def save_run(self, version_id: str, dataset_id: str, dataset_hash: str, result: dict,
                 engine_version: str, *, lab_path: Path,
                 expected_definition_hash: str,
                 lab_passphrase: str | None = None) -> dict:
        return self._save_research("dca_runs", version_id, dataset_id, dataset_hash,
            result, engine_version, lab_path=lab_path,
            expected_definition_hash=expected_definition_hash, lab_passphrase=lab_passphrase)

    def save_validation(self, version_id: str, dataset_id: str, dataset_hash: str,
                        result: dict, engine_version: str, *, lab_path: Path,
                        expected_definition_hash: str,
                        lab_passphrase: str | None = None) -> dict:
        if (engine_version != "dca-holdout-1.0"
                or result.get("method") != "DCA_CHRONOLOGICAL_HOLDOUT"
                or {"id", "version_id", "dataset_id", "dataset_hash", "created_at",
                    "engine_version", "definition_hash", "result_hash"}.intersection(result)):
            raise LabError("Formato de validación DCA no admitido")
        return self._save_research("dca_validation_runs", version_id, dataset_id, dataset_hash,
            result, engine_version, lab_path=lab_path,
            expected_definition_hash=expected_definition_hash, lab_passphrase=lab_passphrase)

    def _save_research(self, table: Literal["dca_runs", "dca_validation_runs"],
                       version_id: str, dataset_id: str, dataset_hash: str, result: dict,
                       engine_version: str, *, lab_path: Path,
                       expected_definition_hash: str,
                       lab_passphrase: str | None) -> dict:
        if table not in ("dca_runs", "dca_validation_runs"):
            raise LabError("Tipo de resultado DCA no admitido")
        run_id = str(uuid4())
        source = Path(lab_path).expanduser().absolute()
        if source.is_symlink() or not source.is_file():
            raise LabError("LAB no está disponible para guardar la simulación DCA")
        with self.connect() as db:
            # Una única transacción lee la versión y el dataset adjunto antes
            # de guardar: una edición concurrente no puede cambiar entradas
            # entre la revalidación y la inserción.
            # SQLCipher admite LAB legado y cifrado; la clave no se guarda.
            try:
                db.execute("ATTACH DATABASE ? AS lab KEY ?",
                           (str(source), lab_passphrase or ""))
                lab_version = db.execute("PRAGMA lab.user_version").fetchone()[0]
            except sqlcipher3.DatabaseError:
                raise LabError("LAB adjunto no se pudo leer; contraseña o formato incompatibles") from None
            if lab_version not in DATASET_READ_VERSIONS:
                raise LabError(f"Versión de LAB adjunto no compatible: {lab_version}")
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("SELECT definition_json FROM dca_versions WHERE id=?", (version_id,)).fetchone()
            if version is None or content_hash(json.loads(version["definition_json"])) != expected_definition_hash:
                raise LabError("La versión DCA cambió antes de guardar la simulación")
            dataset = db.execute("SELECT * FROM lab.datasets WHERE id=?", (dataset_id,)).fetchone()
            if dataset is None:
                raise LabError("Dataset no encontrado antes de guardar DCA")
            material = {"symbol": dataset["symbol"], "timeframe": dataset["timeframe"],
                        "source": dataset["source"], "candles": json.loads(dataset["candles_json"])}
            if dataset["verified_public"]:
                material["verified_public"] = True
            if content_hash(material) != dataset_hash or dataset["hash"] != dataset_hash:
                raise LabError("El dataset cambió antes de guardar la simulación DCA")
            saved = {**result, "definition_hash": expected_definition_hash,
                     "result_hash": content_hash(result)}
            db.execute(f"INSERT INTO {table} VALUES (?,?,?,?,?,?,?)",
                       (run_id, version_id, dataset_id, dataset_hash,
                        datetime.now(timezone.utc).isoformat(), engine_version, _json(saved)))
        return self.run(run_id) if table == "dca_runs" else self.validation(run_id)

    @staticmethod
    def _validation_payload(row) -> dict:
        try:
            payload = json.loads(row["result_json"])
        except (ValueError, TypeError):
            raise LabError("Resultado de validación DCA ilegible") from None
        metadata = ("id", "version_id", "dataset_id", "dataset_hash", "created_at", "engine_version")
        if not isinstance(payload, dict) or set(metadata).intersection(payload):
            raise LabError("Metadatos de validación DCA alterados")
        return {**payload, **{key: row[key] for key in metadata}}

    def validation(self, run_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM dca_validation_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise LabError("Validación DCA no encontrada")
            return self._validation_payload(row)

    def validations(self) -> list[dict]:
        if automation_format(self.path) in ("missing", "empty"):
            return []
        with self.connect() as db:
            return [{key: saved.get(key) for key in ("id", "version_id", "dataset_id",
                       "dataset_hash", "created_at", "engine_version", "fraction", "train_bars", "test_bars")}
                    for row in db.execute("SELECT * FROM dca_validation_runs ORDER BY created_at DESC,id")
                    for saved in (self._validation_payload(row),)]

    def run(self, run_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM dca_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise LabError("Simulación DCA no encontrada")
            return {key: row[key] for key in ("id", "version_id", "dataset_id", "dataset_hash", "created_at", "engine_version")} | json.loads(row["result_json"])

    def runs(self) -> list[dict]:
        if automation_format(self.path) in ("missing", "empty"):
            return []
        with self.connect() as db:
            rows = db.execute("SELECT * FROM dca_runs ORDER BY created_at DESC").fetchall()
            return [{key: row[key] for key in ("id", "version_id", "dataset_id", "dataset_hash", "created_at", "engine_version")} | {
                "metrics": {mode: json.loads(row["result_json"])["results"][mode]["metrics"]
                            for mode in ("conditional", "fixed", "lump")}} for row in rows]

    def save_paper_dca_gap_report(self, snapshot: dict, downloaded: list[dict], result: dict) -> dict:
        """Archiva sólo evidencia hipotética; no escribe sesiones ni eventos PAPER."""
        try:
            replay = analyze_dca_paper_gap(snapshot, downloaded, now=snapshot["reconstruction_now"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LabError(f"Informe DCA no verificable: {exc}") from exc
        if replay != result or result.get("session_id") != snapshot.get("id"):
            raise LabError("El informe DCA no coincide con sus datos originales")
        material = {"snapshot": snapshot, "downloaded": downloaded, "result": result}
        digest = content_hash(material)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM paper_dca_sessions WHERE id=?", (snapshot["id"],)).fetchone()
            if (row is None or row["status"] != "GAP"
                    or row["version_id"] != snapshot["version_id"]
                    or row["dataset_id"] != snapshot["dataset_id"]
                    or row["symbol"] != snapshot["symbol"]
                    or row["source"] != snapshot["source"]
                    or row["dataset_hash"] != snapshot["dataset_hash"]
                    or row["engine_version"] != snapshot["engine_version"]
                    or row["next_exec_t"] != snapshot["next_exec_t"]
                    or row["tick_index"] != snapshot["tick_index"]
                    or json.loads(row["rule_json"]) != snapshot["rule"]
                    or json.loads(row["state_json"]) != snapshot["state"]
                    or json.loads(row["overlap_json"]) != snapshot["overlap"]):
                raise LabError("La sesión PAPER DCA cambió antes de guardar el informe")
            version = db.execute("SELECT definition_json FROM dca_versions WHERE id=?",
                                 (snapshot["version_id"],)).fetchone()
            if version is None or json.loads(version["definition_json"]) != snapshot["rule"]:
                raise LabError("La versión DCA cambió antes de guardar el informe")
            existing = db.execute("SELECT id FROM paper_dca_gap_reports WHERE evidence_hash=?", (digest,)).fetchone()
            identifier = existing["id"] if existing else str(uuid4())
            if not existing:
                db.execute("INSERT INTO paper_dca_gap_reports VALUES (?,?,?,?,?)",
                           (identifier, snapshot["id"], datetime.now(timezone.utc).isoformat(),
                            _json(material), digest))
        return self.paper_dca_gap_report(identifier)

    def paper_dca_gap_report(self, report_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM paper_dca_gap_reports WHERE id=?", (report_id,)).fetchone()
            if row is None:
                raise LabError("Informe DCA no encontrado")
            saved = dict(row)
        try:
            material = json.loads(saved["evidence_json"])
            snapshot, downloaded, result = (material[key] for key in ("snapshot", "downloaded", "result"))
            replay = analyze_dca_paper_gap(snapshot, downloaded, now=snapshot["reconstruction_now"])
        except (KeyError, TypeError, ValueError, LabError) as exc:
            raise LabError(f"Informe DCA archivado no verificable: {exc}") from exc
        if (content_hash(material) != saved["evidence_hash"] or replay != result
                or snapshot["id"] != saved["session_id"]):
            raise LabError("Informe DCA archivado alterado")
        return {"id": saved["id"], "session_id": saved["session_id"],
                "hash": saved["evidence_hash"], "created_at": saved["created_at"],
                "verified": True, "snapshot": snapshot, "downloaded": downloaded, "result": result}

