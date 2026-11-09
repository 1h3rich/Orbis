"""Impide que otra clave duplique una identidad local para el mismo UID.

El inventario inicial reclama el UID observado por View; una reserva REAL
comprueba además que Trade y View coinciden. Ninguno prueba titularidad ni
conciliación. El UID no se almacena ni se devuelve: una sal aleatoria de la
bóveda protege su huella. Una rotación requiere recuperación explícita.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import re
import secrets
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.portfolio.models import (
    RealLiveSpotIntent, RealLiveStop, RealMexcAccountLink, RealMexcIdentitySalt, RealMexcUidLink, RealMexcViewBinding,
)
if TYPE_CHECKING:
    from app.portfolio.vault import RealVault


class MexcUidGuardError(ValueError):
    """La identidad MEXC no permite una reserva nueva."""


def inspect_mexc_view_identity(vault: RealVault, account_ref: str, uid: str) -> dict:
    """Diagnóstico sin escritura ni UID/referencias antiguas en la respuesta."""
    if not isinstance(account_ref, str) or not re.fullmatch(r"[0-9a-f]{64}", account_ref):
        raise MexcUidGuardError("Referencia MEXC inválida")
    if not isinstance(uid, str) or not re.fullmatch(r"[1-9][0-9]{0,31}", uid):
        raise MexcUidGuardError("UID MEXC inválido")
    with vault.session() as db:
        salt = db.get(RealMexcIdentitySalt, 1)
        if salt is None:
            return {"local_identity_status": "NOT_RECORDED", "local_stopped": False,
                    "unresolved_intents": 0, "trade_uid_matched": False}
        if not re.fullmatch(r"[0-9a-f]{64}", salt.salt_hex):
            raise MexcUidGuardError("Sal de identidad MEXC corrupta")
        digest = hmac.new(bytes.fromhex(salt.salt_hex), uid.encode("ascii"),
                          hashlib.sha256).hexdigest()
        own = db.get(RealMexcUidLink, account_ref)
        if own is not None and not hmac.compare_digest(own.uid_digest, digest):
            status, refs = "KEY_UID_CHANGED", [account_ref]
        else:
            matches = db.scalars(select(RealMexcUidLink).where(
                RealMexcUidLink.uid_digest == digest).limit(2)).all()
            if not matches:
                status, refs = "NOT_RECORDED", []
            elif len(matches) > 1:
                status, refs = "AMBIGUOUS_LOCAL_IDENTITY", []
            else:
                refs = [matches[0].account_ref]
                status = "SAME_KEY" if refs[0] == account_ref else "ROTATED_KEY_MATCH"
        if not refs:
            return {"local_identity_status": status, "local_stopped": False,
                    "unresolved_intents": 0, "trade_uid_matched": False}
        ref = refs[0]
        stop = db.get(RealLiveStop, ref)
        unresolved = db.query(RealLiveSpotIntent).filter(
            RealLiveSpotIntent.account_ref == ref,
            RealLiveSpotIntent.status.in_(("RESERVED", "SUBMISSION_UNKNOWN")),
        ).count()
        return {"local_identity_status": status,
                "local_stopped": bool(stop is not None and stop.stopped),
                "unresolved_intents": unresolved, "trade_uid_matched": False}


def assert_known_mexc_uid(vault: RealVault, account_ref: str, uid: str) -> bool:
    """Comprueba una huella v16 existente; ausencia legacy no inventa vínculo.

    Devuelve False si la intención es anterior al registro v16. El llamador
    debe mantener las demás comprobaciones de clave View, Trade y orden.
    """
    if not isinstance(account_ref, str) or not re.fullmatch(r"[0-9a-f]{64}", account_ref):
        raise MexcUidGuardError("Referencia MEXC inválida")
    if not isinstance(uid, str) or not re.fullmatch(r"[1-9][0-9]{0,31}", uid):
        raise MexcUidGuardError("UID MEXC inválido")
    with vault.session() as db:
        link = db.get(RealMexcUidLink, account_ref)
        if link is None:
            return False
        salt = db.get(RealMexcIdentitySalt, 1)
        if salt is None or not re.fullmatch(r"[0-9a-f]{64}", salt.salt_hex):
            raise MexcUidGuardError("Sal de identidad MEXC ausente o corrupta")
        digest = hmac.new(bytes.fromhex(salt.salt_hex), uid.encode("ascii"),
                          hashlib.sha256).hexdigest()
        if not hmac.compare_digest(link.uid_digest, digest):
            raise MexcUidGuardError("El UID MEXC actual no coincide con el envío archivado")
        return True



def assert_rotated_mexc_uid(db: Session, archive_ref: str, view_ref: str, uid: str) -> None:
    """Verify a replacement credential against the original UID without binding it."""
    message = "La clave View renovada no corresponde inequívocamente a la cuenta MEXC archivada"
    if (view_ref == archive_ref or not isinstance(view_ref, str)
            or not re.fullmatch(r"[0-9a-f]{64}", view_ref)
            or not isinstance(uid, str) or not re.fullmatch(r"[1-9][0-9]{0,31}", uid)):
        raise MexcUidGuardError(message)
    salt = db.get(RealMexcIdentitySalt, 1)
    if (salt is None or not re.fullmatch(r"[0-9a-f]{64}", salt.salt_hex)
            or db.get(RealMexcUidLink, view_ref) is not None
            or db.get(RealMexcAccountLink, view_ref) is not None):
        raise MexcUidGuardError(message)
    digest = hmac.new(bytes.fromhex(salt.salt_hex), uid.encode("ascii"), hashlib.sha256).hexdigest()
    matching_refs = db.scalars(select(RealMexcUidLink.account_ref).where(
        RealMexcUidLink.uid_digest == digest,
    ).limit(2)).all()
    if matching_refs != [archive_ref]:
        raise MexcUidGuardError(message)


@dataclass(frozen=True, slots=True)
class MexcTransferRecoveryView:
    """Scope a View read to a transfer's original book, without binding the new key."""

    account_id: str
    archive_ref: str
    credential_ref: str

    def verify(self, db: Session, uid: str) -> None:
        if self.credential_ref == self.archive_ref:
            return
        link = db.get(RealMexcAccountLink, self.archive_ref)
        if link is None or link.account_id != self.account_id:
            raise MexcUidGuardError("El movimiento perdió su vínculo con la cuenta MEXC original")
        assert_rotated_mexc_uid(db, self.archive_ref, self.credential_ref, uid)



def verify_mexc_view_binding(db: Session, binding: RealMexcViewBinding) -> str:
    """Read the original book only if the stored replacement still has an unambiguous scope."""
    book = db.get(RealMexcAccountLink, binding.archive_ref)
    identity = db.get(RealMexcUidLink, binding.archive_ref)
    if (book is None or identity is None or binding.credential_ref == binding.archive_ref
            or any(not re.fullmatch(r"[0-9a-f]{64}", value)
                   for value in (binding.archive_ref, binding.credential_ref, binding.uid_digest))
            or not hmac.compare_digest(binding.uid_digest, identity.uid_digest)
            or binding.bound_at < max(book.created_at, identity.observed_at)
            or db.get(RealMexcAccountLink, binding.credential_ref) is not None
            or db.get(RealMexcUidLink, binding.credential_ref) is not None):
        raise MexcUidGuardError("El vínculo View guardado no es verificable")
    matching_refs = db.scalars(select(RealMexcUidLink.account_ref).where(
        RealMexcUidLink.uid_digest == binding.uid_digest).limit(2)).all()
    if matching_refs != [binding.archive_ref]:
        raise MexcUidGuardError("El UID del vínculo View guardado es ambiguo")
    return book.account_id



def saved_mexc_view_scope(db: Session, credential_ref: str) -> MexcTransferRecoveryView | None:
    if db.get(RealMexcViewBinding, credential_ref) is not None:
        raise MexcUidGuardError("La clave View original fue reemplazada. Conecta la clave renovada.")
    binding = db.scalar(select(RealMexcViewBinding).where(
        RealMexcViewBinding.credential_ref == credential_ref))
    if binding is None:
        return None
    return MexcTransferRecoveryView(verify_mexc_view_binding(db, binding),
                                    binding.archive_ref, binding.credential_ref)


def save_mexc_view_binding(db: Session, scope: MexcTransferRecoveryView, uid: str,
                           *, now: datetime) -> None:
    """Replace the operational fingerprint inside the same transaction as STOP."""
    if scope.credential_ref == scope.archive_ref:
        raise MexcUidGuardError("La clave View original no puede ser su propio reemplazo")
    scope.verify(db, uid)
    owner = db.scalar(select(RealMexcViewBinding.archive_ref).where(
        RealMexcViewBinding.credential_ref == scope.credential_ref))
    if owner is not None and owner != scope.archive_ref:
        raise MexcUidGuardError("La clave View ya está vinculada a otro libro")
    identity = db.get(RealMexcUidLink, scope.archive_ref)
    if identity is None:
        raise MexcUidGuardError("Falta la identidad MEXC original")
    binding = db.get(RealMexcViewBinding, scope.archive_ref)
    if binding is None:
        binding = RealMexcViewBinding(archive_ref=scope.archive_ref)
        db.add(binding)
    elif now < binding.bound_at:
        raise MexcUidGuardError("El reloj local retrocedió antes de renovar View")
    binding.credential_ref = scope.credential_ref
    binding.uid_digest = identity.uid_digest
    binding.bound_at = now
    verify_mexc_view_binding(db, binding)


def claim_mexc_uid_in_transaction(db: Session, account_ref: str, uid: str,
                                  *, now: datetime | None = None) -> None:
    """Reclama UID seudónimo dentro de un bloqueo de escritura ya adquirido."""
    if not isinstance(account_ref, str) or not re.fullmatch(r"[0-9a-f]{64}", account_ref):
        raise MexcUidGuardError("Referencia MEXC inválida")
    if not isinstance(uid, str) or not re.fullmatch(r"[1-9][0-9]{0,31}", uid):
        raise MexcUidGuardError("UID MEXC inválido")
    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise MexcUidGuardError("Hora sin zona horaria")
    observed_at = observed_at.astimezone(timezone.utc)

    salt = db.get(RealMexcIdentitySalt, 1)
    if salt is None:
        salt = RealMexcIdentitySalt(id=1, salt_hex=secrets.token_hex(32))
        db.add(salt)
    elif not re.fullmatch(r"[0-9a-f]{64}", salt.salt_hex):
        raise MexcUidGuardError("Sal de identidad MEXC corrupta")
    digest = hmac.new(bytes.fromhex(salt.salt_hex), uid.encode("ascii"),
                      hashlib.sha256).hexdigest()
    existing = db.get(RealMexcUidLink, account_ref)
    if existing is not None and not hmac.compare_digest(existing.uid_digest, digest):
        raise MexcUidGuardError("La referencia MEXC cambió de UID; revisar identidad")
    other = db.scalars(select(RealMexcUidLink).where(
        RealMexcUidLink.uid_digest == digest,
        RealMexcUidLink.account_ref != account_ref,
    ).limit(1)).first()
    if other is not None:
        raise MexcUidGuardError(
            "Otra clave apunta al mismo UID MEXC; revisar rotación, límites y parada"
        )
    if existing is None:
        db.add(RealMexcUidLink(account_ref=account_ref, uid_digest=digest,
                               observed_at=observed_at))


def bind_uid_for_spot_submission(vault: RealVault, account_ref: str, uid: str,
                                 *, now: datetime | None = None) -> None:
    """Vincula la primera clave con bloqueo duradero; no concilia saldos."""
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            claim_mexc_uid_in_transaction(db, account_ref, uid, now=now)
            db.commit()
        except Exception:
            db.rollback()
            raise
