"""Real OS-process exclusion, not merely a Python threading flag."""
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from app.derivatives.perp_observed_lease import ObservedSessionLease
from scripts.verify_orbis import environment


def test_live_shared_ownership_blocks_another_process_closure_then_releases(tmp_path):
    path, session = tmp_path / 'paper.db', str(uuid4())
    code = '''import sys
from pathlib import Path
from app.derivatives.perp_observed_lease import ObservedSessionLease
from app.derivatives.perp_observed_types import ObservedError
try:
 with ObservedSessionLease(Path(sys.argv[1]),sys.argv[2],exclusive=True):pass
except ObservedError:raise SystemExit(17)
'''
    def probe():
        return subprocess.run([sys.executable, '-c', code, str(path), session],
                              cwd=Path(__file__).resolve().parents[1], env=environment(tmp_path / 'child'),
                              capture_output=True, text=True, timeout=15)
    lease = ObservedSessionLease(path, session, exclusive=False)
    try:
        assert probe().returncode == 17
    finally:
        lease.close()
    result = probe()
    assert result.returncode == 0, result.stderr
