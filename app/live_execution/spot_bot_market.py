"""Archivo de mercado propio REAL. No concede permiso de órdenes ni hereda caja PAPER."""

from __future__ import annotations

from datetime import datetime,timezone
from typing import Callable

from sqlalchemy import select

from app.automation.dca import adjusted_dca_request, dca_engine_for_rule, dca_signal_window_key, live_dca_signal, validate_dca
from app.lab.engine import CURRENT_ENGINE_VERSION,IndicatorCache,INTERVALS,evaluate,validate_candles,validate_strategy
from app.lab.store import content_hash
from app.live_execution.dca_bot_source import DcaBotObservation
from app.live_execution.policy import _utc
from app.live_execution.spot_bot_source import SpotBotObservation
from app.portfolio.models import RealSpotBotMarketStream,RealSpotBotMarketObservation


PROTOCOL = "REAL_BOT_MARKET_V1"


def market_seed(source:SpotBotObservation|DcaBotObservation,bot_id:str,policy_id:str,*,now:datetime) -> dict:
    return {"protocol":PROTOCOL,"bot_id":bot_id,"policy_id":policy_id,"source":source.binding(),
            "definition":source.definition,"candles":[bar.json() for bar in source.candles],
            "candle_t":source.candle_t,"observed_at":source.observed_at.isoformat(),
            "origin_observation_digest":source.observation_digest,"approved_at":_utc(now,"now").isoformat()}


def seed_pointer(seed:dict,digest:str) -> dict:
    return {"policy_id":seed["policy_id"],"sequence_no":0,"digest":digest,"candle_t":seed["candle_t"],"observed_at":seed["observed_at"]}


def save_market_seed(db,seed:dict,config:dict,state:dict) -> None:
    from app.live_execution.spot_bot_store import encoded
    raw,digest = encoded(seed)
    config.setdefault("market_stream_seeds",{})[seed["policy_id"]] = digest
    state.setdefault("market_heads",{})[seed["policy_id"]] = seed_pointer(seed,digest)
    db.add(RealSpotBotMarketStream(policy_id=seed["policy_id"],bot_id=seed["bot_id"],
        seed_json=raw,seed_digest=digest,created_at=datetime.fromisoformat(seed["approved_at"])))


def read_market_stream(db,policy_id:str,*,through:int|None=None,_parents:frozenset=frozenset()) -> tuple[dict,list,dict]:
    """Reproduce la cadena y reglas; datos rehasheados no sustituyen la semilla aprobada."""
    # La caché sólo vive durante ESTA lectura y sobre una instantánea SQL real.
    # No abrir otro BEGIN si el llamante ya posee su transacción (incluso una
    # escritura). Nunca confirmar, revertir ni reutilizar datos entre llamadas.
    connection = db.connection()
    if not connection.connection.dbapi_connection.in_transaction:
        connection.exec_driver_sql("BEGIN")
    return _read_market_stream(db,policy_id,through=through,parents=_parents,records={})


def _read_market_stream(db,policy_id:str,*,through:int|None,parents:frozenset,records:dict) -> tuple[dict,list,dict]:
    from app.live_execution.spot_bot_store import SpotBotStoreError,bot_authorizations,bot_record,decoded
    try:
        if policy_id in parents or len(parents) >= 128: raise ValueError("market lineage cycle")
        row = db.get(RealSpotBotMarketStream,policy_id)
        if row is None: raise ValueError("missing stream")
        if row.bot_id not in records: records[row.bot_id] = bot_record(db,row.bot_id)
        bot,config,state = records[row.bot_id]
        seed = decoded(row.seed_json,row.seed_digest)
        source = seed["source"]
        authorization = next(item for item in bot_authorizations(config) if item["policy_id"] == policy_id)
        if (config.get("market_stream_seeds",{}).get(policy_id) != row.seed_digest
                or seed["protocol"] != PROTOCOL or seed["bot_id"] != bot.id or seed["policy_id"] != policy_id
                or authorization["source"] != source or datetime.fromisoformat(seed["approved_at"]) != row.created_at
                or not seed["origin_observation_digest"]):
            raise ValueError("scope")
        definition = validate_dca(seed["definition"]) if source.get("kind") == "DCA" else validate_strategy(seed["definition"])
        if (content_hash(definition) != source["definition_hash"] or definition != seed["definition"]
                or source["engine_version"] != (dca_engine_for_rule(definition) if source.get("kind") == "DCA" else CURRENT_ENGINE_VERSION)):
            raise ValueError("definition")
        step = INTERVALS[source["timeframe"]]
        stamp = _utc(datetime.fromisoformat(seed["observed_at"]),"observed_at")
        approved = _utc(row.created_at,"approved_at")
        bars,gaps = validate_candles(seed["candles"],source["timeframe"],now=int(approved.timestamp()))
        if gaps or bars[-1].t != seed["candle_t"] or not datetime.fromtimestamp(bars[-1].t+step,timezone.utc) <= stamp <= approved:
            raise ValueError("seed coverage")
        if "volume_gate" in definition and (source["timeframe"] != "1d"
                or len(bars) <= definition["volume_gate"]["period"] or "research_binding" not in authorization):
            raise ValueError("DCA volume scope")
        if "continuity" in authorization:
            parent = authorization["continuity"]["market_pointer"]
            previous,previous_bars,verified_parent = _read_market_stream(db,parent["policy_id"],
                through=parent["sequence_no"],parents=parents|{policy_id},records=records)
            expected = {"protocol":PROTOCOL,"bot_id":bot.id,"policy_id":policy_id,"source":previous["source"],
                "definition":previous["definition"],"candles":[bar.json() for bar in previous_bars],
                "candle_t":parent["candle_t"],"observed_at":parent["observed_at"],
                "origin_observation_digest":parent["digest"],"approved_at":approved.isoformat()}
            if verified_parent != parent or seed != expected: raise ValueError("renewed market seed")
        pointer = {"policy_id":policy_id,"sequence_no":0,"digest":row.seed_digest,"candle_t":bars[-1].t,"observed_at":stamp.isoformat()}
        head = state["market_heads"][policy_id]
        if through is not None and (type(through) is not int or not 0 <= through <= head["sequence_no"]):
            raise ValueError("prefix")
        query = select(RealSpotBotMarketObservation).where(RealSpotBotMarketObservation.policy_id == policy_id)
        if through is not None: query = query.where(RealSpotBotMarketObservation.sequence_no <= through)
        for item in db.scalars(query.order_by(RealSpotBotMarketObservation.sequence_no)).all():
            payload = decoded(item.payload_json,item.payload_digest)
            checked = _utc(item.observed_at,"observed_at")
            if (item.sequence_no != pointer["sequence_no"]+1 or item.candle_t != bars[-1].t+step
                    or item.previous_digest != pointer["digest"] or payload != {
                        "protocol":PROTOCOL,"policy_id":policy_id,"sequence_no":item.sequence_no,
                        "previous_digest":pointer["digest"],"candle":payload.get("candle"),"observed_at":checked.isoformat()}
                    or payload["candle"]["t"] != item.candle_t or checked < max(approved,stamp)
                    or checked < datetime.fromtimestamp(item.candle_t+step,timezone.utc)):
                raise ValueError("chain")
            added,missing = validate_candles([bar.json() for bar in bars[-2:]]+[payload["candle"]],source["timeframe"],now=int(checked.timestamp()))
            if missing: raise ValueError("coverage")
            bars.append(added[-1]); stamp = checked
            pointer = {"policy_id":policy_id,"sequence_no":item.sequence_no,"digest":item.payload_digest,
                       "candle_t":item.candle_t,"observed_at":checked.isoformat()}
        if through is not None and pointer["sequence_no"] != through: raise ValueError("missing observation")
        if through is None and pointer != head: raise ValueError("head")
        return seed,bars,pointer
    except (KeyError,ValueError,TypeError,IndexError,StopIteration) as exc:
        if isinstance(exc,SpotBotStoreError): raise
        raise SpotBotStoreError("El archivo de mercado REAL del bot no es verificable") from exc


def _observation(seed:dict,bars:list,pointer:dict) -> SpotBotObservation|DcaBotObservation:
    source = seed["source"]
    stamp = datetime.fromisoformat(pointer["observed_at"])
    common = {key:source[key] for key in ("session_id","version_id","dataset_id","dataset_hash","source_run_id",
                                          "engine_version","symbol","timeframe","definition_hash")}
    common.update(definition=seed["definition"],candles=tuple(bars),candle_t=bars[-1].t,observed_at=stamp,
                  observation_digest=pointer["digest"],market_observation=pointer)
    if source.get("kind") == "DCA": return DcaBotObservation(**common,run_hash=source["run_hash"])
    indicators = IndicatorCache(bars)
    entry,evidence = evaluate(seed["definition"]["entry"],bars,len(bars)-1,indicators,base_timeframe=source["timeframe"])
    exit_match,exit_evidence = (evaluate(seed["definition"]["exit"],bars,len(bars)-1,indicators,base_timeframe=source["timeframe"])
                               if "exit" in seed["definition"] else (None,None))
    return SpotBotObservation(**common,entry_match=entry,entry_evidence=evidence,exit_match=exit_match,exit_evidence=exit_evidence)


def observe_bot_market(vault,bot_id:str,claim:str,*,candles:Callable,clock:Callable,assert_active:Callable):
    """Añade como máximo una vela contigua; nunca recupera huecos para operar."""
    from app.live_execution.spot_bot_store import SpotBotStoreError,assert_spot_bot_cycle,bot_record,encoded
    assert_active()
    with vault.session() as db:
        _,config,_ = bot_record(db,bot_id)
        seed,bars,pointer = read_market_stream(db,config["policy_id"])
        from app.live_execution.journal import _stored_policy
        max_age = _stored_policy(db,config["policy_id"]).max_data_age_seconds
    now = _utc(clock(),"now"); step = INTERVALS[seed["source"]["timeframe"]]
    due = int(now.timestamp())//step*step-step
    if bars[-1].t > due or now < datetime.fromisoformat(pointer["observed_at"]):
        raise SpotBotStoreError("El reloj de mercado REAL retrocedió")
    if due > bars[-1].t+step: raise SpotBotStoreError("Hueco de velas REAL; revisa el bot antes de continuar")
    if due == bars[-1].t: return _observation(seed,bars,pointer)
    assert_active()
    # Verify the whole denominator for relative volume before using a new close.
    volume = seed["definition"].get("volume_gate")
    count = volume["period"] if volume else 2
    raw = candles(seed["source"]["symbol"],timeframe=seed["source"]["timeframe"],
                  limit=count+1,since=bars[-count].t,before=due+step)
    checked = _utc(clock(),"now")
    try:
        if checked < now or int(checked.timestamp())//step*step-step != due or not isinstance(raw,list) or len(raw)>count+2:
            raise ValueError("window")
        if any(not isinstance(row,dict) or type(row.get("t")) is not int or row["t"] > due+step for row in raw):
            raise ValueError("rows")
        closed = [row for row in raw if row["t"]+step <= int(checked.timestamp())]
        if len(closed) == count:
            overlap,gaps = validate_candles([bars[-count-1].json()]+closed,seed["source"]["timeframe"],now=int(checked.timestamp()))
            if not gaps and overlap == bars[-count-1:] and (checked-datetime.fromtimestamp(due+step,timezone.utc)).total_seconds() <= max_age:
                # El proveedor aún no ha publicado el cierre. No comprar ni
                # inventar una vela; seguir consultando dentro de la vigencia.
                return _observation(seed,bars,pointer)
        downloaded,gaps = validate_candles(closed,seed["source"]["timeframe"],now=int(checked.timestamp()))
        if (gaps or len(downloaded) != count+1 or downloaded[:-1] != bars[-count:] or downloaded[-1].t != due):
            raise ValueError("overlap")
    except (TypeError,ValueError,KeyError,IndexError):
        raise SpotBotStoreError("Velas MEXC REAL incompletas, cambiadas o de otra ventana") from None
    assert_active()
    next_bar = downloaded[-1]
    payload = {"protocol":PROTOCOL,"policy_id":config["policy_id"],"sequence_no":pointer["sequence_no"]+1,
               "previous_digest":pointer["digest"],"candle":next_bar.json(),"observed_at":checked.isoformat()}
    raw_json,digest = encoded(payload)
    with vault.session() as db:
        try:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            assert_active(); assert_spot_bot_cycle(vault,bot_id,claim,db=db)
            _,_,latest = read_market_stream(db,config["policy_id"])
            # El claim excluye competidores; cotejar también la revisión del archivo.
            if latest != pointer: raise SpotBotStoreError("El mercado REAL cambió durante la consulta")
            db.add(RealSpotBotMarketObservation(policy_id=config["policy_id"],sequence_no=payload["sequence_no"],
                candle_t=next_bar.t,payload_json=raw_json,payload_digest=digest,previous_digest=pointer["digest"],observed_at=checked))
            row,_,state = bot_record(db,bot_id)
            state["market_heads"][config["policy_id"]] = {"policy_id":config["policy_id"],"sequence_no":payload["sequence_no"],
                "digest":digest,"candle_t":next_bar.t,"observed_at":checked.isoformat()}
            row.state_json,row.state_digest = encoded(state)
            db.commit()
        except Exception:
            db.rollback(); raise
    bars.append(next_bar)
    return _observation(seed,bars,{"policy_id":config["policy_id"],"sequence_no":payload["sequence_no"],
        "digest":digest,"candle_t":next_bar.t,"observed_at":checked.isoformat()})


def verify_market_decision(db,payload:dict,policy_id:str) -> None:
    from app.live_execution.spot_bot_store import SpotBotStoreError
    pointer = payload.get("market_observation")
    if pointer is None: raise SpotBotStoreError("La decisión perdió su observación de mercado REAL")
    try:
        if pointer["policy_id"] != policy_id or type(pointer["sequence_no"]) is not int or pointer["sequence_no"] < 0:
            raise ValueError("pointer")
        seed,bars,verified = read_market_stream(db,policy_id,through=pointer["sequence_no"])
        if (verified != pointer or payload["observation_digest"] != pointer["digest"]
                or payload["candle_t"] != bars[-1].t or payload["source"] != seed["source"]):
            raise ValueError("observation")
        if _utc(datetime.fromisoformat(payload["real_quote"]["at"]),"quote_at") < datetime.fromisoformat(pointer["observed_at"]):
            raise ValueError("quote before observation")
        rule = seed["definition"]
        if seed["source"].get("kind") == "DCA" and dca_signal_window_key(rule):
            from decimal import Decimal
            factor, rejection, evidence = live_dca_signal(rule, bars)
            detail = payload["dca"]
            base = (rule["amount"] if detail["kind"] == "BASE" else
                    next(item["amount"] for item in rule["dips"] if item["drop_pct"] == detail["drop_pct"]))
            requested = adjusted_dca_request(Decimal(base), factor)
            if (rejection is not None or payload["reason"] != "DCA_" + detail["kind"]
                    or detail["signal_close"] != str(bars[-1].c)
                    or detail["base_requested"] != base or detail["requested"] != str(requested)
                    or not Decimal(0) < Decimal(detail["budget_limit"]) <= requested
                    or any(key not in detail or detail[key] != value for key, value in evidence.items())):
                raise ValueError("DCA signal")
    except (KeyError,ValueError,TypeError,StopIteration,ArithmeticError):
        raise SpotBotStoreError("La decisión no corresponde a su mercado REAL archivado") from None
