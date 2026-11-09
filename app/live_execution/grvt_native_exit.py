"""Reconoce y concilia salidas OTOCO ya ejecutadas. Nunca envía órdenes.

Los estados terminales sólo permiten iniciar la auditoría. COMPLETE exige
dos lecturas financieras estables y el recibo plano del grupo original.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, localcontext
import time
from typing import TYPE_CHECKING

from app.live_execution.grvt_filled_strategy_exit import GrvtStrategyExitError
from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError,native_partial_role,native_recovery_reason
from app.live_execution.grvt_testnet_bracket_journal import read_bracket
from app.live_execution.grvt_testnet_bracket_observation import observe_bracket_orders
from app.live_execution.grvt_testnet_order import GrvtTestnetOrderClient
from app.portfolio.vault import RealVault, VaultError

if TYPE_CHECKING:
    from app.live_execution.grvt_testnet_strategy_close import StrategyCloseProgress


_TERMINAL = {'FILLED','CANCELLED','REJECTED'}
NATIVE_CHECK_INTERVAL_NS = 5_000_000_000


@dataclass(frozen=True, slots=True)
class NativeGroupExit:
    parent_client_order_id: str
    observed_at_ns: int
    roles: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NativePartialExit:
    parent_client_order_id: str
    observed_at_ns: int
    role: str


@dataclass(frozen=True, slots=True)
class NativeRecoveryExit:
    parent_client_order_id: str
    observed_at_ns: int
    reason: str


class NativeExitObserved(GrvtStrategyExitError):
    """La evaluación de estrategia encontró una salida nativa, no otro CLOSE."""
    def __init__(self, outcome: NativeGroupExit | NativePartialExit | NativeRecoveryExit):
        super().__init__('Salida nativa observada; requiere conciliación del grupo')
        self.outcome = outcome


def classify_native_exit(parent: str, observed: dict) -> NativeGroupExit | NativePartialExit | NativeRecoveryExit | None:
    """Sólo acepta estados ya verificados por el lector de los tres contratos."""
    rows = observed['orders']
    recovery=native_recovery_reason(observed)
    if recovery is not None:
        return NativeRecoveryExit(parent,observed['observed_at_ns'],recovery)
    role=native_partial_role(observed)
    if role is not None:
        return NativePartialExit(parent,observed['observed_at_ns'],role)
    if observed['needs_review'] or not observed['entry_fully_filled']:
        raise GrvtTestnetBracketError('Estados nativos requieren revisión; no se envía otro cierre')
    with localcontext() as context:
        context.prec = 100
        entry = Decimal(rows[0]['traded_size'])
        closed = sum((Decimal(row['traded_size']) for row in rows[1:]), Decimal(0))
    if closed == 0:
        return None
    if entry <= 0 or entry != closed:
        raise GrvtTestnetBracketError('Salida nativa parcial o contradictoria')
    return NativeGroupExit(parent,observed['observed_at_ns'],tuple(row['role'] for row in rows[1:]
        if Decimal(row['traded_size']) > 0))


def inspect_native_exit(vault: RealVault, client: GrvtTestnetOrderClient, parent: str, *,
        clock_ns: Callable[[], int]) -> NativeGroupExit | NativePartialExit | NativeRecoveryExit | None:
    from app.live_execution.grvt_testnet_strategy_close import _exclusive
    generation = vault.session_generation
    if generation is None:
        raise VaultError('Bóveda bloqueada')
    with _exclusive(vault), vault.guard_session_generation(generation):
        clock_ns()
        group = read_bracket(vault,parent)
        if group['emergency_attempts']:
            raise GrvtTestnetBracketError('El grupo tiene reducciones que necesitan su propio recibo')
        return classify_native_exit(parent,observe_bracket_orders(vault,client,parent,clock_ns=clock_ns))


def settle_native_exit(vault: RealVault, client: GrvtTestnetOrderClient, parent: str, *,
        deadline_ns: int, clock_ns: Callable[[], int], wait: Callable[[float], bool],
        on_progress: Callable[[StrategyCloseProgress], None]) -> StrategyCloseProgress:
    """Lecturas y recibo local, dentro del permiso/revocación del llamador.

    No cancela una salida pendiente del exchange, no reduce un parcial, no
    usa una firma y no crea solicitudes CLOSE de la estrategia.
    """
    from app.live_execution.grvt_testnet_bracket_finance import read_bracket_audits,audit_bracket,reconcile_bracket
    from app.live_execution.grvt_testnet_strategy_close import _exclusive,_progress,_datetime
    from app.live_execution.grvt_testnet_journal import stop_testnet_subaccount
    generation = vault.session_generation
    if generation is None:
        raise VaultError('Bóveda bloqueada')
    monotonic_deadline = time.monotonic()+max(0,(deadline_ns-clock_ns())/1_000_000_000)
    while True:
        clock_ns()
        if time.monotonic() >= monotonic_deadline:
            return _progress(parent,'EXPIRED')
        with _exclusive(vault), vault.guard_session_generation(generation):
            group = read_bracket(vault,parent)
            stop_testnet_subaccount(vault,group['sub_account_id'],now=_datetime(clock_ns()),
                reason='AUTO_EXIT',parent_client_order_id=parent)
            if group['emergency_attempts']:
                raise GrvtTestnetBracketError('El grupo cambió durante la revisión nativa')
            if group['reconciliation'] is not None:
                if group['reconciliation']['result']['positions']:
                    raise GrvtTestnetBracketError('El recibo del grupo conserva una posición')
                return _progress(parent,'COMPLETE','0')
            state = observe_bracket_orders(vault,client,parent,clock_ns=clock_ns)
            if not isinstance(classify_native_exit(parent,state),NativeGroupExit):
                raise GrvtTestnetBracketError('La salida nativa ya no se verifica')
            at = clock_ns()
            if any(row['status'] not in _TERMINAL for row in state['orders']):
                status = 'WAITING_AUDIT'
            else:
                audits = read_bracket_audits(vault,parent)
                if audits and at-audits[-1]['observed_at_ns'] < NATIVE_CHECK_INTERVAL_NS:
                    status = 'WAITING_AUDIT'
                else:
                    audited = audit_bracket(vault,client,parent,clock_ns=clock_ns)
                    if audited['result']['positions']:
                        raise GrvtTestnetBracketError('Las ejecuciones nativas no explican una cuenta plana')
                    if audits:
                        receipt = reconcile_bracket(vault,parent,now_ns=clock_ns())
                        if receipt['result']['positions']:
                            raise GrvtTestnetBracketError('La conciliación nativa conserva una posición')
                        return _progress(parent,'COMPLETE','0')
                    status = 'AUDITING_GROUP'
        on_progress(_progress(parent,status))
        if wait(5):
            return _progress(parent,'STOPPED')
        clock_ns()  # Revocación/TTL: no reconstruye ni reinicia por sí solo.
