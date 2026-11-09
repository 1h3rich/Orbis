"""Diagnóstico agregado de traslados MEXC; nunca habilita ejecución."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.portfolio.domain import PortfolioError
from app.portfolio.mexc_transfer_matches import verify_mexc_transfer_match
from app.portfolio.mexc_transfers import read_transfer_snapshot
from app.portfolio.models import (
    MexcTransferSnapshot, RealAccount, RealMexcAccountLink, RealMexcTransferMatch,
)
from app.portfolio.repository import list_events, verify_paired_transfers


_MAX_SNAPSHOTS = 500
_MATCH_WINDOW = timedelta(hours=24)
_TRANSFER_KINDS = frozenset((
    "TRANSFER_IN", "TRANSFER_OUT", "CASH_TRANSFER_IN", "CASH_TRANSFER_OUT",
))


def _coverage(windows: list[tuple[int, int]]) -> list[dict]:
    merged: list[list[int]] = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [{"start_ms": start, "end_ms": end} for start, end in merged]


def audit_mexc_transfer_archive(db: Session, account_id: str) -> dict:
    """Cuenta referencias distintas, no filas repetidas entre fotografías.

    Sólo usa capturas con UID View comprobado para cobertura y movimientos
    observados. Un archivo incompleto, sin IDs o con contradicciones no puede
    presentarse como saldo o historial conciliado.
    """
    if db.get(RealAccount, account_id) is None:
        raise PortfolioError("Real account not found")
    links = db.query(RealMexcAccountLink).filter(
        RealMexcAccountLink.account_id == account_id).all()
    if len(links) != 1:
        raise PortfolioError("Real account needs exactly one linked MEXC connection")
    account_ref = links[0].account_ref
    archive = db.query(MexcTransferSnapshot).filter(
        MexcTransferSnapshot.account_ref == account_ref).order_by(
            MexcTransferSnapshot.observed_at, MexcTransferSnapshot.id,
        ).limit(_MAX_SNAPSHOTS + 1).all()
    if len(archive) > _MAX_SNAPSHOTS:
        raise PortfolioError("MEXC transfer archive exceeds audit limit")
    verify_paired_transfers(db)
    matches = db.query(RealMexcTransferMatch).filter(
        RealMexcTransferMatch.account_ref == account_ref,
        RealMexcTransferMatch.account_id == account_id).all()
    for match in matches:
        verify_mexc_transfer_match(db, match, check_history=True)
    by_ref = {(match.direction, match.source_ref): match for match in matches}
    confirmed_event_ids = {match.event_id for match in matches}
    local_events = [event for event in list_events(db, account_id)
                    if event.input.kind in _TRANSFER_KINDS]

    windows: list[tuple[int, int]] = []
    verified_count = unverified_count = missing_ref_rows = 0
    non_final_rows = internal_withdrawals = route_unknown = 0
    observed: dict[tuple[str, str], set[tuple]] = defaultdict(set)
    known_destinations: dict[tuple[str, str], set[str]] = defaultdict(set)
    known_contracts: dict[tuple[str, str], set[str]] = defaultdict(set)
    known_chain_refs: dict[tuple[str, str], set[str]] = defaultdict(set)
    withdrawal_chain_owners: dict[str, set[tuple[str, str]]] = defaultdict(set)
    history: dict[tuple[str, str], list[tuple[datetime, int, int]]] = defaultdict(list)
    ambiguous: set[tuple[str, str]] = set()
    final_refs: set[tuple[str, str]] = set()
    for item in archive:
        snapshot = read_transfer_snapshot(db, item.id)
        if not snapshot["identity_verified"]:
            unverified_count += 1
            continue
        verified_count += 1
        windows.append((snapshot["start_ms"], snapshot["end_ms"]))
        observed_at = datetime.fromisoformat(snapshot["observed_at"])
        for direction, field, final_status in (
                ("deposit", "deposits", 5), ("withdrawal", "withdrawals", 7)):
            seen_in_snapshot: set[tuple[str, str]] = set()
            for row in snapshot[field]:
                ref = row.get("source_ref")
                if ref is not None:
                    key = (direction, ref)
                    if key in seen_in_snapshot:
                        ambiguous.add(key)
                    seen_in_snapshot.add(key)
                    observed[key].add((row["asset"], row["network"], row["amount"],
                                       row["fee"], row["time_ms"], row.get("transfer_type")))
                    if row.get("destination_ref"):
                        known_destinations[key].add(row["destination_ref"])
                    if row.get("token_contract"):
                        known_contracts[key].add(row["token_contract"])
                    if row.get("chain_ref"):
                        known_chain_refs[key].add(row["chain_ref"])
                        if direction == "withdrawal":
                            withdrawal_chain_owners[row["chain_ref"]].add(key)
                    history[key].append((observed_at, row["status"], final_status))
                if row["status"] != final_status:
                    non_final_rows += 1
                    continue
                if direction == "withdrawal" and row.get("transfer_type") != 0:
                    if row.get("transfer_type") == 1:
                        internal_withdrawals += 1
                    else:
                        route_unknown += 1
                    continue
                if ref is None:
                    missing_ref_rows += 1
                    continue
                final_refs.add(key)
    conflicted = ambiguous | {key for key, variants in observed.items() if len(variants) != 1}
    conflicted |= {key for key, variants in known_destinations.items() if len(variants) > 1}
    conflicted |= {key for key, variants in known_contracts.items() if len(variants) > 1}
    conflicted |= {key for key, variants in known_chain_refs.items() if len(variants) > 1}
    for owners in withdrawal_chain_owners.values():
        if len(owners) > 1:
            conflicted.update(owners)
    for key, observations in history.items():
        observations.sort()
        final_seen = False
        previous_time = previous_status = None
        for stamp, status, final_status in observations:
            if stamp == previous_time and status != previous_status:
                conflicted.add(key)
            if status == final_status:
                final_seen = True
            elif final_seen:
                conflicted.add(key)
            previous_time, previous_status = stamp, status
    unmatched = sorted(final_refs - set(by_ref) - conflicted)
    unobserved_matches = sorted(set(by_ref) - final_refs)
    unlinked_local = [event for event in local_events if event.id not in confirmed_event_ids]
    coverage_windows = _coverage(windows)
    covered_ranges = [(datetime.fromtimestamp(row["start_ms"] / 1000, timezone.utc),
                       datetime.fromtimestamp(row["end_ms"] / 1000, timezone.utc))
                      for row in coverage_windows]

    def fully_covered(event) -> bool:
        # Es la misma ventana ±24 h que usa el comparador de candidatos.
        lower, upper = event.occurred_at - _MATCH_WINDOW, event.occurred_at + _MATCH_WINDOW
        return any(start <= lower and upper <= end for start, end in covered_ranges)

    covered_unlinked = [event for event in unlinked_local if fully_covered(event)]
    legacy_matches = sum(1 for match in matches if not read_transfer_snapshot(
        db, match.snapshot_id)["identity_verified"])
    return {
        "account_id": account_id, "account_ref": account_ref,
        "conclusion": "ARCHIVE_AUDIT_NOT_ACCOUNT_RECONCILIATION",
        "ready_for_execution": False,
        "snapshots": {"total": len(archive), "verified": verified_count,
                      "unverified": unverified_count},
        "coverage_windows": coverage_windows,
        "counts": {"final_distinct_references": len(final_refs),
                   "confirmed_matches": len(matches), "legacy_matches": legacy_matches,
                   "unmatched_final_references": len(unmatched),
                   "conflicted_references": len(conflicted),
                   "matches_not_in_verified_snapshots": len(unobserved_matches),
                   "unlinked_local_events": len(unlinked_local),
                   "unlinked_local_fully_covered": len(covered_unlinked),
                   "unlinked_local_partial_or_outside": len(unlinked_local) - len(covered_unlinked),
                   "final_rows_without_reference": missing_ref_rows,
                   "non_final_rows": non_final_rows,
                   "internal_withdrawal_rows": internal_withdrawals,
                   "unknown_withdrawal_route_rows": route_unknown},
        "unmatched_references": [
            {"direction": direction, "source_ref": ref} for direction, ref in unmatched[:100]],
        "unlinked_local_event_ids": [event.id for event in unlinked_local[:100]],
        "detail_truncated": len(unmatched) > 100 or len(unlinked_local) > 100,
    }
