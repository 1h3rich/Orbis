"""Peticiones simultáneas sobre bases locales nuevas; no es prueba de estrés."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier

from app.automation.store import AutomationStore
from app.lab.engine import validate_candles
from app.lab.store import LabStore


def simultaneous(count, action):
    barrier = Barrier(count)

    def worker(index):
        barrier.wait(timeout=10)
        return action(index)

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(worker, range(count)))


def test_lab_concurrent_first_import_deduplicates_dataset(tmp_path):
    path = tmp_path / "lab.db"
    start = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
    bars, gaps = validate_candles([
        {"t": start + i * 3600, "o": "100", "h": "101", "l": "99", "c": "100", "v": "10"}
        for i in range(4)
    ], "1h", now=start + 10 * 3600)
    ids = simultaneous(8, lambda _index: LabStore(path).save_dataset("BTC/USDT", "1h", "fixture", bars, gaps)["id"])
    assert len(set(ids)) == 1
    assert len(LabStore(path).datasets()) == 1


def test_dca_concurrent_initialization_and_versions(tmp_path):
    path = tmp_path / "automation.db"
    created = simultaneous(8, lambda index: AutomationStore(path).create_rule(f"Plan {index}", {"amount": str(index + 1)}))
    assert len({row["id"] for row in created}) == 8
    store = AutomationStore(path)
    assert len(store.rules()) == 8
    first_id = created[0]["id"]
    versions = simultaneous(8, lambda index: AutomationStore(path).add_version(first_id, {"amount": str(index + 10)}))
    assert sorted(row["version"] for row in versions) == list(range(2, 10))
    assert [row["version"] for row in store.versions(first_id)] == list(range(1, 10))
