"""Actual launcher and full app; only public GRVT transport/clock controlled."""
import pytest
from tests.perp_dataset_fixture import source
patch=pytest.MonkeyPatch()
adapter,_=source(patch)
patch.setattr('app.marketdata.adapters.grvt.GrvtAdapter',lambda:adapter)
from scripts.run_orbis_local import main
if __name__=='__main__':raise SystemExit(main())
