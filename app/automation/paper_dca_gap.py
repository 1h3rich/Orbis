"""Investigación hipotética de días perdidos en DCA PAPER, sin asientos."""

from __future__ import annotations

from decimal import InvalidOperation, localcontext

from app.automation.dca import DcaState, apply_dca_step, dca_signal_window_key, precision_for_version, validate_dca, validate_dca_state
from app.lab.engine import LabError, validate_candles
from app.lab.store import content_hash


DAY = 86_400
MAX_MISSED_DAYS = 90


def dca_paper_overlap_count(rule: dict) -> int:
    """Guarda velas suficientes para verificar el proveedor y la señal siguiente."""
    if "volume_gate" in rule:
        return rule["volume_gate"]["period"]
    average = rule.get("sma_gate") or rule.get("ma_adjustment")
    return max(2, average["period"] - 1) if average and average.get("kind", "sma") == "sma" else 2


def analyze_dca_paper_gap(snapshot: dict, downloaded: list[dict], *, now: int) -> dict:
    """Usa cierres conocidos como precio proxy; nunca reconstruye fills PAPER."""
    if (snapshot.get("status") != "GAP" or type(now) is not int
            or type(snapshot.get("next_exec_t")) is not int
            or type(snapshot.get("tick_index")) is not int
            or snapshot["tick_index"] < 0):
        raise LabError("Hueco DCA no verificable")
    expected = snapshot["next_exec_t"]
    today = now // DAY * DAY
    missed = (today - expected) // DAY + 1
    if not 1 <= missed <= MAX_MISSED_DAYS:
        raise LabError("El hueco DCA supera la cobertura teórica verificable")
    rule = validate_dca(snapshot["rule"])
    if rule != snapshot["rule"]:
        raise LabError("Regla DCA cambió desde la sesión PAPER")
    overlap = snapshot.get("overlap")
    count = dca_paper_overlap_count(rule)
    if not isinstance(overlap, list) or len(overlap) != count:
        raise LabError("Solapamiento DCA no verificable")
    bars, gaps = validate_candles(downloaded, "1d", now=now)
    if (gaps or len(bars) != missed + count
            or [bar.json() for bar in bars[:count]] != overlap
            or bars[-1].t != today - DAY
            or bars[count].t != expected - DAY):
        raise LabError("Histórico MEXC revisado o incompleto; sin reconstrucción DCA")
    try:
        state = DcaState.from_json(snapshot["state"])
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise LabError("Estado DCA de origen no verificable") from None
    try:
        validate_dca_state(state, rule, tick_index=snapshot["tick_index"],
                           engine_version=snapshot["engine_version"])
    except (KeyError, TypeError, ValueError, InvalidOperation, LabError):
        raise LabError("Estado DCA de origen no verificable") from None
    steps = []
    with localcontext() as context:
        context.prec = precision_for_version(snapshot["engine_version"])
        for offset, bar in enumerate(bars[count:]):
            execution_t = expected + offset * DAY
            if bar.t != execution_t - DAY:
                raise LabError("Señal DCA fuera de su día")
            buys, skipped = apply_dca_step(
                state, rule, signal_time=bar.t, signal_close=bar.c,
                execution_time=execution_t, execution_price=bar.c,
                mark_price=bar.c, index=snapshot["tick_index"] + offset + 1,
                engine_version=snapshot["engine_version"],
                signal_bars=bars[offset:offset + count + 1] if dca_signal_window_key(rule) else None,
            )
            steps.append({"signal_t": bar.t, "execution_t": execution_t,
                          "close_proxy": str(bar.c), "hypothetical_buys": buys,
                          "hypothetical_skips": skipped})
    return {"session_id": snapshot["id"], "scope": "THEORETICAL_DCA_POST_GAP_ONLY",
            "source": "mexc-spot-public", "dataset_hash": snapshot["dataset_hash"],
            "engine_version": snapshot["engine_version"], "missed_days": missed,
            "download_hash": content_hash(downloaded), "downloaded_candles": downloaded,
            "steps": steps, "theoretical_state": state.json(),
            "paper_events_written": 0, "persisted": False,
            "execution_model": "SIGNAL_CLOSE_PROXY_NOT_MARKET_EXECUTION",
            "limitations": ["La vela de señal cerrada sólo aproxima el precio de ejecución",
                            "No se observaron bids, asks ni órdenes durante el hueco",
                            "Este estado es una rama teórica; el saldo PAPER observado no cambia"]}
