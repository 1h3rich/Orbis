"""LAB activo SQLCipher: escritura, reapertura y fallo cerrado."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from app.lab.backup import create_lab_backup, restore_lab_backup
from app.lab.engine import LabError, validate_candles
from app.lab.secure_backup import (create_secure_lab_backup, restore_secure_lab_backup,
                                   verify_secure_lab_backup)
from app.lab.store import LabStore


PASSWORD = "LAB active passphrase with 'quote'"


def bars():
    rows = [{"t": 1735689600 + i * 3600, "o": str(100 + i),
             "h": str(102 + i), "l": str(99 + i), "c": str(101 + i),
             "v": "10"} for i in range(10)]
    return validate_candles(rows, "1h", now=1735689600 + 20 * 3600)[0]


def test_encrypted_lab_store_creates_reads_and_writes_without_plaintext(tmp_path):
    path = tmp_path / "lab.db"
    store = LabStore(path, passphrase=PASSWORD)
    dataset = store.save_dataset("BTC/USDT", "1h", "fixture", bars(), [])
    assert path.read_bytes()[:16] != b"SQLite format 3\x00"
    assert path.stat().st_mode & 0o777 == 0o600
    assert LabStore(path, passphrase=PASSWORD).dataset(dataset["id"])[0]["hash"] == dataset["hash"]
    before = path.read_bytes()
    with pytest.raises(LabError, match="contraseña o formato"):
        LabStore(path, passphrase="incorrect password").datasets()
    with pytest.raises(LabError, match="contraseña o formato"):
        LabStore(path).datasets()
    assert path.read_bytes() == before
    assert LabStore(path, passphrase=PASSWORD).datasets()[0]["id"] == dataset["id"]


def test_encrypted_lab_backup_restores_encrypted_active_with_preimage(tmp_path):
    path = tmp_path / "lab.db"
    store = LabStore(path, passphrase=PASSWORD)
    dataset = store.save_dataset("BTC/USDT", "1h", "fixture", bars(), [])
    secure = create_secure_lab_backup(store, "independent backup password")
    assert secure["encrypted"] is True
    assert secure["verified"]["verified_datasets"] == 1
    with pytest.raises(LabError, match="no admite copias sin cifrar"):
        create_lab_backup(store)
    with pytest.raises(LabError, match="copia sin cifrar"):
        restore_lab_backup(store, "lab-20261015-000000-000000000000.db", "RESTAURAR")
    more_bars = bars()
    more = store.save_dataset("ETH/USDT", "1h", "fixture", more_bars, [])
    assert len(store.datasets()) == 2
    restored = restore_secure_lab_backup(store, secure["name"], "RESTAURAR",
                                         "independent backup password")
    assert restored["restored"] is True
    assert path.read_bytes()[:16] != b"SQLite format 3\x00"
    assert len(LabStore(path, passphrase=PASSWORD).datasets()) == 1
    preimage = tmp_path / "lab-backups" / restored["previous_lab_backup"]
    assert verify_secure_lab_backup(preimage, "independent backup password")["counts"]["datasets"] == 2
    with pytest.raises(LabError, match="Dataset no encontrado"):
        store.dataset(more["id"])
    assert store.dataset(dataset["id"])[0]["hash"] == dataset["hash"]
    with pytest.raises(LabError, match="formato esperado"):
        restore_lab_backup(LabStore(path), secure["name"], "RESTAURAR",
                           "independent backup password")
    with pytest.raises(LabError):
        restore_secure_lab_backup(LabStore(path, passphrase="wrong active password"),
                                  secure["name"], "RESTAURAR", "independent backup password")
    assert store.dataset(dataset["id"])[0]["hash"] == dataset["hash"]


def test_plain_store_rejects_password_instead_of_converting_in_place(tmp_path):
    path = tmp_path / "lab.db"
    plain = LabStore(path)
    dataset = plain.save_dataset("BTC/USDT", "1h", "fixture", bars(), [])
    before = path.read_bytes()
    with pytest.raises(LabError, match="contraseña o formato"):
        LabStore(path, passphrase=PASSWORD).datasets()
    assert path.read_bytes() == before
    assert plain.dataset(dataset["id"])[0]["hash"] == dataset["hash"]


def test_cli_restores_encrypted_active_only_with_both_passwords(tmp_path):
    path = tmp_path / "lab.db"
    store = LabStore(path, passphrase=PASSWORD)
    store.save_dataset("BTC/USDT", "1h", "fixture", bars(), [])
    backup = create_secure_lab_backup(store, "independent backup password")
    store.save_dataset("ETH/USDT", "1h", "fixture", bars(), [])
    script = Path(__file__).resolve().parents[1] / "scripts" / "restore_lab.py"
    command = [sys.executable, str(script), "--lab-db", str(path),
               "--restore", backup["name"], "--confirm", "RESTAURAR",
               "--passphrase-stdin"]
    missing = subprocess.run(command, input="independent backup password\n",
                             text=True, capture_output=True)
    assert missing.returncode == 1 and "--active-passphrase-stdin" in missing.stderr
    assert len(store.datasets()) == 2
    restored = subprocess.run([*command, "--active-passphrase-stdin"],
                              input="independent backup password\n" + PASSWORD + "\n",
                              text=True, capture_output=True, check=True)
    assert json.loads(restored.stdout)["restored"] is True
    assert len(LabStore(path, passphrase=PASSWORD).datasets()) == 1
    assert path.read_bytes()[:16] != b"SQLite format 3\x00"
    assert PASSWORD not in restored.stdout + restored.stderr
