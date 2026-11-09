"""El stop móvil GRVT se puede reconstruir sin el JSON de sesión."""

import json
import sqlite3
from decimal import Decimal

import pytest

from app.core.paper_archive_chain import next_archive_digest
from app.derivatives.signal_monitor import SignalMonitor, SignalMonitorError


@pytest.mark.parametrize(("side", "expected_full", "expected_pending"), [
    ("LONG", Decimal("190.00"), Decimal("114.00")),
    ("SHORT", Decimal("63.00"), Decimal("84.00")),
])
def test_archived_trailing_stop_crosses_page_boundary_and_detects_tamper(
        side, expected_full, expected_pending):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE paper_signal_all_candles (session_id TEXT, candle_t INTEGER, candle_json TEXT, origin TEXT)")
    db.execute("CREATE TABLE paper_signal_archive_chain (session_id TEXT, candle_t INTEGER, digest TEXT)")
    start = 1_735_689_600
    step = 3600
    session_id = "archived-stop-test"
    digest = None
    for index in range(530):
        candle_t = start + index * step
        high = "200" if index == 529 else "120" if index == 513 else "100"
        low = "60" if index == 529 else "80" if index == 513 else "95"
        candle_json = json.dumps({"t": candle_t, "o": "95", "h": high,
                                  "l": low, "c": "96", "v": "10"}, sort_keys=True)
        origin = "SEED" if index < 500 else "OBSERVED"
        db.execute("INSERT INTO paper_signal_all_candles VALUES (?,?,?,?)",
                   (session_id, candle_t, candle_json, origin))
        digest = next_archive_digest(digest, session_id, candle_t, origin,
                                     candle_json, "grvt")
        db.execute("INSERT INTO paper_signal_archive_chain VALUES (?,?,?)",
                   (session_id, candle_t, digest))
    session = {"id": session_id, "timeframe": "1h",
               "last_t": start + 529 * step}
    position = {"entry_price": "100", "side": side,
                "opened_at_ns": (start + 512 * step) * 1_000_000_000}
    definition = {"direction": side, "stop_loss": "0.2", "trailing_stop": "0.05"}

    assert SignalMonitor._archived_stop_level(db, session, position, definition) == (
        expected_full, "TRAILING_STOP")
    pending_session = {**session, "last_t": start + 528 * step}
    with pytest.raises(SignalMonitorError, match="archivado no verificable"):
        SignalMonitor._archived_stop_level(db, pending_session, position, definition)
    assert SignalMonitor._archived_stop_level(
        db, pending_session, position, definition, allow_next_uncommitted=True) == (
            expected_pending, "TRAILING_STOP")
    with pytest.raises(SignalMonitorError, match="archivado no verificable"):
        SignalMonitor._archived_stop_level(
            db, {**session, "last_t": start + 527 * step}, position, definition,
            allow_next_uncommitted=True)
    db.execute("UPDATE paper_signal_all_candles SET candle_json=? WHERE session_id=? AND candle_t=?",
               (json.dumps({"t": start + 513 * step, "h": "130", "l": "70"}),
                session_id, start + 513 * step))
    with pytest.raises(SignalMonitorError, match="archivado no verificable"):
        SignalMonitor._archived_stop_level(db, session, position, definition)
    db.close()
