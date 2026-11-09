"""Bóveda local SQLCipher para los registros REAL.

No migra automáticamente datos PAPER ni crea una base sin contraseña. Una
bóveda existente con versión desconocida se deja intacta hasta migrarla.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import hashlib
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from threading import RLock
from typing import Iterator
from uuid import uuid4

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, URL
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.portfolio.domain import EventInput, PortfolioError, PortfolioEvent, fold_events
from app.portfolio.models import RealAccount, RealBase, RealMexcParityObservation, RealMexcOpenOrdersObservation, RealMexcOrderTestAttempt, RealMexcTransferMatch, MexcTransferIdentityEvidence
from app.portfolio.mexc_parity_archive import MexcParityArchiveError, read_observation
from app.portfolio.mexc_open_orders_archive import MexcOpenOrdersArchiveError, read_observation as read_open_orders_observation
from app.portfolio.mexc_order_test_archive import MexcOrderTestArchiveError, read_attempt
from app.portfolio.mexc_transfer_matches import verify_mexc_transfer_match
from app.portfolio.repository import list_events, verify_paired_transfers


SCHEMA_VERSION = 82


class VaultError(RuntimeError):
    """La bóveda no puede abrirse o no está en un estado seguro."""


class RealVault:
    """Dueño único del motor REAL y de su ciclo de desbloqueo/bloqueo."""

    def __init__(self, path: Path):
        self.path = Path(path).expanduser().resolve()
        self._engine: Engine | None = None
        self._lock = RLock()
        self._session_generation = 0
        self._dex_replay_cache: dict[str, object] = {}
        self._grvt_perp_replay_cache: dict[str, object] = {}
        self.last_migration_backup: Path | None = None

    @property
    def unlocked(self) -> bool:
        return self._engine is not None

    @property
    def session_generation(self) -> int | None:
        """Identidad efímera de desbloqueo para cancelar trabajos de sesiones antiguas."""
        with self._lock:
            return self._session_generation if self._engine is not None else None

    @contextmanager
    def guard_session_generation(self, generation: int) -> Iterator[None]:
        """Impide bloquear/reabrir mientras una acción usa esta sesión.

        Reservado para acciones externas cortas: el bloqueo de la bóveda espera
        a que termine el tramo protegido, en vez de continuar con credenciales
        de una sesión anterior dentro de una sesión nueva.
        """
        with self._lock:
            if self._engine is None or self._session_generation != generation:
                raise VaultError("The real vault session changed")
            yield

    @contextmanager
    def try_guard_session_generation(self, generation: int) -> Iterator[bool]:
        """Registro oportunista de revocación: nunca retrasa STOP por un RPC.

        False no acredita escritura. El consumidor debe conservar la ausencia
        de final como interrupción, sin inventar un cierre del diario.
        """
        acquired = self._lock.acquire(blocking=False)
        try:
            yield acquired and self._engine is not None and self._session_generation == generation
        finally:
            if acquired:
                self._lock.release()

    def unlock(self, passphrase: str, *, create: bool = False,
               allow_legacy_for_verification: bool = False) -> None:
        if not isinstance(passphrase, str) or len(passphrase) < 12:
            raise VaultError("The vault passphrase must contain at least 12 characters")
        with self._lock:
            if self._engine is not None:
                raise VaultError("The real vault is already unlocked")
            exists = self.path.exists()
            if create and exists:
                raise VaultError("The real vault already exists")
            if not create and not exists:
                raise VaultError("No real vault exists at this path")
            if exists and not self.path.is_file():
                raise VaultError("The vault path is not a regular file")
            if exists:
                with self.path.open("rb") as vault_file:
                    if vault_file.read(16) == b"SQLite format 3\x00":
                        raise VaultError("Refusing to open an unencrypted real vault")
            else:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

            url = URL.create("sqlite+pysqlcipher", password=passphrase, database=str(self.path))
            engine = create_engine(url, connect_args={"timeout": 5})

            @event.listens_for(engine, "connect")
            def _secure_connection(dbapi_connection, connection_record) -> None:
                cursor = dbapi_connection.cursor()
                try:
                    cursor.execute("PRAGMA foreign_keys = ON")
                    cursor.execute("PRAGMA secure_delete = ON")
                    cursor.execute("PRAGMA temp_store = MEMORY")
                finally:
                    cursor.close()

            try:
                with engine.begin() as connection:
                    # Fuerza la comprobación de la clave antes de aceptar sesiones.
                    connection.exec_driver_sql("SELECT count(*) FROM sqlite_master").scalar_one()
                    integrity = connection.exec_driver_sql("PRAGMA cipher_integrity_check").scalars().all()
                    if integrity and integrity != ["ok"]:
                        raise VaultError("Encrypted vault integrity check failed")
                    version = connection.exec_driver_sql("PRAGMA user_version").scalar_one()
                    if create:
                        if version != 0:
                            raise VaultError("New vault contains an unexpected schema")
                        RealBase.metadata.create_all(connection)
                        connection.exec_driver_sql(f"PRAGMA user_version = {SCHEMA_VERSION}")
                    elif version not in range(1, SCHEMA_VERSION + 1):
                        raise VaultError(f"Unsupported real vault schema version: {version}")
                with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                    journal = connection.exec_driver_sql("PRAGMA journal_mode = DELETE").scalar_one()
                    if str(journal).lower() != "delete":
                        raise VaultError("Real vault requires a checkpointed DELETE journal")
                if exists and version < SCHEMA_VERSION and not allow_legacy_for_verification:
                    engine.dispose()
                    self._migrate_legacy(passphrase, version)
                    self.unlock(passphrase)
                    return
                self._engine = engine
                self._session_generation += 1
                if not exists:
                    self.path.chmod(0o600)
            except Exception as exc:
                engine.dispose()
                if isinstance(exc, VaultError):
                    raise
                raise VaultError("Cannot unlock the real vault; check the passphrase and file") from exc

    def _migrate_legacy(self, passphrase: str, version: int) -> None:
        """Migración sobre copia cifrada, con preimagen recuperable y verificación."""
        directory = self.path.parent / "backups"
        if directory.is_symlink():
            raise VaultError("Backup directory cannot be a symlink")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not directory.is_dir():
            raise VaultError("Backup directory is unavailable")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        nonce = uuid4().hex[:12]
        backup = directory / f"pre-migrate-v{version}-{stamp}-{nonce}.db"
        staged = self.path.with_name(f".{self.path.name}.migrate-{nonce}")
        try:
            self._copy_exclusive(self.path, backup)
            before = self._verify_portfolio_file(backup, passphrase, expected_version=version)
            observations_before = self._observation_counts(backup, passphrase)
            execution_before = self._execution_counts(backup, passphrase)
            self._copy_exclusive(backup, staged)
            candidate = RealVault(staged)
            candidate.unlock(passphrase, allow_legacy_for_verification=True)
            try:
                assert candidate._engine is not None
                with candidate._engine.begin() as connection:
                    if version == 1:
                        RealBase.metadata.tables["mexc_observed_fills"].create(connection)
                        RealBase.metadata.tables["mexc_observation_batches"].create(connection)
                    elif version == 2:
                        for name, kind in (("requested_start_ms", "INTEGER"),
                                           ("requested_end_ms", "INTEGER"),
                                           ("request_count", "INTEGER"),
                                           ("coverage", "VARCHAR(48)")):
                            connection.exec_driver_sql(f"ALTER TABLE mexc_observation_batches ADD COLUMN {name} {kind}")
                    if version <= 3:
                        RealBase.metadata.tables["mexc_transfer_snapshots"].create(connection, checkfirst=True)
                    for table in ("real_live_spot_policies", "real_live_spot_intents", "real_live_stops"):
                        RealBase.metadata.tables[table].create(connection, checkfirst=True)
                    columns = {row[1] for row in connection.exec_driver_sql(
                        "PRAGMA table_info(real_live_spot_intents)").all()}
                    for name in ("limit_price_quote", "quantity_base", "request_json",
                                 "signal_source_json", "signal_source_digest"):
                        if name not in columns:
                            connection.exec_driver_sql(
                                f"ALTER TABLE real_live_spot_intents ADD COLUMN {name} TEXT")
                    event_columns = {row[1] for row in connection.exec_driver_sql(
                        "PRAGMA table_info(real_events)").all()}
                    if "fee_asset" not in event_columns:
                        connection.exec_driver_sql("ALTER TABLE real_events ADD COLUMN fee_asset VARCHAR(12)")
                    RealBase.metadata.tables["real_live_order_observations"].create(connection, checkfirst=True)
                    RealBase.metadata.tables["real_mexc_account_links"].create(connection, checkfirst=True)
                    RealBase.metadata.tables["real_mexc_identity_salt"].create(connection, checkfirst=True)
                    RealBase.metadata.tables["real_mexc_uid_links"].create(connection, checkfirst=True)
                    RealBase.metadata.tables["real_mexc_view_bindings"].create(connection, checkfirst=True)
                    RealBase.metadata.tables["real_mexc_parity_observations"].create(connection, checkfirst=True)
                    RealBase.metadata.tables["real_mexc_open_orders_observations"].create(connection, checkfirst=True)
                    RealBase.metadata.tables["real_mexc_order_test_attempts"].create(connection, checkfirst=True)
                    RealBase.metadata.tables["real_wallet_identity_salt"].create(connection, checkfirst=True)
                    RealBase.metadata.tables["real_wallet_links"].create(connection, checkfirst=True)
                    RealBase.metadata.tables["real_mexc_fill_bookings"].create(connection, checkfirst=True)
                    RealBase.metadata.tables["real_live_spot_settlements"].create(connection, checkfirst=True)
                    RealBase.metadata.tables["grvt_order_evidence"].create(connection, checkfirst=True)
                    RealBase.metadata.tables["grvt_testnet_order_intents"].create(connection, checkfirst=True)
                    RealBase.metadata.tables["grvt_testnet_stops"].create(connection, checkfirst=True)
                    RealBase.metadata.tables["grvt_testnet_audit_evidence"].create(connection, checkfirst=True)
                    RealBase.metadata.tables["grvt_testnet_preflight_evidence"].create(connection, checkfirst=True)
                    RealBase.metadata.tables["grvt_testnet_cancel_attempts"].create(connection, checkfirst=True)
                    RealBase.metadata.tables["grvt_testnet_reconciliations"].create(connection, checkfirst=True)
                    for table in ("grvt_testnet_oco_groups", "grvt_testnet_oco_legs", "grvt_testnet_oco_claims", "grvt_testnet_oco_observations", "grvt_testnet_oco_cancels", "grvt_testnet_oco_audits", "grvt_testnet_oco_reconciliations"):
                        RealBase.metadata.tables[table].create(connection, checkfirst=True)
                    for table in ('grvt_testnet_bracket_groups','grvt_testnet_bracket_legs','grvt_testnet_bracket_claims','grvt_testnet_bracket_observations','grvt_testnet_bracket_cancels'):
                        RealBase.metadata.tables[table].create(connection,checkfirst=True)
                    for table in ('grvt_testnet_bracket_audits','grvt_testnet_bracket_audit_heads','grvt_testnet_bracket_reconciliations'):
                        RealBase.metadata.tables[table].create(connection,checkfirst=True)
                    RealBase.metadata.tables['grvt_testnet_bracket_emergencies'].create(connection,checkfirst=True)
                    RealBase.metadata.tables['grvt_testnet_strategy_closes'].create(connection,checkfirst=True)
                    RealBase.metadata.tables['grvt_testnet_bracket_strategies'].create(connection,checkfirst=True)
                    for table in ('grvt_testnet_exit_watches','grvt_testnet_exit_watch_events'):
                        RealBase.metadata.tables[table].create(connection,checkfirst=True)
                    RealBase.metadata.tables['grvt_testnet_bot_runs'].create(connection,checkfirst=True)
                    RealBase.metadata.tables['grvt_testnet_bot_cycles'].create(connection,checkfirst=True)
                    for table in ('grvt_testnet_stop_controls','grvt_testnet_stop_events'):
                        RealBase.metadata.tables[table].create(connection,checkfirst=True)
                    for table in ('grvt_mainnet_brackets','grvt_mainnet_legs','grvt_mainnet_controls','grvt_mainnet_observations'):
                        RealBase.metadata.tables[table].create(connection,checkfirst=True)
                    for table in ('grvt_mainnet_audits','grvt_mainnet_audit_heads','grvt_mainnet_reconciliations','grvt_mainnet_settlement_controls'):
                        RealBase.metadata.tables[table].create(connection,checkfirst=True)
                    for table in ('grvt_mainnet_recoveries','grvt_mainnet_recovery_events','grvt_mainnet_reductions'):
                        RealBase.metadata.tables[table].create(connection,checkfirst=True)
                    for table in ('grvt_mainnet_bot_runs','grvt_mainnet_bot_events','grvt_mainnet_bot_selections'):
                        RealBase.metadata.tables[table].create(connection,checkfirst=True)
                    for table in ('grvt_mainnet_bot_exits','grvt_mainnet_stop_origins','grvt_mainnet_entry_fills'):
                        RealBase.metadata.tables[table].create(connection,checkfirst=True)
                    for table in ('grvt_mainnet_limit_attempts','grvt_mainnet_limit_slots','grvt_mainnet_limit_events',
                                  'grvt_mainnet_limit_reductions'):
                        RealBase.metadata.tables[table].create(connection,checkfirst=True)
                    RealBase.metadata.tables["real_mexc_transfer_matches"].create(connection, checkfirst=True)
                    RealBase.metadata.tables["mexc_transfer_identity_evidence"].create(connection, checkfirst=True)
                    RealBase.metadata.tables["real_spot_bots"].create(connection, checkfirst=True)
                    RealBase.metadata.tables["real_spot_bot_decisions"].create(connection, checkfirst=True)
                    RealBase.metadata.tables["real_spot_bot_market_streams"].create(connection, checkfirst=True)
                    RealBase.metadata.tables["real_spot_bot_market_observations"].create(connection, checkfirst=True)
                    RealBase.metadata.tables["real_wallet_funding_intents"].create(connection, checkfirst=True)
                    RealBase.metadata.tables["real_wallet_funding_credits"].create(connection, checkfirst=True)
                    RealBase.metadata.tables["real_wallet_funding_bookings"].create(connection, checkfirst=True)
                    RealBase.metadata.tables["real_wallet_swap_intents"].create(connection, checkfirst=True)
                    RealBase.metadata.tables['real_dex_deployment_intents'].create(connection,checkfirst=True)
                    RealBase.metadata.tables['real_dex_control_intents'].create(connection,checkfirst=True)
                    RealBase.metadata.tables['real_dex_operator_grants'].create(connection,checkfirst=True)
                    RealBase.metadata.tables['real_dex_operator_intents'].create(connection,checkfirst=True)
                    RealBase.metadata.tables['real_dex_bot_bindings'].create(connection,checkfirst=True)
                    RealBase.metadata.tables['real_dex_gas_funding_intents'].create(connection,checkfirst=True)
                    RealBase.metadata.tables['real_dex_gas_return_intents'].create(connection,checkfirst=True)
                    RealBase.metadata.tables['real_dex_deployment_resolutions'].create(connection,checkfirst=True)
                    RealBase.metadata.tables['real_dex_native_resolutions'].create(connection,checkfirst=True)
                    RealBase.metadata.tables['real_dex_gas_cancellation_intents'].create(connection,checkfirst=True)
                    RealBase.metadata.tables['real_dex_wallet_cancellation_intents'].create(connection,checkfirst=True)
                    RealBase.metadata.tables['real_dex_call_resolutions'].create(connection,checkfirst=True)
                    RealBase.metadata.tables['real_dex_external_observations'].create(connection,checkfirst=True)
                    RealBase.metadata.tables['real_dex_external_order_proofs'].create(connection,checkfirst=True)
                    RealBase.metadata.tables['real_dex_receipt_order_proofs'].create(connection,checkfirst=True)
                    RealBase.metadata.tables['real_dex_gas_order_proofs'].create(connection,checkfirst=True)
                    for name in ('real_economic_acceptances', 'real_economic_acceptance_accounts'):
                        RealBase.metadata.tables[name].create(connection, checkfirst=True)
                    for name in ('real_dex_external_bookings','real_dex_external_booking_members',
                            'real_dex_external_booking_accounts','real_dex_external_acceptance_links'):
                        RealBase.metadata.tables[name].create(connection,checkfirst=True)
                    for name in ('real_dex_external_acceptance_groups','real_dex_external_group_links'):
                        RealBase.metadata.tables[name].create(connection,checkfirst=True)
                    for name in ('real_dex_receipt_bookings','real_dex_receipt_booking_events'):
                        RealBase.metadata.tables[name].create(connection,checkfirst=True)
                    RealBase.metadata.tables['real_dex_bot_runs'].create(connection,checkfirst=True)
                    RealBase.metadata.tables['real_dex_bot_decisions'].create(connection,checkfirst=True)
                    for name in ('real_dex_research_bindings', 'real_dex_research_authorizations'):
                        RealBase.metadata.tables[name].create(connection, checkfirst=True)
                    RealBase.metadata.tables["real_wallet_swap_bookings"].create(connection, checkfirst=True)
                    RealBase.metadata.tables["real_wallet_swap_booking_events"].create(connection, checkfirst=True)
                    RealBase.metadata.tables["real_mexc_withdrawals"].create(connection, checkfirst=True)
                    for name in ("real_mexc_withdrawal_observations","real_mexc_withdrawal_receipts","real_mexc_withdrawal_bookings"):
                        RealBase.metadata.tables[name].create(connection,checkfirst=True)
                    RealBase.metadata.tables["grvt_testnet_market_samples"].create(connection, checkfirst=True)
                    connection.exec_driver_sql(f"PRAGMA user_version = {SCHEMA_VERSION}")
            finally:
                candidate.lock()
            after = self._verify_portfolio_file(staged, passphrase, expected_version=SCHEMA_VERSION)
            observations_after = self._observation_counts(staged, passphrase)
            execution_after = self._execution_counts(staged, passphrase)
            if (before != after or observations_before != observations_after
                    or execution_before != execution_after):
                raise VaultError("Migrated vault changed protected row counts")
            os.replace(staged, self.path)
            self.last_migration_backup = backup
        except Exception as exc:
            # Una copia parcial queda cifrada y recuperable; jamás se elimina
            # automáticamente el archivo de origen ni la preimagen.
            if isinstance(exc, VaultError):
                raise
            raise VaultError("Real vault migration failed; original file was preserved") from exc

    def lock(self) -> None:
        with self._lock:
            self._dex_replay_cache.clear()
            self._grvt_perp_replay_cache.clear()
            if self._engine is not None:
                self._session_generation += 1
                self._engine.dispose()
                self._engine = None

    def backup(self, destination: Path) -> Path:
        """Copia consistente con VACUUM INTO; verifica cifrado y reapertura.

        Nunca sobrescribe una copia existente. El destino debe estar en una
        ruta elegida por el usuario y su contenido sigue cifrado con la misma
        contraseña de la bóveda origen.
        """
        destination = Path(destination).expanduser().resolve()
        with self._lock:
            if self._engine is None:
                raise VaultError("The real vault is locked")
            if destination == self.path or destination.exists():
                raise VaultError("Backup destination already exists or is the live vault")
            if not destination.parent.is_dir():
                raise VaultError("Backup destination directory does not exist")
            try:
                with self._engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                    connection.exec_driver_sql("VACUUM INTO ?", (str(destination),))
                with destination.open("rb") as backup_file:
                    if backup_file.read(16) == b"SQLite format 3\x00":
                        raise VaultError("Backup is not encrypted")
                destination.chmod(0o600)
                RealVault._verify_portfolio_file(destination, self._engine.url.password)
                return destination
            except Exception:
                # No se elimina un archivo parcial automáticamente: puede
                # contener datos recuperables. El usuario debe revisarlo.
                raise

    def verify_managed_backup(self, source: Path) -> dict:
        """Verifica una instantánea cifrada sin abrir ni alterar la copia original."""
        with self._lock:
            if self._engine is None or self._engine.url.password is None:
                raise VaultError("The real vault is locked")
            if source.is_symlink() or not source.is_file():
                raise VaultError("Backup must be a regular file")
            source = source.resolve()
            if source.parent != self.path.parent / "backups" or not source.name.endswith(".db"):
                raise VaultError("Backup must be inside the managed backups directory")
            try:
                with TemporaryDirectory(prefix=".verify-", dir=source.parent) as temporary:
                    snapshot = Path(temporary) / "snapshot.db"
                    self._copy_exclusive(source, snapshot)
                    with source.open("rb") as original, snapshot.open("rb") as copied:
                        if hashlib.file_digest(original, "sha256").digest() != hashlib.file_digest(copied, "sha256").digest():
                            raise VaultError("Backup changed during verification")
                    accounts, events = self._verify_portfolio_file(snapshot, self._engine.url.password)
                return {"verified": True, "name": source.name,
                        "accounts": accounts, "events": events,
                        "note": "Instantánea verificada; se comprobará de nuevo antes de restaurar"}
            except (OSError, PortfolioError, SQLAlchemyError, ValueError) as exc:
                raise VaultError("Backup could not be verified; the original was preserved") from exc

    @staticmethod
    def _copy_exclusive(source: Path, destination: Path) -> None:
        """Copia un archivo cifrado a nombre nuevo, completo y con permisos 0600."""
        with source.open("rb") as origin:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as target:
                shutil.copyfileobj(origin, target, length=1024 * 1024)
                target.flush()
                os.fsync(target.fileno())

    @staticmethod
    def _observation_counts(path: Path, passphrase: str) -> tuple[int, ...]:
        candidate = RealVault(path)
        candidate.unlock(passphrase, allow_legacy_for_verification=True)
        try:
            with candidate.session() as db:
                connection = db.connection()
                names = {row[0] for row in connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table'").all()}
                return tuple(connection.exec_driver_sql(f"SELECT count(*) FROM {name}").scalar_one()
                             if name in names else 0 for name in (
                                 "mexc_observed_fills", "mexc_observation_batches",
                                 "mexc_transfer_snapshots", "grvt_order_evidence",
                                 "mexc_transfer_identity_evidence",
                                 "real_mexc_parity_observations",
                                 "real_mexc_open_orders_observations"))
        finally:
            candidate.lock()

    @staticmethod
    def _execution_counts(path: Path, passphrase: str) -> tuple[int, ...]:
        candidate = RealVault(path)
        candidate.unlock(passphrase, allow_legacy_for_verification=True)
        try:
            with candidate.session() as db:
                connection = db.connection()
                names = {row[0] for row in connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table'").all()}
                return tuple(connection.exec_driver_sql(f"SELECT count(*) FROM {name}").scalar_one()
                             if name in names else 0 for name in (
                                 "real_live_spot_policies", "real_live_spot_intents",
                                 "real_live_stops", "real_live_order_observations",
                                 "real_mexc_account_links", "real_mexc_fill_bookings",
                                 "real_mexc_identity_salt", "real_mexc_uid_links", "real_mexc_view_bindings",
                                 "real_wallet_identity_salt", "real_wallet_links",
                                 "real_live_spot_settlements", "grvt_testnet_order_intents",
                                 "grvt_testnet_stops", "grvt_testnet_audit_evidence",
                                 "grvt_testnet_preflight_evidence",
                                 "grvt_testnet_cancel_attempts",
                                 "grvt_testnet_reconciliations",
                                 "grvt_testnet_oco_groups", "grvt_testnet_oco_legs", "grvt_testnet_oco_claims", "grvt_testnet_oco_observations",
                                 "grvt_testnet_oco_cancels",
                                 "grvt_testnet_oco_audits",
                                 "grvt_testnet_oco_reconciliations",
                                 'grvt_testnet_bracket_groups','grvt_testnet_bracket_legs','grvt_testnet_bracket_claims','grvt_testnet_bracket_observations','grvt_testnet_bracket_cancels',
                                 'grvt_testnet_bracket_audits','grvt_testnet_bracket_audit_heads','grvt_testnet_bracket_reconciliations',
                                 'grvt_testnet_bracket_emergencies', 'grvt_testnet_market_samples',
                                 'grvt_testnet_strategy_closes',
                                 'grvt_testnet_bracket_strategies',
                                 'grvt_testnet_exit_watches','grvt_testnet_exit_watch_events',
                                 'grvt_testnet_bot_runs',
                                 'grvt_testnet_bot_cycles',
                                 'grvt_testnet_stop_controls','grvt_testnet_stop_events',
                                 'grvt_mainnet_brackets','grvt_mainnet_legs','grvt_mainnet_controls','grvt_mainnet_observations',
                                 'grvt_mainnet_audits','grvt_mainnet_audit_heads','grvt_mainnet_reconciliations',
                                 'grvt_mainnet_settlement_controls',
                                 'grvt_mainnet_recoveries','grvt_mainnet_recovery_events','grvt_mainnet_reductions',
                                 'grvt_mainnet_bot_runs','grvt_mainnet_bot_events','grvt_mainnet_bot_selections',
                                 'grvt_mainnet_bot_exits','grvt_mainnet_stop_origins','grvt_mainnet_entry_fills',
                                 'grvt_mainnet_limit_attempts','grvt_mainnet_limit_slots','grvt_mainnet_limit_events',
                                 'grvt_mainnet_limit_reductions',
                                 "real_mexc_order_test_attempts",
                                 "real_mexc_transfer_matches", "real_spot_bots",
                                 "real_spot_bot_decisions", "real_spot_bot_market_streams","real_spot_bot_market_observations",
                                 "real_wallet_funding_intents",
                                 "real_wallet_funding_credits", "real_wallet_funding_bookings",
                                 "real_mexc_withdrawals","real_mexc_withdrawal_observations",
                                 "real_mexc_withdrawal_receipts","real_mexc_withdrawal_bookings",
                                 "real_wallet_swap_intents", "real_wallet_swap_bookings", "real_wallet_swap_booking_events",
                                 'real_dex_deployment_intents','real_dex_control_intents',
                                 'real_dex_operator_grants','real_dex_operator_intents','real_dex_bot_bindings',
                                 'real_dex_gas_funding_intents','real_dex_gas_return_intents','real_dex_bot_runs','real_dex_bot_decisions',
                                 'real_dex_receipt_bookings','real_dex_receipt_booking_events','real_dex_deployment_resolutions','real_dex_native_resolutions','real_dex_gas_cancellation_intents','real_dex_wallet_cancellation_intents','real_dex_call_resolutions',
                                 'real_dex_research_bindings','real_dex_research_authorizations','real_dex_receipt_order_proofs','real_dex_gas_order_proofs','real_dex_external_observations','real_dex_external_order_proofs',
                                 'real_dex_external_bookings','real_dex_external_booking_members','real_dex_external_booking_accounts','real_dex_external_acceptance_links',
                                 'real_dex_external_acceptance_groups','real_dex_external_group_links'))
        finally:
            candidate.lock()

    @staticmethod
    def _verify_portfolio_file(path: Path, passphrase: str, *,
                               expected_version: int | None = None) -> tuple[int, int]:
        candidate = RealVault(path)
        candidate.unlock(passphrase, allow_legacy_for_verification=True)
        try:
            with candidate.session() as db:
                connection = db.connection()
                version = connection.exec_driver_sql("PRAGMA user_version").scalar_one()
                if expected_version is not None and version != expected_version:
                    raise VaultError("Backup has an unexpected schema version")
                if connection.exec_driver_sql("PRAGMA quick_check").scalar_one() != "ok":
                    raise VaultError("Backup SQLite integrity check failed")
                if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
                    raise VaultError("Backup has broken account references")
                if version >= 81:
                    from app.live_execution.mexc_uid_guard import MexcUidGuardError, verify_mexc_view_binding
                    from app.portfolio.models import RealMexcViewBinding
                    try:
                        for binding in db.query(RealMexcViewBinding).all():
                            verify_mexc_view_binding(db, binding)
                    except MexcUidGuardError as exc:
                        raise VaultError("Backup has an invalid MEXC View binding") from exc
                has_acceptances = False
                if version >= 74:
                    try:
                        has_acceptances = connection.exec_driver_sql('SELECT 1 FROM real_economic_acceptances LIMIT 1').first() is not None
                    except SQLAlchemyError as exc:
                        raise VaultError('Backup has invalid economic acceptance schema') from exc
                for account in db.query(RealAccount).all():
                    try:
                        events = (list_events(db, account.id) if version >= 18
                                  else RealVault._legacy_portfolio_events(connection, account.id))
                        fold_events(account.id, events)
                    except PortfolioError as exc:
                        if has_acceptances:
                            raise VaultError('Backup has invalid economic acceptance or current accounting evidence') from exc
                        raise
                if version >= 18:
                    verify_paired_transfers(db)
                if version >= 20:
                    try:
                        for row in db.query(RealMexcParityObservation).all():
                            report = read_observation(row)
                            if "causal" in report:
                                from app.live_execution.mexc_continuity_review import MexcContinuityError, verify_continuity_review
                                try:
                                    verify_continuity_review(db, candidate, row)
                                except MexcContinuityError as exc:
                                    raise VaultError("Backup has invalid MEXC causal evidence") from exc
                    except MexcParityArchiveError as exc:
                        raise VaultError("Backup has invalid MEXC parity evidence") from exc
                if version >= 21:
                    try:
                        for row in db.query(RealMexcOpenOrdersObservation).all():
                            read_open_orders_observation(row)
                    except MexcOpenOrdersArchiveError as exc:
                        raise VaultError("Backup has invalid MEXC open-order evidence") from exc
                if version >= 22:
                    try:
                        for row in db.query(RealMexcOrderTestAttempt).all():
                            read_attempt(row)
                    except MexcOrderTestArchiveError as exc:
                        raise VaultError("Backup has invalid MEXC order-test evidence") from exc
                if version >= 23:
                    try:
                        for row in db.query(RealMexcTransferMatch).all():
                            verify_mexc_transfer_match(db, row)
                    except PortfolioError as exc:
                        raise VaultError("Backup has invalid MEXC transfer matches") from exc
                if version >= 24:
                    try:
                        from app.portfolio.mexc_transfers import verify_transfer_identity_evidence
                        for row in db.query(MexcTransferIdentityEvidence).all():
                            verify_transfer_identity_evidence(db, row)
                    except PortfolioError as exc:
                        raise VaultError("Backup has invalid MEXC transfer identity evidence") from exc
                if version >= 25:
                    try:
                        from app.live_execution.spot_bot_store import SpotBotStoreError, bot_record, bot_decision_payload
                        from app.portfolio.models import RealSpotBot, RealSpotBotDecision
                        for row in db.query(RealSpotBot).all():
                            bot_record(db, row.id)
                        for row in db.query(RealSpotBotDecision).all():
                            payload = bot_decision_payload(db,row)
                            if payload.get("bot_id") != row.bot_id or payload.get("intent_id") != row.intent_id:
                                raise SpotBotStoreError("Bot decision scope changed")
                    except (SpotBotStoreError, TypeError, ValueError) as exc:
                        raise VaultError("Backup has invalid REAL bot evidence") from exc
                if version >= 31:
                    try:
                        from app.live_execution.spot_bot_market import read_market_stream
                        from app.portfolio.models import RealSpotBotMarketStream
                        for stream in db.query(RealSpotBotMarketStream).all():
                            read_market_stream(db,stream.policy_id)
                    except (SpotBotStoreError,ValueError,TypeError) as exc:
                        raise VaultError("Backup has invalid REAL bot market evidence") from exc
                if version >= 26:
                    try:
                        from app.portfolio.wallet_funding import funding_record
                        from app.portfolio.models import RealWalletFundingIntent
                        for row in db.query(RealWalletFundingIntent).all():
                            funding_record(db, row.id)
                    except (PortfolioError,ValueError,TypeError) as exc:
                        raise VaultError("Backup has invalid wallet funding evidence") from exc
                if version >= 32:
                    try:
                        from app.portfolio.wallet_swap import swap_record
                        from app.portfolio.models import RealWalletSwapIntent
                        for row in db.query(RealWalletSwapIntent).all():
                            swap_record(db, row.id)
                    except (PortfolioError, ValueError, TypeError) as exc:
                        raise VaultError("Backup has invalid wallet swap evidence") from exc
                if version >= 33:
                    try:
                        from app.portfolio.wallet_swap_booking import booking_record
                        from app.portfolio.models import RealWalletSwapBooking
                        for row in db.query(RealWalletSwapBooking).all():
                            booking_record(db, row.action_id)
                    except (PortfolioError, ValueError, TypeError) as exc:
                        raise VaultError("Backup has invalid wallet swap booking evidence") from exc
                if version >= 27:
                    try:
                        from app.portfolio.wallet_funding_credit import credit_record, booking_record
                        from app.portfolio.models import RealWalletFundingCredit, RealWalletFundingBooking
                        for row in db.query(RealWalletFundingCredit).all():
                            credit_record(db,row.funding_id,check_history=False)
                        for row in db.query(RealWalletFundingBooking).all():
                            booking_record(db,row.funding_id,check_history=False)
                    except (PortfolioError,ValueError,TypeError) as exc:
                        raise VaultError("Backup has invalid wallet funding credit or booking evidence") from exc
                if version >= 28:
                    try:
                        from app.portfolio.mexc_withdrawal_journal import withdrawal_record
                        from app.portfolio.models import RealMexcWithdrawal
                        for row in db.query(RealMexcWithdrawal).all():
                            withdrawal_record(db,row.id)
                    except (PortfolioError,ValueError,TypeError) as exc:
                        raise VaultError("Backup has invalid MEXC withdrawal evidence") from exc
                if version >= 29:
                    try:
                        from app.portfolio.mexc_withdrawal_observation import observation_record
                        from app.portfolio.mexc_withdrawal_receipt import receipt_record
                        from app.portfolio.mexc_withdrawal_booking import booking_record
                        from app.portfolio.models import RealMexcWithdrawalObservation,RealMexcWithdrawalReceipt,RealMexcWithdrawalBooking
                        for row in db.query(RealMexcWithdrawalObservation).all():
                            observation_record(db,row.id)
                        for row in db.query(RealMexcWithdrawalReceipt).all():
                            receipt_record(db,row.withdrawal_id,check_history=False)
                        for row in db.query(RealMexcWithdrawalBooking).all():
                            booking_record(db,row.withdrawal_id,check_history=False)
                    except (PortfolioError,ValueError,TypeError) as exc:
                        raise VaultError("Backup has invalid MEXC withdrawal receipt or booking") from exc
                if version >= 30:
                    try:
                        from app.live_execution.grvt_testnet_preflight import (
                            verified_archived_preflight, GrvtTestnetPreflightError)
                        from app.live_execution.grvt_testnet_reconciliation import (
                            require_reconciled_continuity, GrvtTestnetReconciliationError)
                        from app.live_execution.grvt_testnet_evidence import GrvtTestnetEvidenceError
                        from app.portfolio.models import GrvtTestnetPreflightEvidence, GrvtTestnetReconciliation
                        for row in db.query(GrvtTestnetPreflightEvidence).all():
                            verified_archived_preflight(db,row)
                        for sub_account_id, in db.query(GrvtTestnetReconciliation.sub_account_id).distinct().all():
                            require_reconciled_continuity(db,sub_account_id)
                    except (GrvtTestnetPreflightError, GrvtTestnetReconciliationError,
                            GrvtTestnetEvidenceError,ValueError,TypeError) as exc:
                        raise VaultError("Backup has invalid GRVT testnet history or reconciliation evidence") from exc
                if version >= 34:
                    try:
                        from app.live_execution.grvt_testnet_oco import verify_all_oco, GrvtTestnetOcoError
                        verify_all_oco(db, include_cancels=version >= 35, include_finance=version >= 36)
                    except (GrvtTestnetOcoError, ValueError, TypeError) as exc:
                        raise VaultError("Backup has invalid GRVT OCO evidence") from exc
                if version >= 37:
                    try:
                        from app.live_execution.grvt_testnet_bracket_journal import verify_all_brackets
                        from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
                        verify_all_brackets(db)
                        if version >= 42:
                            from app.live_execution.grvt_testnet_strategy_close import verify_all_strategy_closes
                            verify_all_strategy_closes(db)
                        if version >= 43:
                            from app.live_execution.grvt_bracket_strategy_snapshot import verify_all_bracket_strategies
                            verify_all_bracket_strategies(db)
                        if version >= 45:
                            from app.live_execution.grvt_exit_watch_archive import verify_all_exit_watches
                            verify_all_exit_watches(db)
                        if version >= 46:
                            from app.live_execution.grvt_bot_archive import verify_all_bot_runs
                            verify_all_bot_runs(db)
                        if version >= 47:
                            from app.live_execution.grvt_stop_control import verify_all_testnet_stop_controls
                            verify_all_testnet_stop_controls(db)
                    except (GrvtTestnetBracketError,ValueError,TypeError) as exc:
                        raise VaultError('Backup has invalid GRVT linked entry evidence') from exc
                if version >= 82:
                    from app.live_execution.grvt_testnet_market_archive import verify_all_market_samples
                    from app.live_execution.grvt_testnet_market_probe import MarketProbeError
                    try:
                        verify_all_market_samples(db)
                    except (MarketProbeError, SQLAlchemyError) as exc:
                        raise VaultError("Backup has invalid GRVT Testnet market samples") from exc
                if version >= 50:
                    try:
                        from app.live_execution.grvt_mainnet_journal import verify_all_mainnet_brackets
                        from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError
                        verify_all_mainnet_brackets(db)
                        if version >= 80:
                            from app.live_execution.grvt_perp_limit_journal import verify_all_limit_attempts
                            verify_all_limit_attempts(db)
                    except (GrvtMainnetError,ValueError,TypeError) as exc:
                        raise VaultError('Backup has invalid GRVT MAINNET entry evidence') from exc
                if version >= 53:
                    try:
                        from app.live_execution.grvt_mainnet_bot_entry import verify_all_bot_grants
                        verify_all_bot_grants(db)
                    except (GrvtMainnetError,ValueError,TypeError) as exc:
                        raise VaultError('Backup has invalid GRVT MAINNET bot evidence') from exc
                if version >= 54:
                    try:
                        from app.live_execution.grvt_mainnet_bot_exit import verify_all_bot_exits
                        from app.live_execution.grvt_mainnet_stop_origin import verify_all_stop_origins
                        verify_all_bot_exits(db);verify_all_stop_origins(db)
                    except (GrvtMainnetError,ValueError,TypeError) as exc:
                        raise VaultError('Backup has invalid GRVT MAINNET exit or STOP evidence') from exc
                if version >= 56:
                    try:
                        from app.live_execution.grvt_mainnet_entry_fills import verify_all_entry_fills
                        verify_all_entry_fills(db)
                    except (GrvtMainnetError,ValueError,TypeError) as exc:
                        raise VaultError('Backup has invalid GRVT MAINNET entry fills') from exc
                if version >= 58:
                    try:
                        from app.portfolio.models import RealDexDeploymentIntent
                        from app.portfolio.dex_deployments import deployment_record
                        for row in db.query(RealDexDeploymentIntent).all():deployment_record(db,row.id)
                    except (PortfolioError,ValueError,TypeError,SQLAlchemyError) as exc:
                        raise VaultError('Backup has invalid DEX deployment evidence') from exc
                if version >= 59:
                    try:
                        from app.portfolio.models import RealDexControlIntent
                        from app.portfolio.dex_controls import control_record
                        for row in db.query(RealDexControlIntent).all():control_record(db,row.id)
                    except (PortfolioError,ValueError,TypeError,SQLAlchemyError) as exc:
                        raise VaultError('Backup has invalid DEX control evidence') from exc
                try:
                    from app.portfolio.dex_operator_journal import verify_all_operator_records
                    verify_all_operator_records(db, version=version)
                except (PortfolioError,ValueError,TypeError,SQLAlchemyError) as exc:
                    raise VaultError('Backup has invalid DEX operator evidence') from exc
                try:
                    from app.portfolio.dex_bot_bindings import verify_all_bindings
                    verify_all_bindings(db, version=version)
                except (PortfolioError,ValueError,TypeError,SQLAlchemyError) as exc:
                    raise VaultError('Backup has invalid DEX bot binding evidence') from exc
                try:
                    from app.portfolio.dex_gas_funding import verify_all_funding
                    verify_all_funding(db,version=version)
                except (PortfolioError,ValueError,TypeError,SQLAlchemyError) as exc:
                    raise VaultError('Backup has invalid DEX gas funding evidence') from exc
                try:
                    from app.portfolio.dex_bot_journal import verify_all_runs
                    verify_all_runs(db,version=version)
                except (PortfolioError,ValueError,TypeError,SQLAlchemyError) as exc:
                    raise VaultError('Backup has invalid causal DEX bot evidence') from exc
                try:
                    from app.portfolio.dex_gas_return import verify_all_returns
                    verify_all_returns(db,version=version)
                except (PortfolioError,ValueError,TypeError,SQLAlchemyError) as exc:
                    raise VaultError('Backup has invalid DEX gas return evidence') from exc
                try:
                    from app.portfolio.dex_research import verify_all_research
                    verify_all_research(db, version=version)
                except (PortfolioError,ValueError,TypeError,SQLAlchemyError) as exc:
                    raise VaultError('Backup has invalid DEX research qualification') from exc
                try:
                    from app.portfolio.dex_deployment_resolution import verify_all_resolutions
                    verify_all_resolutions(db,version=version)
                except (PortfolioError,ValueError,TypeError,SQLAlchemyError) as exc:
                    raise VaultError('Backup has invalid DEX replacement resolution evidence') from exc
                try:
                    from app.portfolio.dex_native_resolution import verify_all_resolutions as verify_native_resolutions
                    verify_native_resolutions(db,version=version)
                except (PortfolioError,ValueError,TypeError,SQLAlchemyError) as exc:
                    raise VaultError('Backup has invalid DEX native replacement resolution evidence') from exc
                try:
                    from app.portfolio.dex_gas_cancellation import verify_all_cancellations
                    verify_all_cancellations(db,version=version)
                except (PortfolioError,ValueError,TypeError,SQLAlchemyError) as exc:
                    raise VaultError('Backup has invalid DEX cancellation evidence') from exc
                try:
                    from app.portfolio.dex_wallet_cancellation import verify_all_cancellations as verify_wallet_cancellations
                    verify_wallet_cancellations(db,version=version)
                except (PortfolioError,ValueError,TypeError,SQLAlchemyError) as exc:
                    raise VaultError('Backup has invalid DEX wallet cancellation evidence') from exc
                try:
                    from app.portfolio.dex_call_resolution import verify_all_resolutions as verify_call_resolutions
                    verify_call_resolutions(db,version=version)
                except (PortfolioError,ValueError,TypeError,SQLAlchemyError) as exc:
                    raise VaultError('Backup has invalid DEX call resolution evidence') from exc
                try:
                    from app.portfolio.dex_external_observation import verify_all_observations
                    verify_all_observations(db,version=version)
                except (PortfolioError,ValueError,TypeError,SQLAlchemyError) as exc:
                    raise VaultError('Backup has invalid DEX external observation evidence') from exc
                try:
                    from app.portfolio.dex_receipt_booking import verify_all_bookings
                    verify_all_bookings(db,version=version)
                except (PortfolioError,ValueError,TypeError,SQLAlchemyError) as exc:
                    raise VaultError('Backup has invalid DEX receipt booking evidence') from exc
                try:
                    from app.portfolio.dex_receipt_order import verify_all_order_records
                    verify_all_order_records(db,version=version)
                except (PortfolioError,ValueError,TypeError,SQLAlchemyError) as exc:
                    raise VaultError('Backup has invalid DEX receipt order evidence') from exc
                try:
                    from app.portfolio.economic_acceptance import verify_all_acceptances
                    verify_all_acceptances(db, version=version)
                except (PortfolioError,ValueError,TypeError,SQLAlchemyError) as exc:
                    raise VaultError('Backup has invalid economic acceptance evidence') from exc
                try:
                    from app.portfolio.economic_external_booking import verify_all_external_bookings
                    verify_all_external_bookings(db,version=version)
                    from app.portfolio.economic_external_group import verify_all_groups
                    verify_all_groups(db,version=version)
                except (PortfolioError,ValueError,TypeError,SQLAlchemyError) as exc:
                    raise VaultError('Backup has invalid external financial review evidence') from exc
                return (db.query(RealAccount).count(),
                        connection.exec_driver_sql("SELECT count(*) FROM real_events").scalar_one())
        finally:
            candidate.lock()

    @staticmethod
    def _legacy_portfolio_events(connection, account_id: str) -> list[PortfolioEvent]:
        """Reconstruye la preimagen v1–v17 antes de añadir `fee_asset`.

        La verificación de la copia cifrada no puede consultar el modelo ORM
        nuevo contra una tabla anterior a la migración.
        """
        rows = connection.exec_driver_sql(
            "SELECT id, request_id, recorded_at, kind, asset, quote_currency, "
            "quantity, unit_price, gross_amount, fee, occurred_at, note "
            "FROM real_events WHERE account_id = ?",
            (account_id,),
        ).mappings().all()
        events = []
        for row in rows:
            quantity = Decimal(row["quantity"]) if row["quantity"] is not None else None
            price = Decimal(row["unit_price"]) if row["unit_price"] is not None else None
            gross = Decimal(row["gross_amount"])
            override = None
            if row["kind"] in ("BUY", "SELL"):
                with localcontext() as context:
                    context.prec = 100
                    if gross != quantity * price:
                        override = gross
            item = EventInput(
                kind=row["kind"], asset=row["asset"], quote_currency=row["quote_currency"],
                quantity=quantity, unit_price=price,
                amount=gross if row["kind"] in ("CONTRIBUTION", "WITHDRAWAL") else None,
                gross_override=override, fee=Decimal(row["fee"]), note=row["note"],
                occurred_at=datetime.fromisoformat(row["occurred_at"].replace("Z", "+00:00")),
            )
            events.append(PortfolioEvent(
                id=row["id"], account_id=account_id, request_id=row["request_id"],
                recorded_at=datetime.fromisoformat(row["recorded_at"].replace("Z", "+00:00")),
                input=item,
            ))
        return events

    def restore_backup(self, source: Path) -> Path:
        """Restaura una copia propia, conservando la bóveda anterior cifrada.

        Requiere la bóveda abierta. Sólo acepta archivos regulares directos de
        su carpeta `backups`; jamás una ruta arbitraria proporcionada por la
        UI. Al terminar queda bloqueada y devuelve la ruta de recuperación.
        """
        with self._lock:
            if self._engine is None:
                raise VaultError("The real vault is locked")
            if source.is_symlink() or not source.is_file():
                raise VaultError("Backup must be a regular file")
            source = source.resolve()
            if source.parent != self.path.parent / "backups" or not source.name.endswith(".db"):
                raise VaultError("Backup must be inside the managed backups directory")
            passphrase = self._engine.url.password
            if passphrase is None:
                raise VaultError("Vault key is unavailable")
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            nonce = uuid4().hex[:12]
            staged = self.path.with_name(f".{self.path.name}.restore-{nonce}")
            recovery = source.parent / f"pre-restore-{stamp}-{nonce}.db"

            # La fuente y la copia preparada deben abrirse y reproducir el
            # libro antes de retirar el fichero activo de la ruta de trabajo.
            self._verify_portfolio_file(source, passphrase)
            try:
                self._copy_exclusive(source, staged)
                self._verify_portfolio_file(staged, passphrase)
                self.lock()
                self._copy_exclusive(self.path, recovery)
                self._verify_portfolio_file(recovery, passphrase)
                os.replace(staged, self.path)
                return recovery
            except (OSError, PortfolioError) as exc:
                # El origen y el fichero activo se conservan si la validación
                # o el reemplazo falla. Cualquier copia parcial queda cifrada
                # para revisión manual, sin borrado silencioso.
                raise VaultError("Restore failed; the current vault was preserved") from exc

    @contextmanager
    def session(self) -> Iterator[Session]:
        with self._lock:
            if self._engine is None:
                raise VaultError("The real vault is locked")
            # Mantener el cerrojo hasta cerrar la sesión impide que el timer
            # de bloqueo descarte el motor en mitad de una escritura.
            with Session(self._engine) as db:
                db.info['orbis_dex_replay_cache'] = self._dex_replay_cache
                db.info['orbis_grvt_perp_replay_cache'] = self._grvt_perp_replay_cache
                yield db
