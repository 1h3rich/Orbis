"""Límites del análisis de huecos, comunes a Spot y GRVT."""

import pytest

from app.lab.engine import LabError
from app.lab.paper_gap import gap_download_range


def test_gap_range_requires_closed_candle_and_never_returns_partial_over_limit():
    session = {"timeframe": "1h", "last_t": 1_735_689_600, "status": "GAP"}
    with pytest.raises(LabError, match="velas cerradas"):
        gap_download_range(session, session["last_t"] + 3600 + 10)
    with pytest.raises(LabError, match="cobertura verificable"):
        gap_download_range(session, session["last_t"] + 5000 * 3600 + 10)
    assert gap_download_range(session, session["last_t"] + 4998 * 3600 + 10) == (
        session["last_t"] - 2 * 3600, session["last_t"] + 4998 * 3600)


def test_active_session_cannot_be_relabelled_as_reconstructed():
    session = {"timeframe": "1h", "last_t": 1_735_689_600, "status": "ACTIVE"}
    with pytest.raises(LabError, match="no está detenida"):
        gap_download_range(session, session["last_t"] + 3 * 3600)
