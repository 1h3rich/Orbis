"""Read-only chart and research projections of archived GRVT TESTNET fills."""

from dataclasses import dataclass
from hashlib import sha256
from typing import Literal, TypedDict

from sqlalchemy.orm import Session

from app.lab.store import content_hash, encoded
from app.live_execution.grvt_bot_archive import _read
from app.live_execution.grvt_testnet_bracket_emergency import read_emergency_attempts
from app.live_execution.grvt_testnet_bracket_finance import (
    verified_bracket_audits, verified_bracket_reconciliation,
)
from app.live_execution.grvt_testnet_bracket_journal import _verified
from app.live_execution.grvt_testnet_evidence import _verified_payload
from app.live_execution.grvt_testnet_reconciliation import verified_reconciliation
from app.portfolio.models import (
    GrvtTestnetAuditEvidence, GrvtTestnetBotRun, GrvtTestnetBracketGroup,
    GrvtTestnetOrderIntent,
)
from app.portfolio.vault import RealVault


Role = Literal["ENTRY", "STOP_LOSS", "TAKE_PROFIT", "IOC"]
Basis = Literal["AUDITED", "RECONCILED"]
MAX_CHART_FILLS = 5_000
MAX_EXPORT_BYTES = 4 * 1024 * 1024
EXPORT_SCHEMA = "orbis.grvt.testnet.bot-research.v1"
EntryEvidence = Literal[
    "NO_VERIFIED_ENTRY", "AUDITED_NO_ENTRY", "AUDITED_ENTRY", "RECONCILED_UNFILLED", "RECONCILED_ENTRY",
]


class BotResearchExportError(ValueError):
    """The verified selection cannot be exported under its format limits."""


class Execution(TypedDict):
    cycle: int
    role: Role
    side: str
    basis: Basis
    event_time_ns: int
    price: str
    size: str
    fee: str
    fee_currency: str


class BotExecutions(TypedDict):
    network: Literal["TESTNET"]
    run_id: str
    sub_account_id: str
    symbol: str
    direction: str
    total: int
    executions: list[Execution]


@dataclass(frozen=True)
class CycleFills:
    executions: list[Execution]
    entry_evidence: EntryEvidence


def _cycle_fills(db: Session, parent: str, cycle: int) -> CycleFills:
    group = db.get(GrvtTestnetBracketGroup, parent)
    if group is None:  # A selected signal may never have been sent.
        return CycleFills([], "NO_VERIFIED_ENTRY")
    plan = _verified(db, group)
    result: list[Execution] = []
    seen: set[tuple[str, str]] = set()

    def append(order: dict, fills: list[dict], role: Role, basis: Basis) -> None:
        for fill in fills:
            key = (order["client_order_id"], fill["trade_id"])
            if key in seen:
                raise ValueError("Fill testnet repetido")
            seen.add(key)
            result.append({
                "cycle": cycle, "role": role, "side": order["side"], "basis": basis,
                "event_time_ns": fill["event_time_ns"], "price": fill["price"],
                "size": fill["size"], "fee": fill["fee"], "fee_currency": fill["fee_currency"],
            })

    audits = verified_bracket_audits(db, group, plan)
    entry_evidence: EntryEvidence = "NO_VERIFIED_ENTRY"
    if audits:
        receipt = verified_bracket_reconciliation(db, group, plan)
        basis: Basis = "RECONCILED" if receipt and receipt["audits"][-1][0] == audits[-1]["id"] else "AUDITED"
        has_entry = bool(audits[-1]["legs"][0]["fills"]["rows"])
        if basis == "RECONCILED":
            entry_evidence = "RECONCILED_ENTRY" if has_entry else "RECONCILED_UNFILLED"
        else:
            entry_evidence = "AUDITED_ENTRY" if has_entry else "AUDITED_NO_ENTRY"
        for leg, evidence in zip(plan["legs"], audits[-1]["legs"], strict=True):
            append(evidence["order"], evidence["fills"]["rows"], leg["role"], basis)

    for attempt in read_emergency_attempts(db, parent):
        if attempt["status"] != "RECONCILED":
            continue
        intent = db.get(GrvtTestnetOrderIntent, attempt["client_order_id"])
        if intent is None:
            raise ValueError("Falta la reducción IOC conciliada")
        receipt = verified_reconciliation(db, intent)
        audit_id, digest = receipt["audits"][-1]
        audit = db.get(GrvtTestnetAuditEvidence, audit_id)
        if audit is None or audit.payload_digest != digest:
            raise ValueError("Falta la auditoría de la reducción IOC")
        evidence = _verified_payload(audit)
        append(evidence["order"], evidence["fills"]["rows"], "IOC", "RECONCILED")
    result.sort(key=lambda fill: (fill["event_time_ns"], fill["role"]))
    return CycleFills(result, entry_evidence)


def _run_cycles(saved: dict) -> list[dict]:
    if "cycles" in saved:
        return saved["cycles"]
    selection = saved["selection"]
    terminal = saved["terminal"]
    outcome = ({"completed_at_ns": terminal["at_ns"]} if terminal and terminal["status"] == "COMPLETE" else None)
    return [{"sequence": 1, "selection": selection, "outcome": outcome}] if selection else []


def read_bot_executions(vault: RealVault, run_id: str, sub_account_id: str) -> BotExecutions | None:
    """Verify the run and its linked evidence without consulting the exchange."""
    with vault.session() as db:
        db.connection().exec_driver_sql("BEGIN")
        row = db.get(GrvtTestnetBotRun, run_id)
        if row is None or row.sub_account_id != sub_account_id:
            return None
        saved = _read(db, row)
        fills = [
            fill
            for cycle in _run_cycles(saved)
            for fill in _cycle_fills(db, cycle["selection"]["parent_client_order_id"], cycle["sequence"]).executions
        ]
        fills.sort(key=lambda fill: (fill["event_time_ns"], fill["cycle"], fill["role"]))
        return {
            "network": "TESTNET", "run_id": run_id, "sub_account_id": sub_account_id,
            "symbol": saved["source"]["symbol"], "direction": saved["source"]["definition"]["direction"],
            "total": len(fills), "executions": fills[-MAX_CHART_FILLS:],
        }


def export_bot_research(vault: RealVault, run_id: str, sub_account_id: str) -> dict | None:
    """Build a deterministic dataset, preserving selected cycles even without fills."""
    lines: list[bytes] = []
    size = fill_count = 0
    with vault.session() as db:
        db.connection().exec_driver_sql("BEGIN")
        row = db.get(GrvtTestnetBotRun, run_id)
        if row is None or row.sub_account_id != sub_account_id:
            return None
        saved = _read(db, row)
        source = saved["source"]
        provenance = {
            "episode_sha256": row.payload_digest, "strategy_version_id": source["version_id"],
            "strategy_definition_sha256": content_hash(source["definition"]),
            "dataset_sha256": source["dataset_hash"], "engine_version": source["engine_version"],
            "paper_dataset_source": "grvt-perp-trade-public",
        }
        for cycle in _run_cycles(saved):
            selection, outcome = cycle["selection"], cycle["outcome"]
            archived = _cycle_fills(db, selection["parent_client_order_id"], cycle["sequence"])
            fills = [
                {**fill, "event_time_ns": str(fill["event_time_ns"])}
                for fill in archived.executions
            ]
            sample = {
                "schema": EXPORT_SCHEMA, "environment": "TESTNET", "cycle": cycle["sequence"],
                "provenance": provenance,
                "input": {
                    "symbol": source["symbol"], "instrument": source["instrument"],
                    "timeframe": source["timeframe"], "direction": source["definition"]["direction"],
                    "paper_signal_recorded_at_ns": str(selection["recorded_at_ns"]),
                    "selected_at_ns": str(selection["selected_at_ns"]),
                    "entry_cap_usdt": saved["entry_cap"], "exit_cap_usdt": saved["exit_cap"],
                    "max_slippage_bps": saved["max_slippage_bps"],
                },
                "outcome": {
                    "entry_evidence": archived.entry_evidence,
                    "completed_at_ns": str(outcome["completed_at_ns"]) if outcome else None,
                    "fills": fills,
                },
            }
            line = (encoded({**sample, "row_sha256": content_hash(sample)}) + "\n").encode("utf-8")
            size += len(line)
            if size > MAX_EXPORT_BYTES:
                raise BotResearchExportError("El episodio supera el tamaño máximo de exportación")
            lines.append(line)
            fill_count += len(fills)
    data = b"".join(lines)
    return {
        "schema": EXPORT_SCHEMA, "record_count": len(lines), "fill_count": fill_count,
        "sha256": sha256(data).hexdigest(), "jsonl": data.decode("utf-8"),
    }
