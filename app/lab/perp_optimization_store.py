"""Encrypted archive of derivative parameter sweeps and their original inputs."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from uuid import uuid4

from app.lab.access import lab_access
from app.lab.engine import LabError
from app.lab.perp_dataset import PerpDataset
from app.lab.perp_experiments import (
    PerpExperiments, _id, read_profile_row, read_run_row, run_payload, write_profile, write_run,
)
from app.lab.perp_optimization import (
    ENGINE, SCHEMA, PerpOptimizationPlan, calculate_perp_optimization, replay_perp_optimization, variant_inputs,
)
from app.lab.perp_profile import ExecutionProfile
from app.lab.perp_simulation import simulation_engine
from app.lab.perp_store import verified_record
from app.lab.store import LabStore, content_hash, encoded, write_strategy_version


PERP_OPTIMIZATIONS_DDL = (
    '''CREATE TABLE perp_optimizations (
        id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
        dataset_id TEXT NOT NULL REFERENCES perp_datasets(id),
        profile_id TEXT NOT NULL REFERENCES perp_profiles(id),
        payload_json TEXT NOT NULL, hash TEXT NOT NULL UNIQUE
    )''',
    'CREATE INDEX ix_perp_optimizations_created ON perp_optimizations(created_at,id)',
)


PERP_OPTIMIZATION_VARIANTS_DDL = '''CREATE TABLE perp_optimization_variants (
    optimization_id TEXT PRIMARY KEY REFERENCES perp_optimizations(id),
    optimization_hash TEXT NOT NULL,
    version_id TEXT NOT NULL REFERENCES strategy_versions(id),
    profile_id TEXT NOT NULL REFERENCES perp_profiles(id),
    development_run_id TEXT NOT NULL REFERENCES perp_runs(id),
    reserve_run_id TEXT NOT NULL REFERENCES perp_runs(id),
    CHECK(development_run_id != reserve_run_id)
)'''


def _originals(db, dataset_id: str, profile_id: str) -> tuple[str, str, PerpDataset, ExecutionProfile, dict]:
    data_header, dataset = verified_record(
        db.execute('SELECT * FROM perp_datasets WHERE id=?', (dataset_id,)).fetchone())
    profile_header, profile, definition = read_profile_row(
        db, db.execute('SELECT * FROM perp_profiles WHERE id=?', (profile_id,)).fetchone())
    return data_header['hash'], profile_header['hash'], dataset, profile, definition


def _replay(result: dict, dataset: PerpDataset, profile: ExecutionProfile,
            definition: dict, plan: PerpOptimizationPlan) -> None:
    if calculate_perp_optimization(dataset, profile, definition, plan) != result:
        raise LabError('La optimización de derivados no reproduce sus resultados')


def read_optimization_row(db, row, *, replay: bool) -> dict:
    if row is None:
        raise LabError('Optimización de derivados no encontrada')
    try:
        result = json.loads(row['payload_json'])
        plan = PerpOptimizationPlan.model_validate(result['plan'])
        data_hash, profile_hash, dataset, profile, definition = _originals(db, row['dataset_id'], row['profile_id'])
        if (content_hash(result) != row['hash'] or result['schema'] != SCHEMA or result['engine'] != ENGINE
                or result['simulation_engine'] != simulation_engine(profile)
                or result['execution_authority'] != 'NONE' or plan.payload() != result['plan']
                or (data_hash, profile_hash, profile.strategy_hash) !=
                   (result['dataset_hash'], result['profile_hash'], result['strategy_hash'])):
            raise LabError('Originales o huella de optimización incompatibles')
        if replay:
            _replay(result, dataset, profile, definition, plan)
        return {'record': {key: row[key] for key in ('id', 'created_at', 'hash', 'dataset_id', 'profile_id')}
                | {'symbol': dataset.symbol, 'timeframe': dataset.timeframe},
                'result': result, 'dataset': dataset, 'profile': profile,
                'definition': definition, 'plan': plan}
    except (ValueError, TypeError, KeyError) as exc:
        raise LabError('Optimización de derivados no verificable: ' + str(exc)[:180]) from exc


def _saved(db, identifier: str) -> dict:
    return read_optimization_row(db, db.execute(
        'SELECT * FROM perp_optimizations WHERE id=?', (identifier,)).fetchone(), replay=False)


def _selected_inputs(saved: dict) -> tuple[ExecutionProfile, dict]:
    result = saved['result']
    selected = result['selected_index']
    if selected is None:
        raise LabError('La optimización no eligió ninguna combinación')
    candidates = result['candidates']
    if type(selected) is not int or not isinstance(candidates, list) or not 0 <= selected < len(candidates):
        raise LabError('Selección guardada de la optimización no válida')
    winner = candidates[selected]
    return variant_inputs(saved['profile'], saved['definition'], winner['stop_loss'], winner['take_profit'])


def _source_strategy(db, saved: dict) -> str:
    source = db.execute('SELECT strategy_id FROM strategy_versions WHERE id=?',
                        (saved['profile'].strategy_version_id,)).fetchone()
    if source is None:
        raise LabError('La versión de origen de la variante no está disponible')
    return source['strategy_id']


def read_optimization_variant(db, saved: dict) -> dict | None:
    record = saved['record']
    row = db.execute('SELECT * FROM perp_optimization_variants WHERE optimization_id=?', (record['id'],)).fetchone()
    if row is None:
        return None
    profile, definition = _selected_inputs(saved)
    strategy_id = _source_strategy(db, saved)
    version = db.execute('SELECT * FROM strategy_versions WHERE id=?', (row['version_id'],)).fetchone()
    if (row['optimization_hash'] != record['hash'] or row['version_id'] != profile.strategy_version_id
            or version is None or version['strategy_id'] != strategy_id
            or json.loads(version['definition_json']) != definition):
        raise LabError('La versión guardada no corresponde a la optimización original')
    result = saved['result']
    winner = result['candidates'][result['selected_index']]
    for field, score in (('development_run_id', winner['development']),
                         ('reserve_run_id', result['out_of_sample'])):
        run = read_run_row(db, db.execute('SELECT * FROM perp_runs WHERE id=?', (row[field],)).fetchone(),
                           replay=False)
        child = run['payload']['result']
        window = {key: child['window'][key] for key in ('entry_start_index', 'end_index')}
        if (run['dataset_id'] != record['dataset_id'] or run['profile_id'] != row['profile_id']
                or run['profile'].payload() != profile.payload() or run['payload']['window'] != window
                or content_hash(child) != score['result_hash']):
            raise LabError('Las pruebas guardadas no corresponden a los tramos de la optimización')
    return {key: row[key] for key in ('optimization_id', 'optimization_hash', 'version_id', 'profile_id',
                                     'development_run_id', 'reserve_run_id')} | {
        'strategy_id': strategy_id, 'version': version['version'], 'execution_authority': 'NONE',
    }


def read_materialized_optimization(db, identifier: str) -> tuple[dict, dict]:
    saved = _saved(db, _id(identifier))
    try:
        variant = read_optimization_variant(db, saved)
    except (TypeError, KeyError, IndexError) as exc:
        raise LabError('La variante guardada de la optimización no es verificable') from exc
    if variant is None:
        raise LabError('Guarda primero la variante de la optimización')
    return saved, variant


def _bundle(db, saved: dict) -> dict:
    bundle = {'optimization': saved['record'], 'result': saved['result']}
    variant = read_optimization_variant(db, saved)
    if variant is not None:
        bundle['variant'] = variant
    return bundle


class PerpOptimizations:
    def __init__(self, lab: LabStore):
        self.lab = lab
        self.experiments = PerpExperiments(lab)

    def create(self, dataset_id: str, profile_id: str, plan: PerpOptimizationPlan) -> dict:
        stamp = self.experiments._stamp()
        dataset_id, profile_id = _id(dataset_id), _id(profile_id)
        with self.lab.connect() as db:
            data_hash, profile_hash, dataset, profile, definition = _originals(db, dataset_id, profile_id)
        result = calculate_perp_optimization(dataset, profile, definition, plan)
        digest = content_hash(result)
        with lab_access.guard(self.lab.path, stamp.epoch), self.lab.connect() as db:
            self.experiments._stamp(stamp)
            db.execute('BEGIN IMMEDIATE')
            if _originals(db, dataset_id, profile_id)[:2] != (data_hash, profile_hash):
                raise LabError('Los originales cambiaron durante la optimización')
            row = db.execute('SELECT * FROM perp_optimizations WHERE hash=?', (digest,)).fetchone()
            if row is None:
                db.execute('INSERT INTO perp_optimizations VALUES (?,?,?,?,?,?)',
                    (str(uuid4()), datetime.now(timezone.utc).isoformat(), dataset_id,
                     profile_id, encoded(result), digest))
                row = db.execute('SELECT * FROM perp_optimizations WHERE hash=?', (digest,)).fetchone()
            return _bundle(db, read_optimization_row(db, row, replay=False))

    def page(self, offset: int, limit: int) -> dict:
        stamp = self.experiments._stamp()
        with lab_access.guard(self.lab.path, stamp.epoch), self.lab.connect() as db:
            self.experiments._stamp(stamp)
            total = db.execute('SELECT count(*) FROM perp_optimizations').fetchone()[0]
            rows = db.execute('SELECT * FROM perp_optimizations ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?',
                              (limit, offset)).fetchall()
            records = [read_optimization_row(db, row, replay=False)['record'] for row in rows]
        return {'items': records, 'total': total, 'offset': offset, 'limit': limit}

    def read(self, identifier: str, *, include_originals: bool = False) -> dict:
        stamp = self.experiments._stamp()
        identifier = _id(identifier)
        with self.lab.connect() as db:
            saved = _saved(db, identifier)
        _replay(saved['result'], saved['dataset'], saved['profile'], saved['definition'], saved['plan'])
        with lab_access.guard(self.lab.path, stamp.epoch), self.lab.connect() as db:
            self.experiments._stamp(stamp)
            current = _saved(db, identifier)
            if current['record'] != saved['record']:
                raise LabError('El archivo cambió durante la reproducción')
            bundle = _bundle(db, current)
            if include_originals:
                bundle.update({'dataset': saved['dataset'].payload(), 'profile': saved['profile'].payload(),
                               'definition': saved['definition']})
        return bundle


    def materialize(self, identifier: str, expected_hash: str) -> dict:
        stamp = self.experiments._stamp()
        identifier = _id(identifier)
        with self.lab.connect() as db:
            saved = _saved(db, identifier)
        if saved['record']['hash'] != expected_hash:
            raise LabError('La optimización cambió desde que la abriste')
        reproduced, executions = replay_perp_optimization(
            saved['dataset'], saved['profile'], saved['definition'], saved['plan'])
        if reproduced != saved['result']:
            raise LabError('La optimización de derivados no reproduce sus resultados')
        if executions is None:
            raise LabError('La optimización no eligió ninguna combinación')
        variant_profile, definition = _selected_inputs(saved)
        with lab_access.guard(self.lab.path, stamp.epoch), self.lab.connect() as db:
            self.experiments._stamp(stamp)
            db.execute('BEGIN IMMEDIATE')
            current = _saved(db, identifier)
            if current['record'] != saved['record']:
                raise LabError('La optimización cambió durante el guardado')
            existing = read_optimization_variant(db, current)
            if existing is not None:
                return existing

            strategy_id = _source_strategy(db, current)
            version_id = variant_profile.strategy_version_id
            version = db.execute('SELECT * FROM strategy_versions WHERE id=?', (version_id,)).fetchone()
            if version is None:
                write_strategy_version(db, strategy_id, definition, version_id)
            elif version['strategy_id'] != strategy_id or json.loads(version['definition_json']) != definition:
                raise LabError('La identidad de la variante coincide con una versión incompatible')

            profile = write_profile(db, variant_profile)
            runs = []
            for result in executions:
                window = {key: result['window'][key] for key in ('entry_start_index', 'end_index')}
                payload = run_payload(variant_profile, result, window)
                runs.append(write_run(db, saved['record']['dataset_id'], profile['id'], payload))
            db.execute('INSERT INTO perp_optimization_variants VALUES (?,?,?,?,?,?)',
                       (identifier, expected_hash, version_id, profile['id'], runs[0]['id'], runs[1]['id']))
            persisted = read_optimization_variant(db, current)
            if persisted is None:
                raise LabError('La variante no se guardó')
            return persisted
