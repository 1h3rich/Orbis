"""Posibles coincidencias entre libro REAL y una fotografía MEXC, sin asientos."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext

from sqlalchemy.orm import Session

from app.portfolio.domain import CALC_PRECISION, PortfolioError
from app.portfolio.models import RealAccount, RealMexcAccountLink
from app.portfolio.models import RealMexcTransferMatch
from app.portfolio.mexc_transfer_matches import verify_mexc_transfer_match
from app.portfolio.mexc_transfers import read_transfer_snapshot
from app.portfolio.repository import list_events, verify_paired_transfers


WINDOW = timedelta(hours=24)


def _is_final(direction: str, status: int) -> bool:
    # MEXC Spot v3: depósito SUCCESS=5; retiro SUCCESS=7. Otros estados
    # incluyen espera, revisión, cancelación y fallo; no son prueba de traslado.
    return status == (5 if direction == "deposit" else 7)


def compare_mexc_transfer_snapshot(db: Session, account_id: str, snapshot_id: str) -> dict:
    """El usuario supone que `account_id` representa MEXC; no verifica titularidad.

    Una igualdad de importe/activo/tiempo es sólo una pista. La instantánea
    externa omite deliberadamente txid, dirección y otros identificadores.
    """
    if db.get(RealAccount, account_id) is None:
        raise PortfolioError("Real account not found")
    snapshot = read_transfer_snapshot(db, snapshot_id)
    verify_paired_transfers(db)
    linked_ref = db.get(RealMexcAccountLink, snapshot["account_ref"])
    other_refs = db.query(RealMexcAccountLink).filter(
        RealMexcAccountLink.account_id == account_id,
        RealMexcAccountLink.account_ref != snapshot["account_ref"],
    ).first()
    if (linked_ref is not None and linked_ref.account_id != account_id) or other_refs is not None:
        return {"snapshot_id": snapshot["id"], "account_id": account_id,
                "account_reference": "CONFLICT", "conclusion": "ACCOUNT_REFERENCE_CONFLICT_NO_COMPARISON",
                "coverage": snapshot["coverage"],
                "identity_verified": snapshot["identity_verified"],
                "rows": [], "observed_rows": []}
    reference_status = "LOCAL_LINK_MATCH_NOT_OWNERSHIP_PROOF" if linked_ref else "UNLINKED_ACCOUNT_ASSUMED"
    first = datetime.fromtimestamp(snapshot["start_ms"] / 1000, timezone.utc)
    last = datetime.fromtimestamp(snapshot["end_ms"] / 1000, timezone.utc)
    indexed: dict[tuple[str, str, Decimal], tuple[list[datetime], list[tuple[int, dict]]]] = {}
    grouped: dict[tuple[str, str, Decimal], list[tuple[datetime, int, dict]]] = {}
    for direction, field in (("deposit", "deposits"), ("withdrawal", "withdrawals")):
        for index, observed in enumerate(snapshot[field]):
            observed_at = datetime.fromtimestamp(observed["time_ms"] / 1000, timezone.utc)
            grouped.setdefault((direction, observed["asset"], Decimal(observed["amount"])), []).append(
                (observed_at, index, observed))
    for key, entries in grouped.items():
        entries.sort(key=lambda entry: (entry[0], entry[1]))
        indexed[key] = ([entry[0] for entry in entries],
                        [(entry[1], entry[2]) for entry in entries])
    local = [event for event in list_events(db, account_id) if event.input.kind in (
        "TRANSFER_IN", "TRANSFER_OUT", "CASH_TRANSFER_IN", "CASH_TRANSFER_OUT")]
    confirmed = db.query(RealMexcTransferMatch).filter(
        RealMexcTransferMatch.account_ref == snapshot["account_ref"],
        RealMexcTransferMatch.account_id == account_id,
    ).all()
    for match in confirmed:
        verify_mexc_transfer_match(db, match, check_history=True)
    by_event = {match.event_id: match for match in confirmed}
    by_source = {(match.direction, match.source_ref): match for match in confirmed}
    rows: list[dict] = []
    for event in local:
        item = event.input
        direction = "deposit" if item.kind.endswith("_IN") else "withdrawal"
        asset = item.asset or item.quote_currency
        amount = item.quantity if item.asset else item.amount
        in_window = first - WINDOW <= event.occurred_at <= last + WINDOW
        complete_window = event.occurred_at - WINDOW >= first and event.occurred_at + WINDOW <= last
        candidates = []
        non_final_matches = 0
        non_external_matches = 0
        if in_window:
            # Para un retiro con tasa en el activo, el libro guarda débito
            # bruto y recepción neta. MEXC no define aquí si `amount` es uno u
            # otro; ambas igualdades son hipótesis, nunca vínculo automático.
            search_amounts = [amount]
            fee_in_asset = (direction == "withdrawal" and item.asset is not None
                            and item.fee > 0 and item.fee_asset == asset)
            if fee_in_asset:
                with localcontext() as context:
                    context.prec = CALC_PRECISION
                    search_amounts.append(amount - item.fee)
            for search_amount in search_amounts:
                times, entries = indexed.get((direction, asset, search_amount), ([], []))
                lower = bisect_left(times, event.occurred_at - WINDOW)
                upper = bisect_right(times, event.occurred_at + WINDOW)
                for offset in range(lower, upper):
                    index, observed = entries[offset]
                    observed_fee = Decimal(observed["fee"])
                    if observed_fee != item.fee or observed_fee > 0 and not fee_in_asset:
                        continue
                    if not _is_final(direction, observed["status"]):
                        non_final_matches += 1
                        continue
                    if direction == "withdrawal" and observed.get("transfer_type") != 0:
                        non_external_matches += 1
                        continue
                    hypothesis = ("AMOUNT_IS_GROSS" if search_amount == amount else
                                  "AMOUNT_IS_NET") if observed_fee > 0 else None
                    candidates.append({"row_index": index, "observed_at": times[offset].isoformat(),
                                       "amount": observed["amount"], "fee": observed["fee"],
                                       "fee_hypothesis": hypothesis,
                                       "status_code": observed["status"],
                                       "network": observed["network"],
                                       "source_ref": observed.get("source_ref")})
        rows.append({"request_id": event.request_id, "local_event_id": event.id,
                     "kind": item.kind, "direction": direction, "asset": asset,
                     "amount": format(amount, "f"), "occurred_at": event.occurred_at.isoformat(),
                     "result": ("AMBIGUOUS" if len(candidates) > 1 else
                                "FEE_HYPOTHESIS_ONLY" if candidates and candidates[0]["fee_hypothesis"] else
                                "POSSIBLE_MATCH" if candidates else
                                "OUTSIDE_COVERAGE" if not in_window else
                                "NON_FINAL_CANDIDATE_ONLY" if non_final_matches else
                                "NON_EXTERNAL_WITHDRAWAL_ONLY" if non_external_matches else
                                "PARTIAL_WINDOW_NO_CANDIDATE" if not complete_window else
                                "NO_EXACT_CANDIDATE"),
                     "candidates": candidates,
                     "confirmed_match_id": by_event[event.id].id if event.id in by_event else None})
    # Una misma fila observada puede parecer corresponder a dos asientos
    # locales. Ninguno debe presentarse como coincidencia singular.
    references: dict[tuple[str, int], list[dict]] = {}
    for row in rows:
        for candidate in row["candidates"]:
            references.setdefault((row["direction"], candidate["row_index"]), []).append(row)
    for matches in references.values():
        if len(matches) > 1:
            for row in matches:
                row["result"] = "AMBIGUOUS"
    observed_rows: list[dict] = []
    for direction, field in (("deposit", "deposits"), ("withdrawal", "withdrawals")):
        for index, observed in enumerate(snapshot[field]):
            matches = references.get((direction, index), [])
            observed_rows.append({
                "direction": direction, "row_index": index,
                "asset": observed["asset"], "amount": observed["amount"],
                "observed_at": datetime.fromtimestamp(observed["time_ms"] / 1000, timezone.utc).isoformat(),
                "status_code": observed["status"], "network": observed["network"],
                "source_ref": observed.get("source_ref"),
                "confirmed_match_id": (by_source[(direction, observed["source_ref"])].id
                                       if observed.get("source_ref") is not None
                                       and (direction, observed["source_ref"]) in by_source else None),
                "transfer_type": observed.get("transfer_type") if direction == "withdrawal" else None,
                "result": ("NOT_FINAL_NO_COMPARISON" if not _is_final(direction, observed["status"]) else
                           "INTERNAL_MEXC_NOT_WALLET" if direction == "withdrawal" and observed.get("transfer_type") == 1 else
                           "ROUTE_UNKNOWN_NO_COMPARISON" if direction == "withdrawal" and observed.get("transfer_type") != 0 else
                           "NO_PAIRED_LOCAL_CANDIDATE" if not matches else
                           "FEE_HYPOTHESIS_ONLY" if len(matches) == 1 and
                           matches[0]["result"] == "FEE_HYPOTHESIS_ONLY" else
                           "POSSIBLE_MATCH" if len(matches) == 1 and len(matches[0]["candidates"]) == 1 else
                           "AMBIGUOUS"),
                "candidate_event_ids": [row["local_event_id"] for row in matches],
            })
    return {"snapshot_id": snapshot["id"], "account_id": account_id,
            "account_reference": reference_status,
            "identity_verified": snapshot["identity_verified"],
            "conclusion": "CANDIDATES_ONLY_NOT_RECONCILED",
            "coverage": snapshot["coverage"], "rows": rows, "observed_rows": observed_rows}
