"""Encrypted validation reports with full child-result replay and session fencing."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from uuid import uuid4

from app.lab.access import lab_access, lab_format
from app.lab.engine import LabError
from app.lab.perp_experiments import PerpExperiments, _SessionStamp, _id, read_profile_row
from app.lab.perp_optimization import calculate_perp_optimization, variant_inputs
from app.lab.perp_optimization_store import read_materialized_optimization
from app.lab.perp_profile import ExecutionProfile, read_profile
from app.lab.perp_store import verified_record
from app.lab.perp_validation import PerpValidationPlan, calculate_perp_validation, validation_contract
from app.lab.store import LabStore, content_hash, encoded


PERP_VALIDATIONS_DDL = (
    '''CREATE TABLE perp_validations (
        id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
        dataset_id TEXT NOT NULL REFERENCES perp_datasets(id),
        profile_id TEXT NOT NULL REFERENCES perp_profiles(id),
        payload_json TEXT NOT NULL, hash TEXT NOT NULL UNIQUE
    )''',
    'CREATE INDEX ix_perp_validations_created ON perp_validations(created_at,id)',
)


def _validation_schema(version: int, optimized: bool) -> str:
    family = 'validation.optimized' if optimized else 'validation'
    return f'orbis.lab.perp.{family}.v{version}'


def _validation_payload(profile: ExecutionProfile, plan: PerpValidationPlan, result: dict,
                        *, origin: dict | None = None) -> dict:
    version, engine, simulated = validation_contract(profile)
    payload = {'schema':_validation_schema(version, origin is not None), 'engine':engine,
               'simulation_engine':simulated, 'dataset_hash':result['dataset_hash'],
               'profile_hash':result['profile_hash'], 'strategy_hash':profile.strategy_hash,
               'plan':plan.payload(), 'result':result}
    if origin is not None:
        payload['optimization_origin'] = origin
    return payload


def _optimized_plan(optimization: dict) -> PerpValidationPlan:
    return PerpValidationPlan(method='CHRONOLOGICAL_HOLDOUT',
                              train_fraction=optimization['plan'].train_fraction)


def _optimized_origin(variant: dict) -> dict:
    return {key:variant[key] for key in (
        'optimization_id', 'optimization_hash', 'version_id', 'development_run_id', 'reserve_run_id')}


def _verify_optimized_windows(optimization: dict, variant: dict, origin: dict,
                              dataset_id: str, profile_id: str, plan: PerpValidationPlan,
                              result: dict) -> None:
    report = optimization['result']
    winner = report['candidates'][report['selected_index']]
    reserve = report['out_of_sample']
    if (origin != _optimized_origin(variant) or dataset_id != optimization['record']['dataset_id']
            or profile_id != variant['profile_id'] or plan != _optimized_plan(optimization)
            or [(row['role'], row['result_hash']) for row in result['windows']] != [
                ('DEVELOPMENT', winner['development']['result_hash']), ('RESERVE', reserve['result_hash'])]):
        raise LabError('La validación no coincide con la reserva original de la variante')
    _require_paper_reserve(reserve)


def _require_paper_reserve(reserve: dict) -> None:
    if reserve['status'] != 'COMPLETE' or reserve['metrics']['open_position'] or reserve['metrics']['liquidated']:
        raise LabError('PAPER requiere una reserva completa, sin liquidación ni posición abierta')


def _replay_optimization(optimization: dict) -> None:
    result = calculate_perp_optimization(optimization['dataset'], optimization['profile'],
                                         optimization['definition'], optimization['plan'])
    if result != optimization['result']:
        raise LabError('La optimización de origen no reproduce su resultado')


def _reproduces_validation(saved: dict) -> bool:
    if saved['optimization'] is not None:
        _replay_optimization(saved['optimization'])
    computed = calculate_perp_validation(saved['dataset'], saved['profile'], saved['definition'], saved['plan'])
    return computed == saved['payload']['result']



def read_validation_row(db, row, *, replay: bool) -> dict:
    if row is None:
        raise LabError('Validación perpetua no encontrada')
    try:
        raw=json.loads(row['payload_json'])
        origin = raw.get('optimization_origin')
        keys = {'schema','engine','simulation_engine','dataset_hash','profile_hash','strategy_hash','plan','result'}
        if origin is not None:
            keys.add('optimization_origin')
        if set(raw) != keys or content_hash(raw) != row['hash']:
            raise LabError('Formato/huella de validación perpetua incompatible')
        plan=PerpValidationPlan.model_validate(raw['plan'])
        data_header,data=verified_record(db.execute('SELECT * FROM perp_datasets WHERE id=?',(row['dataset_id'],)).fetchone())
        profile_header,profile,definition=read_profile_row(db,db.execute('SELECT * FROM perp_profiles WHERE id=?',(row['profile_id'],)).fetchone())
        version, engine, simulated = validation_contract(profile)
        result=raw['result']
        if ((raw['schema'],raw['engine'],raw['simulation_engine']) !=
                (_validation_schema(version, origin is not None),engine,simulated)
                or plan.payload()!=raw['plan'] or raw['dataset_hash']!=data_header['hash']
                or raw['profile_hash']!=profile_header['hash'] or raw['strategy_hash']!=profile.strategy_hash
                or result.get('dataset_hash')!=data_header['hash']
                or result.get('profile_hash')!=profile_header['hash']
                or result.get('strategy_hash')!=profile.strategy_hash
                or result.get('schema')!=f'orbis.lab.perp.validation.result.v{version}'
                or result.get('engine')!=engine or result.get('simulation_engine')!=simulated
                or result.get('plan')!=plan.payload()):
            raise LabError('Los originales de validación perpetua han cambiado')
        optimization = None
        if origin is not None:
            optimization, variant = read_materialized_optimization(db, origin['optimization_id'])
            _verify_optimized_windows(optimization, variant, origin, row['dataset_id'], row['profile_id'], plan, result)
        saved = {'id':row['id'],'created_at':row['created_at'],'hash':row['hash'],
            'dataset_id':row['dataset_id'],'profile_id':row['profile_id'],
            'payload':raw,'dataset':data,'profile':profile,'definition':definition,'plan':plan,
            'optimization':optimization}
        if replay and not _reproduces_validation(saved):
            raise LabError('La validación perpetua no reproduce sus resultados')
        return saved
    except (ValueError,TypeError,KeyError,AttributeError) as exc:
        raise LabError('Validación perpetua no verificable: '+str(exc)[:180]) from exc


def validation_summary(saved: dict, *, verification: str) -> dict:
    summary = {key:saved[key] for key in ('id','created_at','hash','dataset_id','profile_id')} | {
        'engine':saved['payload']['engine'],'simulation_engine':saved['payload']['simulation_engine'],
        'symbol':saved['dataset'].symbol,'timeframe':saved['dataset'].timeframe,
        'profile':saved['profile'].payload(),'plan':saved['plan'].payload(),
        'verification':verification}
    if 'optimization_origin' in saved['payload']:
        summary['optimization_origin'] = saved['payload']['optimization_origin']
    return summary


def _write_validation(db, dataset_id: str, profile_id: str, payload: dict) -> dict:
    digest = content_hash(payload)
    existing = db.execute('SELECT * FROM perp_validations WHERE hash=?', (digest,)).fetchone()
    if existing is None:
        db.execute('INSERT INTO perp_validations VALUES (?,?,?,?,?,?)',
                   (str(uuid4()), datetime.now(timezone.utc).isoformat(),
                    dataset_id, profile_id, encoded(payload), digest))
        existing = db.execute('SELECT * FROM perp_validations WHERE hash=?', (digest,)).fetchone()
    return validation_summary(read_validation_row(db, existing, replay=False), verification='COMPUTED_AND_SAVED')


class PerpValidations:
    def __init__(self, lab: LabStore):
        self.lab=lab
        self.experiments=PerpExperiments(lab)

    def create(self, dataset_id: str, profile_id: str, plan: PerpValidationPlan) -> dict:
        stamp=self.experiments._stamp()
        plan=PerpValidationPlan.model_validate(plan.payload())
        with self.lab.connect() as db:
            data_header,data=verified_record(db.execute('SELECT * FROM perp_datasets WHERE id=?',(_id(dataset_id),)).fetchone())
            profile_header,profile,definition=read_profile_row(db,db.execute('SELECT * FROM perp_profiles WHERE id=?',(_id(profile_id),)).fetchone())
        result=calculate_perp_validation(data,profile,definition,plan)
        payload = _validation_payload(profile, plan, result)
        if self.experiments._stamp()!=stamp:
            raise LabError('LAB cambió o se bloqueó durante la validación; no se guardó')
        with lab_access.guard(self.lab.path,stamp.epoch),self.lab.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if self.experiments._stamp()!=stamp:
                raise LabError('La sesión LAB cambió antes de guardar la validación')
            current_data,_=verified_record(db.execute('SELECT * FROM perp_datasets WHERE id=?',(dataset_id,)).fetchone())
            current_profile,_,_=read_profile_row(db,db.execute('SELECT * FROM perp_profiles WHERE id=?',(profile_id,)).fetchone())
            if current_data['hash']!=data_header['hash'] or current_profile['hash']!=profile_header['hash']:
                raise LabError('Los originales cambiaron durante la validación')
            return _write_validation(db, dataset_id, profile_id, payload)

    def create_from_optimization(self, identifier: str, expected_hash: str) -> dict:
        stamp = self.experiments._stamp()
        with self.lab.connect() as db:
            optimization, variant = read_materialized_optimization(db, identifier)
        record, report = optimization['record'], optimization['result']
        if record['hash'] != expected_hash:
            raise LabError('La optimización cambió desde que la abriste')
        _require_paper_reserve(report['out_of_sample'])
        _replay_optimization(optimization)
        winner = report['candidates'][report['selected_index']]
        profile, definition = variant_inputs(optimization['profile'], optimization['definition'],
                                             winner['stop_loss'], winner['take_profit'])
        plan = _optimized_plan(optimization)
        result = calculate_perp_validation(optimization['dataset'], profile, definition, plan)
        origin = _optimized_origin(variant)
        dataset_id, profile_id = record['dataset_id'], variant['profile_id']
        _verify_optimized_windows(optimization, variant, origin, dataset_id, profile_id, plan, result)
        payload = _validation_payload(profile, plan, result, origin=origin)
        with lab_access.guard(self.lab.path, stamp.epoch), self.lab.connect() as db:
            self.experiments._stamp(stamp)
            db.execute('BEGIN IMMEDIATE')
            return _write_validation(db, dataset_id, profile_id, payload)

    def page(self, *, offset: int=0, limit: int=25) -> dict:
        if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=100:
            raise LabError('Página de validaciones no válida')
        if lab_format(self.lab.path) in ('missing','empty'):
            return {'items':[],'total':0,'offset':offset,'limit':limit}
        with self.lab.connect() as db:
            total=db.execute('SELECT count(*) FROM perp_validations').fetchone()[0]
            rows=db.execute('SELECT * FROM perp_validations ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?',(limit,offset)).fetchall()
            return {'items':[validation_summary(read_validation_row(db,row,replay=False),verification='HASH_AND_ORIGINALS_ONLY') for row in rows],
                    'total':total,'offset':offset,'limit':limit}

    def _verified(self, identifier: str, *, expected_session: _SessionStamp | None = None) -> dict:
        stamp=self.experiments._stamp(expected_session)
        with self.lab.connect() as db:
            saved=read_validation_row(db,db.execute('SELECT * FROM perp_validations WHERE id=?',(_id(identifier),)).fetchone(),replay=False)
        if not _reproduces_validation(saved):
            raise LabError('La validación perpetua no se reproduce')
        if self.experiments._stamp()!=stamp:
            raise LabError('LAB cambió o se bloqueó durante la lectura')
        with lab_access.guard(self.lab.path,stamp.epoch),self.lab.connect() as db:
            current=read_validation_row(db,db.execute('SELECT * FROM perp_validations WHERE id=?',(identifier,)).fetchone(),replay=False)
            if current['hash']!=saved['hash']:
                raise LabError('La validación cambió durante la lectura')
        return saved

    def read(self, identifier: str) -> dict:
        saved=self._verified(identifier)
        version, _, _ = validation_contract(saved['profile'])
        return {'schema':f'orbis.lab.perp.validation.bundle.v{version}',
            'validation':validation_summary(saved,verification='REPLAY_VERIFIED'),
            'execution':saved['payload'],'dataset':saved['dataset'].payload(),
            'strategy':{'version_id':saved['profile'].strategy_version_id,'definition':saved['definition']}}

    def reference(self, identifier: str) -> dict:
        from app.research.perp_reference import reference_from_verified_validation
        return reference_from_verified_validation(self._verified(identifier)).model_dump(mode='json',by_alias=True)

    def inspect_window(self, identifier: str, index: int) -> dict:
        """Explicitly save/reuse a real child run so the common chart can open it.

        Verification and materialization share the original session epoch. A
        child is a research record only; it cannot start PAPER or REAL workers.
        """
        stamp=self.experiments._stamp()
        saved=self._verified(identifier,expected_session=stamp)
        windows=saved['payload']['result']['windows']
        if type(index) is not int or not 0<=index<len(windows):
            raise LabError('Ventana de validación no válida')
        window=windows[index]
        if self.experiments._stamp()!=stamp:
            raise LabError('La sesión LAB cambió durante la inspección')
        # Profiles of path sensitivity differ only in the two declared paths.
        selected=self.experiments.save_profile(read_profile(window['profile']),expected_session=stamp)
        if self.experiments._stamp()!=stamp:
            raise LabError('La sesión LAB cambió durante la inspección')
        run=self.experiments.run(saved['dataset_id'],selected['id'],
            entry_start_index=window['entry_start_index'],end_index=window['end_index'],expected_session=stamp)
        # A final replay catches changed originals/validation between requests;
        # stale materialized records never get presented as the selected report.
        if self.experiments._stamp()!=stamp or self._verified(identifier,expected_session=stamp)['hash']!=saved['hash']:
            raise LabError('La validación cambió durante la inspección')
        bundle=self.experiments.read(run['id'],expected_session=stamp)
        if content_hash(bundle['execution']['result'])!=window['result_hash']:
            raise LabError('La ventana no corresponde a la validación')
        return {'validation_id':identifier,'window_index':index,'run':bundle['run']}
