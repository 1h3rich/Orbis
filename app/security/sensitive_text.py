"""Reconocimiento conservador de direcciones completas en texto local.

No identifica todas las cadenas posibles ni sustituye la revisión del
usuario antes de compartir datos, pero evita los formatos comunes que ORBIS
no necesita almacenar en alias, notas o evidencia de investigación.
"""

from __future__ import annotations

import re


_EVM = re.compile(r"0x[a-fA-F0-9]{40}(?![a-fA-F0-9])")
_BECH32 = re.compile(r"(?<![A-Za-z0-9])bc1[a-zA-Z0-9]{25,90}(?![A-Za-z0-9])")
_BASE58 = re.compile(
    r"(?<![A-Za-z0-9])[123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz]{26,62}(?![A-Za-z0-9])"
)
_EVM_WORD = re.compile(r"(?<![0-9a-fA-F])0x[0-9a-fA-F]{64}(?![0-9a-fA-F])")
_API_KEY = re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}(?![A-Za-z0-9_-])")
_BARE_SECRET = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])")


def contains_wallet_address(value: str) -> bool:
    """Detecta direcciones EVM, Bitcoin y base58 completas habituales."""
    if _EVM.search(value) or _BECH32.search(value):
        return True
    # Las métricas de LAB pueden tener 30–60 cifras decimales consecutivas;
    # no son direcciones base58. Una dirección habitual contiene letras.
    return any(any(char.isalpha() for char in match.group())
               for match in _BASE58.finditer(value))


def contains_wallet_address_in_data(value: object) -> bool:
    """Revisa sólo valores de un paquete JSON, sin inferir claves privadas."""
    if isinstance(value, str):
        return contains_wallet_address(value)
    if isinstance(value, dict):
        return any(contains_wallet_address_in_data(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(contains_wallet_address_in_data(item) for item in value)
    return False


def contains_sensitive_ai_text(value: str, *, question: bool = False) -> bool:
    """Bloquea identificadores/secretos reconocibles antes de una llamada externa.

    Los SHA-256 sin prefijo son parte normal de la evidencia LAB; sólo en la
    pregunta libre se bloquean también cadenas hexadecimales desnudas.
    No sustituye la revisión explícita de la vista previa por el usuario.
    """
    return bool(contains_wallet_address(value) or _EVM_WORD.search(value)
                or _API_KEY.search(value) or question and _BARE_SECRET.search(value))


def contains_sensitive_ai_data(value: object) -> bool:
    if isinstance(value, str):
        return contains_sensitive_ai_text(value)
    if isinstance(value, dict):
        return any(contains_sensitive_ai_data(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(contains_sensitive_ai_data(item) for item in value)
    return False
