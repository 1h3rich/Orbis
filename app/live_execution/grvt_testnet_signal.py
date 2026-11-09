"""Vincula una acción GRVT PAPER reciente con un único intento testnet.

No configura credenciales. La transacción PAPER mantiene
estable la señal mientras el transporte testnet reclama su journal duradero.
"""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from decimal import Decimal
import json
import re
import secrets
import time
from typing import Callable

from app.derivatives.paper_store import PaperPerpStore
from app.lab.engine import supports_engine_version, validate_strategy
from app.lab.paper_signal_replay import verify_paper_signals
from app.lab.store import LabStore
from app.live_execution.grvt_testnet_order import GrvtTestnetLimits, GrvtTestnetOrderClient, _validate_order
from app.live_execution.grvt_testnet_journal import require_new_testnet_submission
from app.live_execution.grvt_testnet_preflight import client_id_for_signal
from app.live_execution.grvt_testnet_quote import (
    GrvtTestnetPriceGuard, GrvtTestnetQuoteError, fetch_testnet_quote,
    guard_testnet_limit_price,
)
from app.live_execution.grvt_testnet_signer import (
    GrvtTestnetLimitDraft, fetch_testnet_instrument, sign_testnet_limit,
    verify_testnet_limit_from_public_instrument, verify_testnet_limit_signature,
)
from app.live_execution.grvt_testnet_submit import submit_testnet_once
from app.marketdata.adapters.grvt import GrvtAdapter
from app.portfolio.vault import RealVault


from app.live_execution.grvt_paper_binding import (
    SIGNAL_MAX_AGE_NS,reviewed_paper_signal,GrvtPaperSignalError as GrvtTestnetSignalError,
    GrvtPaperSignalBinding as GrvtTestnetSignalBinding,
)

def list_testnet_signal_candidates(paper: PaperPerpStore, *, now_ns: int) -> list[dict]:
    """Selector local acotado, no autorización ni sustituto del replay al enviar.

    Sólo la última acción de cada sesión automática puede aparecer. No devuelve
    fondos PAPER ni su tamaño como propuesta de capital testnet.
    """
    if type(now_ns) is not int or now_ns <= 0:
        raise GrvtTestnetSignalError("Reloj de señal inválido")
    items = []
    with paper.connect() as db:
        db.execute("BEGIN")
        for session in db.execute("""SELECT s.*,a.name AS account_name,a.stopped_at
                FROM paper_signal_sessions s JOIN paper_perp_accounts a ON a.id=s.account_id
                WHERE s.execution_mode='AUTO_PAPER' ORDER BY s.started_at_ns DESC,s.id LIMIT 100"""):
            action = db.execute("""SELECT * FROM paper_signal_actions WHERE session_id=?
                ORDER BY recorded_at_ns DESC,candle_t DESC LIMIT 1""", (session["id"],)).fetchone()
            if action is None or action["status"] != "EXECUTED":
                continue
            try:
                definition = validate_strategy(json.loads(session["definition_json"]))
                instrument = GrvtAdapter()._instrument(session["symbol"])
                if instrument is None:
                    continue
                client_id = client_id_for_signal(session["id"], action["request_id"])
            except (ValueError, TypeError, KeyError):
                continue
            position = db.execute("SELECT closed_at_ns FROM paper_perp_positions WHERE id=? AND account_id=?",
                                  (action["position_id"], session["account_id"])).fetchone()
            opening = action["action"] == "OPEN"
            age = now_ns - action["recorded_at_ns"]
            reason = "REVALIDATE_ON_SUBMIT"
            if not 0 <= age <= SIGNAL_MAX_AGE_NS:
                reason = "EXPIRED"
            elif session["status"] not in ("ACTIVE", "STOPPED", "GAP") or (
                    opening and (session["status"] != "ACTIVE" or session["stopped_at"] is not None)):
                reason = "STOPPED"
            elif position is None or (opening and (position["closed_at_ns"] is not None or
                    session["position_id"] != action["position_id"])) or (
                    not opening and position["closed_at_ns"] is None):
                reason = "POSITION_CHANGED"
            buying = (definition["direction"] == "LONG") == opening
            items.append({"session_id": session["id"], "action_request_id": action["request_id"],
                          "client_order_id": client_id, "account_name": session["account_name"],
                          "version_id": session["version_id"], "symbol": session["symbol"],
                          "timeframe": session["timeframe"], "instrument": instrument,
                          "direction": definition["direction"], "action": action["action"],
                          "side": "BUY" if buying else "SELL", "reduce_only": not opening,
                          "candidate": reason == "REVALIDATE_ON_SUBMIT", "reason": reason,
                          "expires_at_ms": (action["recorded_at_ns"] + SIGNAL_MAX_AGE_NS) // 1_000_000})
    return items


def submit_signal_testnet_once(
    paper: PaperPerpStore, lab: LabStore, vault: RealVault,
    client: GrvtTestnetOrderClient, order: dict, limits: GrvtTestnetLimits,
    *, session_id: str, action_request_id: str,
    clock_ns: Callable[[], int] = time.time_ns,
    submitter: Callable = submit_testnet_once,
    signature_verifier: Callable = verify_testnet_limit_from_public_instrument,
    pre_claim_guard: Callable[[], GrvtTestnetPriceGuard] | None = None,
) -> tuple[GrvtTestnetSignalBinding, object]:
    """Puerta IOC existente; no admite FOK ni otro contrato silenciosamente."""
    return _submit_verified_signal_once(paper,lab,vault,client,order,limits,
        session_id=session_id,action_request_id=action_request_id,clock_ns=clock_ns,
        submitter=submitter,signature_verifier=signature_verifier,pre_claim_guard=pre_claim_guard,
        contract_validator=_validate_order)


def _submit_verified_signal_once(
    paper: PaperPerpStore, lab: LabStore, vault: RealVault,
    client: GrvtTestnetOrderClient, order: dict, limits: GrvtTestnetLimits,
    *, session_id: str, action_request_id: str, contract_validator: Callable,
    clock_ns: Callable[[], int] = time.time_ns,
    submitter: Callable = submit_testnet_once,
    signature_verifier: Callable = verify_testnet_limit_from_public_instrument,
    pre_claim_guard: Callable[[], GrvtTestnetPriceGuard] | None = None,
    include_signal_timing: bool = False,
    include_strategy_context: bool = False,
) -> tuple[GrvtTestnetSignalBinding, object]:
    """Revalida PAPER y LAB antes de delegar el único POST al transporte testnet.

    El tamaño/precio son independientes de los fondos PAPER; el transporte
    testnet vuelve a comprobar riesgo y disponibilidad de la subcuenta.
    La ruta HTTP local exige firma externa, confirmación y bóveda desbloqueada.
    """
    now_ns = clock_ns()
    if type(now_ns) is not int or now_ns <= 0:
        raise GrvtTestnetSignalError("Reloj de señal inválido")
    client_id = client_id_for_signal(session_id, action_request_id)
    if contract_validator(order, limits, now_ns) != client_id:
        raise GrvtTestnetSignalError("La orden no corresponde al ID estable de la señal")
    with reviewed_testnet_signal(paper,lab,limits,session_id=session_id,action_request_id=action_request_id,
            side='BUY' if order['legs'][0]['is_buying_asset'] else 'SELL',reduce_only=order['reduce_only'],
            now_ns=now_ns,clock_ns=clock_ns) as (binding,source,definition,recorded_at,fresh_clock):
        signature_verifier(order, limits, now_ns=now_ns)
        submit_kwargs = {"clock_ns": fresh_clock, "signal_source": source}
        if include_signal_timing:
            submit_kwargs['signal_recorded_at_ns']=recorded_at
        if include_strategy_context:
            # La transacción de reviewed_testnet_signal conserva estable la
            # sesión mientras se lee el contexto que se archivará en REAL.
            with paper.connect() as db:
                session=db.execute('SELECT timeframe,engine_version FROM paper_signal_sessions WHERE id=?',
                    (session_id,)).fetchone()
                submit_kwargs['strategy_context']={'definition':definition,
                    'timeframe':session['timeframe'],'engine_version':session['engine_version']}
        if pre_claim_guard is not None:
            submit_kwargs["pre_claim_guard"] = pre_claim_guard
        result = submitter(vault, client, order, limits, **submit_kwargs)
        return binding, result


@contextmanager
def reviewed_testnet_signal(paper: PaperPerpStore, lab: LabStore, limits: GrvtTestnetLimits, *,
                            session_id: str, action_request_id: str, side: str, reduce_only: bool,
                            now_ns: int, clock_ns: Callable[[], int]):
    """Compatibilidad TESTNET; la reproducción PAPER es independiente de la red."""
    with reviewed_paper_signal(paper,lab,instrument=limits.instrument,session_id=session_id,
            action_request_id=action_request_id,side=side,reduce_only=reduce_only,
            now_ns=now_ns,clock_ns=clock_ns) as evidence:
        yield evidence


def sign_and_submit_signal_testnet_once(
    paper: PaperPerpStore, lab: LabStore, vault: RealVault,
    client_factory: Callable[[str], GrvtTestnetOrderClient], api_key: str,
    limits: GrvtTestnetLimits, *, session_id: str, action_request_id: str,
    dedicated_key_hex: str, expected_signer: str, builder: str,
    side: str, size: Decimal, limit_price: Decimal, reduce_only: bool,
    max_spread_bps: int = 100, max_slippage_bps: int = 100,
    clock_ns: Callable[[], int] = time.time_ns,
    instrument_fetcher: Callable = fetch_testnet_instrument,
    quote_fetcher: Callable = fetch_testnet_quote,
    signal_submitter: Callable = submit_signal_testnet_once,
    submission_guard: Callable = require_new_testnet_submission,
) -> tuple[GrvtTestnetSignalBinding, object]:
    """Firma y envía sólo un intento testnet; la clave existe en esta llamada.

    No es un trabajador automático ni conserva credenciales. El puente PAPER
    vuelve a validar procedencia, lado, riesgo, parada y diario antes del POST.
    """
    if not isinstance(dedicated_key_hex, str) or not re.fullmatch(
            r"0x[0-9a-fA-F]{64}", dedicated_key_hex):
        raise GrvtTestnetSignalError("Clave Trade testnet inválida")
    now_ns = clock_ns()
    if type(now_ns) is not int or now_ns <= 0:
        raise GrvtTestnetSignalError("Reloj testnet inválido")
    client_id = client_id_for_signal(session_id, action_request_id)
    submission_guard(vault, limits.sub_account_id, client_id)
    instrument = instrument_fetcher(limits.instrument)
    quote = quote_fetcher(limits.instrument)
    if quote.instrument != limits.instrument:
        raise GrvtTestnetQuoteError("Cotización de instrumento testnet distinto")
    guard_testnet_limit_price(
        side, limit_price, quote, max_spread_bps=max_spread_bps,
        max_slippage_bps=max_slippage_bps, now_ns=clock_ns(),
    )
    draft = GrvtTestnetLimitDraft(
        sub_account_id=limits.sub_account_id, instrument=limits.instrument,
        side=side, size=size, limit_price=limit_price,
        reduce_only=reduce_only, builder=builder,
        client_order_id=client_id, nonce=secrets.randbits(32),
        expiration_ns=now_ns + 60_000_000_000,
    )
    order = sign_testnet_limit(
        draft, limits, instrument,
        dedicated_private_key=bytes.fromhex(dedicated_key_hex[2:]),
        expected_signer=expected_signer, now_ns=now_ns,
    )
    verify_testnet_limit_signature(order, limits, instrument, now_ns=now_ns)
    client = client_factory(api_key)
    def latest_quote_guard() -> GrvtTestnetPriceGuard:
        latest = quote_fetcher(limits.instrument)
        if latest.instrument != limits.instrument:
            raise GrvtTestnetQuoteError("Cotización de instrumento testnet distinto")
        guard_testnet_limit_price(
            side, limit_price, latest, max_spread_bps=max_spread_bps,
            max_slippage_bps=max_slippage_bps, now_ns=clock_ns(),
        )
        return GrvtTestnetPriceGuard(latest, max_spread_bps, max_slippage_bps)
    return signal_submitter(
        paper, lab, vault, client, order, limits,
        session_id=session_id, action_request_id=action_request_id,
        clock_ns=clock_ns,
        signature_verifier=lambda candidate, policy, *, now_ns: verify_testnet_limit_signature(
            candidate, policy, instrument, now_ns=now_ns,
        ),
        pre_claim_guard=latest_quote_guard,
    )
