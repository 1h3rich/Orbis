"""Pinned, replayed LAB/PAPER origin for a new perpetual REAL grant.

Saved evidence never confers authority. Only the ephemeral verified capability
can create a grant; the REAL coordinator must still hold separate RAM consent.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import time
from typing import Literal

from pydantic import Field, model_validator

from app.derivatives.paper_access import paper_perp_access
from app.derivatives.perp_observed_origin import VerifiedObservedOrigin, verified_observed_origin
from app.derivatives.perp_observed_archive_format import replay_observed_session
from app.derivatives.perp_observed_wire import ExactInput
from app.lab.engine import INTERVALS, validate_strategy
from app.lab.store import content_hash, encoded
from app.research.perp_live_review import ObservedReviewPrefix, review_perp_live
from app.research.perp_reference import PerpResearchReference


class PerpBotSource(ExactInput):
    schema_name: Literal['orbis.grvt.perp.bot.source.v1'] = Field(alias='schema')
    session_id: str
    symbol: str
    instrument: str
    timeframe: str
    engine_version: Literal['lab-1.16']
    version_id: str
    dataset_id: str
    dataset_hash: str
    session_started_at_ns: int
    definition: dict
    research: PerpResearchReference
    paper: ObservedReviewPrefix
    execution_authority: Literal['NONE']

    @model_validator(mode='after')
    def coherent(self):
        research, paper = self.research, self.paper
        if (self.session_id != paper.session_id or self.timeframe not in INTERVALS
                or self.symbol != research.symbol or self.timeframe != research.timeframe
                or self.instrument != research.symbol.replace('/','_').replace(':PERP','_Perp')
                or self.version_id != research.profile.strategy_version_id
                or self.dataset_id != research.dataset_id or self.dataset_hash != research.dataset_hash
                or self.session_started_at_ns != int(paper.started_at_ns)
                or validate_strategy(self.definition) != self.definition
                or content_hash(self.definition) != research.profile.strategy_hash
                or paper.closed_trades < 1 or paper.observed_bars < 1
                or paper.stored_status not in ('RUNNING','CLOSE_ONLY','STOPPED')
                or paper.source_advanced or paper.has_estimated_closure):
            raise ValueError('El origen perpetuo no acredita una prueba LAB/PAPER compatible')
        return self


@dataclass(frozen=True)
class VerifiedPerpBotOrigin:
    origin: VerifiedObservedOrigin = field(repr=False)
    archive: object = field(repr=False)
    stamp: object = field(repr=False)
    source_json: str
    header: tuple = field(repr=False)
    tail: tuple = field(repr=False)

    def source(self) -> PerpBotSource:
        return PerpBotSource.model_validate(json.loads(self.source_json))

    def recheck(self,require_permission=lambda:None) -> None:
        # A preview capability can outlive a SQL row edit. Replaying again
        # checks actual original event bytes, not merely their stored hashes.
        # Compatible appended evidence is allowed; the approved prefix stays
        # pinned by hold_current. Do this before collecting the fresh seed.
        require_permission()
        with self.hold_current(),self.archive.paper.connect() as db:
            db.execute('BEGIN')
            row=db.execute('SELECT * FROM paper_observed_sessions WHERE id=?',
                (self.source().session_id,)).fetchone()
            replay_observed_session(db,row)
            require_permission()

    @contextmanager
    def hold_current(self):
        source = self.source()
        with self.origin.hold_current(), paper_perp_access.guard(self.archive.paper.path,self.stamp.epoch):
            self.archive._stamp(self.stamp)
            with self.archive.paper.connect() as db:
                db.execute('BEGIN')
                row = db.execute('SELECT * FROM paper_observed_sessions WHERE id=?',(source.session_id,)).fetchone()
                anchor = db.execute('SELECT hash FROM paper_observed_events WHERE session_id=? AND sequence=?',
                    (source.session_id,self.tail[0])).fetchone()
                maximum = db.execute('SELECT max(sequence) FROM paper_observed_events WHERE session_id=?',
                    (source.session_id,)).fetchone()[0]
                if (row is None or tuple(row[key] for key in _PINNED) != self.header
                        or row['sequence'] < self.tail[0] or maximum != row['sequence']
                        or anchor is None or anchor['hash'] != self.tail[1]
                        or row['sequence']==self.tail[0] and tuple(row[key] for key in _TAIL) != self.tail
                        or row['status'] not in ('RUNNING','CLOSE_ONLY','STOPPED')
                        or db.execute('SELECT 1 FROM paper_observed_gap_closes WHERE session_id=?',
                            (source.session_id,)).fetchone() is not None):
                    raise ValueError('El origen PAPER cambió o perdió su prefijo probado')
                self.archive._stamp(self.stamp)
                yield


_PINNED = ('seed_json','seed_hash','origin_json','origin_hash','runtime_id','start_request_id','request_hash')
_TAIL = ('sequence','head_hash','checkpoint_json','status')


def verified_perp_bot_origin(lab, archive, session_id, sub_account_id, client, *,
                            clock_ns=time.time_ns, require_permission=lambda:None) -> VerifiedPerpBotOrigin:
    review = review_perp_live(lab,archive,session_id,sub_account_id,client,
        clock_ns=clock_ns,require_permission=require_permission)
    if review.blockers != ['PERP_EXECUTOR_NOT_IMPLEMENTED']:
        raise ValueError('Resuelve los bloqueos de la revisión LAB/PAPER antes de autorizar REAL')
    # The old review remains execution_supported:false; it is evidence only.
    origin = verified_observed_origin(lab,review.research.artifact_id)
    if origin.reference() != review.research:
        raise ValueError('La validación LAB cambió durante la preparación REAL')
    source = PerpBotSource(schema_name='orbis.grvt.perp.bot.source.v1',
        session_id=review.paper.session_id,symbol=review.research.symbol,
        instrument=review.research.symbol.replace('/','_').replace(':PERP','_Perp'),
        timeframe=review.research.timeframe,engine_version=review.research.profile.signal_engine,
        version_id=review.research.profile.strategy_version_id,dataset_id=review.research.dataset_id,
        dataset_hash=review.research.dataset_hash,session_started_at_ns=int(review.paper.started_at_ns),
        definition=json.loads(origin.definition_json),research=review.research,paper=review.paper,
        execution_authority='NONE')
    stamp = archive._stamp()
    require_permission()
    with archive.paper.connect() as db:
        db.execute('BEGIN')
        row = db.execute('SELECT * FROM paper_observed_sessions WHERE id=?',(source.session_id,)).fetchone()
        if row is None or (row['seed_hash'],row['origin_hash']) != (source.paper.seed_hash,source.paper.origin_hash):
            raise ValueError('El origen PAPER fue sustituido')
        replay_observed_session(db,row)
        anchor=db.execute('SELECT hash FROM paper_observed_events WHERE session_id=? AND sequence=?',
            (source.session_id,source.paper.sequence)).fetchone()
        if anchor is None or anchor['hash'] != source.paper.head_hash:
            raise ValueError('El prefijo revisado PAPER fue sustituido')
        header = tuple(row[key] for key in _PINNED)
        tail = tuple(row[key] for key in _TAIL)
    verified = VerifiedPerpBotOrigin(origin,archive,stamp,encoded(source.payload()),header,tail)
    with verified.hold_current():
        require_permission()
    return verified
