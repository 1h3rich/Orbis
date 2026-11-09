"""Procedencia DCA probada, sin trasladar efectivo o unidades PAPER a REAL."""

from dataclasses import dataclass
from datetime import datetime,timezone,timedelta
import hashlib
import json

from app.automation.dca import (
    AVERAGE_PAPER_ONLY, VOLUME_LIVE_UNAVAILABLE, dca_engine_for_rule, dca_signal_window_key, dca_uses_average, validate_dca,
)
from app.automation.paper_dca import PaperDca, _ema_origin_for_rule
from app.automation.verification import verify_dca_run
from app.lab.engine import Candle,validate_candles
from app.lab.store import content_hash
from app.live_execution.spot_bot_source import SpotBotSourceError
from app.live_execution.policy import _utc


@dataclass(frozen=True,slots=True)
class DcaBotObservation:
    session_id:str
    version_id:str
    dataset_id:str
    dataset_hash:str
    source_run_id:str
    engine_version:str
    symbol:str
    timeframe:str
    definition_hash:str
    definition:dict
    candles:tuple[Candle,...]
    candle_t:int
    observed_at:datetime
    observation_digest:str
    run_hash:str
    market_observation:dict|None = None

    def binding(self) -> dict:
        return {"kind":"DCA",**{name:getattr(self,name) for name in (
            "session_id","version_id","dataset_id","dataset_hash","source_run_id",
            "engine_version","symbol","timeframe","definition_hash","run_hash")}}


class DcaBotSource:
    def __init__(self,paper:PaperDca, *, allow_moving_average:bool=False, allow_volume:bool=False):
        self.paper,self.store,self.lab = paper,paper.store,paper.lab
        self.path = paper.store.path
        self.allow_moving_average = allow_moving_average
        self.allow_volume = allow_volume

    def current_passphrase(self): return self.store.current_passphrase()
    def connect(self): return self.store.connect()
    def sessions(self, *, session_id=None): return self.paper.sessions(session_id=session_id)

    def observation(self,session_id:str,*,now:datetime,verify_backtest:bool=False) -> DcaBotObservation:
        now = _utc(now,"now")
        try:
            with self.connect() as db:
                db.execute("BEGIN")
                row = db.execute("SELECT * FROM paper_dca_sessions WHERE id=?",(session_id,)).fetchone()
                if row is None or row["status"] != "ACTIVE":
                    raise ValueError("source")
                session = dict(row)
                rule = validate_dca(json.loads(row["rule_json"]))
                if "volume_gate" in rule and not self.allow_volume:
                    raise SpotBotSourceError(VOLUME_LIVE_UNAVAILABLE)
                engine = dca_engine_for_rule(rule)
                if dca_uses_average(rule) and not self.allow_moving_average:
                    raise SpotBotSourceError(AVERAGE_PAPER_ONLY)
                if session["engine_version"] != engine or not session["source_run_id"]:
                    raise ValueError("engine")
                meta,history = self.lab.dataset(session["dataset_id"])
                volume_origin = history[-rule["volume_gate"]["period"]:] if "volume_gate" in rule else None
                if not self.paper._state_valid(db,row,json.loads(row["state_json"]),rule,
                                               ema_origin=_ema_origin_for_rule(rule,history), volume_origin=volume_origin):
                    raise ValueError("paper ledger")
                events = [dict(item) for item in db.execute("SELECT * FROM paper_dca_events WHERE session_id=? AND sequence_no=0 ORDER BY execution_t",(session_id,))]
            version = self.store.version(session["version_id"])
            saved = self.store.run(session["source_run_id"])
            if (validate_dca(version["definition"]) != rule or meta["hash"] != session["dataset_hash"]
                    or not meta["verified_public"] or meta["source"] != "mexc-spot-public" or meta["gaps"]
                    or meta["symbol"] != session["symbol"] or meta["timeframe"] != "1d"
                    or not session["symbol"].endswith("/USDT") or not events
                    or saved["version_id"] != session["version_id"] or saved["dataset_id"] != session["dataset_id"]
                    or saved["dataset_hash"] != meta["hash"] or saved["engine_version"] != engine
                    or verify_backtest and not verify_dca_run(self.store,self.lab,saved)["verified"]):
                raise ValueError("origin")
            metadata = {"id","version_id","dataset_id","dataset_hash","created_at","engine_version","definition_hash","result_hash"}
            stored = {key:value for key,value in saved.items() if key not in metadata}
            if saved.get("definition_hash") != content_hash(version["definition"]) or saved.get("result_hash") != content_hash(stored):
                raise ValueError("backtest")
            key = dca_signal_window_key(rule)
            if key:
                window = json.loads(events[0]["event_json"])[key]
                if window[:-1] != history[-(len(window)-1):]:
                    raise ValueError("initial overlap")
            for event in events:
                payload = json.loads(event["event_json"])
                candle = payload["signal_candle"]
                if payload.get("kind") != "OBSERVATION" or candle["t"] != event["execution_t"]-86400 or candle["t"] != history[-1]["t"]+86400:
                    raise ValueError("coverage")
                history.append(candle)
            bars,gaps = validate_candles(history,"1d",now=int(now.timestamp()))
            event = events[-1]
            stamp = datetime.fromtimestamp(event["observed_at_ns"]//1_000_000_000,timezone.utc)+timedelta(microseconds=event["observed_at_ns"]%1_000_000_000//1000)
            if gaps or not datetime.fromtimestamp(bars[-1].t+86400,timezone.utc) <= stamp <= now:
                raise ValueError("clock")
            return DcaBotObservation(session_id,session["version_id"],meta["id"],meta["hash"],saved["id"],
                engine,session["symbol"],"1d",content_hash(rule),rule,tuple(bars),bars[-1].t,stamp,
                hashlib.sha256(event["event_json"].encode()).hexdigest(),saved["result_hash"])
        except Exception as exc:
            if isinstance(exc,SpotBotSourceError): raise
            raise SpotBotSourceError("El origen DCA probado/PAPER no es verificable") from exc


def verified_native_bot_observation(paper,session_id,*,now,verify_backtest=False):
    if isinstance(paper,DcaBotSource):
        return paper.observation(session_id,now=now,verify_backtest=verify_backtest)
    from app.live_execution.spot_bot_source import verified_spot_bot_observation
    return verified_spot_bot_observation(paper,session_id,now=now,verify_backtest=verify_backtest)
