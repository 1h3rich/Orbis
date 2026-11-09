"""One checked, local snapshot of a Spot PAPER journal."""

from dataclasses import dataclass
from decimal import Decimal, localcontext
import json
from typing import TYPE_CHECKING

from app.automation.paper_dca import PaperDca, PaperDcaUnavailable
from app.automation.spot_paper_access import spot_paper_access, spot_paper_format
from app.lab.access import lab_access, lab_format
from app.lab.engine import LabError
from app.lab.paper_signal_replay import verify_paper_signals
from app.lab.store import content_hash

if TYPE_CHECKING:
    from app.automation.spot_strategy_paper import SpotStrategyPaper


@dataclass(frozen=True)
class SpotSnapshot:
    session: dict
    definition: dict
    events: list[dict]
    observed_candles: int
    ledger: dict
    digest: str


def spot_snapshot_stamp(paper: "SpotStrategyPaper") -> tuple:
    stamps = []
    for path, file_format, access in ((paper.path, spot_paper_format, spot_paper_access),
                                      (paper.lab.path, lab_format, lab_access)):
        kind = file_format(path)
        epoch = access.epoch(path)
        if kind not in ("plain", "encrypted_or_invalid"):
            raise LabError("Abre LAB y Spot PAPER para verificar el diario")
        if kind == "encrypted_or_invalid" and epoch is None and (
                path != paper.path or paper.passphrase is None):
            raise LabError("Desbloquea LAB y Spot PAPER para verificar el diario")
        try:
            info = path.stat()
        except OSError:
            raise LabError("El archivo local cambió durante la lectura de Spot PAPER") from None
        stamps.append((kind, epoch, info.st_dev, info.st_ino))
    return tuple(stamps)


def _paper_metrics(session: dict, state: dict, definition: dict, events: list[dict]) -> dict:
    ns = session["started_at_ns"]
    fee_rate, slippage = (Decimal(definition[field]) for field in ("fee_rate", "slippage_rate"))
    capital, fees = Decimal(definition["initial_capital"]), Decimal(0)
    buys, sells = 0, 0
    with localcontext() as context:
        context.prec = 80
        for event in events:
            observed_ns = event["observed_at_ns"]
            if type(observed_ns) is not int or observed_ns < ns:
                raise LabError("Orden temporal del diario PAPER no verificable")
            ns = observed_ns
            payload = json.loads(event["payload_json"])
            action = payload.get("action")
            if action is None:
                continue
            bid, ask, _ = PaperDca._quote(payload["quote"], session["symbol"], ns)
            buy = action["side"] == "BUY"
            price = ask * (1 + slippage) if buy else bid * (1 - slippage)
            gross = Decimal(action["units"]) * price
            fee = gross * fee_rate
            if (price != Decimal(action["price"]) or fee != Decimal(action["fee"])
                    or not buy and gross - fee != Decimal(action["cash_change"])
                    or buy and abs(gross + fee + Decimal(action["cash_change"])) > capital * Decimal("1e-70")):
                raise LabError("Precio o comisión del diario PAPER no verificables")
            fees += fee
            buys += int(buy)
            sells += int(not buy)
        realized, cash, units = (Decimal(state[field]) for field in ("realized_pnl", "cash", "units"))
        # Only cash is directly settled in the journal. Live estimates remain in the session view.
        equity = cash if units == 0 else None
        return {
            "through_ns": str(ns), "position_open": units > 0,
            "buy_count": buys, "sell_count": sells,
            "metrics": {"initial_capital": str(capital), "cash": str(cash),
                        "equity": str(equity) if equity is not None else None,
                        "realized_pnl": str(realized), "fees_paid": str(fees),
                        "return_pct": str(realized / capital * 100) if equity is not None else None,
                        "closed_trades": state["closed_trades"]},
        }


def verified_spot_snapshot(paper: "SpotStrategyPaper", session_id: str) -> SpotSnapshot:
    try:
        with paper.connect() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise LabError("Sesión PAPER spot no encontrada")
            session = dict(row)
            definition = json.loads(session["definition_json"])
            state = json.loads(session["state_json"])
            history = json.loads(session["history_json"])
            if not paper._valid_session_state(db, row, state, definition):
                raise LabError("El libro o el archivo PAPER no coincide con su diario")
            events = [dict(event) for event in db.execute("""SELECT candle_t,kind,observed_at_ns,payload_json
                FROM events WHERE session_id=? ORDER BY rowid""", (session_id,))]
        observations = [json.loads(event["payload_json"]) | {"candle_t": event["candle_t"]}
                        for event in events if event["kind"] == "OBSERVATION"]
        signals = verify_paper_signals(paper.lab, session, definition, history, observations)
        if not signals["verified"] and signals["status"] != "no_observations":
            raise LabError(signals["reason"])
        return SpotSnapshot(session, definition, events, len(observations),
                            _paper_metrics(session, state, definition, events),
                            content_hash({"session": session, "events": events}))
    except PaperDcaUnavailable as exc:
        raise LabError(str(exc)) from None
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        if isinstance(exc, LabError):
            raise
        raise LabError("La evidencia archivada de Spot PAPER no es verificable") from None
