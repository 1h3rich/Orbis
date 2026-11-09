"""El aviso de reinicio no expone rutas ni reinicia sesiones cifradas."""

from app.core.runtime_source import RuntimeSourceMonitor


def test_runtime_source_detects_edits_additions_and_removals(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    source = app / "module.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    monitor = RuntimeSourceMonitor(tmp_path)
    assert monitor.restart_required() is False
    static = app / "web" / "static"
    static.mkdir(parents=True)
    (static / "app.js").write_text("// Se sirve en la próxima recarga\n", encoding="utf-8")
    assert monitor.restart_required() is False
    source.write_text("VALUE = 2\n", encoding="utf-8")
    assert monitor.restart_required() is True
    restored = RuntimeSourceMonitor(tmp_path)
    assert restored.restart_required() is False
    another = app / "new.py"
    another.write_text("PASS = True\n", encoding="utf-8")
    assert restored.restart_required() is True
    fresh = RuntimeSourceMonitor(tmp_path)
    another.unlink()
    assert fresh.restart_required() is True


def test_runtime_api_is_read_only_and_does_not_expose_source(client):
    response = client.get("/system/runtime")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert set(response.json()) == {"restart_required"}
    assert type(response.json()["restart_required"]) is bool
