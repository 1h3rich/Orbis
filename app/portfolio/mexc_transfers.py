"""Instantáneas MEXC cifradas, saneadas y separadas del libro REAL."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal
import hashlib
import hmac
import json
import re
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.live_execution.mexc_uid_guard import MexcTransferRecoveryView
from app.portfolio.domain import PortfolioError, utc_datetime
from app.portfolio.models import (
    MexcTransferIdentityEvidence, MexcTransferSnapshot,
    RealAccount, RealMexcAccountLink, RealMexcIdentitySalt, RealMexcTransferMatch, RealMexcUidLink,
    RealWalletIdentitySalt,
)
from app.portfolio.wallet_links import _salt


_REF = re.compile(r"^[0-9a-f]{64}$")
_EVM_TX_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
BNB_NETWORKS = frozenset(("BSC", "BEP20", "BEP20(BSC)"))
EVM_NETWORKS = frozenset(("ETH", "ERC20")) | BNB_NETWORKS


def destination_message(account_ref: str, direction: str, network: str, address: str) -> bytes:
    # Conservar las huellas ETH ya archivadas; BSC tiene dominio de cadena propio.
    prefix = f"mexc-{direction}-destination-v1"
    if network in BNB_NETWORKS:
        prefix = f"mexc-{direction}-destination-v2:0x38"
    return f"{prefix}:{account_ref}:{address.lower()}".encode("ascii")
_ASSET = re.compile(r"^[A-Z0-9]{2,24}$")
_NETWORK = re.compile(r"^[A-Za-z0-9()._-]{1,40}$")
_ROW_KEYS = frozenset(("direction", "asset", "network", "amount", "fee", "status", "time_ms"))
_OPTIONAL_KEYS = {
    "deposit": frozenset(("source_ref", "destination_ref", "token_contract")),
    "withdrawal": frozenset(("source_ref", "destination_ref", "chain_ref",
                             "transfer_type", "token_contract")),
}
_SNAPSHOT_KEYS = frozenset(("source", "accounting", "coverage", "start_ms", "end_ms",
                            "observed_at", "deposits", "withdrawals"))


def _decimal(raw: object, *, positive: bool) -> str:
    if not isinstance(raw, str) or len(raw) > 100:
        raise PortfolioError("Transfer amount must be decimal text")
    try:
        value = Decimal(raw)
    except Exception as exc:
        raise PortfolioError("Transfer amount is invalid") from exc
    if (not value.is_finite() or len(value.as_tuple().digits) > 40
            or abs(value.as_tuple().exponent) > 100
            or value < 0 or positive and value == 0):
        raise PortfolioError("Transfer amount is invalid")
    return format(value, "f")


def _validate(account_ref: str, snapshot: dict) -> tuple[datetime, int, int, list[dict], list[dict], str]:
    if not isinstance(account_ref, str) or not _REF.fullmatch(account_ref):
        raise PortfolioError("Invalid MEXC account reference")
    if not isinstance(snapshot, dict) or set(snapshot) not in (_SNAPSHOT_KEYS, _SNAPSHOT_KEYS | {"requests"}):
        raise PortfolioError("MEXC transfer snapshot has unexpected fields")
    if (snapshot["source"] != "MEXC_SPOT_PRIVATE_READONLY"
            or snapshot["accounting"] != "OBSERVED_NOT_IMPORTED"
            or snapshot["coverage"] != "RECENT_WINDOW_BELOW_PAGE_LIMIT"):
        raise PortfolioError("MEXC transfer snapshot source is invalid")
    first, last = snapshot["start_ms"], snapshot["end_ms"]
    if (type(first) is not int or type(last) is not int or not 0 < first < last
            or last - first > 90 * 86_400_000):
        raise PortfolioError("MEXC transfer window is invalid")
    requests = snapshot.get("requests", 2)
    if type(requests) is not int or not 2 <= requests <= 128:
        raise PortfolioError("MEXC transfer request count is invalid")
    try:
        observed_at = utc_datetime(datetime.fromisoformat(snapshot["observed_at"]))
    except (TypeError, ValueError, PortfolioError) as exc:
        raise PortfolioError("MEXC transfer observation time is invalid") from exc
    parsed = {}
    for name, direction in (("deposits", "deposit"), ("withdrawals", "withdrawal")):
        rows = snapshot[name]
        if (not isinstance(rows, list) or len(rows) > 10000
                or requests == 2 and len(rows) >= 1000):
            raise PortfolioError("MEXC transfer page is incomplete or invalid")
        cleaned = []
        for row in rows:
            if (not isinstance(row, dict)
                    or not _ROW_KEYS <= set(row)
                    or set(row) - _ROW_KEYS - _OPTIONAL_KEYS[direction]
                    or row["direction"] != direction):
                raise PortfolioError("MEXC transfer row has unexpected fields")
            asset, network, status, stamp = (row[key] for key in ("asset", "network", "status", "time_ms"))
            if (not isinstance(asset, str) or not _ASSET.fullmatch(asset)
                    or network is not None and (not isinstance(network, str) or not _NETWORK.fullmatch(network))
                    or type(status) is not int or not 0 <= status <= 20
                    or type(stamp) is not int or not first <= stamp <= last):
                raise PortfolioError("MEXC transfer row is invalid")
            clean = {"direction": direction, "asset": asset, "network": network,
                     "amount": _decimal(row["amount"], positive=True),
                     "fee": _decimal(row["fee"], positive=False),
                     "status": status, "time_ms": stamp}
            if "transfer_type" in row:
                route = row["transfer_type"]
                if route is not None and (type(route) is not int or route not in (0, 1)):
                    raise PortfolioError("MEXC withdrawal route is invalid")
                clean["transfer_type"] = route
            if "source_ref" in row:
                reference = row["source_ref"]
                if not isinstance(reference, str) or not _REF.fullmatch(reference):
                    raise PortfolioError("MEXC transfer reference is invalid")
                clean["source_ref"] = reference
            if "destination_ref" in row:
                reference = row["destination_ref"]
                if not isinstance(reference, str) or not _REF.fullmatch(reference):
                    raise PortfolioError("MEXC transfer destination reference is invalid")
                clean["destination_ref"] = reference
            if "chain_ref" in row:
                reference = row["chain_ref"]
                if not isinstance(reference, str) or not _REF.fullmatch(reference):
                    raise PortfolioError("MEXC withdrawal chain reference is invalid")
                clean["chain_ref"] = reference
            if "token_contract" in row:
                contract = row["token_contract"]
                if (network not in EVM_NETWORKS or not isinstance(contract, str)
                        or not _EVM_ADDRESS.fullmatch(contract)
                        or int(contract[2:], 16) == 0):
                    raise PortfolioError("MEXC token contract is invalid")
                clean["token_contract"] = contract.lower()
            cleaned.append(clean)
        parsed[name] = cleaned
    body = {"deposits": parsed["deposits"], "withdrawals": parsed["withdrawals"]}
    if "requests" in snapshot:
        body["requests"] = requests
    payload = json.dumps(body,
                         ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return observed_at, first, last, parsed["deposits"], parsed["withdrawals"], payload


def _verified_uid_digest(db: Session, account_ref: str, uid: str | None) -> str | None:
    if uid is None:
        return None
    if not isinstance(uid, str) or not re.fullmatch(r"[1-9][0-9]{0,31}", uid):
        raise PortfolioError("MEXC View UID is invalid")
    link = db.get(RealMexcUidLink, account_ref)
    if link is None:
        return None
    salt = db.get(RealMexcIdentitySalt, 1)
    if salt is None or not _REF.fullmatch(salt.salt_hex):
        raise PortfolioError("MEXC View identity salt is missing or invalid")
    digest = hmac.new(bytes.fromhex(salt.salt_hex), uid.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(digest, link.uid_digest):
        raise PortfolioError("MEXC View UID differs from the linked account")
    return digest


def verify_transfer_identity_evidence(db: Session, row: MexcTransferIdentityEvidence) -> None:
    snapshot = db.get(MexcTransferSnapshot, row.snapshot_id)
    link = db.get(RealMexcUidLink, row.account_ref)
    if (snapshot is None or link is None or snapshot.account_ref != row.account_ref
            or snapshot.observed_at != row.observed_at
            or not _REF.fullmatch(row.uid_digest)
            or not hmac.compare_digest(link.uid_digest, row.uid_digest)):
        raise PortfolioError("MEXC transfer identity evidence is inconsistent")


def save_transfer_snapshot(db: Session, account_ref: str, snapshot: dict,
                           *, view_uid: str | None = None,
                           recovery: MexcTransferRecoveryView | None = None) -> dict:
    """Archiva una consulta completa como fotografía; no fusiona transferencias."""
    identifier = str(uuid4())
    try:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        if recovery is not None:
            if recovery.archive_ref != account_ref or view_uid is None:
                raise PortfolioError("La recuperación no corresponde a la captura MEXC")
            recovery.verify(db, view_uid)
        if not isinstance(snapshot, dict):
            raise PortfolioError("MEXC transfer snapshot is invalid")
        prepared = dict(snapshot)
        for field, direction in (("deposits", "deposit"), ("withdrawals", "withdrawal")):
            rows = snapshot.get(field)
            if not isinstance(rows, list):
                raise PortfolioError("MEXC transfer rows are invalid")
            prepared_rows = []
            for row in rows:
                if (not isinstance(row, dict) or "source_ref" in row
                        or "destination_ref" in row or "chain_ref" in row):
                    raise PortfolioError("MEXC transfer row has unexpected fields")
                clean = dict(row)
                if "source_identity" in clean:
                    source_id = clean.pop("source_identity")
                    if source_id is not None:
                        if (not isinstance(source_id, str) or not 1 <= len(source_id) <= 256
                                or not source_id.isascii() or not source_id.isprintable()):
                            raise PortfolioError("MEXC transfer source identity is invalid")
                        message = f"mexc-transfer-v1:{account_ref}:{direction}:{source_id}".encode("ascii")
                        clean["source_ref"] = hmac.new(_salt(db), message, hashlib.sha256).hexdigest()
                if "destination_identity" in clean:
                    destination = clean.pop("destination_identity")
                    if (clean.get("network") not in EVM_NETWORKS
                            or not isinstance(destination, str)
                            or not _EVM_ADDRESS.fullmatch(destination)):
                        raise PortfolioError("MEXC EVM destination is invalid")
                    message = destination_message(account_ref, direction, clean["network"], destination)
                    clean["destination_ref"] = hmac.new(_salt(db), message, hashlib.sha256).hexdigest()
                if "chain_identity" in clean:
                    chain_hash = clean.pop("chain_identity")
                    if (direction != "withdrawal" or clean.get("network") not in ("ETH", "ERC20")
                            or not isinstance(chain_hash, str) or not _EVM_TX_HASH.fullmatch(chain_hash)):
                        raise PortfolioError("MEXC withdrawal chain identity is invalid")
                    message = f"mexc-chain-tx-v1:{account_ref}:withdrawal:{chain_hash.lower()}".encode("ascii")
                    clean["chain_ref"] = hmac.new(_salt(db), message, hashlib.sha256).hexdigest()
                prepared_rows.append(clean)
            prepared[field] = prepared_rows
        observed_at, first, last, deposits, withdrawals, payload = _validate(account_ref, prepared)
        uid_digest = _verified_uid_digest(db, account_ref, view_uid)
        digest = hashlib.sha256(payload.encode("ascii")).hexdigest()
        db.add(MexcTransferSnapshot(
            id=identifier, account_ref=account_ref, observed_at=observed_at,
            requested_start_ms=first, requested_end_ms=last,
            deposit_count=len(deposits), withdrawal_count=len(withdrawals),
            payload_json=payload, payload_digest=digest,
        ))
        if uid_digest is not None:
            db.flush()
            db.add(MexcTransferIdentityEvidence(
                snapshot_id=identifier, account_ref=account_ref,
                uid_digest=uid_digest, observed_at=observed_at,
            ))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"id": identifier, "deposit_count": len(deposits),
            "withdrawal_count": len(withdrawals), "requests": snapshot.get("requests", 2),
            "coverage": "RECENT_WINDOW_BELOW_PAGE_LIMIT",
            "identity_verified": uid_digest is not None}


def list_transfer_snapshots(db: Session) -> list[dict]:
    rows = db.query(MexcTransferSnapshot).order_by(MexcTransferSnapshot.observed_at.desc(),
                                                     MexcTransferSnapshot.id.desc()).limit(100).all()
    verified = {item.snapshot_id for item in db.query(MexcTransferIdentityEvidence).filter(
        MexcTransferIdentityEvidence.snapshot_id.in_([row.id for row in rows])).all()} if rows else set()
    return [{"id": row.id, "account_ref": row.account_ref, "observed_at": row.observed_at.isoformat(),
             "start_ms": row.requested_start_ms, "end_ms": row.requested_end_ms,
             "deposit_count": row.deposit_count, "withdrawal_count": row.withdrawal_count,
             "coverage": "RECENT_WINDOW_BELOW_PAGE_LIMIT",
             "identity_verified": row.id in verified} for row in rows]


def find_mexc_deposit_by_evm_tx_hash(db: Session, account_id: str,
                                     tx_hash: str,
                                     destinations: list[str] | None = None) -> dict:
    """Cruza un hash EVM transitorio con la huella de depósitos MEXC archivados.

    No consulta la cadena, no guarda el hash y no demuestra origen wallet.
    Una ausencia sólo significa que no está en las capturas verificadas.
    """
    if not isinstance(tx_hash, str) or not _EVM_TX_HASH.fullmatch(tx_hash):
        raise PortfolioError("Hash de transacción EVM inválido")
    if destinations is not None and (not isinstance(destinations, list)
                                     or len(destinations) > 20
                                     or any(not isinstance(item, str) or not _EVM_ADDRESS.fullmatch(item)
                                            for item in destinations)):
        raise PortfolioError("Destinos EVM inválidos")
    if db.get(RealAccount, account_id) is None:
        raise PortfolioError("Cuenta REAL no encontrada")
    links = db.query(RealMexcAccountLink).filter(
        RealMexcAccountLink.account_id == account_id).all()
    if len(links) != 1:
        raise PortfolioError("La cuenta REAL requiere una conexión MEXC única")
    account_ref = links[0].account_ref
    salt = db.get(RealWalletIdentitySalt, 1)
    if salt is None or not _REF.fullmatch(salt.salt_hex):
        raise PortfolioError("Huella local de transferencias MEXC no disponible")
    key = bytes.fromhex(salt.salt_hex)
    variants = {tx_hash, tx_hash.lower(), "0x" + tx_hash[2:].upper(),
                tx_hash[2:], tx_hash[2:].lower(), tx_hash[2:].upper()}
    references = {hmac.new(key, f"mexc-transfer-v1:{account_ref}:deposit:{value}".encode("ascii"),
                           hashlib.sha256).hexdigest() for value in variants}
    archive = db.query(MexcTransferSnapshot).filter(
        MexcTransferSnapshot.account_ref == account_ref).order_by(
            MexcTransferSnapshot.observed_at, MexcTransferSnapshot.id,
        ).limit(501).all()
    if len(archive) > 500:
        raise PortfolioError("El archivo MEXC supera el límite de búsqueda")
    found: list[tuple[datetime, str, dict]] = []
    verified = 0
    for item in archive:
        snapshot = read_transfer_snapshot(db, item.id)
        if not snapshot["identity_verified"]:
            continue
        verified += 1
        for row in snapshot["deposits"]:
            if row.get("source_ref") in references:
                found.append((datetime.fromisoformat(snapshot["observed_at"]), item.id, row))
    base = {"account_id": account_id, "verified_snapshots": verified,
            "chain_receipt_verified": False, "wallet_ownership_verified": False,
            "real_book_link_checked": True, "book_link_status": "NO_MATCHING_DEPOSIT",
            "ready_for_execution": False,
            "conclusion": "MEXC_ARCHIVE_TXID_MATCH_ONLY_NOT_RECONCILED"}
    if not found:
        return {**base, "status": "NOT_FOUND_IN_VERIFIED_ARCHIVE"}
    found.sort(key=lambda item: (item[0], item[1]))
    refs = {row.get("source_ref") for _, _, row in found}
    facts = {(row["asset"], row["network"], row["amount"], row["fee"], row["time_ms"])
             for _, _, row in found}
    known_destinations = {row["destination_ref"] for _, _, row in found if row.get("destination_ref")}
    known_contracts = {row["token_contract"] for _, _, row in found if row.get("token_contract")}
    seen_snapshot: set[str] = set()
    conflicted = (len(refs) != 1 or len(facts) != 1 or len(known_destinations) > 1
                  or len(known_contracts) > 1)
    final_seen = False
    previous_time = previous_status = None
    for observed_at, snapshot_id, row in found:
        if snapshot_id in seen_snapshot or (observed_at == previous_time
                                            and row["status"] != previous_status):
            conflicted = True
        seen_snapshot.add(snapshot_id)
        if row["status"] == 5:
            final_seen = True
        elif final_seen:
            conflicted = True
        previous_time, previous_status = observed_at, row["status"]
    _, snapshot_id, latest = found[-1]
    status = ("CONTRADICTORY_MEXC_DEPOSIT" if conflicted else
              "FINAL_MEXC_DEPOSIT" if latest["status"] == 5 else
              "NOT_FINAL_MEXC_DEPOSIT")
    matches = db.query(RealMexcTransferMatch).filter(
        RealMexcTransferMatch.account_ref == account_ref,
        RealMexcTransferMatch.account_id == account_id,
        RealMexcTransferMatch.direction == "deposit",
        RealMexcTransferMatch.source_ref.in_(refs),
    ).all()
    book_link_status = "NO_EXPLICIT_LINK"
    if matches:
        book_link_status = "INCONSISTENT_LINK"
        if len(matches) == 1 and not conflicted and status == "FINAL_MEXC_DEPOSIT":
            origin = read_transfer_snapshot(db, matches[0].snapshot_id)
            if not origin["identity_verified"]:
                book_link_status = "LEGACY_LINK_UNVERIFIED_IDENTITY"
            else:
                # Importación local para no crear un ciclo con el verificador.
                from app.portfolio.mexc_transfer_matches import verify_mexc_transfer_match
                try:
                    verify_mexc_transfer_match(db, matches[0], check_history=True)
                except PortfolioError:
                    pass
                else:
                    book_link_status = "VERIFIED_EXPLICIT_LINK"
    destination_ref = next(iter(known_destinations), None)
    token_contract = next(iter(known_contracts), None)
    return {**base, "status": status, "book_link_status": book_link_status,
            "asset": latest["asset"],
            "network": latest["network"], "amount": latest["amount"],
            "token_contract": token_contract,
            "destination_evidence_available": destination_ref is not None,
            "matched_destination_indices": [index for index, destination in enumerate(destinations or [])
                if destination_ref is not None and hmac.compare_digest(
                    destination_ref, hmac.new(key,
                        destination_message(account_ref,"deposit",latest["network"],destination),
                        hashlib.sha256).hexdigest())],
            "mexc_status_code": latest["status"], "time_ms": latest["time_ms"],
            "source_ref": latest["source_ref"], "snapshot_id": snapshot_id,
            "observations": len(found)}


def find_mexc_withdrawal_by_evm_tx_hash(db: Session, account_id: str,
                                        tx_hash: str, recipient: str | None = None) -> dict:
    """Busca un retiro por huella de transacción EVM, sin inferir importe neto.

    Sólo usa capturas con UID View verificado. No consulta la cadena ni crea
    asientos; `transactionFee` de MEXC no define por sí solo bruto/neto.
    """
    if not isinstance(tx_hash, str) or not _EVM_TX_HASH.fullmatch(tx_hash):
        raise PortfolioError("Hash de transacción EVM inválido")
    if recipient is not None and (not isinstance(recipient, str)
                                  or not _EVM_ADDRESS.fullmatch(recipient)):
        raise PortfolioError("Destino EVM inválido")
    if db.get(RealAccount, account_id) is None:
        raise PortfolioError("Cuenta REAL no encontrada")
    links = db.query(RealMexcAccountLink).filter(RealMexcAccountLink.account_id == account_id).all()
    if len(links) != 1:
        raise PortfolioError("La cuenta REAL requiere una conexión MEXC única")
    account_ref = links[0].account_ref
    salt = db.get(RealWalletIdentitySalt, 1)
    if salt is None or not _REF.fullmatch(salt.salt_hex):
        raise PortfolioError("Huella local de transferencias MEXC no disponible")
    key = bytes.fromhex(salt.salt_hex)
    chain_ref = hmac.new(key,
        f"mexc-chain-tx-v1:{account_ref}:withdrawal:{tx_hash.lower()}".encode("ascii"),
        hashlib.sha256).hexdigest()
    destination_ref = (hmac.new(key,
        f"mexc-withdrawal-destination-v1:{account_ref}:{recipient.lower()}".encode("ascii"),
        hashlib.sha256).hexdigest() if recipient is not None else None)
    archive = db.query(MexcTransferSnapshot).filter(
        MexcTransferSnapshot.account_ref == account_ref).order_by(
            MexcTransferSnapshot.observed_at, MexcTransferSnapshot.id).limit(501).all()
    if len(archive) > 500:
        raise PortfolioError("El archivo MEXC supera el límite de búsqueda")
    found: list[tuple[datetime, str, dict]] = []
    by_source: dict[str, list[dict]] = defaultdict(list)
    verified = 0
    for item in archive:
        snapshot = read_transfer_snapshot(db, item.id)
        if not snapshot["identity_verified"]:
            continue
        verified += 1
        for row in snapshot["withdrawals"]:
            if row.get("source_ref"):
                by_source[row["source_ref"]].append(row)
            if row.get("chain_ref") == chain_ref:
                found.append((datetime.fromisoformat(snapshot["observed_at"]), item.id, row))
    base = {"account_id": account_id, "verified_snapshots": verified,
            "chain_receipt_verified": False, "wallet_ownership_verified": False,
            "real_book_link_checked": False, "ready_for_execution": False,
            "amount_semantics": "MEXC_GROSS_NET_UNVERIFIED",
            "conclusion": "MEXC_WITHDRAWAL_TX_MATCH_ONLY_NOT_RECONCILED"}
    if not found:
        return {**base, "status": "NOT_FOUND_IN_VERIFIED_ARCHIVE"}
    found.sort(key=lambda value: (value[0], value[1]))
    refs = {row.get("source_ref") for _, _, row in found}
    conflicted = len(refs) != 1 or None in refs
    history = by_source.get(next(iter(refs)), []) if len(refs) == 1 and None not in refs else []
    facts = {(row["asset"], row["network"], row["amount"], row["fee"],
              row["time_ms"], row.get("transfer_type")) for row in history}
    known_chain = {row["chain_ref"] for row in history if row.get("chain_ref")}
    known_destinations = {row["destination_ref"] for row in history if row.get("destination_ref")}
    known_contracts = {row["token_contract"] for row in history if row.get("token_contract")}
    if (len(facts) != 1 or len(known_chain) != 1 or len(known_destinations) > 1
            or len(known_contracts) > 1):
        conflicted = True
    final_seen = False
    previous_time = previous_status = None
    seen_snapshots: set[str] = set()
    for observed_at, snapshot_id, row in found:
        if snapshot_id in seen_snapshots or observed_at == previous_time and row["status"] != previous_status:
            conflicted = True
        seen_snapshots.add(snapshot_id)
        if row["status"] == 7:
            final_seen = True
        elif final_seen:
            conflicted = True
        previous_time, previous_status = observed_at, row["status"]
    latest = found[-1][2]
    status = ("CONTRADICTORY_MEXC_WITHDRAWAL" if conflicted else
              "NON_EXTERNAL_MEXC_WITHDRAWAL" if latest.get("transfer_type") != 0 else
              "FINAL_MEXC_WITHDRAWAL" if latest["status"] == 7 else
              "NOT_FINAL_MEXC_WITHDRAWAL")
    archived_destination = next(iter(known_destinations), None)
    token_contract = next(iter(known_contracts), None)
    return {**base, "status": status, "asset": latest["asset"],
            "network": latest["network"], "amount": latest["amount"],
            "fee": latest["fee"], "token_contract": token_contract,
            "mexc_status_code": latest["status"],
            "time_ms": latest["time_ms"], "observations": len(found),
            "destination_evidence_available": archived_destination is not None,
            "recipient_matches_archive": (archived_destination is not None
                and destination_ref is not None
                and hmac.compare_digest(archived_destination, destination_ref))}


def read_transfer_snapshot(db: Session, identifier: str) -> dict:
    try:
        if not isinstance(identifier, str):
            raise ValueError("not a string")
        identifier = str(UUID(identifier))
    except (TypeError, ValueError) as exc:
        raise PortfolioError("Invalid MEXC transfer snapshot id") from exc
    row = db.get(MexcTransferSnapshot, identifier)
    if row is None:
        raise PortfolioError("MEXC transfer snapshot not found")
    if hashlib.sha256(row.payload_json.encode("ascii")).hexdigest() != row.payload_digest:
        raise PortfolioError("MEXC transfer snapshot integrity check failed")
    try:
        payload = json.loads(row.payload_json)
        candidate = {"source": "MEXC_SPOT_PRIVATE_READONLY", "accounting": "OBSERVED_NOT_IMPORTED",
                     "coverage": "RECENT_WINDOW_BELOW_PAGE_LIMIT", "start_ms": row.requested_start_ms,
                     "end_ms": row.requested_end_ms, "observed_at": row.observed_at.isoformat(),
                     **payload}
        _, _, _, deposits, withdrawals, _ = _validate(row.account_ref, candidate)
    except (ValueError, TypeError, PortfolioError) as exc:
        raise PortfolioError("MEXC transfer snapshot is corrupt") from exc
    if len(deposits) != row.deposit_count or len(withdrawals) != row.withdrawal_count:
        raise PortfolioError("MEXC transfer snapshot count mismatch")
    # Las preimágenes v23 poseen vínculos, pero aún no la tabla de procedencia.
    has_identity_table = db.connection().exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mexc_transfer_identity_evidence'"
    ).first() is not None
    identity = db.get(MexcTransferIdentityEvidence, identifier) if has_identity_table else None
    if identity is not None:
        verify_transfer_identity_evidence(db, identity)
    return {"id": row.id, "account_ref": row.account_ref,
            "observed_at": row.observed_at.isoformat(), "start_ms": row.requested_start_ms,
            "end_ms": row.requested_end_ms, "coverage": "RECENT_WINDOW_BELOW_PAGE_LIMIT",
            "accounting": "OBSERVED_NOT_IMPORTED", "requests": payload.get("requests", 2),
            "identity_verified": identity is not None,
            "deposits": deposits, "withdrawals": withdrawals}


def compare_transfer_snapshots(db: Session, before_id: str, after_id: str) -> dict:
    """Compara referencias observadas, nunca acredita un traslado ni crea asientos."""
    before = read_transfer_snapshot(db, before_id)
    after = read_transfer_snapshot(db, after_id)
    if before["id"] == after["id"]:
        raise PortfolioError("Choose two different MEXC transfer snapshots")
    if before["account_ref"] != after["account_ref"]:
        raise PortfolioError("MEXC transfer snapshots belong to different local connections")
    if datetime.fromisoformat(before["observed_at"]) >= datetime.fromisoformat(after["observed_at"]):
        raise PortfolioError("MEXC transfer snapshots are not in observation order")
    first = max(before["start_ms"], after["start_ms"])
    last = min(before["end_ms"], after["end_ms"])
    if first > last:
        raise PortfolioError("MEXC transfer snapshots do not overlap")

    def collect(snapshot: dict) -> tuple[dict[tuple[str, str], list[dict]], int]:
        indexed: dict[tuple[str, str], list[dict]] = defaultdict(list)
        unlinked = 0
        for field in ("deposits", "withdrawals"):
            for row in snapshot[field]:
                if not first <= row["time_ms"] <= last:
                    continue
                reference = row.get("source_ref")
                if reference is None:
                    unlinked += 1
                else:
                    indexed[(row["direction"], reference)].append(row)
        return indexed, unlinked

    old, old_unlinked = collect(before)
    new, new_unlinked = collect(after)
    counts = {key: 0 for key in (
        "unchanged", "status_changed", "finality_regression", "fields_changed",
        "not_returned", "newly_returned", "ambiguous_reference")}
    changes: list[dict] = []
    for direction, reference in sorted(old.keys() | new.keys()):
        earlier = old.get((direction, reference), [])
        later = new.get((direction, reference), [])
        if len(earlier) > 1 or len(later) > 1:
            outcome = "ambiguous_reference"
        elif not earlier:
            outcome = "newly_returned"
        elif not later:
            outcome = "not_returned"
        else:
            a, b = earlier[0], later[0]
            fields = ("asset", "network", "amount", "fee", "time_ms", "transfer_type")
            destination_changed = (a.get("destination_ref") and b.get("destination_ref")
                                   and a["destination_ref"] != b["destination_ref"])
            contract_changed = (a.get("token_contract") and b.get("token_contract")
                                and a["token_contract"] != b["token_contract"])
            chain_changed = (a.get("chain_ref") and b.get("chain_ref")
                             and a["chain_ref"] != b["chain_ref"])
            if (any(a.get(field) != b.get(field) for field in fields)
                    or destination_changed or contract_changed or chain_changed):
                outcome = "fields_changed"
            elif a["status"] == b["status"]:
                outcome = "unchanged"
            elif (direction == "deposit" and a["status"] == 5 and b["status"] != 5
                  or direction == "withdrawal" and a["status"] == 7 and b["status"] != 7):
                outcome = "finality_regression"
            else:
                outcome = "status_changed"
        counts[outcome] += 1
        if outcome != "unchanged" and len(changes) < 200:
            changes.append({"direction": direction, "source_ref": reference,
                            "outcome": outcome, "before": earlier[0] if len(earlier) == 1 else None,
                            "after": later[0] if len(later) == 1 else None,
                            "before_count": len(earlier), "after_count": len(later)})
    total_changes = sum(value for key, value in counts.items() if key != "unchanged")
    return {"before_id": before["id"], "after_id": after["id"],
            "account_ref": before["account_ref"], "overlap_start_ms": first,
            "overlap_end_ms": last, "counts": counts,
            "unlinked_before": old_unlinked, "unlinked_after": new_unlinked,
            "changes": changes, "truncated": total_changes > len(changes),
            "conclusion": "OBSERVATIONAL_NOT_RECONCILED"}
