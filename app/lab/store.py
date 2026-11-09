"""Persistencia LAB separada, inmutable y reproducible (SQLite local)."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from uuid import uuid4

import sqlcipher3

from app.lab.ai_research import COMPARISON_SCHEMA
from app.lab.schema import CURRENT_SCHEMA_VERSION
from app.lab.engine import (Candle, INTERVALS, LabError, MAX_DATASET_CANDLES,
                            validate_candle_values)
from app.lab.paper_gap import analyze_paper_gap
from app.lab.reserve_reuse import reserve_reuse
from app.lab.access import lab_access, lab_format
from app.core.local_sqlite import ensure_private_sqlite
from app.security.sensitive_text import (contains_sensitive_ai_data, contains_sensitive_ai_text,
                                         contains_wallet_address)


def encoded(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(value: object) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def validate_dataset_coverage(timeframe: str, candles: list[dict], gaps: list[dict],
                              start_t: int, end_t: int) -> None:
    """Comprueba que los metadatos de cobertura describen las velas guardadas."""
    if timeframe not in INTERVALS or not isinstance(candles, list) or not candles:
        raise LabError("La cobertura del dataset no es válida")
    step = INTERVALS[timeframe]
    times = [bar.get("t") if isinstance(bar, dict) else None for bar in candles]
    if (any(type(stamp) is not int or stamp <= 0 or stamp % step for stamp in times)
            or start_t != times[0] or end_t != times[-1]):
        raise LabError("La cobertura del dataset no coincide con sus velas")
    actual_gaps = []
    for previous, current in zip(times, times[1:]):
        difference = current - previous
        if difference <= 0 or difference % step:
            raise LabError("La cobertura del dataset contiene velas desordenadas")
        if difference > step:
            actual_gaps.append({"after": previous, "before": current,
                                "missing": difference // step - 1})
    if gaps != actual_gaps:
        raise LabError("La cobertura del dataset no coincide con sus huecos")


RESEARCH_DDL = (
    """CREATE TABLE research_runs (
        id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES datasets(id),
        dataset_hash TEXT NOT NULL, created_at TEXT NOT NULL,
        engine_version TEXT NOT NULL, rule_json TEXT NOT NULL, result_json TEXT NOT NULL
    )""",
    """CREATE TABLE research_events (
        run_id TEXT NOT NULL REFERENCES research_runs(id), index_no INTEGER NOT NULL,
        event_json TEXT NOT NULL, PRIMARY KEY(run_id, index_no)
    )""",
)


def write_strategy_version(db: sqlite3.Connection, strategy_id: str, definition: dict, version_id: str) -> dict:
    strategy = db.execute("SELECT name FROM strategies WHERE id=?", (strategy_id,)).fetchone()
    if not strategy:
        raise LabError("Estrategia no encontrada")
    version = db.execute("SELECT COALESCE(MAX(version),0)+1 FROM strategy_versions WHERE strategy_id=?",
                         (strategy_id,)).fetchone()[0]
    db.execute("INSERT INTO strategy_versions VALUES (?,?,?,?,?)",
               (version_id, strategy_id, version, datetime.now(timezone.utc).isoformat(), encoded(definition)))
    return {"id": strategy_id, "name": strategy["name"], "version": version,
            "version_id": version_id, "definition": definition}


class LabStore:
    def __init__(self, path: Path, *, passphrase: str | None = None,
                 allow_create: bool = True):
        self.path = Path(path).expanduser().absolute()
        if passphrase is not None and not 12 <= len(passphrase) <= 1024:
            raise LabError("La contraseña LAB debe tener entre 12 y 1024 caracteres")
        self._passphrase = passphrase
        self._allow_create = allow_create

    @property
    def encrypted(self) -> bool:
        return self.current_passphrase() is not None

    def current_passphrase(self) -> str | None:
        return self._passphrase if self._passphrase is not None else lab_access.get(self.path)

    @contextmanager
    def connect(self):
        if not self._allow_create and lab_format(self.path) in ("missing", "empty"):
            raise LabError("Crea primero la base LAB cifrada en Ajustes")
        ensure_private_sqlite(self.path)
        db = sqlcipher3.connect(self.path, timeout=5)
        try:
            db.row_factory = sqlcipher3.Row
            passphrase = self.current_passphrase()
            if passphrase is not None:
                # SQLCipher no admite parámetros en PRAGMA key.
                db.execute("PRAGMA key = '" + passphrase.replace("'", "''") + "'")
            db.execute("PRAGMA foreign_keys=ON")
            try:
                version = db.execute("PRAGMA user_version").fetchone()[0]
            except sqlcipher3.DatabaseError:
                raise LabError("LAB no se pudo abrir; contraseña o formato incompatibles") from None
            if 0 <= version < CURRENT_SCHEMA_VERSION:
                # La segunda lectura, bajo bloqueo de escritura, impide que
                # dos peticiones inicialicen o migren la misma base a la vez.
                db.execute("BEGIN IMMEDIATE")
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version == 0:
                    for statement in ("""CREATE TABLE datasets (
                    id TEXT PRIMARY KEY, symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
                    source TEXT NOT NULL, fetched_at TEXT NOT NULL, start_t INTEGER NOT NULL,
                    end_t INTEGER NOT NULL, hash TEXT NOT NULL UNIQUE, gaps_json TEXT NOT NULL,
                    candles_json TEXT NOT NULL
                )""", """CREATE TABLE strategies (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL
                )""", """CREATE TABLE strategy_versions (
                    id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL REFERENCES strategies(id),
                    version INTEGER NOT NULL, created_at TEXT NOT NULL, definition_json TEXT NOT NULL,
                    UNIQUE(strategy_id, version)
                )""", """CREATE TABLE runs (
                    id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES datasets(id),
                    version_id TEXT NOT NULL REFERENCES strategy_versions(id), created_at TEXT NOT NULL,
                    engine_version TEXT NOT NULL, result_json TEXT NOT NULL
                )""", """CREATE TABLE detected_events (
                    run_id TEXT NOT NULL REFERENCES runs(id), index_no INTEGER NOT NULL,
                    event_json TEXT NOT NULL, PRIMARY KEY(run_id, index_no)
                )""", """CREATE TABLE simulated_trades (
                    run_id TEXT NOT NULL REFERENCES runs(id), index_no INTEGER NOT NULL,
                    trade_json TEXT NOT NULL, PRIMARY KEY(run_id, index_no)
                )"""):
                        db.execute(statement)
                if version in (0, 1):
                    for statement in RESEARCH_DDL:
                        db.execute(statement)
                    db.execute("PRAGMA user_version=2")
                    version = 2
                if version == 2:
                    # Una etiqueta source introducida manualmente no prueba
                    # que el histórico se descargó del proveedor indicado.
                    db.execute("ALTER TABLE datasets ADD COLUMN verified_public INTEGER NOT NULL DEFAULT 0")
                    db.execute("PRAGMA user_version=3")
                    version = 3
                if version == 3:
                    db.execute("""CREATE TABLE validation_runs (
                        id TEXT PRIMARY KEY,
                        dataset_id TEXT NOT NULL REFERENCES datasets(id),
                        version_id TEXT NOT NULL REFERENCES strategy_versions(id),
                        dataset_hash TEXT NOT NULL, created_at TEXT NOT NULL,
                        engine_version TEXT NOT NULL, result_json TEXT NOT NULL
                    )""")
                    db.execute("PRAGMA user_version=4")
                    version = 4
                if version == 4:
                    db.execute("""CREATE TABLE optimization_runs (
                        id TEXT PRIMARY KEY,
                        dataset_id TEXT NOT NULL REFERENCES datasets(id),
                        version_id TEXT NOT NULL REFERENCES strategy_versions(id),
                        dataset_hash TEXT NOT NULL, created_at TEXT NOT NULL,
                        engine_version TEXT NOT NULL, optimizer_version TEXT NOT NULL,
                        result_json TEXT NOT NULL
                    )""")
                    db.execute("PRAGMA user_version=5")
                    version = 5
                if version == 5:
                    db.execute("""CREATE TABLE strategy_imports (
                        strategy_id TEXT PRIMARY KEY REFERENCES strategies(id),
                        source_strategy_id TEXT NOT NULL, source_version_id TEXT NOT NULL,
                        source_version INTEGER NOT NULL, source_definition_hash TEXT NOT NULL,
                        imported_at TEXT NOT NULL
                    )""")
                    db.execute("PRAGMA user_version=6")
                    version = 6
                if version == 6:
                    db.execute("""CREATE TABLE dataset_refreshes (
                        parent_id TEXT NOT NULL REFERENCES datasets(id),
                        child_id TEXT NOT NULL REFERENCES datasets(id),
                        parent_hash TEXT NOT NULL, child_hash TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY(parent_id, child_id),
                        CHECK(parent_id != child_id)
                    )""")
                    db.execute("PRAGMA user_version=7")
                    version = 7
                if version == 7:
                    db.execute("""CREATE TABLE paper_gap_reports (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        venue TEXT NOT NULL CHECK(venue IN ('MEXC_SPOT','GRVT_PERP')),
                        dataset_id TEXT NOT NULL REFERENCES datasets(id),
                        dataset_hash TEXT NOT NULL,
                        version_id TEXT NOT NULL REFERENCES strategy_versions(id),
                        engine_version TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        snapshot_json TEXT NOT NULL,
                        downloaded_json TEXT NOT NULL,
                        result_json TEXT NOT NULL,
                        hash TEXT NOT NULL UNIQUE
                    )""")
                    db.execute("""CREATE INDEX ix_paper_gap_reports_session
                                  ON paper_gap_reports(session_id, created_at)""")
                    db.execute("PRAGMA user_version=8")
                    version = 8
                if version == 8:
                    db.execute("""CREATE TABLE ai_analyses (
                        id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('research','backtest')),
                        run_id TEXT NOT NULL, created_at TEXT NOT NULL,
                        evidence_json TEXT NOT NULL, question TEXT NOT NULL,
                        answer TEXT NOT NULL, model TEXT NOT NULL, usage_json TEXT NOT NULL,
                        hash TEXT NOT NULL
                    )""")
                    db.execute("CREATE INDEX ix_ai_analyses_run ON ai_analyses(kind,run_id,created_at)")
                    db.execute("PRAGMA user_version=9")
                    version = 9
                if version == 9:
                    from app.lab.perp_store import PERP_DATASETS_DDL
                    db.execute(PERP_DATASETS_DDL)
                    db.execute("PRAGMA user_version=10")
                    version = 10
                if version == 10:
                    from app.lab.perp_experiments import PERP_EXPERIMENTS_DDL
                    for statement in PERP_EXPERIMENTS_DDL:
                        db.execute(statement)
                    db.execute("PRAGMA user_version=11")
                    version = 11
                if version == 11:
                    from app.lab.perp_validation_store import PERP_VALIDATIONS_DDL
                    for statement in PERP_VALIDATIONS_DDL:
                        db.execute(statement)
                    db.execute("PRAGMA user_version=12")
                    version = 12
                if version == 12:
                    from app.lab.perp_rules import PERP_RULE_OBSERVATIONS_DDL
                    for statement in PERP_RULE_OBSERVATIONS_DDL:
                        db.execute(statement)
                    db.execute("PRAGMA user_version=13")
                    version = 13
                if version == 13:
                    from app.lab.perp_optimization_store import PERP_OPTIMIZATIONS_DDL
                    for statement in PERP_OPTIMIZATIONS_DDL:
                        db.execute(statement)
                    db.execute("PRAGMA user_version=14")
                    version = 14
                if version == 14:
                    from app.lab.perp_optimization_store import PERP_OPTIMIZATION_VARIANTS_DDL
                    db.execute(PERP_OPTIMIZATION_VARIANTS_DDL)
                    db.execute("PRAGMA user_version=15")
                    version = 15
                db.commit()
            if version != CURRENT_SCHEMA_VERSION:
                raise LabError(f"Versión de LAB no compatible: {version}")
            self.path.chmod(0o600)
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def save_ai_analysis(self, kind: str, run_id: str, evidence: dict,
                         question: str, result: dict) -> dict:
        """Archivo optativo de interpretación, nunca credenciales ni datos REAL."""
        if (contains_sensitive_ai_text(question, question=True) or contains_sensitive_ai_data(evidence)
                or contains_sensitive_ai_text(result["answer"], question=True)
                or contains_sensitive_ai_data(result)):
            raise LabError("El archivo de IA no admite direcciones completas ni identificadores sensibles")
        if not self.encrypted or lab_format(self.path) != "encrypted_or_invalid":
            raise LabError("El archivo de IA requiere LAB cifrado y desbloqueado")
        if kind not in ("research", "backtest"):
            raise LabError("Tipo de análisis de IA no compatible")
        if evidence.get("source_run") != run_id or evidence.get("environment") != "LAB":
            raise LabError("La evidencia de IA no corresponde a esta ejecución LAB")
        material = {"kind": kind, "run_id": run_id, "evidence": evidence,
                    "question": question, "answer": result["answer"],
                    "model": result["model"], "usage": result.get("usage")}
        record = {"id": str(uuid4()), "created_at": datetime.now(timezone.utc).isoformat(),
                  **material, "hash": content_hash(material)}
        with self.connect() as db:
            table = "research_runs" if kind == "research" else "runs"
            if evidence.get("schema") == COMPARISON_SCHEMA:
                if kind != "backtest":
                    raise LabError("La comparación requiere dos backtests")
                self._verify_ai_comparison_sources(db, run_id, evidence)
            elif db.execute(f"SELECT id FROM {table} WHERE id=?", (run_id,)).fetchone() is None:
                raise LabError("La ejecución LAB ya no está disponible")
            db.execute("""INSERT INTO ai_analyses
                (id,kind,run_id,created_at,evidence_json,question,answer,model,usage_json,hash)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (record["id"], kind, run_id, record["created_at"], encoded(evidence), question,
                 result["answer"], result["model"], encoded(result.get("usage")), record["hash"]))
        return record

    @staticmethod
    def _verify_ai_comparison_sources(db, run_id: str, evidence: dict) -> None:
        sources = evidence.get("runs")
        if (not isinstance(sources, list) or len(sources) != 2 or
                any(not isinstance(source, dict) for source in sources) or
                sources[0].get("run_id") != run_id or sources[1].get("run_id") == run_id):
            raise LabError("La comparación no identifica dos backtests distintos")
        db.execute("BEGIN IMMEDIATE")
        for source in sources:
            saved = db.execute("SELECT version_id, result_json FROM runs WHERE id=?",
                               (source.get("run_id"),)).fetchone()
            if saved is None or saved["version_id"] != source.get("strategy_version_id"):
                raise LabError("Cambió una de las pruebas de la comparación")
            result = json.loads(saved["result_json"])
            if result.get("result_hash") != source.get("result_hash"):
                raise LabError("Cambió una de las pruebas de la comparación")

    def ai_analyses(self, kind: str, run_id: str) -> list[dict]:
        if kind not in ("research", "backtest"):
            raise LabError("Tipo de análisis de IA no compatible")
        with self.connect() as db:
            rows = db.execute("""SELECT * FROM ai_analyses WHERE kind=? AND run_id=?
                                 ORDER BY created_at DESC,id DESC""", (kind, run_id)).fetchall()
            return [self._ai_record(row) for row in rows]

    @staticmethod
    def _ai_record(row) -> dict:
        try:
            material = {"kind": row["kind"], "run_id": row["run_id"],
                        "evidence": json.loads(row["evidence_json"]), "question": row["question"],
                        "answer": row["answer"], "model": row["model"], "usage": json.loads(row["usage_json"])}
            if not isinstance(material["evidence"], dict) or content_hash(material) != row["hash"]:
                raise ValueError("hash")
        except (ValueError, TypeError) as exc:
            raise LabError("El archivo de IA tiene una huella incorrecta") from exc
        return {"id": row["id"], "created_at": row["created_at"], "hash": row["hash"], **material}

    def ai_archive_page(self, *, kind: str | None, offset: int, limit: int) -> dict:
        filters = "WHERE kind=?" if kind is not None else ""
        params = (kind, limit + 1, offset) if kind is not None else (limit + 1, offset)
        with self.connect() as db:
            rows = db.execute(f"""SELECT * FROM ai_analyses {filters}
                ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?""", params).fetchall()
        items = []
        for row in rows[:limit]:
            record = self._ai_record(row)
            evidence = record["evidence"]
            dataset = evidence.get("dataset", {})
            comparison = evidence.get("schema") == COMPARISON_SCHEMA
            if not isinstance(dataset, dict):
                dataset = {}
            if comparison:
                base, alternative = (source["dataset"] for source in evidence["runs"])
                dataset = {key: base[key] if base[key] == alternative[key]
                           else f"{base[key]} / {alternative[key]}" for key in ("symbol", "timeframe")}
            items.append({key: record[key] for key in ("id", "kind", "run_id", "created_at", "question", "model", "hash")} |
                         {"symbol": dataset.get("symbol"), "timeframe": dataset.get("timeframe")} |
                         ({"comparison": True} if comparison else {}))
        return {"items": items, "offset": offset, "has_more": len(rows) > limit}

    def ai_archive_record(self, analysis_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM ai_analyses WHERE id=?", (analysis_id,)).fetchone()
        return self._ai_record(row) if row is not None else None

    def ai_archive_selection(self, expected: dict[str, str]) -> list[dict]:
        marks = ",".join("?" for _ in expected)
        with self.connect() as db:
            rows = db.execute(f"""SELECT * FROM ai_analyses WHERE id IN ({marks})
                ORDER BY created_at,id""", tuple(expected)).fetchall()
        if len(rows) != len(expected):
            raise LabError("Falta un análisis seleccionado; actualiza la lista")
        records = []
        for row in rows:
            record = self._ai_record(row)
            if record["hash"] != expected[record["id"]]:
                raise LabError("Un análisis seleccionado cambió; vuelve a seleccionarlo")
            records.append(record)
        return records

    def delete_ai_analysis(self, kind: str, run_id: str, analysis_id: str,
                           expected_hash: str) -> bool:
        """Borra sólo el registro solicitado; el histórico LAB queda intacto."""
        if not self.encrypted or lab_format(self.path) != "encrypted_or_invalid":
            raise LabError("El archivo de IA requiere LAB cifrado y desbloqueado")
        if kind not in ("research", "backtest"):
            raise LabError("Tipo de análisis de IA no compatible")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT * FROM ai_analyses
                WHERE id=? AND kind=? AND run_id=?""",
                (analysis_id, kind, run_id)).fetchone()
            if row is None:
                return False
            self._ai_record(row)
            if row["hash"] != expected_hash:
                raise LabError("El análisis de IA cambió; vuelve a cargar el archivo")
            db.execute("DELETE FROM ai_analyses WHERE id=?", (analysis_id,))
        return True

    def save_dataset(self, symbol: str, timeframe: str, source: str, candles: list[Candle], gaps: list[dict],
                     *, verified_public: bool = False) -> dict:
        if not isinstance(source, str) or contains_wallet_address(source):
            raise LabError("La fuente LAB no admite direcciones completas de wallet")
        if not isinstance(candles, list) or not 3 <= len(candles) <= MAX_DATASET_CANDLES:
            raise LabError(f"Se requieren entre 3 y {MAX_DATASET_CANDLES} velas")
        validate_candle_values(candles, timeframe)
        data = [bar.json() for bar in candles]
        validate_dataset_coverage(timeframe, data, gaps, candles[0].t, candles[-1].t)
        # Huella de contenido y metadatos relevantes; no de la hora de importación.
        material = {"symbol": symbol, "timeframe": timeframe, "source": source, "candles": data}
        if verified_public:
            material["verified_public"] = True
        digest = content_hash(material)
        with self.connect() as db:
            return self._save_dataset_row(db, symbol, timeframe, source, candles, gaps,
                                          data, digest, verified_public)

    @staticmethod
    def _save_dataset_row(db, symbol, timeframe, source, candles, gaps, data, digest, verified_public):
        existing = db.execute("SELECT * FROM datasets WHERE hash=?", (digest,)).fetchone()
        if existing:
            return LabStore._dataset_meta(existing)
        db.execute("""INSERT OR IGNORE INTO datasets
                    (id,symbol,timeframe,source,fetched_at,start_t,end_t,hash,gaps_json,candles_json,verified_public)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                   (str(uuid4()), symbol, timeframe, source, datetime.now(timezone.utc).isoformat(),
                    candles[0].t, candles[-1].t, digest, encoded(gaps), encoded(data), int(verified_public)))
        return LabStore._dataset_meta(db.execute("SELECT * FROM datasets WHERE hash=?", (digest,)).fetchone())

    def save_refreshed_dataset(self, parent_id: str, parent_hash: str, symbol: str,
                               timeframe: str, source: str, candles: list[Candle]) -> dict:
        """Guarda la nueva semilla y su procedencia en una sola transacción."""
        if not isinstance(candles, list) or not 3 <= len(candles) <= MAX_DATASET_CANDLES:
            raise LabError(f"Se requieren entre 3 y {MAX_DATASET_CANDLES} velas para actualizar PAPER")
        validate_candle_values(candles, timeframe)
        step = INTERVALS[timeframe]
        if any(current.t - previous.t != step for previous, current in zip(candles, candles[1:])):
            raise LabError("El histórico actualizado contiene huecos o velas desordenadas")
        data = [bar.json() for bar in candles]
        digest = content_hash({"symbol": symbol, "timeframe": timeframe,
                               "source": source, "candles": data, "verified_public": True})
        with self.connect() as db:
            parent = db.execute("SELECT * FROM datasets WHERE id=?", (parent_id,)).fetchone()
            if (parent is None or parent["hash"] != parent_hash
                    or not parent["verified_public"] or parent["source"] != source
                    or parent["symbol"] != symbol or parent["timeframe"] != timeframe):
                raise LabError("El histórico de origen cambió o no corresponde a la nueva semilla")
            parent_material = {"symbol": parent["symbol"], "timeframe": parent["timeframe"],
                               "source": parent["source"],
                               "candles": json.loads(parent["candles_json"]),
                               "verified_public": True}
            if content_hash(parent_material) != parent_hash:
                raise LabError("La huella del histórico de origen no coincide con sus velas")
            child = self._save_dataset_row(db, symbol, timeframe, source, candles, [],
                                           data, digest, True)
            if child["id"] != parent_id:
                cycle = db.execute("""WITH RECURSIVE descendants(id) AS (
                                        SELECT child_id FROM dataset_refreshes WHERE parent_id=?
                                        UNION
                                        SELECT r.child_id FROM dataset_refreshes r
                                        JOIN descendants d ON r.parent_id=d.id
                                      ) SELECT 1 FROM descendants WHERE id=? LIMIT 1""",
                                   (child["id"], parent_id)).fetchone()
                if cycle:
                    raise LabError("La actualización formaría un ciclo de procedencia")
                existing = db.execute("""SELECT parent_hash,child_hash FROM dataset_refreshes
                                         WHERE parent_id=? AND child_id=?""",
                                      (parent_id, child["id"])).fetchone()
                if existing and (existing["parent_hash"] != parent_hash or
                                 existing["child_hash"] != child["hash"]):
                    raise LabError("La procedencia guardada tiene huellas distintas")
                if not existing:
                    db.execute("""INSERT INTO dataset_refreshes
                                  (parent_id,child_id,parent_hash,child_hash,created_at)
                                  VALUES (?,?,?,?,?)""",
                               (parent_id, child["id"], parent_hash, child["hash"],
                                datetime.now(timezone.utc).isoformat()))
            return child

    def dataset_lineage(self, dataset_id: str) -> list[dict]:
        """Orígenes directos fijados con huella; no infiere equivalencia de resultados."""
        self.dataset(dataset_id)
        with self.connect() as db:
            rows = db.execute("""SELECT r.parent_id,r.parent_hash,r.child_hash,r.created_at,
                                       p.hash AS current_parent_hash,c.hash AS current_child_hash
                                FROM dataset_refreshes r
                                JOIN datasets p ON p.id=r.parent_id
                                JOIN datasets c ON c.id=r.child_id
                                WHERE r.child_id=? ORDER BY r.created_at,r.parent_id""",
                              (dataset_id,)).fetchall()
            if any(row["parent_hash"] != row["current_parent_hash"] or
                   row["child_hash"] != row["current_child_hash"] for row in rows):
                raise LabError("La procedencia del histórico tiene huellas distintas")
            return [{key: row[key] for key in ("parent_id", "parent_hash", "child_hash", "created_at")}
                    for row in rows]

    def save_paper_gap_report(self, venue: str, snapshot: dict,
                              downloaded: list[dict], result: dict) -> dict:
        """Archiva evidencia teórica sin tocar el libro PAPER ni datos REAL."""
        source = {"MEXC_SPOT": "mexc-spot-public",
                  "GRVT_PERP": "grvt-perp-trade-public"}.get(venue)
        if source is None or not isinstance(snapshot, dict) or not isinstance(downloaded, list):
            raise LabError("Reconstrucción PAPER inválida")
        try:
            replay = analyze_paper_gap(snapshot, snapshot["definition"], snapshot["history"],
                                       downloaded, now=snapshot["reconstruction_now"], source=source)
        except (KeyError, TypeError, ValueError) as exc:
            raise LabError(f"Reconstrucción PAPER no verificable: {exc}") from exc
        if replay != result or result["session_id"] != snapshot["id"]:
            raise LabError("El resultado del hueco no coincide con sus velas y reglas")
        material = {"venue": venue, "snapshot": snapshot,
                    "downloaded": downloaded, "result": result}
        digest = content_hash(material)
        with self.connect() as db:
            dataset = db.execute("SELECT * FROM datasets WHERE id=?",
                                 (snapshot["dataset_id"],)).fetchone()
            version = db.execute("SELECT definition_json FROM strategy_versions WHERE id=?",
                                 (snapshot["version_id"],)).fetchone()
            if (dataset is None or dataset["hash"] != snapshot["dataset_hash"]
                    or dataset["symbol"] != snapshot["symbol"]
                    or dataset["timeframe"] != snapshot["timeframe"]
                    or snapshot["history"][:len(json.loads(dataset["candles_json"]))]
                    != json.loads(dataset["candles_json"])
                    or version is None or json.loads(version[0]) != snapshot["definition"]):
                raise LabError("El origen LAB cambió antes de guardar el hueco")
            existing = db.execute("SELECT id FROM paper_gap_reports WHERE hash=?", (digest,)).fetchone()
            if existing:
                identifier = existing["id"]
            else:
                identifier = str(uuid4())
                db.execute("""INSERT INTO paper_gap_reports VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (identifier, snapshot["id"], venue, snapshot["dataset_id"],
                            snapshot["dataset_hash"], snapshot["version_id"],
                            snapshot["engine_version"], datetime.now(timezone.utc).isoformat(),
                            encoded(snapshot), encoded(downloaded), encoded(result), digest))
        return self.paper_gap_report(identifier)

    def paper_gap_report(self, report_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM paper_gap_reports WHERE id=?", (report_id,)).fetchone()
            if row is None:
                raise LabError("Reconstrucción PAPER no encontrada")
            saved = dict(row)
        try:
            snapshot = json.loads(saved["snapshot_json"])
            downloaded = json.loads(saved["downloaded_json"])
            result = json.loads(saved["result_json"])
            material = {"venue": saved["venue"], "snapshot": snapshot,
                        "downloaded": downloaded, "result": result}
            source = {"MEXC_SPOT": "mexc-spot-public",
                      "GRVT_PERP": "grvt-perp-trade-public"}[saved["venue"]]
            replay = analyze_paper_gap(snapshot, snapshot["definition"], snapshot["history"],
                                       downloaded, now=snapshot["reconstruction_now"], source=source)
            dataset, seed = self.dataset(saved["dataset_id"])
            version = self.version(saved["version_id"])
        except (LabError, KeyError, TypeError, ValueError) as exc:
            raise LabError(f"Reconstrucción PAPER no verificable: {exc}") from exc
        if (content_hash(material) != saved["hash"] or replay != result
                or saved["session_id"] != snapshot["id"]
                or saved["dataset_id"] != snapshot["dataset_id"]
                or saved["dataset_hash"] != snapshot["dataset_hash"]
                or saved["version_id"] != snapshot["version_id"]
                or saved["engine_version"] != snapshot["engine_version"]
                or dataset["hash"] != saved["dataset_hash"]
                or snapshot["history"][:len(seed)] != seed
                or version["definition"] != snapshot["definition"]):
            raise LabError("Reconstrucción PAPER alterada o con origen distinto")
        return {"id": saved["id"], "hash": saved["hash"], "created_at": saved["created_at"],
                "venue": saved["venue"], "dataset_id": saved["dataset_id"],
                "version_id": saved["version_id"], "snapshot": snapshot,
                "downloaded": downloaded, "result": result}

    def paper_gap_reports(self, session_id: str) -> list[dict]:
        with self.connect() as db:
            identifiers = [row[0] for row in db.execute(
                "SELECT id FROM paper_gap_reports WHERE session_id=? ORDER BY created_at DESC,id DESC",
                (session_id,))]
        return [{key: saved[key] for key in ("id", "hash", "created_at", "venue", "result")}
                for identifier in identifiers
                for saved in (self.paper_gap_report(identifier),)]

    @staticmethod
    def _dataset_meta(row: sqlite3.Row) -> dict:
        candles = json.loads(row["candles_json"])
        gaps = json.loads(row["gaps_json"])
        validate_dataset_coverage(row["timeframe"], candles, gaps,
                                  row["start_t"], row["end_t"])
        return {key: row[key] for key in ("id", "symbol", "timeframe", "source", "fetched_at", "start_t", "end_t", "hash")} | {
            "verified_public": bool(row["verified_public"]),
            "gaps": gaps, "count": len(candles)}

    def datasets(self) -> list[dict]:
        if lab_format(self.path) in ("missing", "empty"):
            return []
        with self.connect() as db:
            return [self._dataset_meta(row) for row in db.execute("SELECT * FROM datasets ORDER BY fetched_at DESC")]

    def dataset(self, dataset_id: str) -> tuple[dict, list[dict]]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone()
            if not row:
                raise LabError("Dataset no encontrado")
            candles = json.loads(row["candles_json"])
            material = {"symbol": row["symbol"], "timeframe": row["timeframe"],
                        "source": row["source"], "candles": candles}
            if row["verified_public"]:
                material["verified_public"] = True
            actual = content_hash(material)
            if actual != row["hash"]:
                raise LabError("Huella del dataset incorrecta; no se puede reproducir la prueba")
            return self._dataset_meta(row), candles

    def dataset_chart_candles(self, dataset_id: str, from_t: int, to_t: int,
                              after_index: int = -1, limit: int = 2000) -> dict:
        """Velas exactas de un tramo; verifica antes la huella del dataset."""
        meta, candles = self.dataset(dataset_id)
        selected = []
        for index, candle in enumerate(candles):
            stamp = candle["t"]
            if index > after_index and from_t <= stamp <= to_t:
                selected.append((index, candle))
                if len(selected) > limit:
                    break
        page = selected[:limit]
        return {"dataset_id": dataset_id, "dataset_hash": meta["hash"],
                "from_t": from_t, "to_t": to_t,
                "candles": [candle for _, candle in page],
                "next_after_index": page[-1][0] if len(selected) > limit else None,
                "has_more": len(selected) > limit}

    def create_strategy(self, name: str, definition: dict) -> dict:
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
            raise LabError("Nombre de estrategia inválido")
        if contains_wallet_address(name):
            raise LabError("El nombre LAB no admite direcciones completas de wallet")
        strategy_id, version_id = str(uuid4()), str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            db.execute("INSERT INTO strategies VALUES (?,?,?)", (strategy_id, name.strip(), now))
            db.execute("INSERT INTO strategy_versions VALUES (?,?,?,?,?)", (version_id, strategy_id, 1, now, encoded(definition)))
        return {"id": strategy_id, "name": name.strip(), "version": 1, "version_id": version_id, "definition": definition}

    def import_strategy(self, name: str, definition: dict, origin: dict) -> dict:
        """Clona una definición y su procedencia en una sola transacción."""
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
            raise LabError("Nombre de estrategia inválido")
        if contains_wallet_address(name):
            raise LabError("El nombre LAB no admite direcciones completas de wallet")
        strategy_id, version_id = str(uuid4()), str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            db.execute("INSERT INTO strategies VALUES (?,?,?)", (strategy_id, name.strip(), now))
            db.execute("INSERT INTO strategy_versions VALUES (?,?,?,?,?)",
                       (version_id, strategy_id, 1, now, encoded(definition)))
            db.execute("""INSERT INTO strategy_imports VALUES (?,?,?,?,?,?)""",
                       (strategy_id, origin["strategy_id"], origin["version_id"],
                        origin["version"], origin["definition_hash"], now))
        return {"id": strategy_id, "name": name.strip(), "version": 1,
                "version_id": version_id, "definition": definition,
                "imported_from": origin}

    def add_version(self, strategy_id: str, definition: dict) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return write_strategy_version(db, strategy_id, definition, str(uuid4()))

    def strategies(self) -> list[dict]:
        if lab_format(self.path) in ("missing", "empty"):
            return []
        with self.connect() as db:
            rows = db.execute("""SELECT s.id, s.name, v.id AS version_id, v.version, v.definition_json,
                                        i.source_strategy_id, i.source_version_id,
                                        i.source_version, i.source_definition_hash
                                 FROM strategies s JOIN strategy_versions v ON v.strategy_id=s.id
                                 LEFT JOIN strategy_imports i ON i.strategy_id=s.id
                                 WHERE v.version=(SELECT MAX(v2.version) FROM strategy_versions v2 WHERE v2.strategy_id=s.id)
                                 ORDER BY s.created_at DESC""").fetchall()
            return [{"id": row["id"], "name": row["name"], "version_id": row["version_id"],
                     "version": row["version"], "definition": json.loads(row["definition_json"]),
                     "imported_from": ({"strategy_id": row["source_strategy_id"],
                                        "version_id": row["source_version_id"],
                                        "version": row["source_version"],
                                        "definition_hash": row["source_definition_hash"]}
                                       if row["source_strategy_id"] is not None else None)} for row in rows]

    def versions(self, strategy_id: str) -> list[dict]:
        if lab_format(self.path) in ("missing", "empty"):
            return []
        with self.connect() as db:
            return [{"id": row["id"], "strategy_id": row["strategy_id"], "version": row["version"],
                     "created_at": row["created_at"], "definition": json.loads(row["definition_json"])}
                    for row in db.execute("SELECT * FROM strategy_versions WHERE strategy_id=? ORDER BY version", (strategy_id,))]

    def all_versions(self) -> list[dict]:
        """Todas las versiones inmutables para elegir la misma definición en PAPER."""
        if lab_format(self.path) in ("missing", "empty"):
            return []
        with self.connect() as db:
            return [{"id": row["id"], "strategy_id": row["strategy_id"],
                     "name": row["name"], "version": row["version"],
                     "created_at": row["created_at"],
                     "definition": json.loads(row["definition_json"])}
                    for row in db.execute("""SELECT v.*,s.name FROM strategy_versions v
                                             JOIN strategies s ON s.id=v.strategy_id
                                             ORDER BY s.name COLLATE NOCASE,v.version,v.id""")]

    def version(self, version_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM strategy_versions WHERE id=?", (version_id,)).fetchone()
            if not row:
                raise LabError("Versión de estrategia no encontrada")
            return {"id": row["id"], "strategy_id": row["strategy_id"], "version": row["version"],
                    "created_at": row["created_at"], "definition": json.loads(row["definition_json"])}

    def strategy_version(self, strategy_id: str, version_id: str) -> dict:
        """Lee una versión exacta y su identidad sin inferir la última versión."""
        with self.connect() as db:
            row = db.execute("""SELECT s.name, v.id, v.strategy_id, v.version,
                                      v.created_at, v.definition_json
                               FROM strategy_versions v JOIN strategies s ON s.id=v.strategy_id
                               WHERE s.id=? AND v.id=?""", (strategy_id, version_id)).fetchone()
            if not row:
                raise LabError("Versión de estrategia no encontrada")
            return {"id": row["strategy_id"], "name": row["name"],
                    "version_id": row["id"], "version": row["version"],
                    "created_at": row["created_at"],
                    "definition": json.loads(row["definition_json"])}

    @staticmethod
    def _verified_inputs(db: sqlite3.Connection, dataset_id: str, version_id: str,
                         expected_dataset_hash: str | None,
                         expected_strategy_hash: str | None) -> tuple[str, str, str]:
        """Comprueba dentro de la transacción las entradas del cálculo previo."""
        dataset = db.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone()
        if not dataset:
            raise LabError("Dataset no encontrado")
        material = {"symbol": dataset["symbol"], "timeframe": dataset["timeframe"],
                    "source": dataset["source"], "candles": json.loads(dataset["candles_json"])}
        if dataset["verified_public"]:
            material["verified_public"] = True
        if content_hash(material) != dataset["hash"] or (expected_dataset_hash is not None
                                                           and dataset["hash"] != expected_dataset_hash):
            raise LabError("Huella del dataset cambió antes de guardar el backtest")
        version = db.execute("SELECT definition_json FROM strategy_versions WHERE id=?", (version_id,)).fetchone()
        if not version:
            raise LabError("Versión de estrategia no encontrada")
        strategy_hash = content_hash(json.loads(version["definition_json"]))
        if expected_strategy_hash is not None and strategy_hash != expected_strategy_hash:
            raise LabError("Versión de estrategia cambió antes de guardar el backtest")
        return dataset["hash"], dataset["source"], strategy_hash

    def save_run(self, dataset_id: str, version_id: str, result: dict, engine_version: str,
                 *, expected_dataset_hash: str | None = None,
                 expected_strategy_hash: str | None = None,
                 replay_of_run_id: str | None = None) -> dict:
        run_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            # La prueba ya se calculó fuera de la transacción. Revalidamos
            # exactamente sus entradas antes de guardar para impedir que una
            # sustitución concurrente se archive como si fuese el mismo run.
            db.execute("BEGIN IMMEDIATE")
            dataset_hash, dataset_source, strategy_hash = self._verified_inputs(
                db, dataset_id, version_id, expected_dataset_hash, expected_strategy_hash)
            if replay_of_run_id is not None:
                source = db.execute("SELECT dataset_id,version_id,result_json FROM runs WHERE id=?",
                                    (replay_of_run_id,)).fetchone()
                if source is None or source["dataset_id"] != dataset_id or source["version_id"] != version_id:
                    raise LabError("La prueba de origen ya no coincide con el histórico y la versión")
                source_result = json.loads(source["result_json"])
                if (source_result.get("dataset_hash") != dataset_hash
                        or (source_result.get("dataset_source") is not None
                            and source_result["dataset_source"] != dataset_source)
                        or (source_result.get("strategy_hash") is not None
                            and source_result["strategy_hash"] != strategy_hash)):
                    raise LabError("Las entradas de la prueba de origen cambiaron")
                result = {**result, "replay_of_run_id": replay_of_run_id}
            result = {**result, "dataset_hash": dataset_hash, "dataset_source": dataset_source,
                      "strategy_hash": strategy_hash, "result_hash": content_hash(result)}
            db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?)",
                       (run_id, dataset_id, version_id, now, engine_version,
                        encoded({key: value for key, value in result.items() if key not in ("events", "trades")})))
            db.executemany("INSERT INTO detected_events VALUES (?,?,?)",
                           [(run_id, i, encoded(row)) for i, row in enumerate(result["events"])])
            db.executemany("INSERT INTO simulated_trades VALUES (?,?,?)",
                           [(run_id, i, encoded(row)) for i, row in enumerate(result["trades"])])
        return self.run(run_id)

    def run(self, run_id: str) -> dict:
        with self.connect() as db:
            # El resumen, los eventos y las operaciones pertenecen a una
            # única fotografía. Un SELECT implícito no inicia una transacción
            # de lectura estable con el controlador SQLite habitual.
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise LabError("Backtest no encontrado")
            result = json.loads(row["result_json"])
            result["events"] = [json.loads(item[0]) for item in db.execute("SELECT event_json FROM detected_events WHERE run_id=? ORDER BY index_no", (run_id,))]
            result["trades"] = [json.loads(item[0]) for item in db.execute("SELECT trade_json FROM simulated_trades WHERE run_id=? ORDER BY index_no", (run_id,))]
            return {"id": row["id"], "dataset_id": row["dataset_id"], "version_id": row["version_id"],
                    "created_at": row["created_at"], "engine_version": row["engine_version"], **result}

    def run_rows_page(self, run_id: str, kind: str, offset: int, limit: int) -> dict:
        """Lee sólo una página de evidencias, en una fotografía consistente."""
        table, column = {
            "events": ("detected_events", "event_json"),
            "trades": ("simulated_trades", "trade_json"),
        }[kind]
        with self.connect() as db:
            db.execute("BEGIN")
            if not db.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
                raise LabError("Backtest no encontrado")
            total = db.execute(f"SELECT COUNT(*) FROM {table} WHERE run_id=?", (run_id,)).fetchone()[0]
            items = [json.loads(row[0]) for row in db.execute(
                f"SELECT {column} FROM {table} WHERE run_id=? ORDER BY index_no LIMIT ? OFFSET ?",
                (run_id, limit, offset))]
            return {"run_id": run_id, "offset": offset, "limit": limit, "total": total, "items": items}

    def run_chart_view(self, run_id: str, *, include_markers: bool = True) -> dict:
        """Resumen fiel para gráfico; opcionalmente difiere las marcas al rango visible."""
        with self.connect() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise LabError("Backtest no encontrado")
            result = json.loads(row["result_json"])
            exit_events = result.pop("exit_events", [])
            events = []
            if include_markers:
                signal_count = 0
                for item in db.execute("SELECT index_no,event_json FROM detected_events WHERE run_id=? ORDER BY index_no", (run_id,)):
                    event = json.loads(item["event_json"])
                    if event.get("action") == "ENTER_NEXT_OPEN":
                        signal_count += 1
                        events.append({"index": item["index_no"], "signal_time": event["signal_time"],
                                       "action": event["action"]})
            else:
                signal_count = db.execute("""SELECT COUNT(*) FROM detected_events
                    WHERE run_id=? AND json_extract(event_json,'$.action')='ENTER_NEXT_OPEN'""",
                    (run_id,)).fetchone()[0]
            trade_fields = ("entry_time", "exit_time", "entry_price", "exit_price", "direction", "pnl", "reason")
            trades = []
            if include_markers:
                trade_count = ambiguous = 0
                for item in db.execute("SELECT trade_json FROM simulated_trades WHERE run_id=? ORDER BY index_no", (run_id,)):
                    trade = json.loads(item[0])
                    trade_count += 1
                    ambiguous += trade["reason"] in ("STOP_AND_TARGET_AMBIGUOUS", "TRAILING_STOP_AND_TARGET_AMBIGUOUS")
                    trades.append({key: trade[key] for key in trade_fields})
            else:
                trade_count, ambiguous = db.execute("""SELECT COUNT(*),
                    COALESCE(SUM(json_extract(trade_json,'$.reason') IN
                        ('STOP_AND_TARGET_AMBIGUOUS','TRAILING_STOP_AND_TARGET_AMBIGUOUS')),0)
                    FROM simulated_trades WHERE run_id=?""", (run_id,)).fetchone()
            return {"id": row["id"], "dataset_id": row["dataset_id"], "version_id": row["version_id"],
                    "created_at": row["created_at"], "engine_version": row["engine_version"],
                    **result, "events": events, "trades": trades,
                    "exit_event_count": len(exit_events), "ambiguous_exits": ambiguous,
                    "view_mode": "chart", "signal_count": signal_count,
                    "trade_count": trade_count, "markers_windowed": not include_markers}

    def run_chart_markers(self, run_id: str, from_t: int, to_t: int) -> dict:
        """Marcas compactas que tocan una ventana temporal, en una sola lectura."""
        with self.connect() as db:
            db.execute("BEGIN")
            if not db.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
                raise LabError("Backtest no encontrado")
            events = []
            for item in db.execute("""SELECT index_no,event_json FROM detected_events WHERE run_id=?
                AND json_extract(event_json,'$.action')='ENTER_NEXT_OPEN'
                AND json_extract(event_json,'$.signal_time') BETWEEN ? AND ? ORDER BY index_no""",
                (run_id, from_t, to_t)):
                event = json.loads(item["event_json"])
                events.append({"index": item["index_no"], "signal_time": event["signal_time"],
                               "action": event["action"]})
            trade_fields = ("entry_time", "exit_time", "entry_price", "exit_price", "direction", "pnl", "reason")
            trades = []
            for item in db.execute("""SELECT trade_json FROM simulated_trades WHERE run_id=?
                AND (json_extract(trade_json,'$.entry_time') BETWEEN ? AND ?
                     OR json_extract(trade_json,'$.exit_time') BETWEEN ? AND ?)
                ORDER BY index_no""", (run_id, from_t, to_t, from_t, to_t)):
                trade = json.loads(item[0])
                trades.append({key: trade[key] for key in trade_fields})
            return {"run_id": run_id, "from_t": from_t, "to_t": to_t,
                    "events": events, "trades": trades}

    def run_chart_curves(self, run_id: str, from_t: int, to_t: int,
                         after_index: int = -1, limit: int = 2000) -> dict:
        """Puntos exactos de capital/drawdown de un tramo, sin alterar el run."""
        with self.connect() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT result_json FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise LabError("Backtest no encontrado")
            result = json.loads(row["result_json"])
            equity, drawdown = result.get("equity"), result.get("drawdown")
            if (not isinstance(equity, list) or not isinstance(drawdown, list)
                    or len(equity) != len(drawdown)):
                raise LabError("Curvas del backtest inconsistentes")
            points = []
            last_time = -1
            for index, (capital, decline) in enumerate(zip(equity, drawdown)):
                capital_time = capital.get("t") if isinstance(capital, dict) else None
                decline_time = decline.get("t") if isinstance(decline, dict) else None
                if (type(capital_time) is not int or capital_time <= last_time
                        or decline_time != capital_time
                        or not isinstance(capital.get("equity"), str)
                        or not isinstance(decline.get("drawdown_pct"), str)):
                    raise LabError("Curvas del backtest inconsistentes")
                last_time = capital_time
                if index <= after_index or capital_time < from_t or capital_time > to_t:
                    continue
                points.append((index, capital, decline))
                if len(points) > limit:
                    break
            page = points[:limit]
            return {"run_id": run_id, "from_t": from_t, "to_t": to_t,
                    "equity": [capital for _, capital, _ in page],
                    "drawdown": [decline for _, _, decline in page],
                    "next_after_index": page[-1][0] if len(points) > limit else None,
                    "has_more": len(points) > limit}

    def runs(self) -> list[dict]:
        if lab_format(self.path) in ("missing", "empty"):
            return []
        with self.connect() as db:
            summaries = []
            for row in db.execute("SELECT * FROM runs ORDER BY created_at DESC"):
                result = json.loads(row["result_json"])
                summaries.append({"id": row["id"], "dataset_id": row["dataset_id"],
                                  "version_id": row["version_id"], "created_at": row["created_at"],
                                  "engine_version": row["engine_version"],
                                  "dataset_hash": result.get("dataset_hash"),
                                  "integrity": "unchecked"})
            return summaries

    def save_validation(self, dataset_id: str, version_id: str, dataset_hash: str,
                        result: dict, engine_version: str, *,
                        lab_engine_version: str | None = None,
                        expected_strategy_hash: str | None = None) -> dict:
        run_id = str(uuid4())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            _, _, strategy_hash = self._verified_inputs(
                db, dataset_id, version_id, dataset_hash, expected_strategy_hash)
            saved = {**result, "strategy_hash": strategy_hash,
                     "result_hash": content_hash(result)}
            if lab_engine_version is not None:
                saved["lab_engine_version"] = lab_engine_version
            db.execute("INSERT INTO validation_runs VALUES (?,?,?,?,?,?,?)",
                       (run_id, dataset_id, version_id, dataset_hash,
                        datetime.now(timezone.utc).isoformat(), engine_version, encoded(saved)))
        return self.validation(run_id)

    def validation(self, run_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM validation_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise LabError("Validación no encontrada")
            return {key: row[key] for key in ("id", "dataset_id", "version_id", "dataset_hash",
                                             "created_at", "engine_version")} | json.loads(row["result_json"])

    def validations(self) -> list[dict]:
        if lab_format(self.path) in ("missing", "empty"):
            return []
        with self.connect() as db:
            rows = db.execute("SELECT * FROM validation_runs ORDER BY created_at DESC,id").fetchall()
            summaries = []
            for row in rows:
                result = json.loads(row["result_json"])
                summary = {key: row[key] for key in ("id", "dataset_id", "version_id", "dataset_hash",
                                                     "created_at", "engine_version")}
                summary.update({"method": result["method"], "train_bars": result["train_bars"],
                                "test_bars": result["test_bars"]})
                if result["method"] == "ROLLING_FIXED_VERSION":
                    summary.update({"folds": result["folds"], "tested_trades": result["tested_trades"],
                                    "positive_folds": result["positive_folds"]})
                else:
                    summary.update({"train_metrics": result["train"]["metrics"],
                                    "test_metrics": result["out_of_sample"]["metrics"]})
                summaries.append(summary)
            return summaries

    def save_optimization(self, dataset_id: str, version_id: str, dataset_hash: str,
                          result: dict, engine_version: str, *,
                          expected_strategy_hash: str | None = None) -> dict:
        run_id = str(uuid4())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            _, _, strategy_hash = self._verified_inputs(
                db, dataset_id, version_id, dataset_hash, expected_strategy_hash)
            saved = {**result, "strategy_hash": strategy_hash,
                     "result_hash": content_hash(result)}
            db.execute("""INSERT INTO optimization_runs VALUES (?,?,?,?,?,?,?,?)""",
                       (run_id, dataset_id, version_id, dataset_hash,
                        datetime.now(timezone.utc).isoformat(), engine_version,
                        result["optimizer_version"], encoded(saved)))
        return self.optimization(run_id)

    def optimization(self, run_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM optimization_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise LabError("Exploración de parámetros no encontrada")
            return {key: row[key] for key in ("id", "dataset_id", "version_id", "dataset_hash",
                                             "created_at", "engine_version", "optimizer_version")} | json.loads(row["result_json"])

    def optimizations(self) -> list[dict]:
        if lab_format(self.path) in ("missing", "empty"):
            return []
        with self.connect() as db:
            rows = db.execute("SELECT * FROM optimization_runs ORDER BY created_at DESC,id").fetchall()
            summaries = []
            for row in rows:
                result = json.loads(row["result_json"])
                walk_forward = result.get("method") == "WALK_FORWARD_GRID_STOP_TARGET"
                summaries.append({key: row[key] for key in ("id", "dataset_id", "version_id", "dataset_hash",
                                                           "created_at", "engine_version", "optimizer_version")} | {
                    "method": result.get("method"),
                    "selected": None if walk_forward else result["selected"],
                    "candidate_count": (len(result["grid"]["stop_losses"]) * len(result["grid"]["take_profits"])
                                        if walk_forward else len(result["candidates"])),
                    "folds": result["folds"] if walk_forward else None})
            return summaries

    def reserve_reuse(self, kind: str, saved: dict) -> dict:
        """Recalcula solapamientos desde LAB guardado; no altera hashes de runs."""
        if kind not in ("validation", "optimization"):
            raise LabError("Tipo de experimento LAB inválido")
        peers = []
        current_result = None
        with self.connect() as db:
            for peer_kind, table in (("validation", "validation_runs"),
                                     ("optimization", "optimization_runs")):
                rows = db.execute(
                    f"SELECT id,result_json FROM {table} WHERE dataset_hash=?",
                    (saved["dataset_hash"],),
                ).fetchall()
                for row in rows:
                    try:
                        stored = json.loads(row["result_json"])
                        raw = {key: value for key, value in stored.items()
                               if key not in ("strategy_hash", "result_hash", "lab_engine_version")}
                        if (not isinstance(stored.get("result_hash"), str)
                                or content_hash(raw) != stored["result_hash"]):
                            raw = {}
                    except (TypeError, ValueError, AttributeError):
                        raw = {}
                    if peer_kind == kind and row["id"] == saved["id"]:
                        current_result = raw
                        continue
                    peers.append({"id": row["id"], "kind": peer_kind, "result": raw})
        return reserve_reuse(current_result or {}, peers)

    def save_research_run(self, dataset_id: str, dataset_hash: str, result: dict,
                          engine_version: str, *, lab_engine_version: str | None = None,
                          expected_rule_hash: str | None = None) -> dict:
        run_id = str(uuid4())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            dataset = db.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone()
            if dataset is None:
                raise LabError("Dataset no encontrado")
            material = {"symbol": dataset["symbol"], "timeframe": dataset["timeframe"],
                        "source": dataset["source"], "candles": json.loads(dataset["candles_json"])}
            if dataset["verified_public"]:
                material["verified_public"] = True
            if content_hash(material) != dataset_hash or dataset["hash"] != dataset_hash:
                raise LabError("Huella del dataset cambió antes de guardar la investigación")
            rule_hash = content_hash(result["rule"])
            if expected_rule_hash is not None and expected_rule_hash != rule_hash:
                raise LabError("La regla cambió antes de guardar la investigación")
            saved = {**result, "rule_hash": rule_hash,
                     "result_hash": content_hash(result)}
            if lab_engine_version is not None:
                saved["lab_engine_version"] = lab_engine_version
            db.execute("INSERT INTO research_runs VALUES (?,?,?,?,?,?,?)",
                       (run_id, dataset_id, dataset_hash, datetime.now(timezone.utc).isoformat(),
                        engine_version, encoded(result["rule"]),
                        encoded({key: value for key, value in saved.items() if key not in ("rule", "events")})))
            db.executemany("INSERT INTO research_events VALUES (?,?,?)",
                           [(run_id, index, encoded(event)) for index, event in enumerate(saved["events"])])
        return self.research_run(run_id)

    def research_run(self, run_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM research_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise LabError("Investigación no encontrada")
            events = [json.loads(item[0]) for item in db.execute(
                "SELECT event_json FROM research_events WHERE run_id=? ORDER BY index_no", (run_id,))]
            return {key: row[key] for key in ("id", "dataset_id", "dataset_hash", "created_at", "engine_version")} | {
                "rule": json.loads(row["rule_json"]), **json.loads(row["result_json"]), "events": events}

    def research_runs(self) -> list[dict]:
        if lab_format(self.path) in ("missing", "empty"):
            return []
        with self.connect() as db:
            rows = db.execute("SELECT * FROM research_runs ORDER BY created_at DESC").fetchall()
            return [{key: row[key] for key in ("id", "dataset_id", "dataset_hash", "created_at", "engine_version")} | {
                "rule": json.loads(row["rule_json"]),
                "summary": json.loads(row["result_json"])["summary"]} for row in rows]
