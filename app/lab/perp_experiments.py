"""Immutable encrypted profiles/runs. Compute outside SQLite write locks."""
from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass,field
from decimal import Decimal
import json
from uuid import UUID, uuid4

from app.lab.access import lab_access,lab_format
from app.lab.engine import INTERVALS,LabError,validate_strategy
from app.lab.perp_book_types import exact
from app.lab.perp_comparison import (COST_FIELDS, build_cost_comparison, build_margin_comparison,
                                     build_run_comparison, margin_grid, margin_outcome)
from app.lab.perp_dataset import PerpDataset
from app.lab.perp_profile import ExecutionProfile, profile_version, read_profile, validate_partial_targets
from app.lab.perp_rules import SECOND_NS, RuleObservation, read_observation, rules_for_window
from app.lab.perp_simulation import ENGINE, LIMIT_ENGINE, PARTIAL_ENGINE, simulate_perpetual, simulation_engine
from app.lab.perp_store import verified_record
from app.lab.store import LabStore,content_hash,encoded


MAX_MARGIN_WORK = 30_000


PERP_EXPERIMENTS_DDL = (
    '''CREATE TABLE perp_profiles (
        id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
        version_id TEXT NOT NULL REFERENCES strategy_versions(id),
        payload_json TEXT NOT NULL, hash TEXT NOT NULL UNIQUE
    )''',
    '''CREATE TABLE perp_runs (
        id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
        dataset_id TEXT NOT NULL REFERENCES perp_datasets(id),
        profile_id TEXT NOT NULL REFERENCES perp_profiles(id),
        payload_json TEXT NOT NULL, hash TEXT NOT NULL UNIQUE
    )''',
    'CREATE INDEX ix_perp_runs_created ON perp_runs(created_at,id)',
)


@dataclass(frozen=True)
class _SessionStamp:
    device: int
    inode: int
    key: str = field(repr=False)
    epoch: object | None = field(repr=False)


def _id(value: str) -> str:
    try:
        if str(UUID(value))!=value:raise ValueError()
    except (ValueError,TypeError,AttributeError):
        raise LabError('Identificador perpetuo no válido') from None
    return value


def _run_contract(profile: ExecutionProfile) -> tuple[str, str]:
    return f'orbis.lab.perp.run.v{profile_version(profile)}', simulation_engine(profile)


def read_profile_row(db, row) -> tuple[dict,ExecutionProfile,dict]:
    if row is None:raise LabError('Perfil perpetuo no encontrado')
    try:
        payload=json.loads(row['payload_json']);profile=read_profile(payload)
        original=db.execute('SELECT definition_json FROM strategy_versions WHERE id=?',(row['version_id'],)).fetchone()
        definition=validate_strategy(json.loads(original['definition_json'])) if original is not None else None
        if (profile.payload()!=payload or content_hash(payload)!=row['hash']
                or profile.strategy_version_id!=row['version_id'] or definition is None
                or content_hash(definition)!=profile.strategy_hash):
            raise LabError('Perfil u original de estrategia alterados')
        validate_partial_targets(profile,definition)
        return {'id':row['id'],'created_at':row['created_at'],'hash':row['hash']},profile,definition
    except (ValueError,TypeError,KeyError) as exc:
        raise LabError('Perfil perpetuo no verificable') from exc


def write_profile(db, profile: ExecutionProfile) -> dict:
    row = db.execute('SELECT definition_json FROM strategy_versions WHERE id=?',
                     (_id(profile.strategy_version_id),)).fetchone()
    definition = validate_strategy(json.loads(row['definition_json'])) if row is not None else None
    if definition is None or content_hash(definition) != profile.strategy_hash:
        raise LabError('La versión de estrategia no corresponde al perfil')
    validate_partial_targets(profile, definition)
    digest = content_hash(profile.payload())
    existing = db.execute('SELECT * FROM perp_profiles WHERE hash=?', (digest,)).fetchone()
    if existing is None:
        db.execute('INSERT INTO perp_profiles VALUES (?,?,?,?,?)',
                   (str(uuid4()), datetime.now(timezone.utc).isoformat(), profile.strategy_version_id,
                    encoded(profile.payload()), digest))
        existing = db.execute('SELECT * FROM perp_profiles WHERE hash=?', (digest,)).fetchone()
    header, parsed, _ = read_profile_row(db, existing)
    return {**header, 'profile': parsed.payload()}


def run_payload(profile: ExecutionProfile, result: dict, window: dict) -> dict:
    schema, engine = _run_contract(profile)
    return {'schema':schema, 'engine':engine, 'dataset_hash':result['dataset_hash'],
            'profile_hash':content_hash(profile.payload()), 'strategy_hash':profile.strategy_hash,
            'window':window, 'result':result}


def write_run(db, dataset_id: str, profile_id: str, payload: dict) -> dict:
    digest = content_hash(payload)
    existing = db.execute('SELECT * FROM perp_runs WHERE hash=?', (digest,)).fetchone()
    if existing is None:
        db.execute('INSERT INTO perp_runs VALUES (?,?,?,?,?,?)',
                   (str(uuid4()), datetime.now(timezone.utc).isoformat(), dataset_id, profile_id,
                    encoded(payload), digest))
        existing = db.execute('SELECT * FROM perp_runs WHERE hash=?', (digest,)).fetchone()
    return read_run_row(db, existing, replay=False)



def read_run_row(db, row, *, replay: bool) -> dict:
    if row is None:raise LabError('Prueba perpetua no encontrada')
    try:
        payload=json.loads(row['payload_json'])
        if (set(payload)!={'schema','engine','dataset_hash','profile_hash','strategy_hash','window','result'}
                or (payload['schema'],payload['engine']) not in (
                    ('orbis.lab.perp.run.v1',ENGINE),('orbis.lab.perp.run.v2',LIMIT_ENGINE),
                    ('orbis.lab.perp.run.v3',PARTIAL_ENGINE))
                or content_hash(payload)!=row['hash']):
            raise LabError('Formato/huella de prueba perpetua incompatible')
        data_header,data=verified_record(db.execute('SELECT * FROM perp_datasets WHERE id=?',(row['dataset_id'],)).fetchone())
        profile_header,profile,definition=read_profile_row(db,db.execute('SELECT * FROM perp_profiles WHERE id=?',(row['profile_id'],)).fetchone())
        if ((payload['schema'],payload['engine'])!=_run_contract(profile)
                or payload['dataset_hash']!=data_header['hash'] or payload['profile_hash']!=profile_header['hash']
                or payload['strategy_hash']!=profile.strategy_hash
                or set(payload['window'])!={'entry_start_index','end_index'}):
            raise LabError('Los originales perpetuos han cambiado')
        # Replay is intentionally outside this helper's SQL caller when used
        # for a detail read. Backup calls it on its own read-only snapshot.
        result=payload['result']
        if (result.get('dataset_hash')!=data_header['hash'] or result.get('profile_hash')!=profile_header['hash']
                or result.get('strategy_hash')!=profile.strategy_hash or result.get('engine')!=simulation_engine(profile)):
            raise LabError('Resultado perpetuo con referencias incorrectas')
        if replay:
            computed=simulate_perpetual(data,profile,definition,**payload['window'])
            if computed!=result:raise LabError('La prueba perpetua no reproduce su resultado guardado')
        return {'id':row['id'],'created_at':row['created_at'],'hash':row['hash'],
            'dataset_id':row['dataset_id'],'profile_id':row['profile_id'],
            'payload':payload,'dataset':data,'profile':profile,'definition':definition,
            'dataset_header':data_header,'profile_header':profile_header}
    except (ValueError,TypeError,KeyError,AttributeError) as exc:
        raise LabError('Prueba perpetua no verificable: '+str(exc)[:180]) from exc


def run_summary(saved: dict, *, replayed: bool = False) -> dict:
    result=saved['payload']['result']
    return {key:saved[key] for key in ('id','created_at','hash','dataset_id','profile_id')} | {
        'engine':saved['payload']['engine'],'symbol':saved['dataset'].symbol,'timeframe':saved['dataset'].timeframe,
        'profile':saved['profile'].payload(),'window':saved['payload']['window'],
        'status':result['status'],'metrics':result['metrics'],
        'verification':'REPLAY_VERIFIED' if replayed else 'HASH_AND_ORIGINALS_ONLY'}


def _comparison_sources(db, identifier: str, observation_hash: str) -> tuple[dict, RuleObservation]:
    saved = read_run_row(db, db.execute('SELECT * FROM perp_runs WHERE id=?',(identifier,)).fetchone(),replay=False)
    row = db.execute('SELECT * FROM perp_rule_observations WHERE hash=?',(observation_hash,)).fetchone()
    if row is None:
        raise LabError('Captura de reglas no encontrada')
    _, observation = read_observation(row)
    if observation.symbol != saved['dataset'].symbol:
        raise LabError('La captura de reglas pertenece a otro mercado')
    return saved, observation


def _scenario(observation: RuleObservation, result: dict) -> dict:
    captured = observation.payload()
    return {'observation':captured,'observation_hash':content_hash(captured),
            'observed_at_ns':str(observation.observed_at_ns),'result_hash':content_hash(result),
            'status':result['status'],'halt_reason':result['halt_reason'],
            'processed_through_ns':result['window']['processed_through_ns'],
            'metrics':result['metrics'],
            'rejected_orders':sum(row['kind']=='ORDER_REJECTED' for row in result['journal'])}


class PerpExperiments:
    def __init__(self,lab: LabStore):self.lab=lab

    def _protected(self) -> None:
        if not self.lab.encrypted or lab_format(self.lab.path)!='encrypted_or_invalid':
            raise LabError('Las pruebas perpetuas requieren LAB cifrado y desbloqueado')

    def _stamp(self, expected: _SessionStamp | None = None) -> _SessionStamp:
        self._protected();info=self.lab.path.stat()
        key=self.lab.current_passphrase()
        if key is None:raise LabError('LAB se bloqueó durante la operación')
        stamp = _SessionStamp(info.st_dev,info.st_ino,key,lab_access.epoch(self.lab.path))
        if expected is not None and stamp != expected:
            raise LabError('La sesión LAB cambió durante la operación')
        return stamp

    def save_profile(self,profile: ExecutionProfile, *, expected_session: _SessionStamp | None = None) -> dict:
        stamp=self._stamp(expected_session);profile=read_profile(profile.payload())
        with lab_access.guard(self.lab.path,stamp.epoch),self.lab.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if self._stamp()!=stamp:raise LabError('La sesión LAB cambió antes de guardar el perfil')
            return write_profile(db, profile)

    def profiles(self) -> list[dict]:
        if lab_format(self.lab.path) in ('missing','empty'):return []
        with self.lab.connect() as db:
            result=[]
            for row in db.execute('SELECT * FROM perp_profiles ORDER BY created_at DESC,id DESC'):
                header,profile,_=read_profile_row(db,row);result.append({**header,'profile':profile.payload()})
            return result

    def run(self,dataset_id: str,profile_id: str,*,entry_start_index: int=0,end_index: int|None=None,
            expected_session: _SessionStamp | None = None) -> dict:
        stamp=self._stamp(expected_session)
        with self.lab.connect() as db:
            data_header,data=verified_record(db.execute('SELECT * FROM perp_datasets WHERE id=?',(_id(dataset_id),)).fetchone())
            p_header,profile,definition=read_profile_row(db,db.execute('SELECT * FROM perp_profiles WHERE id=?',(_id(profile_id),)).fetchone())
        end=len(data.trade.bars) if end_index is None else end_index
        result=simulate_perpetual(data,profile,definition,entry_start_index=entry_start_index,end_index=end)
        payload=run_payload(profile,result,{'entry_start_index':entry_start_index,'end_index':end})
        if self._stamp()!=stamp:raise LabError('LAB cambió o se bloqueó durante la prueba; no se guardó')
        with lab_access.guard(self.lab.path,stamp.epoch),self.lab.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if self._stamp()!=stamp:raise LabError('La sesión LAB cambió antes de guardar')
            # Repeat original verification under the transaction, not just the
            # file identity. Same-inode writes by another process are detected.
            current_data,_=verified_record(db.execute('SELECT * FROM perp_datasets WHERE id=?',(dataset_id,)).fetchone())
            current_profile,_,_=read_profile_row(db,db.execute('SELECT * FROM perp_profiles WHERE id=?',(profile_id,)).fetchone())
            if current_data['hash']!=data_header['hash'] or current_profile['hash']!=p_header['hash']:
                raise LabError('Los originales cambiaron durante la prueba')
            saved=run_summary(write_run(db,dataset_id,profile_id,payload))
            saved['verification']='COMPUTED_AND_SAVED'
            return saved

    def page(self,*,offset: int=0,limit: int=25) -> dict:
        if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=100:
            raise LabError('Página perpetua no válida')
        if lab_format(self.lab.path) in ('missing','empty'):return {'items':[],'total':0,'offset':offset,'limit':limit}
        with self.lab.connect() as db:
            total=db.execute('SELECT count(*) FROM perp_runs').fetchone()[0]
            rows=db.execute('SELECT * FROM perp_runs ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?',(limit,offset)).fetchall()
            return {'items':[run_summary(read_run_row(db,row,replay=False)) for row in rows],
                    'total':total,'offset':offset,'limit':limit}

    def read(self,identifier: str, *, expected_session: _SessionStamp | None = None) -> dict:
        stamp=self._stamp(expected_session)
        with self.lab.connect() as db:
            saved=read_run_row(db,db.execute('SELECT * FROM perp_runs WHERE id=?',(_id(identifier),)).fetchone(),replay=False)
        computed=simulate_perpetual(saved['dataset'],saved['profile'],saved['definition'],**saved['payload']['window'])
        if computed!=saved['payload']['result']:raise LabError('El resultado perpetuo no se reproduce')
        if self._stamp()!=stamp:raise LabError('LAB cambió o se bloqueó durante la lectura')
        with lab_access.guard(self.lab.path,stamp.epoch),self.lab.connect() as db:
            current=read_run_row(db,db.execute('SELECT * FROM perp_runs WHERE id=?',(identifier,)).fetchone(),replay=False)
            if current['hash']!=saved['hash']:raise LabError('La prueba cambió durante la lectura')
        schema = f"orbis.lab.perp.bundle.v{profile_version(saved['profile'])}"
        return {'schema':schema,'run':run_summary(saved,replayed=True),
                'execution':saved['payload'],'dataset':saved['dataset'].payload(),
                'strategy':{'version_id':saved['profile'].strategy_version_id,'definition':saved['definition']}}

    def compare_runs(self, base_id: str, base_hash: str, other_id: str, other_hash: str,
                     *, expected_hash: str | None = None) -> dict:
        stamp = self._stamp()
        if _id(base_id) == _id(other_id):
            raise LabError('Elige dos pruebas de derivados distintas')
        references = ((base_id, base_hash), (other_id, other_hash))

        def load(db):
            records = []
            for identifier, digest in references:
                saved = read_run_row(db, db.execute(
                    'SELECT * FROM perp_runs WHERE id=?', (identifier,)).fetchone(), replay=False)
                if saved['hash'] != digest:
                    raise LabError('Una prueba cambió; vuelve a seleccionarla')
                records.append(saved)
            return records

        with self.lab.connect() as db:
            db.execute('BEGIN')
            base, other = load(db)
        verified = []
        for saved in (base, other):
            computed = simulate_perpetual(saved['dataset'], saved['profile'], saved['definition'],
                                          **saved['payload']['window'])
            if computed != saved['payload']['result']:
                raise LabError('Una prueba no reproduce su resultado guardado')
            self._stamp(stamp)
            verified.append(computed)
        report = build_run_comparison(base, other, *verified)
        with lab_access.guard(self.lab.path, stamp.epoch), self.lab.connect() as db:
            db.execute('BEGIN')
            self._stamp(stamp)
            load(db)
        if expected_hash is not None and expected_hash != report['hash']:
            raise LabError('La comparación cambió; vuelve a revisarla')
        return report

    @staticmethod
    def _pinned_run(db, identifier: str, run_hash: str) -> dict:
        saved = read_run_row(db, db.execute(
            'SELECT * FROM perp_runs WHERE id=?', (identifier,)).fetchone(), replay=False)
        if saved['hash'] != run_hash:
            raise LabError('La prueba cambió; vuelve a abrirla')
        return saved

    @staticmethod
    def _replay_original(saved: dict) -> dict:
        baseline = simulate_perpetual(saved['dataset'], saved['profile'], saved['definition'],
                                      **saved['payload']['window'])
        if baseline != saved['payload']['result']:
            raise LabError('La prueba original no reproduce su resultado guardado')
        return baseline

    def compare_costs(self, identifier: str, run_hash: str, fee_rate: str, slippage_rate: str,
                      *, expected_hash: str | None = None) -> dict:
        stamp = self._stamp()
        identifier = _id(identifier)

        with self.lab.connect() as db:
            db.execute('BEGIN')
            saved = self._pinned_run(db, identifier, run_hash)
        dataset, profile, definition = saved['dataset'], saved['profile'], saved['definition']
        window = saved['payload']['window']
        alternate_definition = validate_strategy({**definition, 'fee_rate': fee_rate, 'slippage_rate': slippage_rate})
        for key in COST_FIELDS:
            if Decimal(alternate_definition[key]) == Decimal(definition[key]):
                alternate_definition[key] = definition[key]

        baseline = self._replay_original(saved)
        self._stamp(stamp)
        alternative = baseline
        if alternate_definition != definition:
            alternate_profile = read_profile({**profile.payload(), 'strategy_hash': content_hash(alternate_definition)})
            alternative = simulate_perpetual(dataset, alternate_profile, alternate_definition, **window)
        report = build_cost_comparison(saved, baseline, alternative, alternate_definition)
        with lab_access.guard(self.lab.path, stamp.epoch), self.lab.connect() as db:
            db.execute('BEGIN')
            self._stamp(stamp)
            self._pinned_run(db, identifier, run_hash)
        if expected_hash is not None and expected_hash != report['hash']:
            raise LabError('La comparación cambió; vuelve a revisarla')
        return report

    def compare_margin(self, identifier: str, run_hash: str, leverages: list[str], margin_modes: list[str],
                       *, expected_hash: str | None = None) -> dict:
        stamp = self._stamp()
        identifier = _id(identifier)
        combinations = margin_grid(leverages, margin_modes)
        with self.lab.connect() as db:
            db.execute('BEGIN')
            saved = self._pinned_run(db, identifier, run_hash)
        dataset, profile, definition = saved['dataset'], saved['profile'], saved['definition']
        window = saved['payload']['window']
        alternatives = [(mode, leverage) for mode, leverage in combinations
                        if mode != profile.margin_mode or Decimal(leverage) != Decimal(profile.leverage)]
        # Each simulation validates and hashes the entire source archive.
        if (len(alternatives) + 1) * len(dataset.trade.bars) > MAX_MARGIN_WORK:
            raise LabError('Reduce los escenarios para comparar este histórico')
        scenarios = [margin_outcome(self._replay_original(saved), base=True)]
        for mode, leverage in alternatives:
            self._stamp(stamp)
            candidate = read_profile({**profile.payload(), 'margin_mode': mode, 'leverage': leverage})
            computed = simulate_perpetual(dataset, candidate, definition, **window)
            scenarios.append(margin_outcome(computed, base=False))
        report = build_margin_comparison(saved, scenarios)
        with lab_access.guard(self.lab.path, stamp.epoch), self.lab.connect() as db:
            db.execute('BEGIN')
            self._stamp(stamp)
            self._pinned_run(db, identifier, run_hash)
        if expected_hash is not None and expected_hash != report['hash']:
            raise LabError('La comparación cambió; vuelve a revisarla')
        return report

    def rules_in_window(self, identifier: str, run_hash: str) -> dict:
        stamp = self._stamp()
        identifier = _id(identifier)
        with self.lab.connect() as db:
            db.execute('BEGIN')
            saved = read_run_row(db, db.execute('SELECT * FROM perp_runs WHERE id=?',(identifier,)).fetchone(),replay=False)
            if saved['hash'] != run_hash:
                raise LabError('La prueba cambió; vuelve a abrirla')
            dataset = saved['dataset']
            bars = dataset.trade.bars
            window = saved['payload']['window']
            start,end = window['entry_start_index'],window['end_index']
            if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(bars):
                raise LabError('Ventana de prueba perpetua no verificable')
            coverage = rules_for_window(db,RuleObservation.from_dataset(dataset),bars[start].t*SECOND_NS,
                (bars[end-1].t+INTERVALS[dataset.timeframe])*SECOND_NS)
        self._stamp(stamp)
        return {'schema':'orbis.lab.perp.rule-window.v1','original_run':{'id':identifier,'hash':run_hash},**coverage}

    @exact
    def compare_rules(self, identifier: str, observation_hash: str) -> dict:
        """Reproduce la prueba y cambia sólo la captura de margen/instrumento."""
        stamp = self._stamp()
        identifier = _id(identifier)
        with self.lab.connect() as db:
            db.execute('BEGIN')
            saved, observation = _comparison_sources(db, identifier, observation_hash)
        dataset, profile, definition = saved['dataset'], saved['profile'], saved['definition']
        window = saved['payload']['window']
        baseline = simulate_perpetual(dataset, profile, definition, **window)
        if baseline != saved['payload']['result']:
            raise LabError('La prueba original no reproduce su resultado guardado')
        alternate = simulate_perpetual(dataset, profile, definition, **window, rule_observation=observation)
        original = _scenario(RuleObservation.from_dataset(dataset), baseline)
        alternative = _scenario(observation, alternate)
        comparable = (original['status'] == alternative['status'] == 'COMPLETE'
                      and original['processed_through_ns'] == alternative['processed_through_ns'])
        difference = (Decimal(alternative['metrics']['final_equity'])-Decimal(original['metrics']['final_equity'])
                      if comparable else None)
        material = {
            'schema':'orbis.lab.perp.rule-comparison.v1',
            'scope':'EXPERIMENTAL_CAPTURED_RULES_NOT_EFFECTIVE_HISTORY',
            'original_run':{'id':identifier,'hash':saved['hash']},
            'dataset_hash':saved['dataset_header']['hash'],'profile_hash':saved['profile_header']['hash'],
            'strategy_hash':profile.strategy_hash,'symbol':dataset.symbol,
            'engine':simulation_engine(profile),'window':baseline['window'],
            'original':original,'alternative':alternative,
            'final_equity_difference':format(difference,'f') if difference is not None else None,
        }
        if self._stamp() != stamp:
            raise LabError('La sesión LAB cambió durante la comparación')
        with lab_access.guard(self.lab.path,stamp.epoch), self.lab.connect() as db:
            db.execute('BEGIN')
            if self._stamp() != stamp:
                raise LabError('La sesión LAB cambió durante la comparación')
            current, current_observation = _comparison_sources(db, identifier, observation_hash)
            if current['hash'] != saved['hash'] or current_observation != observation:
                raise LabError('Los originales cambiaron durante la comparación')
        return {**material,'hash':content_hash(material)}
