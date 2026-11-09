"""LAB-owned immutable perp inputs; separate from every existing price dataset."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from uuid import uuid4

from app.lab.access import lab_format
from app.lab.engine import LabError
from app.lab.perp_dataset import PerpDataset, read_perp_dataset
from app.lab.store import LabStore, content_hash, encoded


PERP_DATASETS_DDL = """CREATE TABLE perp_datasets (
    id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
    payload_json TEXT NOT NULL, hash TEXT NOT NULL UNIQUE
)"""


def verified_record(row) -> tuple[dict, PerpDataset]:
    try:
        payload = json.loads(row['payload_json'])
        parsed = read_perp_dataset(payload)
        if parsed.payload() != payload or content_hash(payload) != row['hash']:
            raise LabError('Dataset perpetuo con huella incorrecta')
        return {'id':row['id'],'created_at':row['created_at'],'hash':row['hash']}, parsed
    except (ValueError,TypeError,KeyError) as exc:
        raise LabError('Archivo perpetuo no verificable: '+str(exc)[:200]) from exc


def summary(header: dict, dataset: PerpDataset) -> dict:
    payload = dataset.payload()
    return {**header,'schema':payload['schema'],'symbol':dataset.symbol,'timeframe':dataset.timeframe,
        'instrument':dataset.instrument,'start':dataset.start,'end':dataset.end,
        'captured_start_ns':str(dataset.captured_start_ns),'captured_end_ns':str(dataset.captured_end_ns),
        'rules_captured_ns':str(max(int(dataset.rules.fetched_at_ns),int(dataset.specification.fetched_at_ns))),
        'trade_count':len(dataset.trade.bars),'mark_count':len(dataset.mark.bars),
        'funding_count':len(dataset.funding)-1,'funding_anchor_ns':dataset.funding[0].funding_time,
        'component_hashes':{key:content_hash(payload[key]) for key in ('trade','mark','funding','rules','specification')},
        'risk_rules_scope':dataset.risk_rules_scope,'historical_execution_model':dataset.historical_execution_model}


class PerpDatasetStore:
    def __init__(self, lab: LabStore):
        self.lab = lab

    def save(self, dataset: PerpDataset) -> dict:
        from app.lab.perp_rules import RuleObservation, insert_observation

        # Reparse a canonical copy: callers cannot mutate nested model lists to
        # bypass validation after a successful download.
        dataset = read_perp_dataset(dataset.payload())
        if not self.lab.encrypted or lab_format(self.lab.path)!='encrypted_or_invalid':
            raise LabError('Los datos perpetuos requieren LAB cifrado y desbloqueado')
        payload=dataset.payload();digest=content_hash(payload)
        with self.lab.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM perp_datasets WHERE hash=?',(digest,)).fetchone()
            insert_observation(db,RuleObservation.from_dataset(dataset))
            if row is not None:
                return summary(*verified_record(row))
            header={'id':str(uuid4()),'created_at':datetime.now(timezone.utc).isoformat(),'hash':digest}
            db.execute('INSERT INTO perp_datasets (id,created_at,payload_json,hash) VALUES (?,?,?,?)',
                (header['id'],header['created_at'],encoded(payload),digest))
        return summary(header,dataset)

    def list(self) -> list[dict]:
        if lab_format(self.lab.path) in ('missing','empty'):
            return []
        with self.lab.connect() as db:
            rows=db.execute('SELECT * FROM perp_datasets ORDER BY created_at DESC,id DESC LIMIT 100').fetchall()
            return [summary(*verified_record(row)) for row in rows]

    def read(self, identifier: str) -> tuple[dict,PerpDataset]:
        with self.lab.connect() as db:
            row=db.execute('SELECT * FROM perp_datasets WHERE id=?',(identifier,)).fetchone()
            if row is None:
                raise LabError('Dataset perpetuo no encontrado')
            return verified_record(row)
