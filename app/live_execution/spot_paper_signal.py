"""Señal causal LAB→PAPER apta para planificar, nunca una orden REAL.

No transforma unidades, efectivo o precio PAPER en capital real. Una capa de
riesgo independiente deberá construir y autorizar cualquier intención REAL.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from uuid import UUID, uuid5

from app.automation.spot_strategy_paper import SpotStrategyPaper
from app.lab.api import ENGINE_VERSION
from app.lab.engine import (IndicatorCache, INTERVALS, LabError, evaluate,
                            supports_engine_version, validate_candles, validate_strategy)
from app.live_execution.journal import (LiveSpotSignalSource, RecordedIntent,
                                        recover_spot_signal_source, reserve_spot_intent)
from app.live_execution.policy import LiveSpotIntent, LiveSpotPolicy, LiveSpotState
from app.portfolio.vault import RealVault


_SIGNAL_NAMESPACE = UUID("970c88ed-a6d2-4ba4-bfd3-9c314687c55a")
_INTENT_NAMESPACE = UUID("34c397b5-7bce-47e3-ac0d-e1d2bf26e082")


class SpotPaperSignalError(ValueError):
    """PAPER no ofrece una señal actual y reproducible para REAL."""


@dataclass(frozen=True, slots=True)
class SpotPaperSignal:
    signal_id: str
    paper_session_id: str
    strategy_version_id: str
    dataset_id: str
    dataset_hash: str
    engine_version: str
    symbol: str
    timeframe: str
    candle_t: int
    signal_at: datetime
    observed_at: datetime
    paper_entry_status: str
    entry_evidence: dict
    observation_digest: str


@dataclass(frozen=True, slots=True)
class RecordedPaperBuy:
    signal: SpotPaperSignal
    intent: LiveSpotIntent
    source: LiveSpotSignalSource
    reservation: RecordedIntent


def _source_for_signal(signal: SpotPaperSignal, intent: LiveSpotIntent) -> LiveSpotSignalSource:
    return LiveSpotSignalSource(
        intent_id=intent.intent_id, signal_id=signal.signal_id,
        paper_session_id=signal.paper_session_id,
        strategy_version_id=signal.strategy_version_id,
        dataset_id=signal.dataset_id, dataset_hash=signal.dataset_hash,
        engine_version=signal.engine_version, symbol=signal.symbol,
        timeframe=signal.timeframe, candle_t=signal.candle_t,
        signal_at=signal.signal_at, observed_at=signal.observed_at,
        entry_evidence=signal.entry_evidence,
        observation_digest=signal.observation_digest,
    )


def current_spot_paper_entry_signal(
    paper: SpotStrategyPaper, session_id: str, *, now: datetime,
    max_age_seconds: int,
) -> SpotPaperSignal:
    """Exige la última vela, versión LAB intacta y entrada PAPER observada.

    El ID determinista sirve para deduplicación posterior, no reserva fondos.
    Sólo lee SQLite local y falla si la sesión dejó de estar activa.
    """
    if (not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None
            or type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 300):
        raise SpotPaperSignalError("Reloj o vigencia de señal inválidos")
    now = now.astimezone(timezone.utc)
    try:
        with paper.connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None or row["status"] != "ACTIVE":
                raise SpotPaperSignalError("Sesión PAPER no activa")
            event = db.execute("""SELECT * FROM events WHERE session_id=? AND candle_t=?
                                  AND kind='OBSERVATION'""", (session_id, row["last_t"])).fetchone()
            if event is None:
                raise SpotPaperSignalError("La última vela no tiene observación PAPER")
            session = dict(row)
            observed_ns = event["observed_at_ns"]
            payload_json = event["payload_json"]
            payload = json.loads(payload_json)
            if type(observed_ns) is not int or not isinstance(payload, dict):
                raise SpotPaperSignalError("Observación PAPER malformada")
            observation_digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        version = paper.lab.version(session["version_id"])
        dataset, original = paper.lab.dataset(session["dataset_id"])
        definition = validate_strategy(version["definition"])
        if (session["engine_version"] != ENGINE_VERSION
                or not supports_engine_version(session["engine_version"], definition)
                or definition["direction"] != "LONG"
                or session["dataset_hash"] != dataset["hash"]
                or not dataset["verified_public"] or dataset["source"] != "mexc-spot-public"
                or dataset["symbol"] != session["symbol"]
                or dataset["timeframe"] != session["timeframe"]
                or json.loads(session["definition_json"]) != definition):
            raise SpotPaperSignalError("Origen LAB/PAPER no coincide")
        history = json.loads(session["history_json"])
        if (not isinstance(history, list) or not history
                or history[:len(original)] != original or history[-1] != payload.get("candle")):
            raise SpotPaperSignalError("Histórico PAPER no coincide con la señal")
        step = INTERVALS[session["timeframe"]]
        signal_t = session["last_t"]
        signal_at = datetime.fromtimestamp(signal_t + step, timezone.utc)
        observed_at = (datetime.fromtimestamp(observed_ns // 1_000_000_000, timezone.utc)
                       + timedelta(microseconds=observed_ns % 1_000_000_000 // 1000))
        paper_status = payload.get("entry_status")
        action = payload.get("action")
        if (signal_t != history[-1]["t"]
                or observed_at < signal_at or observed_at > now
                or (now - signal_at).total_seconds() > max_age_seconds
                or paper_status not in ("EXECUTED_PAPER", "SKIPPED_NO_CAPITAL")
                or (paper_status == "EXECUTED_PAPER" and
                    (not isinstance(action, dict) or action.get("side") != "BUY"
                     or action.get("reason") != "ENTRY_RULE"))
                or (paper_status == "SKIPPED_NO_CAPITAL" and action is not None)):
            raise SpotPaperSignalError("Señal PAPER ausente o caducada")
        bars, gaps = validate_candles(history, session["timeframe"], now=int(now.timestamp()))
        if gaps:
            raise SpotPaperSignalError("Histórico PAPER discontinuo")
        matched, evidence = evaluate(definition["entry"], bars, len(bars) - 1,
                                     IndicatorCache(bars), base_timeframe=session["timeframe"])
        if not matched or payload.get("entry") != evidence or payload.get("entry_match") is not True:
            raise SpotPaperSignalError("La regla LAB no reproduce la señal PAPER")
    except (LabError, KeyError, IndexError, TypeError, ValueError, OverflowError, OSError) as exc:
        if isinstance(exc, SpotPaperSignalError):
            raise
        raise SpotPaperSignalError("Señal LAB/PAPER no verificable") from exc
    return SpotPaperSignal(
        signal_id=str(uuid5(_SIGNAL_NAMESPACE, f"{session_id}:{signal_t}")),
        paper_session_id=session_id, strategy_version_id=session["version_id"],
        dataset_id=session["dataset_id"], dataset_hash=dataset["hash"],
        engine_version=session["engine_version"], symbol=session["symbol"],
        timeframe=session["timeframe"], candle_t=signal_t,
        signal_at=signal_at, observed_at=observed_at,
        paper_entry_status=paper_status, entry_evidence=evidence,
        observation_digest=observation_digest,
    )


def live_buy_intent_from_signal(
    signal: SpotPaperSignal, policy: LiveSpotPolicy, *, quantity_base: Decimal,
    limit_price_quote: Decimal, estimated_fee_quote: Decimal, now: datetime,
) -> LiveSpotIntent:
    """Construye el pedido exacto; la autorización y el envío van aparte.

    Toma tamaño/precio/comisión exclusivamente del planificador REAL. La
    intención se identifica por cuenta, versión, mercado, temporalidad y vela,
    no por sesión PAPER ni política: dos sesiones sobre la misma señal no
    pueden reservarla como órdenes distintas para esa cuenta.
    """
    if not isinstance(signal, SpotPaperSignal) or not isinstance(policy, LiveSpotPolicy):
        raise SpotPaperSignalError("Señal o política REAL inválida")
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise SpotPaperSignalError("Reloj REAL sin zona horaria")
    now = now.astimezone(timezone.utc)
    if (signal.strategy_version_id != policy.strategy_version_id
            or signal.symbol.replace("/", "") != policy.symbol
            or not policy.symbol.endswith("USDT")
            or signal.engine_version != ENGINE_VERSION
            or signal.signal_at > now
            or (now - signal.signal_at).total_seconds() > policy.max_data_age_seconds):
        raise SpotPaperSignalError("Señal y política REAL no coinciden o caducaron")
    try:
        intent_id = str(uuid5(_INTENT_NAMESPACE, "|".join((
            policy.account_ref, signal.strategy_version_id, policy.symbol,
            signal.timeframe, str(signal.candle_t), "BUY",
        ))))
        return LiveSpotIntent(
            intent_id=intent_id, policy_id=policy.policy_id,
            strategy_version_id=signal.strategy_version_id,
            account_ref=policy.account_ref, symbol=policy.symbol, side="BUY",
            quantity_base=quantity_base, limit_price_quote=limit_price_quote,
            estimated_fee_quote=estimated_fee_quote, signal_at=signal.signal_at,
        )
    except ValueError as exc:
        raise SpotPaperSignalError("Tamaño, precio o comisión REAL inválidos") from exc


def reserve_current_paper_buy(
    vault: RealVault, paper: SpotStrategyPaper, session_id: str,
    policy: LiveSpotPolicy, state: LiveSpotState, *,
    quantity_base: Decimal, limit_price_quote: Decimal,
    estimated_fee_quote: Decimal, now: datetime,
) -> RecordedPaperBuy:
    """Relee PAPER bajo bloqueo antes de una reserva local, nunca de un POST.

    La política debe estar registrada en la bóveda y `state` debe proceder de
    una conciliación REAL independiente. El bloqueo impide que la sesión PAPER
    se detenga o avance entre esta comprobación y la reserva; no sustituye la
    revalidación justo antes de un futuro envío externo.
    """
    if not isinstance(paper, SpotStrategyPaper) or not isinstance(policy, LiveSpotPolicy):
        raise SpotPaperSignalError("Sesión PAPER o política REAL inválida")
    with paper.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        signal = current_spot_paper_entry_signal(
            paper, session_id, now=now,
            max_age_seconds=policy.max_data_age_seconds,
        )
        intent = live_buy_intent_from_signal(
            signal, policy, quantity_base=quantity_base,
            limit_price_quote=limit_price_quote,
            estimated_fee_quote=estimated_fee_quote, now=now,
        )
        source = _source_for_signal(signal, intent)
        reservation = reserve_spot_intent(
            vault, intent, state, now=now, signal_source=source)
    return RecordedPaperBuy(signal, intent, source, reservation)


@contextmanager
def verified_archived_paper_buy_source(
    vault: RealVault, paper: SpotStrategyPaper, policy: LiveSpotPolicy,
    intent: LiveSpotIntent, *, now: datetime,
):
    """Mantiene PAPER estable mientras un transporte verifica y envía.

    Sólo acredita que la fuente local archivada sigue siendo la última señal
    reproducible. No acredita saldo, reglas MEXC, órdenes ajenas ni P/L REAL.
    """
    if not isinstance(paper, SpotStrategyPaper):
        raise SpotPaperSignalError("Sesión PAPER necesaria para el envío")
    with paper.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        source = recover_spot_signal_source(vault, intent.intent_id)
        if source.observation_digest is None:
            raise SpotPaperSignalError("La procedencia PAPER antigua no fijó la observación exacta")
        signal = current_spot_paper_entry_signal(
            paper, source.paper_session_id, now=now,
            max_age_seconds=policy.max_data_age_seconds,
        )
        recreated = live_buy_intent_from_signal(
            signal, policy, quantity_base=intent.quantity_base,
            limit_price_quote=intent.limit_price_quote,
            estimated_fee_quote=intent.estimated_fee_quote, now=now,
        )
        if recreated != intent or _source_for_signal(signal, recreated) != source:
            raise SpotPaperSignalError("La señal PAPER cambió desde la reserva")
        yield source
