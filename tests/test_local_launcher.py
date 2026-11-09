"""Arranque de usuario: un único proceso local, sin tocar bases personales."""

from pathlib import Path
import os
import select
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]


def test_launcher_from_other_directory_serves_only_local_ui(tmp_path):
    # Una base heredada ya existente sigue abriéndose sin migrarla por sorpresa.
    with sqlite3.connect(tmp_path / "legacy.db") as db:
        db.execute("CREATE TABLE legacy_fixture (id INTEGER PRIMARY KEY)")
    env = os.environ.copy()
    env.update({
        "ORBIS_DATABASE_URL": f"sqlite:///{tmp_path / 'legacy.db'}",
        "ORBIS_LAB_PATH": str(tmp_path / "lab.db"),
        "ORBIS_AUTOMATION_PATH": str(tmp_path / "automation.db"),
        "ORBIS_PAPER_PERP_PATH": str(tmp_path / "paper-perp.db"),
        "ORBIS_SPOT_STRATEGY_PATH": str(tmp_path / "spot-paper.db"),
        "ORBIS_REAL_VAULT_PATH": str(tmp_path / "real-vault.db"),
    })
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "run_orbis_local.py"),
         "--port", "0", "--no-browser"], cwd=tmp_path, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert process.stdout is not None
        ready, _, _ = select.select([process.stdout], [], [], 10)
        assert ready, "El lanzador no anunció su dirección local"
        announced = process.stdout.readline().strip()
        assert announced.startswith("ORBIS local: http://127.0.0.1:")
        url = announced.removeprefix("ORBIS local: ")
        assert url.endswith("/ui/")
        # La suite completa puede ocupar la CPU mientras el proceso hijo
        # termina de importar módulos. Un timeout aislado no es fallo de arranque.
        deadline = time.monotonic() + 20
        while True:
            try:
                with urlopen(url, timeout=1) as response:
                    assert response.status == 200
                    assert b"Orbis" in response.read()
                break
            except (URLError, TimeoutError):
                assert process.poll() is None, "ORBIS terminó antes de servir la UI"
                assert time.monotonic() < deadline, "ORBIS no abrió la UI local"
                time.sleep(0.1)
        if Path(f"/proc/{process.pid}/cwd").exists():
            assert Path(f"/proc/{process.pid}/cwd").resolve() == ROOT
        with urlopen(Request(url.replace("/ui/", "/lab/status")), timeout=2) as response:
            assert response.status == 200
        assert process.poll() is None
    finally:
        process.terminate()
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)


def test_launcher_does_not_reuse_an_occupied_port(tmp_path):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        port = occupied.getsockname()[1]
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "run_orbis_local.py"),
             "--port", str(port), "--no-browser"],
            cwd=tmp_path, text=True, capture_output=True, timeout=10,
        )
    assert result.returncode == 1
    assert "No se pudo abrir ORBIS" in result.stderr
    assert "ORBIS local:" not in result.stdout


def test_launcher_restarts_on_same_port_after_clean_shutdown(tmp_path):
    """TIME_WAIT de clientes locales no debe dejar la UI fuera de servicio."""
    with sqlite3.connect(tmp_path / "legacy.db") as db:
        db.execute("CREATE TABLE legacy_fixture (id INTEGER PRIMARY KEY)")
    env = os.environ.copy()
    env.update({
        "ORBIS_DATABASE_URL": f"sqlite:///{tmp_path / 'legacy.db'}",
        "ORBIS_LAB_PATH": str(tmp_path / "lab.db"),
        "ORBIS_AUTOMATION_PATH": str(tmp_path / "automation.db"),
        "ORBIS_PAPER_PERP_PATH": str(tmp_path / "paper-perp.db"),
        "ORBIS_SPOT_STRATEGY_PATH": str(tmp_path / "spot-paper.db"),
        "ORBIS_REAL_VAULT_PATH": str(tmp_path / "real-vault.db"),
    })
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]

    for _ in range(2):
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "scripts" / "run_orbis_local.py"),
             "--port", str(port), "--no-browser"],
            cwd=tmp_path, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        try:
            assert process.stdout is not None
            ready, _, _ = select.select([process.stdout], [], [], 10)
            assert ready, "El lanzador no anunció el puerto"
            assert process.stdout.readline().strip() == f"ORBIS local: http://127.0.0.1:{port}/ui/"
            deadline = time.monotonic() + 20
            while True:
                try:
                    with urlopen(f"http://127.0.0.1:{port}/ui/", timeout=2) as response:
                        assert response.status == 200
                        response.read()
                    break
                except (URLError, TimeoutError):
                    assert process.poll() is None, "ORBIS terminó antes de abrir el puerto"
                    assert time.monotonic() < deadline, "ORBIS no abrió el puerto"
                    time.sleep(0.1)
        finally:
            process.terminate()
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
