"""Prueba EIP-191 efímera de control EOA; no autoriza transacciones."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from uuid import UUID

from eth_account import Account
from eth_account.messages import encode_defunct


_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
_CHAIN = re.compile(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]{0,15})\Z")
_SIGNATURE = re.compile(r"0x[0-9a-fA-F]{130}\Z")
_NONCE = re.compile(r"[0-9a-f]{64}\Z")


class WalletProofError(ValueError):
    """Prueba de control inválida; nunca incorpora datos privados al mensaje."""


def canonical_wallet_address(address: str) -> str:
    if not isinstance(address, str) or not _ADDRESS.fullmatch(address):
        raise WalletProofError("Dirección EVM inválida")
    return address.lower()


def canonical_chain(chain_id: str) -> str:
    if not isinstance(chain_id, str) or not _CHAIN.fullmatch(chain_id):
        raise WalletProofError("Red EVM inválida")
    return hex(int(chain_id, 16))


@dataclass(frozen=True, slots=True)
class WalletChallenge:
    nonce: str
    account_id: str
    chain_id: str
    address_digest: str
    origin: str
    expires_at: datetime
    session_generation: int


def challenge_message(*, origin: str, account_id: str, chain_id: str,
                      address: str, nonce: str, expires_at: datetime) -> str:
    try:
        UUID(account_id)
    except (TypeError, ValueError) as exc:
        raise WalletProofError("Cuenta REAL inválida") from exc
    chain_id = canonical_chain(chain_id)
    address = canonical_wallet_address(address)
    if (not isinstance(origin, str) or not re.fullmatch(
            r"http://(?:127\.0\.0\.1|\[::1\]|localhost|testserver|testclient)(?::[0-9]{1,5})?", origin)
            or not isinstance(nonce, str) or not _NONCE.fullmatch(nonce)
            or not isinstance(expires_at, datetime) or expires_at.tzinfo is None):
        raise WalletProofError("Desafío de wallet inválido")
    expiry = expires_at.astimezone(timezone.utc).isoformat(timespec="seconds")
    return ("ORBIS · Verificar control de wallet EOA\n"
            "Solo comprueba control de esta wallet para una cuenta REAL durante la sesión desbloqueada.\n"
            "NO autoriza pagos, aprobaciones, retiros ni operativa automática.\n"
            f"Origen local: {origin}\n"
            f"Cuenta REAL: {account_id}\n"
            f"Red EVM: {chain_id}\n"
            f"Dirección: {address}\n"
            f"Nonce: {nonce}\n"
            f"Caduca: {expiry}")


def verify_eoa_signature(message: str, address: str, signature: str) -> bool:
    if not isinstance(signature, str) or not _SIGNATURE.fullmatch(signature):
        raise WalletProofError("Firma EOA inválida")
    try:
        recovered = Account.recover_message(encode_defunct(text=message),
                                            signature=signature)
    except Exception:
        raise WalletProofError("Firma EOA inválida") from None
    return recovered.lower() == canonical_wallet_address(address)
