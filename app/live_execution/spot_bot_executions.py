"""Proyección paginada de fills propios REAL verificados, sin red ni ejecución."""

from __future__ import annotations

import hashlib
import json
import re

from app.lab.store import content_hash, encoded
from app.live_execution.journal import _stored_policy
from app.live_execution.spot_bot_store import SpotBotStoreError,bot_authorizations,bot_record,owned_spot_bot_ledger
from app.portfolio.vault import RealVault


RESEARCH_SCHEMA = "orbis.mexc.real.bot-fills.v1"
MAX_RESEARCH_ROWS = 5000
MAX_RESEARCH_BYTES = 4 * 1024 * 1024


def _snapshot(vault: RealVault, account_id: str, bot_id: str,
              execution_policies: dict[str, str] | None = None):
    with vault.session() as db:
        # Read the decisions, fills, bookings and authorizations from one snapshot.
        db.connection().exec_driver_sql("BEGIN")
        row, config, _ = bot_record(db, bot_id)
        if row.account_id != account_id:
            raise SpotBotStoreError("El bot no pertenece a esta cuenta")
        policy = _stored_policy(db, row.policy_id)
        items: list[dict] = []
        _, ledger_revision = owned_spot_bot_ledger(vault, bot_id, db=db, executions=items,
                                                  execution_policies=execution_policies)
    digest = hashlib.sha256(json.dumps({"ledger_revision":ledger_revision,"items":items},
        sort_keys=True,separators=(",",":"),ensure_ascii=True).encode("ascii")).hexdigest()
    return config, policy, items, digest


def spot_bot_executions_page(vault: RealVault,account_id: str,bot_id: str,*,limit: int = 500,
                             offset: int = 0,revision: str | None = None) -> dict:
    if (type(limit) is not int or not 1 <= limit <= 500 or type(offset) is not int or offset < 0
            or offset > 0 and revision is None
            or revision is not None and (not isinstance(revision,str) or not re.fullmatch(r"[0-9a-f]{64}",revision))):
        raise SpotBotStoreError("Paginación de ejecuciones REAL no válida")
    config, policy, items, digest = _snapshot(vault, account_id, bot_id)
    if revision is not None and revision != digest:
        raise SpotBotStoreError("Las ejecuciones REAL cambiaron durante la consulta; actualiza el gráfico")
    if offset > len(items):
        raise SpotBotStoreError("Cursor de ejecuciones fuera de la instantánea")
    end = min(offset+limit,len(items))
    return {"bot_id":bot_id,"account_id":account_id,"environment":"REAL","provider":"mexc",
        "symbol":config["source"]["symbol"],"timeframe":config["source"]["timeframe"],
        "base_asset":policy.base_asset,"quote_asset":policy.quote_asset,
        "evidence":"OWN_FINAL_SETTLED_BOOKED_FILLS","revision":digest,"total":len(items),
        "offset":offset,"next_offset":end if end < len(items) else None,"executions":items[offset:end]}


def export_spot_bot_fills(vault: RealVault, account_id: str, bot_id: str, *, revision: str) -> dict:
    """Export only own reconciled fills, scoped to the revision reviewed in the chart."""
    execution_policies: dict[str, str] = {}
    config, _, items, digest = _snapshot(vault, account_id, bot_id, execution_policies)
    if revision != digest:
        raise SpotBotStoreError("Las ejecuciones REAL cambiaron; actualiza el gráfico")
    if not items:
        raise SpotBotStoreError("El bot aún no tiene fills asentados")
    if len(items) > MAX_RESEARCH_ROWS:
        raise SpotBotStoreError("El bot supera el máximo de fills de la exportación")
    authorizations = {item["policy_id"]:(number,item)
                      for number,item in enumerate(bot_authorizations(config),1)}
    orders: dict[str, int] = {}
    lines: list[bytes] = []
    size = 0
    for index, fill in enumerate(items, 1):
        authorization_index, authorization = authorizations[execution_policies[fill["fill_id"]]]
        source = authorization["source"]
        binding = authorization.get("research_binding")
        reserve = binding["research"] if binding is not None else None
        order = orders.setdefault(fill["intent_id"],len(orders)+1)
        sample = {
            "schema":RESEARCH_SCHEMA, "environment":"REAL", "provider":"mexc", "execution_authority":"NONE",
            "evidence":"OWN_FINAL_SETTLED_BOOKED_FILLS", "fill_index":index, "order_index":order,
            "market":{"symbol":source["symbol"],"timeframe":source["timeframe"]},
            "provenance":{
                "authorization_index":authorization_index,
                "strategy_kind":source.get("kind","STRATEGY"),
                "strategy_version_id":source["version_id"],
                "definition_sha256":source["definition_hash"],
                "paper_session_id":source["session_id"],
                "paper_dataset_sha256":source["dataset_hash"],
                "paper_engine":source["engine_version"],
                "reserve":({"artifact_id":reserve["artifact_id"],"artifact_sha256":reserve["artifact_digest"],
                            "method":reserve["method"],"windows":reserve["windows"]}
                           if reserve is not None else None),
            },
            "fill":{key:fill[key] for key in ("side","time_ms","price","quantity_base","gross_quote",
                                             "fee","fee_asset","reason","signal_at","decision_at")},
        }
        line = (encoded({**sample,"row_sha256":content_hash(sample)})+"\n").encode("utf-8")
        size += len(line)
        if size > MAX_RESEARCH_BYTES:
            raise SpotBotStoreError("El archivo de fills supera el tamaño máximo de exportación")
        lines.append(line)
    data = b"".join(lines)
    return {"schema":RESEARCH_SCHEMA,"revision":digest,"record_count":len(lines),
            "sha256":hashlib.sha256(data).hexdigest(),"jsonl":data.decode("utf-8")}
