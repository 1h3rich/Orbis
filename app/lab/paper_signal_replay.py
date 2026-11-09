"""Reproduce señales PAPER con la versión LAB y las velas archivadas.

No reconstruye fills, órdenes ni precios ejecutables. Una coincidencia prueba
consistencia del evaluador local, no autenticidad de datos externos.
"""

from __future__ import annotations

from collections import deque
import json
import sqlite3

import sqlcipher3

from app.core.paper_archive_chain import ArchiveChainError, verified_archive_page
from app.lab.engine import (CURRENT_ENGINE_VERSION, PREVIOUS_ENGINE_VERSION,
                            LEGACY_TRAILING_ENGINE_VERSION, INTERVALS, IndicatorCache,
                            LabError, evaluate, paper_precision_for_engine, validate_candles,
                            validate_strategy)
from app.lab.paper_indicator_state import (advance_paper_indicator_state,
                                           build_paper_indicator_state)
from app.lab.paper_verification import verify_paper_source
from app.lab.recent_paper_evaluator import RECENT_BARS, evaluate_recent, recent_supported
from app.lab.store import LabStore


def verify_segmented_base_signals(definition: dict, seed: list[dict], observations,
                                  *, timeframe: str, engine_version: str) -> dict:
    """Reproduce señales base con memoria acotada tras la semilla LAB.

    Es una prueba de paridad de reglas, no una verificación del origen LAB ni
    de fills. El llamador debe cotejar semilla y archivo por separado.
    """
    def report(status: str, reason: str, checked: int = 0,
               first_mismatch_t: int | None = None) -> dict:
        return {"verified": status == "verified", "status": status,
                "reason": reason, "checked_observations": checked,
                "first_mismatch_t": first_mismatch_t}

    if not recent_supported(definition, engine_version):
        return report("unsupported", "Motor legado o reglas multitemporales requieren replay completo")
    if timeframe not in INTERVALS:
        return report("mismatch", "Temporalidad PAPER inválida")
    try:
        step = INTERVALS[timeframe]
        if not isinstance(seed, list) or len(seed) < 3:
            return report("mismatch", "Semilla LAB incompleta")
        seed_bars, gaps = validate_candles(seed, timeframe, now=seed[-1]["t"] + 2 * step)
        if gaps:
            return report("mismatch", "Semilla LAB discontinua")
        window = deque(seed_bars[-RECENT_BARS:], maxlen=RECENT_BARS)
        precision = paper_precision_for_engine(engine_version)
        snapshot = build_paper_indicator_state(definition, seed, precision)
        previous_t = seed_bars[-1].t
        checked = 0
        for observation in observations:
            raw = observation["candle"]
            if (not isinstance(raw, dict) or type(raw.get("t")) is not int
                    or raw["t"] != previous_t + step
                    or observation["candle_t"] != raw["t"]):
                return report("mismatch", "Vela PAPER ausente o discontinua", checked,
                              raw.get("t") if isinstance(raw, dict) else None)
            overlap, gaps = validate_candles([window[-2].json(), window[-1].json(), raw],
                                             timeframe, now=raw["t"] + 2 * step)
            if gaps:
                return report("mismatch", "Vela PAPER discontinua", checked, raw["t"])
            window.append(overlap[-1])
            snapshot = advance_paper_indicator_state(snapshot, definition, raw,
                                                     previous_t=previous_t, precision=precision)
            previous_t = raw["t"]
            recent = list(window)
            if snapshot["count"] >= RECENT_BARS:
                entry, entry_detail = evaluate_recent(definition["entry"], recent, snapshot,
                                                      engine_version=engine_version,
                                                      base_timeframe=timeframe)
                exit_match, exit_detail = (evaluate_recent(definition["exit"], recent, snapshot,
                                                            engine_version=engine_version,
                                                            base_timeframe=timeframe)
                                           if "exit" in definition else (None, None))
            else:
                cache = IndicatorCache(recent, precision=precision)
                index = len(recent) - 1
                entry, entry_detail = evaluate(definition["entry"], recent, index, cache,
                                               base_timeframe=timeframe)
                exit_match, exit_detail = (evaluate(definition["exit"], recent, index, cache,
                                                    base_timeframe=timeframe)
                                           if "exit" in definition else (None, None))
            if (observation["entry_match"] is not entry
                    or observation["exit_match"] is not exit_match
                    or observation["entry"] != entry_detail
                    or observation["exit"] != exit_detail):
                return report("mismatch", "Señal PAPER segmentada distinta de LAB",
                              checked, raw["t"])
            checked += 1
        if not checked:
            return report("no_observations", "Todavía no hay velas PAPER para verificar")
        return report("verified", "Señales y evidencias coinciden con ventana acotada", checked)
    except (LabError, KeyError, TypeError, ValueError, IndexError, ArithmeticError) as exc:
        return report("mismatch", f"Replay segmentado no verificable: {exc}")


def verify_archived_paper_signals(lab: LabStore, db, session: dict, definition: dict,
                                  kind: str, *, page_size: int = 512) -> dict:
    """Coteja origen, cadena, observaciones y señales sin leer el JSON de sesión.

    La conexión debe conservar una instantánea de lectura mientras dura el
    replay. Se limita al motor actual y a reglas de temporalidad base.
    """
    def report(status: str, reason: str) -> dict:
        return {"verified": False, "status": status, "reason": reason,
                "checked_observations": 0, "first_mismatch_t": None}

    if not recent_supported(definition, session.get("engine_version")):
        return report("unsupported", "Motor legado o reglas multitemporales requieren replay completo")
    if kind not in ("spot", "grvt") or session.get("timeframe") not in INTERVALS:
        return report("mismatch", "Tipo de archivo o temporalidad PAPER inválida")
    if type(page_size) is not int or not 1 <= page_size <= 1000:
        return report("mismatch", "Tamaño de segmento PAPER inválido")
    try:
        source_definition = validate_strategy(lab.version(session["version_id"])["definition"])
        meta, seed = lab.dataset(session["dataset_id"])
        if (source_definition != definition or meta["hash"] != session["dataset_hash"]
                or meta["symbol"] != session["symbol"]
                or meta["timeframe"] != session["timeframe"]):
            return report("source_unverified", "Versión o dataset LAB distintos de PAPER")
        step = INTERVALS[session["timeframe"]]
        if kind == "spot":
            observation_rows = db.execute("""SELECT candle_t,payload_json FROM events
                                             WHERE session_id=? AND kind='OBSERVATION'
                                             ORDER BY candle_t""", (session["id"],))
        else:
            observation_rows = db.execute("""SELECT candle_t,entry_match,exit_match,evidence_json
                                             FROM paper_signal_observations
                                             WHERE session_id=? ORDER BY candle_t""", (session["id"],))

        def archived_observations():
            cursor, end_digest, seed_count, previous_t = None, None, 0, None
            while True:
                page = verified_archive_page(db, session["id"], kind, cursor, page_size)
                if page["candles"] and page["anchor_digest"] != end_digest:
                    raise ValueError("Segmentos PAPER sin continuidad de huellas")
                for item in page["candles"]:
                    candle = item["candle"]
                    stamp = candle["t"]
                    if previous_t is not None and stamp != previous_t + step:
                        raise ValueError("Archivo PAPER con velas discontinuas")
                    previous_t = stamp
                    if seed_count < len(seed):
                        if item["origin"] != "SEED" or candle != seed[seed_count]:
                            raise ValueError("Semilla archivada distinta de LAB")
                        seed_count += 1
                        continue
                    if item["origin"] != "OBSERVED":
                        raise ValueError("Origen de vela PAPER posterior a LAB inválido")
                    row = next(observation_rows, None)
                    if row is None or row["candle_t"] != stamp:
                        raise ValueError("Observación PAPER ausente o fuera de orden")
                    if kind == "spot":
                        payload = json.loads(row["payload_json"])
                        observed = {"candle_t": stamp, "candle": payload.get("candle"),
                                    "entry_match": payload.get("entry_match"),
                                    "exit_match": payload.get("exit_match"),
                                    "entry": payload.get("entry"), "exit": payload.get("exit")}
                    else:
                        evidence = json.loads(row["evidence_json"])
                        observed = {"candle_t": stamp, "candle": evidence.get("candle"),
                                    "entry_match": bool(row["entry_match"]),
                                    "exit_match": None if row["exit_match"] is None else bool(row["exit_match"]),
                                    "entry": evidence.get("entry"), "exit": evidence.get("exit")}
                    if observed["candle"] != candle:
                        raise ValueError("Vela archivada distinta de la observación")
                    yield observed
                end_digest = page["end_digest"]
                if not page["has_more"]:
                    break
                cursor = page["next_after_t"]
            if seed_count != len(seed) or previous_t != session["last_t"]:
                raise ValueError("Archivo PAPER incompleto o con hora final divergente")
            if next(observation_rows, None) is not None:
                raise ValueError("Sobran observaciones PAPER fuera del archivo")

        return verify_segmented_base_signals(
            definition, seed, archived_observations(), timeframe=session["timeframe"],
            engine_version=session["engine_version"])
    except LabError as exc:
        return report("source_unverified", f"Origen LAB no disponible: {exc}")
    except (ArchiveChainError, KeyError, TypeError, ValueError, IndexError,
            sqlite3.DatabaseError, sqlcipher3.DatabaseError) as exc:
        return report("mismatch", f"Archivo PAPER no verificable: {exc}")


def verify_paper_signals(lab: LabStore, session: dict, definition: dict,
                         history: list[dict], observations: list[dict]) -> dict:
    """Comprueba cada cierre observado, su resultado y su explicación.

    `observations` contiene candle_t, entry_match, exit_match, candle,
    entry y exit. El prefijo histórico LAB es una semilla, no observaciones
    nuevas PAPER. Fallos de integridad no se convierten en cero señales.
    """
    def report(status: str, reason: str, checked: int = 0,
               first_mismatch_t: int | None = None) -> dict:
        return {"verified": status == "verified", "status": status,
                "reason": reason, "checked_observations": checked,
                "first_mismatch_t": first_mismatch_t}

    if session.get("engine_version") not in ("lab-1.13", LEGACY_TRAILING_ENGINE_VERSION,
                                              PREVIOUS_ENGINE_VERSION, CURRENT_ENGINE_VERSION):
        return report("engine_unavailable", "La semántica original PAPER no admite este replay")
    source = verify_paper_source(lab, session, definition, history)
    if not source["verified"]:
        return report("source_unverified", source["reason"])
    try:
        _, seed = lab.dataset(session["dataset_id"])
        step = INTERVALS[session["timeframe"]]
        bars, gaps = validate_candles(history, session["timeframe"],
                                      now=session["last_t"] + 2 * step)
        if gaps or len(observations) != len(bars) - len(seed):
            return report("mismatch", "Faltan o sobran observaciones PAPER")
        if not observations:
            return report("no_observations", "Todavía no hay velas PAPER para verificar")
        cache = IndicatorCache(bars, precision=paper_precision_for_engine(session["engine_version"]))
        for count, (index, observation) in enumerate(
            zip(range(len(seed), len(bars)), observations), start=1
        ):
            bar = bars[index]
            if (observation["candle_t"] != bar.t
                    or observation["candle"] != bar.json()):
                return report("mismatch", "Vela PAPER distinta de la observada",
                              count - 1, bar.t)
            entry, entry_detail = evaluate(definition["entry"], bars, index, cache,
                                           base_timeframe=session["timeframe"])
            exit_match, exit_detail = (evaluate(definition["exit"], bars, index, cache,
                                                base_timeframe=session["timeframe"])
                                       if "exit" in definition else (None, None))
            if (observation["entry_match"] is not entry
                    or observation["exit_match"] is not exit_match
                    or observation["entry"] != entry_detail
                    or observation["exit"] != exit_detail):
                return report("mismatch", "Señal PAPER distinta de la regla LAB",
                              count - 1, bar.t)
    except (LabError, KeyError, TypeError, ValueError, IndexError) as exc:
        return report("mismatch", f"No se pudieron reproducir las observaciones: {exc}")
    return report("verified", "Cada señal coincide con las velas y reglas archivadas",
                  len(observations))
