"""Fixed, explicit local-store lifetimes. No persistence or REAL authority."""
from _thread import RLock
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr


DEFAULT_SESSION_SECONDS = 900
MAX_SESSION_SECONDS = 30 * 24 * 60 * 60


@contextmanager
def pending_unlock(lock: RLock, attempts: dict[Path, object], path: Path) -> Iterator[object]:
    """Latest unlock intent wins; a lock removes its publication capability.

    Verification runs outside the store lock. The caller must check the token
    and publish its key together under that same lock. Cleanup never removes a
    newer request, including when verification fails or an older task finishes.
    """
    token = object()
    with lock:
        attempts[path] = token
    try:
        yield token
    finally:
        with lock:
            if attempts.get(path) is token:
                attempts.pop(path)


def fixed_session_duration(value: int | None, *, default: int) -> int:
    duration = default if value is None else value
    if type(duration) is not int or not 1 <= duration <= MAX_SESSION_SECONDS:
        raise ValueError("El plazo debe ser un número entero de segundos, hasta 30 días")
    return duration


class LocalStoreUnlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passphrase: SecretStr = Field(min_length=12, max_length=1024)
    session_seconds: int = Field(default=DEFAULT_SESSION_SECONDS, strict=True,
                                 ge=DEFAULT_SESSION_SECONDS, le=MAX_SESSION_SECONDS)
