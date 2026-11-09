"""A verified, ephemeral LAB handoff capability; never accepted over JSON."""
from __future__ import annotations

from dataclasses import dataclass,field
from contextlib import contextmanager
import json
import hmac
import secrets

from app.lab.access import lab_access
from app.lab.engine import LabError
from app.lab.perp_experiments import PerpExperiments,_SessionStamp
from app.lab.perp_validation_store import PerpValidations,read_validation_row
from app.lab.store import LabStore,encoded
from app.research.perp_reference import OBSERVED_REFERENCE, ObservedResearchReference, reference_from_verified_validation


_STAMP_SECRET=secrets.token_bytes(32)


@dataclass(frozen=True)
class _OriginStamp:
    device: int
    inode: int
    key_digest: bytes=field(repr=False)
    epoch: object|None=field(repr=False)


def _identity(stamp: _SessionStamp) -> _OriginStamp:
    return _OriginStamp(stamp.device,stamp.inode,
        hmac.digest(_STAMP_SECRET,stamp.key.encode('utf-8'),'sha256'),stamp.epoch)


@dataclass(frozen=True)
class VerifiedObservedOrigin:
    lab: LabStore=field(repr=False,compare=False)
    stamp: _OriginStamp=field(repr=False)
    validation_id: str
    definition_json: str
    reference_json: str
    trade_prefix_json: str=field(repr=False)
    probes: tuple=field(repr=False)
    version_number: int
    optimization_origin_json: str | None=field(default=None,repr=False)

    def reference(self) -> ObservedResearchReference:
        return OBSERVED_REFERENCE.validate_json(self.reference_json)

    def review_lab_origin(self) -> dict:
        reference = self.reference()
        result = {'version_id':reference.profile.strategy_version_id, 'version':self.version_number}
        if self.optimization_origin_json is not None:
            result['optimization'] = json.loads(self.optimization_origin_json)
        return result

    def check_session(self) -> None:
        if _identity(PerpExperiments(self.lab)._stamp())!=self.stamp:
            raise LabError('La sesión LAB cambió durante la operación')

    @contextmanager
    def hold_current(self):
        with lab_access.guard(self.lab.path,self.stamp.epoch),self.lab.connect() as db:
            self.check_session()
            db.execute('BEGIN')
            for sql,parameters,expected in self.probes:
                row=db.execute(sql,parameters).fetchone()
                if row is None or tuple(row)!=expected:
                    raise LabError('El origen LAB cambió antes del handoff PAPER')
            if self.optimization_origin_json is not None:
                row = db.execute('SELECT * FROM perp_validations WHERE id=?', (self.validation_id,)).fetchone()
                current = read_validation_row(db, row, replay=False)
                if encoded(current['payload']['optimization_origin']) != self.optimization_origin_json:
                    raise LabError('La optimización LAB cambió antes del handoff PAPER')
            yield


def verified_observed_origin(lab: LabStore,validation_id: str) -> VerifiedObservedOrigin:
    experiments=PerpExperiments(lab);stamp=experiments._stamp()
    saved=PerpValidations(lab)._verified(validation_id,expected_session=stamp)
    reserves=[w for w in saved['payload']['result']['windows'] if w['role']=='RESERVE']
    if not reserves or any(w['status']!='COMPLETE' or w['metrics']['open_position'] for w in reserves):
        raise LabError('El origen requiere reservas completas y sin exposición pendiente')
    reference=reference_from_verified_validation(saved).model_dump(mode='json',by_alias=True)
    queries=(
        ('SELECT payload_json,hash,dataset_id,profile_id FROM perp_validations WHERE id=?',(validation_id,)),
        ('SELECT payload_json,hash FROM perp_datasets WHERE id=?',(saved['dataset_id'],)),
        ('SELECT payload_json,hash,version_id FROM perp_profiles WHERE id=?',(saved['profile_id'],)),
    )
    strategy_sql = 'SELECT definition_json,version FROM strategy_versions WHERE id=?'
    strategy_args = (saved['profile'].strategy_version_id,)
    with lab_access.guard(lab.path,stamp.epoch),lab.connect() as db:
        experiments._stamp(stamp)
        db.execute('BEGIN')
        current=read_validation_row(db,db.execute('SELECT * FROM perp_validations WHERE id=?',(validation_id,)).fetchone(),replay=False)
        if current['hash']!=saved['hash']:
            raise LabError('La validación cambió al fijar el origen PAPER')
        version = db.execute(strategy_sql, strategy_args).fetchone()
        if version is None or type(version['version']) is not int or version['version'] < 1:
            raise LabError('Número de versión LAB no verificable')
        version_number = version['version']
        probes=tuple((sql,parameters,tuple(db.execute(sql,parameters).fetchone())) for sql,parameters in queries)
        probes += ((strategy_sql, strategy_args, tuple(version)),)
    optimization = saved['payload'].get('optimization_origin')
    return VerifiedObservedOrigin(lab,_identity(stamp),validation_id,encoded(saved['definition']),encoded(reference),
        encoded([bar.model_dump() for bar in saved['dataset'].trade.bars]),probes,
        version_number=version_number,
        optimization_origin_json=encoded(optimization) if optimization is not None else None)
