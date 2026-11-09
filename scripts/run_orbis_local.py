"""Arranque local de Orbis sin recarga de desarrollo ni exposición en LAN."""

from __future__ import annotations

import argparse
from getpass import getpass
from http.client import HTTPConnection
import os
from pathlib import Path
import socket
import sys
from threading import Thread
import time
import webbrowser


ROOT = Path(__file__).resolve().parents[1]
HOST = "127.0.0.1"


def _open_when_ready(port: int) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        connection = HTTPConnection(HOST, port, timeout=1)
        try:
            connection.request("GET", "/ui/")
            response = connection.getresponse()
            response.read()
            if response.status == 200:
                webbrowser.open(f"http://{HOST}:{port}/ui/")
                return
        except OSError:
            pass
        finally:
            connection.close()
        time.sleep(0.2)
    print("ORBIS está iniciándose; abre la URL local mostrada arriba.", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inicia ORBIS sólo en este equipo")
    parser.add_argument("--port", type=int, default=8000,
                        help="Puerto local; 0 elige uno disponible (predeterminado: 8000)")
    parser.add_argument("--no-browser", action="store_true",
                        help="No abrir automáticamente la interfaz")
    parser.add_argument("--require-encrypted-paper", action="store_true",
                        help="Fallar sin crear SQLite en claro si PAPER no es SQLCipher")
    parser.add_argument('--terminal-paper-unlock',action='store_true',
                        help='Pedir la contraseña PAPER por terminal en lugar de abrir Ajustes con PAPER cerrado')
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("el puerto debe estar entre 0 y 65535")

    # El almacén PAPER legado es relativo al directorio del proceso. Fijar
    # la raíz evita crear por accidente otra cartera al lanzar desde fuera.
    os.chdir(ROOT)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from sqlalchemy.engine import make_url
    from app.database.legacy_runtime import (
        create_new_encrypted_legacy_database, legacy_database_format)

    if args.require_encrypted_paper:
        os.environ["ORBIS_DATABASE_REQUIRE_ENCRYPTED"] = "1"
    database_url = make_url(os.environ.get("ORBIS_DATABASE_URL", "sqlite:///./orbis.db"))
    local_file = (database_url.get_backend_name() == "sqlite" and database_url.database
                  and database_url.database != ":memory:" and not database_url.query.get("uri"))
    database_path = Path(database_url.database) if local_file else None
    if args.terminal_paper_unlock:os.environ.pop('ORBIS_DATABASE_GUI_ACCESS',None)
    else:os.environ['ORBIS_DATABASE_GUI_ACCESS']='1'
    new_encrypted = bool(database_path and legacy_database_format(database_path) == "missing")
    if new_encrypted and args.terminal_paper_unlock:
        first = getpass("Nueva contraseña para PAPER cifrado: ")
        second = getpass("Repite la contraseña: ")
        if first != second:
            parser.error("las contraseñas PAPER no coinciden")
        create_new_encrypted_legacy_database(database_path, first)
        os.environ["ORBIS_DATABASE_REQUIRE_ENCRYPTED"] = "1"
        passphrase = first
    else:
        passphrase = None
    if (os.environ.get("ORBIS_DATABASE_REQUIRE_ENCRYPTED") == "1" and
            (not database_path or legacy_database_format(database_path) not in
                (('encrypted_or_invalid',) if args.terminal_paper_unlock else ('encrypted_or_invalid','missing')))):
        parser.error("PAPER cifrado requerido: selecciona una base SQLCipher existente")
    if (args.terminal_paper_unlock and database_path and legacy_database_format(database_path)=='encrypted_or_invalid'
            and 'ORBIS_DATABASE_KEY_FD' not in os.environ):
        if passphrase is None:
            passphrase = getpass("Contraseña de PAPER heredado cifrado: ")
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, passphrase.encode("utf-8"))
        finally:
            os.close(write_fd)
        os.environ["ORBIS_DATABASE_KEY_FD"] = str(read_fd)
    passphrase=None
    if new_encrypted and args.terminal_paper_unlock:first=second=None
    import uvicorn

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Permite reiniciar en el mismo puerto mientras conexiones locales
        # anteriores terminan su TIME_WAIT; no habilita uso compartido activo.
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((HOST, args.port))
        listener.listen(128)
        port = listener.getsockname()[1]
        print(f"ORBIS local: http://{HOST}:{port}/ui/", flush=True)
        if not args.no_browser:
            Thread(target=_open_when_ready, args=(port,), daemon=True).start()
        from app.main import app
        protection = app.state.storage_protection
        manual_protection=app.state.manual_paper_protection
        while True:
            server = uvicorn.Server(uvicorn.Config(
                app, host=HOST, port=port, reload=False, workers=1,
                proxy_headers=False, access_log=False, log_level="info"))
            protection.attach(lambda: setattr(server, 'should_exit', True))
            manual_protection.attach(lambda: setattr(server,'should_exit',True))
            # Keep our original descriptor bound while uvicorn closes its copy;
            # another process cannot claim the port during offline protection.
            serving_socket = listener.dup()
            try:
                server.run(sockets=[serving_socket])
            finally:
                serving_socket.close()
                protection.server_stopped()
                manual_protection.server_stopped()
            if not server.started:
                return 1
            manual_performed=manual_protection.perform_offline()
            other_performed=protection.perform_offline()
            if not manual_performed and not other_performed:
                return 0
            # The same app object starts a fresh lifespan; encrypted stores have
            # no ephemeral access keys and no credentials are reconnected.

    except OSError as exc:
        print(f"No se pudo abrir ORBIS en {HOST}:{args.port}: {exc.strerror or exc}",
              file=sys.stderr)
        return 1
    finally:
        listener.close()


if __name__ == "__main__":
    raise SystemExit(main())
