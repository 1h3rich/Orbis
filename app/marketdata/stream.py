"""Stream WS/poll con reconexión 1-2-4-8 máx 30s (cliente + servidor).

El servidor emite snapshots cada 5s para mercados soportados; GRVT recibe
una consulta puntual y se cierra el WS local hasta disponer de streaming
nativo. El
cliente JS reconecta con backoff 1s,2s,4s,8s (máx 30s) y cae a SSE/poll.
"""

from __future__ import annotations

BACKOFF_STEPS = (1, 2, 4, 8)
BACKOFF_MAX = 30
POLL_SECONDS = 5


def backoff_delays(attempts: int = 8) -> list[int]:
    """Secuencia de esperas de reconexión (1,2,4,8,8... máx 30s)."""
    out = []
    for i in range(attempts):
        if i < len(BACKOFF_STEPS):
            out.append(BACKOFF_STEPS[i])
        else:
            out.append(8 if 8 <= BACKOFF_MAX else BACKOFF_MAX)
    return out
