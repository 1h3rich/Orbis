"""Comprueba que las velas PAPER observadas coinciden con su sesión y evento."""

from __future__ import annotations

import json
from typing import Literal


def observed_archive_matches(db, session_id: str, history_json: str,
                             kind: Literal["spot", "grvt"]) -> bool:
    try:
        bars = json.loads(history_json)
        history = {bar["t"]: bar for bar in bars}
        if len(history) != len(bars):
            return False
        if kind == "spot":
            event_rows = db.execute("""SELECT candle_t,payload_json FROM events
                                       WHERE session_id=? AND kind='OBSERVATION'""",
                                    (session_id,))
            observed = {row["candle_t"]: json.loads(row["payload_json"])["candle"]
                        for row in event_rows}
            archive_rows = db.execute("""SELECT candle_t,candle_json FROM observed_candles
                                         WHERE session_id=?""", (session_id,))
        elif kind == "grvt":
            event_rows = db.execute("""SELECT candle_t,evidence_json FROM paper_signal_observations
                                       WHERE session_id=?""", (session_id,))
            observed = {row["candle_t"]: json.loads(row["evidence_json"])["candle"]
                        for row in event_rows}
            archive_rows = db.execute("""SELECT candle_t,candle_json FROM paper_signal_candles
                                         WHERE session_id=?""", (session_id,))
        else:
            raise ValueError("Tipo de archivo PAPER inválido")
        archive = {row["candle_t"]: json.loads(row["candle_json"])
                   for row in archive_rows}
        return (len(observed) == len(archive) and observed == archive
                and all(candle["t"] == stamp and history.get(stamp) == candle
                        for stamp, candle in archive.items()))
    except (KeyError, TypeError, ValueError, IndexError):
        return False


def full_archive_matches(db, session_id: str, history_json: str, kind: Literal["spot", "grvt"]) -> bool:
    """El archivo completo debe reproducir exactamente la sesión en orden."""
    table = "session_candles" if kind == "spot" else "paper_signal_all_candles" if kind == "grvt" else None
    if table is None:
        raise ValueError("Tipo de archivo PAPER inválido")
    try:
        history = json.loads(history_json)
        rows = db.execute(f"SELECT candle_t,candle_json,origin FROM {table} WHERE session_id=? ORDER BY candle_t",
                          (session_id,)).fetchall()
        if len(rows) != len(history):
            return False
        for row, candle in zip(rows, history, strict=True):
            if row["candle_t"] != candle["t"] or json.loads(row["candle_json"]) != candle:
                return False
            if row["origin"] not in ("SEED", "OBSERVED"):
                return False
        observed = {row["candle_t"] for row in db.execute(
            ("SELECT candle_t FROM observed_candles WHERE session_id=?" if kind == "spot" else
             "SELECT candle_t FROM paper_signal_candles WHERE session_id=?"), (session_id,))}
        return observed == {row["candle_t"] for row in rows if row["origin"] == "OBSERVED"}
    except (KeyError, TypeError, ValueError):
        return False
