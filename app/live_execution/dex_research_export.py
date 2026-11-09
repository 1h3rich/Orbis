"""Research rows from the exact DEX chart snapshot, without custody data."""
import hashlib

from sqlalchemy.orm import Session

from app.lab.store import content_hash, encoded
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_executions import read_executions
from app.portfolio.dex_bot_bindings import binding_record
from app.portfolio.dex_bot_journal import run_record
from app.portfolio.dex_research import authorization_qualification
from app.portfolio.dex_verified_read import scope

RESEARCH_SCHEMA = "orbis.uniswap.real.bot-swaps.v1"
MAX_RESEARCH_ROWS = 5000
MAX_RESEARCH_BYTES = 4 * 1024 * 1024


def _provenance(db: Session, run_id: str, authorization_index: int) -> dict:
    row, _, _, archive = run_record(db, run_id)
    review = archive["review"]
    source = review["source"]
    _, binding, _ = binding_record(db, row.binding_id)
    saved = binding["origin"]["backtest"]
    qualification = authorization_qualification(db, run_id)
    evidence = qualification["evidence"] if qualification is not None else None
    reserve = evidence["research"] if evidence is not None else None
    return {
        "authorization_index": authorization_index,
        "strategy_kind": review["source_kind"],
        "strategy_version_id": source["version_id"],
        "definition_sha256": source["definition_hash"],
        "paper_session_id": source["session_id"],
        "paper_dataset_sha256": source["dataset_hash"],
        "paper_observation_candle_t": review["candle_t"],
        "paper_engine": source["engine_version"],
        "lab_backtest_id": saved["id"],
        "lab_backtest_result_sha256": saved["result_hash"],
        "reserve": ({
            "artifact_id": reserve["artifact_id"],
            "artifact_sha256": reserve["artifact_digest"],
            "method": reserve["method"],
            "windows": reserve["windows"],
            "historical_model": evidence["historical_model"],
        } if reserve is not None else None),
    }


def export_dex_bot_swaps(db: Session, *, account_id: str, run_id: str, revision: str) -> dict:
    """Read one snapshot and resolve each swap against its own archived permission."""
    with scope(db):
        snapshot = read_executions(db, account_id=account_id, run_id=run_id)
        if snapshot["revision"] != revision:
            raise DexExecutorError("Las ejecuciones DEX cambiaron; actualiza el gráfico")
        if not snapshot["total"]:
            raise DexExecutorError("El bot aún no tiene swaps confirmados")
        if snapshot["total"] > MAX_RESEARCH_ROWS:
            raise DexExecutorError("El bot supera el máximo de swaps de la exportación")

        authorization_indices = {
            identifier: index for index, identifier in enumerate(snapshot["family_run_ids"], 1)
        }
        provenance: dict[str, dict] = {}
        data = bytearray()
        for index, fill in enumerate(snapshot["executions"], 1):
            identifier = fill["run_id"]
            if identifier not in provenance:
                provenance[identifier] = _provenance(db, identifier, authorization_indices[identifier])
            sample = {
                "schema": RESEARCH_SCHEMA, "environment": "REAL", "provider": snapshot["provider"],
                "chain_id": snapshot["chain_id"], "execution_authority": "NONE",
                "evidence": snapshot["evidence"], "swap_index": index,
                "market": {key: snapshot[key] for key in ("symbol", "timeframe", "base_asset", "quote_asset")},
                "provenance": provenance[identifier],
                "signal": {
                    **{key: fill[key] for key in ("reason", "signal_candle_t", "decision_at", "reference_price",
                                                 "reference_at", "reference_observed_at", "activation_kind", "activation_key")},
                    "reference_provider": snapshot["reference_provider"],
                    **({"volume":fill["volume"]} if "volume" in fill else {}),
                },
                "swap": {
                    **{key: fill[key] for key in ("side", "role", "occurred_at", "quantity_base", "gross_quote",
                                                 "price", "financially_booked", "partial", "gas_fee_wei")},
                    "gas_fee_bnb": fill["fee"],
                    "realized_pnl_quote_before_gas": fill["realized_pnl_quote"],
                    "receipt_kind": fill.get("receipt_kind", "OPERATOR"),
                },
            }
            data.extend((encoded({**sample, "row_sha256": content_hash(sample)}) + "\n").encode("utf-8"))
            if len(data) > MAX_RESEARCH_BYTES:
                raise DexExecutorError("El archivo de swaps supera el tamaño máximo de exportación")

    return {"schema": RESEARCH_SCHEMA, "revision": revision, "record_count": snapshot["total"],
            "sha256": hashlib.sha256(data).hexdigest(), "jsonl": data.decode("utf-8")}
