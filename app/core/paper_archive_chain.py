"""Huella encadenada de los archivos Spot y GRVT PAPER.

La cadena detecta alteraciones accidentales de una vela o de su orden. No es
una firma ni protege frente a alguien que pueda reescribir toda la base.
"""

from __future__ import annotations

from hashlib import sha256
import json
import sqlite3
from typing import Literal

import sqlcipher3


_GENESIS = bytes(32)
_TABLES = {"spot": ("session_candles", "session_archive_chain"),
           "grvt": ("paper_signal_all_candles", "paper_signal_archive_chain")}


class ArchiveChainError(ValueError):
    """La página archivada no se puede presentar como íntegra."""


def next_archive_digest(previous: str | None, session_id: str, candle_t: int,
                        origin: str, candle_json: str,
                        kind: Literal["spot", "grvt"] = "spot") -> str:
    if not isinstance(session_id, str) or not session_id or type(candle_t) is not int or candle_t <= 0:
        raise ValueError("Identidad de vela PAPER inválida")
    if origin not in ("SEED", "OBSERVED") or not isinstance(candle_json, str):
        raise ValueError("Origen o contenido PAPER inválido")
    if kind not in ("spot", "grvt"):
        raise ValueError("Tipo de archivo PAPER inválido")
    prior = _GENESIS if previous is None else bytes.fromhex(previous)
    if len(prior) != 32:
        raise ValueError("Huella PAPER anterior inválida")
    record = json.dumps([f"{kind}-paper-archive-v1", session_id, candle_t, origin, candle_json],
                        ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return sha256(prior + record).hexdigest()


def chained_archive_matches(db, session_id: str,
                            kind: Literal["spot", "grvt"] = "spot") -> bool:
    """Comprueba filas, orden y huellas sin cargar el archivo completo en RAM."""
    if kind not in _TABLES:
        raise ValueError("Tipo de archivo PAPER inválido")
    archive_table, chain_table = _TABLES[kind]
    try:
        archive = db.execute(f"""SELECT candle_t,candle_json,origin FROM {archive_table}
                                 WHERE session_id=? ORDER BY candle_t""", (session_id,))
        chain = db.execute(f"""SELECT candle_t,digest FROM {chain_table}
                               WHERE session_id=? ORDER BY candle_t""", (session_id,))
        previous = None
        count = 0
        for candle, link in zip(archive, chain, strict=True):
            if candle["candle_t"] != link["candle_t"]:
                return False
            expected = next_archive_digest(previous, session_id, candle["candle_t"],
                                           candle["origin"], candle["candle_json"], kind)
            if link["digest"] != expected:
                return False
            previous = expected
            count += 1
        return count > 0
    except (KeyError, TypeError, ValueError, IndexError, AttributeError,
            sqlite3.DatabaseError, sqlcipher3.DatabaseError):
        return False


def verified_archive_page(db, session_id: str, kind: Literal["spot", "grvt"],
                          after_t: int | None, limit: int) -> dict:
    """Lee hasta `limit` velas y verifica la cadena local del tramo.

    El llamador debe mantener una instantánea de lectura durante las consultas.
    La huella inicial viene de la fila anterior; no sustituye una auditoría
    completa de la cadena desde su génesis.
    """
    if kind not in _TABLES or type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Página de archivo PAPER inválida")
    if after_t is not None and (type(after_t) is not int or after_t < 0):
        raise ValueError("Cursor de archivo PAPER inválido")
    archive_table, chain_table = _TABLES[kind]
    try:
        rows = db.execute(f"""SELECT candle_t,candle_json,origin FROM {archive_table}
                              WHERE session_id=? AND candle_t>?
                              ORDER BY candle_t LIMIT ?""",
                          (session_id, after_t if after_t is not None else -1,
                           limit + 1)).fetchall()
        visible = rows[:limit]
        if not visible:
            return {"candles": [], "next_after_t": None, "has_more": False,
                    "anchor_digest": None, "end_digest": None}
        first_t, last_t = visible[0]["candle_t"], visible[-1]["candle_t"]
        prior_candle = db.execute(f"""SELECT candle_t FROM {archive_table}
                                     WHERE session_id=? AND candle_t<?
                                     ORDER BY candle_t DESC LIMIT 1""", (session_id, first_t)).fetchone()
        prior_link = db.execute(f"""SELECT candle_t,digest FROM {chain_table}
                                   WHERE session_id=? AND candle_t<?
                                   ORDER BY candle_t DESC LIMIT 1""", (session_id, first_t)).fetchone()
        if bool(prior_candle) != bool(prior_link) or (
                prior_candle and prior_link and prior_candle["candle_t"] != prior_link["candle_t"]):
            raise ArchiveChainError("Falta el enlace anterior del archivo PAPER")
        anchor = prior_link["digest"] if prior_link else None
        links = db.execute(f"""SELECT candle_t,digest FROM {chain_table}
                               WHERE session_id=? AND candle_t>=? AND candle_t<=?
                               ORDER BY candle_t""", (session_id, first_t, last_t)).fetchall()
        if len(links) != len(visible):
            raise ArchiveChainError("Faltan huellas en el archivo PAPER")
        previous = anchor
        candles = []
        for row, link in zip(visible, links, strict=True):
            if row["candle_t"] != link["candle_t"]:
                raise ArchiveChainError("Orden de huellas PAPER divergente")
            expected = next_archive_digest(previous, session_id, row["candle_t"],
                                           row["origin"], row["candle_json"], kind)
            if expected != link["digest"]:
                raise ArchiveChainError("Huella de vela PAPER divergente")
            candle = json.loads(row["candle_json"])
            if not isinstance(candle, dict) or candle.get("t") != row["candle_t"]:
                raise ArchiveChainError("Vela PAPER archivada inválida")
            candles.append({"candle": candle, "origin": row["origin"]})
            previous = expected
        return {"candles": candles,
                "next_after_t": last_t if len(rows) > limit else None,
                "has_more": len(rows) > limit,
                "anchor_digest": anchor, "end_digest": previous}
    except ArchiveChainError:
        raise
    except (KeyError, TypeError, ValueError, IndexError, AttributeError,
            sqlite3.DatabaseError, sqlcipher3.DatabaseError) as exc:
        raise ArchiveChainError("Archivo PAPER no verificable") from exc
