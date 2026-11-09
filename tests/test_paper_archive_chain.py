"""Lectura encadenada por páginas, incluida una serie mayor que el límite PAPER."""

import json
import sqlite3

import pytest

from app.core.paper_archive_chain import (ArchiveChainError, next_archive_digest,
                                          verified_archive_page)


@pytest.mark.parametrize("kind,tables", [
    ("spot", ("session_candles", "session_archive_chain")),
    ("grvt", ("paper_signal_all_candles", "paper_signal_archive_chain")),
])
def test_verified_archive_pages_cross_ten_thousand_bars_and_reject_boundary_tamper(kind, tables):
    archive_table, chain_table = tables
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(f"CREATE TABLE {archive_table} (session_id TEXT,candle_t INTEGER,candle_json TEXT,origin TEXT)")
    db.execute(f"CREATE TABLE {chain_table} (session_id TEXT,candle_t INTEGER,digest TEXT)")
    session_id, previous = "paper-session", None
    archive_rows, chain_rows = [], []
    for candle_t in range(1, 10051):
        origin = "SEED" if candle_t <= 10 else "OBSERVED"
        raw = json.dumps({"t": candle_t, "c": str(100 + candle_t)}, separators=(",", ":"))
        previous = next_archive_digest(previous, session_id, candle_t, origin, raw, kind)
        archive_rows.append((session_id, candle_t, raw, origin))
        chain_rows.append((session_id, candle_t, previous))
    db.executemany(f"INSERT INTO {archive_table} VALUES (?,?,?,?)", archive_rows)
    db.executemany(f"INSERT INTO {chain_table} VALUES (?,?,?)", chain_rows)
    cursor, count, prior_digest = None, 0, None
    while True:
        page = verified_archive_page(db, session_id, kind, cursor, 512)
        assert len(page["candles"]) <= 512
        assert page["anchor_digest"] == prior_digest
        count += len(page["candles"])
        prior_digest = page["end_digest"]
        if not page["has_more"]:
            break
        cursor = page["next_after_t"]
    assert count == 10050 and prior_digest == previous
    assert verified_archive_page(db, session_id, kind, 10050, 512)["candles"] == []

    db.execute(f"UPDATE {archive_table} SET candle_json=? WHERE session_id=? AND candle_t=513",
               ('{"t":513,"c":"alterado"}', session_id))
    with pytest.raises(ArchiveChainError, match="Huella de vela PAPER divergente"):
        verified_archive_page(db, session_id, kind, 512, 512)
    db.close()
