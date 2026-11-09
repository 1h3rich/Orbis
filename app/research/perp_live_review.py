"""Replayed LAB/PAPER and private instrument-setting comparison for REAL review.

This is a review, not a grant. It does not adapt the new book to the legacy
price-only runner, copy PAPER balances or infer executable historical returns.
"""
from __future__ import annotations

import json
import time
from typing import Callable, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.derivatives.paper_access import paper_perp_access
from app.derivatives.perp_observed_archive import ObservedArchive, identifier
from app.derivatives.perp_observed_archive_format import OriginSnapshot, replay_observed_session
from app.derivatives.perp_observed_origin import verified_observed_origin
from app.derivatives.perp_observed_wire import ObservedSeed
from app.integrations.grvt_position_config import PositionConfiguration, leverage, scope, verify_configuration_pair
from app.lab.store import content_hash
from app.research.perp_reference import PerpPartialResearchReference, PerpResearchReference


class ObservedReviewPrefix(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)
    session_id: str
    sequence: int = Field(ge=0)
    head_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    seed_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    origin_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    stored_status: Literal['RUNNING','CLOSE_ONLY','STOPPED','GAP','HALTED','LIQUIDATED']
    started_at_ns: str
    last_received_at_ns: str
    observed_bars: int = Field(ge=0)
    closed_trades: int = Field(ge=0)
    has_estimated_closure: bool
    source_advanced: bool

    @model_validator(mode='after')
    def valid(self) -> Self:
        if str(UUID(self.session_id)) != self.session_id:
            raise ValueError('Sesión PAPER no canónica')
        for value in (self.started_at_ns, self.last_received_at_ns):
            if (not value.isascii() or not value.isdecimal() or value != str(int(value))
                    or not 0 < int(value) < 2**63):
                raise ValueError('Hora PAPER no válida')
        if int(self.last_received_at_ns) < int(self.started_at_ns):
            raise ValueError('Tiempo PAPER regresado')
        return self


Blocker = Literal['NO_OBSERVED_CLOSED_TRADE', 'NO_OBSERVED_CANDLE', 'PAPER_GAP',
    'PAPER_HALTED', 'PAPER_LIQUIDATED', 'ESTIMATED_CLOSURE_EXCLUDED', 'SOURCE_ADVANCED',
    'MARGIN_MODE_MISMATCH', 'LEVERAGE_MISMATCH', 'LEVERAGE_OUTSIDE_ACCOUNT_RANGE',
    'PERP_EXECUTOR_NOT_IMPLEMENTED']


def blockers(reference: PerpResearchReference, paper: ObservedReviewPrefix,
             config: PositionConfiguration) -> list[Blocker]:
    result: list[Blocker] = []
    if not paper.closed_trades: result.append('NO_OBSERVED_CLOSED_TRADE')
    if not paper.observed_bars: result.append('NO_OBSERVED_CANDLE')
    status_blockers: dict[str,Blocker] = {'GAP':'PAPER_GAP','HALTED':'PAPER_HALTED','LIQUIDATED':'PAPER_LIQUIDATED'}
    if paper.stored_status in status_blockers:
        result.append(status_blockers[paper.stored_status])
    if paper.has_estimated_closure: result.append('ESTIMATED_CLOSURE_EXCLUDED')
    if paper.source_advanced: result.append('SOURCE_ADVANCED')
    if reference.profile.margin_mode != config.margin_type: result.append('MARGIN_MODE_MISMATCH')
    requested = leverage(reference.profile.leverage)
    if requested != leverage(config.leverage): result.append('LEVERAGE_MISMATCH')
    if not leverage(config.min_leverage) <= requested <= leverage(config.max_leverage):
        result.append('LEVERAGE_OUTSIDE_ACCOUNT_RANGE')
    # The archived reference itself remains NONE. A future RAM grant must have
    # its own budget, config/preflight and fresh-signal dispatch, not this hash.
    result.append('PERP_EXECUTOR_NOT_IMPLEMENTED')
    return result


class PerpLiveReviewPayload(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True, populate_by_name=True)
    schema_name: Literal['orbis.grvt.perp.live.review.v1'] = Field(alias='schema')
    research: PerpResearchReference
    paper: ObservedReviewPrefix
    configurations: list[PositionConfiguration] = Field(min_length=2, max_length=2)
    checked_at_ns: str
    blockers: list[Blocker]
    sizing: Literal['REQUIRES_SEPARATE_REAL_BUDGET']
    execution_supported: Literal[False]
    execution_authority: Literal['NONE']

    @model_validator(mode='after')
    def coherent(self) -> Self:
        if (not self.checked_at_ns.isascii() or not self.checked_at_ns.isdecimal()
                or self.checked_at_ns != str(int(self.checked_at_ns))):
            raise ValueError('Hora de revisión inválida')
        first, last = self.configurations
        verify_configuration_pair(first, last, now_ns=int(self.checked_at_ns))
        instrument = self.research.symbol.replace('/','_').replace(':PERP','_Perp')
        if (last.instrument != instrument or int(self.paper.last_received_at_ns) > int(self.checked_at_ns)
                or self.blockers != blockers(self.research, self.paper, last)):
            raise ValueError('Revisión de derivados incoherente')
        return self

    def payload(self) -> dict:
        return self.model_dump(mode='json', by_alias=True, exclude={'sha256'})


class PerpLiveReview(PerpLiveReviewPayload):
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def digest(self) -> Self:
        if content_hash(self.payload()) != self.sha256:
            raise ValueError('Revisión de derivados alterada')
        return self


def review_perp_live(lab, archive: ObservedArchive, session_id: str, sub_account_id: str, client, *,
                     clock_ns: Callable = time.time_ns, require_permission: Callable = lambda: None) -> PerpLiveReview:
    """Replay originals, release SQL, read private settings twice, then recheck.

    Appends may occur while the network is read. They do not invalidate the
    pinned prefix, but are explicitly reported and prevent treating it as a
    current review. Lock/re-authentication/source replacement fails closed.
    """
    session_id = identifier(session_id)
    require_permission()
    stamp = archive._stamp()
    with archive.paper.connect() as db:
        db.execute('BEGIN')
        header = db.execute('SELECT * FROM paper_observed_sessions WHERE id=?', (session_id,)).fetchone()
        replayed = replay_observed_session(db, header)
        header = dict(header)
        source = OriginSnapshot.model_validate(json.loads(header['origin_json']))
        if isinstance(source.reference, PerpPartialResearchReference):
            raise ValueError('Las salidas parciales están disponibles en LAB y PAPER')
        if not isinstance(source.reference, PerpResearchReference):
            raise ValueError('LIMIT está disponible en LAB y PAPER')
        seed = ObservedSeed.model_validate(json.loads(header['seed_json']))
        estimated = db.execute('SELECT 1 FROM paper_observed_gap_closes WHERE session_id=?', (session_id,)).fetchone() is not None
    origin = verified_observed_origin(lab, source.reference.artifact_id)
    trade_prefix = json.loads(origin.trade_prefix_json)
    if (origin.reference().model_dump(mode='json', by_alias=True) != source.reference.model_dump(mode='json', by_alias=True)
            or seed.definition != json.loads(origin.definition_json)
            or source.trade_prefix_count != len(trade_prefix)
            or source.trade_prefix_hash != content_hash(trade_prefix)
            or [bar.model_dump() for bar in seed.history[:len(trade_prefix)]] != trade_prefix):
        raise ValueError('El original LAB no corresponde al prefijo PAPER')
    instrument = replayed.state.last_quote.instrument
    scope(sub_account_id, instrument)
    archive._stamp(stamp)
    origin.check_session()
    require_permission()
    first = client.position_configuration(sub_account_id, instrument)
    require_permission()
    archive._stamp(stamp)
    origin.check_session()
    last = client.position_configuration(sub_account_id, instrument)
    require_permission()
    at = clock_ns()
    verify_configuration_pair(first, last, now_ns=at)
    if last.sub_account_id != sub_account_id or last.instrument != instrument:
        raise ValueError('Configuración GRVT de otra cuenta o instrumento')
    with origin.hold_current(), paper_perp_access.guard(archive.paper.path, stamp.epoch), archive.paper.connect() as db:
        archive._stamp(stamp)
        db.execute('BEGIN')
        current = db.execute('SELECT * FROM paper_observed_sessions WHERE id=?', (session_id,)).fetchone()
        anchor = db.execute('SELECT hash FROM paper_observed_events WHERE session_id=? AND sequence=?',
                            (session_id, replayed.sequence)).fetchone()
        maximum = db.execute('SELECT max(sequence) FROM paper_observed_events WHERE session_id=?', (session_id,)).fetchone()[0]
        if (current is None or any(current[key] != header[key] for key in
                ('seed_json','seed_hash','origin_json','origin_hash','runtime_id','start_request_id','request_hash'))
                or current['sequence'] < replayed.sequence or maximum != current['sequence']
                or anchor is None or anchor['hash'] != replayed.head_hash
                or current['sequence'] == replayed.sequence and any(current[key] != header[key]
                    for key in ('head_hash','checkpoint_json','status'))):
            raise ValueError('El prefijo PAPER cambió durante la revisión')
        estimated = estimated or db.execute('SELECT 1 FROM paper_observed_gap_closes WHERE session_id=?', (session_id,)).fetchone() is not None
        paper = ObservedReviewPrefix(session_id=session_id, sequence=replayed.sequence, head_hash=replayed.head_hash,
            seed_hash=replayed.seed_hash, origin_hash=replayed.origin_hash, stored_status=replayed.state.status,
            started_at_ns=str(replayed.state.started_at_ns), last_received_at_ns=str(replayed.state.last_received_ns),
            observed_bars=len(replayed.state.history)-len(seed.history), closed_trades=replayed.state.closed_trades,
            has_estimated_closure=estimated, source_advanced=current['sequence'] != replayed.sequence)
        require_permission()
        origin.check_session()
        archive._stamp(stamp)
        # SQL/LAB rechecks can be expensive. Age is checked at publication too.
        at = clock_ns()
        payload = PerpLiveReviewPayload(schema_name='orbis.grvt.perp.live.review.v1', research=source.reference,
            paper=paper, configurations=[first,last], checked_at_ns=str(at), blockers=blockers(source.reference,paper,last),
            sizing='REQUIRES_SEPARATE_REAL_BUDGET', execution_supported=False, execution_authority='NONE')
    return PerpLiveReview.model_validate({**payload.payload(), 'sha256': content_hash(payload.payload())})
