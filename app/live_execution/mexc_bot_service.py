"""Servicio local para autorizar, consultar y detener bots MEXC nativos."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from threading import Event, Lock, RLock
import time
from uuid import UUID, uuid5

from sqlalchemy import select

from app.automation.spot_paper_access import spot_paper_format
from app.live_execution.mexc_bot_supervisor import MexcBotPermissionError, MexcBotSupervisor
from app.live_execution.mexc_spot_bot import activate_mexc_spot_bot
from app.live_execution.spot_bot_rearm import prior_rearm, rearm_mexc_spot_bot, rearm_request
from app.live_execution.policy import _money,_utc
from app.live_execution.dca_bot_source import DcaBotSource,verified_native_bot_observation as verified_spot_bot_observation
from app.live_execution.spot_bot_store import (
    LEGACY_CANCEL_REVIEWS, LEGACY_CANCEL_STATE, LEGACY_FILL_RECOVERIES, SpotBotStoreError, bot_authorizations, bot_record, decoded,
    owned_spot_bot_ledger, pause_spot_bot,bot_decision_payload,
)
from app.portfolio.models import RealSpotBot, RealSpotBotDecision, RealLiveSpotIntent, RealLiveOrderObservation
from app.portfolio.vault import VaultError
from app.research.mexc_binding import archived_dca_anchor, check_mexc_research, mexc_research_options


POLICY_NAMESPACE = UUID("38870fac-c26f-4b5e-a02a-52c4c79e5d5c")


class MexcBotService:
    def __init__(self, vault, paper, *, quote_get=None, rules_fetch=None,
                 supervisor_factory=MexcBotSupervisor, clock=None,dca_paper=None):
        self.vault, self.paper = vault, paper
        self.dca_source = DcaBotSource(dca_paper, allow_moving_average=True, allow_volume=True) if dca_paper is not None else None
        self.quote_get, self.rules_fetch = quote_get, rules_fetch
        self.factory = supervisor_factory
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._workers: dict[str, MexcBotSupervisor] = {}
        self._accounts: dict[str, str] = {}
        self._reviews: dict[str, tuple[str, Event]] = {}
        self._operational:dict[str,dict] = {}
        self.on_operational_idle = None
        self._lock = RLock()
        self._registry_lock = Lock()

    def _worker(self, bot_id):
        # Nunca mantener este cerrojo mientras se espera SQLite o una hebra.
        with self._registry_lock:
            return self._workers.get(bot_id)

    @contextmanager
    def _review_permission(self, account_id, bot_id):
        # Visible para STOP antes de esperar cualquier cerrojo/lectura larga.
        # No sustituye el vínculo confiable de una hebra ya existente.
        cancelled = Event()
        with self._registry_lock:
            if bot_id in self._reviews:
                raise SpotBotStoreError("Ya hay una revisión de este bot en curso")
            self._reviews[bot_id] = (account_id, cancelled)
        try:
            yield cancelled
        finally:
            with self._registry_lock:
                self._reviews.pop(bot_id, None)

    def _repository(self,kind):
        if kind not in ("STRATEGY","DCA"): raise SpotBotStoreError("Tipo de bot no admitido")
        return self.dca_source if kind == "DCA" else self.paper

    def _paper_authorized(self,kind="STRATEGY") -> None:
        repository = self._repository(kind)
        if (repository is None or spot_paper_format(repository.path) != "encrypted_or_invalid"
                or repository.current_passphrase() is None):
            if kind == "DCA": raise MexcBotPermissionError("Desbloquea DCA cifrado antes de operar con su bot")
            raise MexcBotPermissionError("Desbloquea Spot PAPER cifrado antes de operar con un bot")

    def dca_versions(self):
        self._paper_authorized("DCA")
        return [{**version,"name":rule["name"],"kind":"DCA"} for rule in self.dca_source.store.rules()
                for version in self.dca_source.store.versions(rule["id"])]

    def candidates(self) -> list[dict]:
        results = []
        available = False
        for kind in ("STRATEGY","DCA"):
            try: self._paper_authorized(kind)
            except MexcBotPermissionError: continue
            available = True
            repository = self._repository(kind)
            versions = {item["id"]:item for item in (self.dca_versions() if kind == "DCA" else repository.lab.all_versions())}
            for item in repository.sessions():
                if item["status"] != "ACTIVE": continue
                try:
                    source = verified_spot_bot_observation(repository,item["id"],now=self.clock(),verify_backtest=kind == "DCA")
                    options = mexc_research_options(repository,source)
                    result = {**source.binding(),'paper_ready':True,'research_options':options,
                              "ready":bool(options),"reason":None if options else 'Guarda una validación fuera de muestra compatible en LAB o DCA'}
                except Exception:
                    result = {"session_id":item["id"],"version_id":item["version_id"],"symbol":item["symbol"],
                              "timeframe":"1d" if kind == "DCA" else item["timeframe"],"ready":False,
                              'paper_ready':False,'research_options':[],
                              "reason":"Requiere backtest fijado y observación PAPER verificable"}
                version = versions.get(item["version_id"],{})
                result.update(name=version.get("name","DCA" if kind == "DCA" else "Estrategia"),
                              version=version.get("version"),source_kind=kind)
                results.append(result)
        if not available: self._paper_authorized()
        return results

    def research_review(self, account_id: str, session_id: str, validation_id: str, *,
                        kind: str, bot_id: str | None, generation: int) -> dict:
        from app.portfolio.models import RealAccount

        with self.vault.guard_session_generation(generation), self.vault.session() as db:
            if db.get(RealAccount, account_id) is None:
                raise SpotBotStoreError("Cuenta REAL no encontrada")
            config = None
            if bot_id is not None:
                row, config, _ = bot_record(db, bot_id)
                if row.account_id != account_id or config['source'].get('kind', 'STRATEGY') != kind:
                    raise SpotBotStoreError("El bot no corresponde a esta cuenta y origen")
        self._paper_authorized(kind)
        repository = self._repository(kind)
        source = verified_spot_bot_observation(repository, session_id, now=self.clock(), verify_backtest=True)
        if config is not None and any(getattr(source, field) != config['source'][field]
                                     for field in ('version_id', 'symbol', 'timeframe', 'definition_hash', 'engine_version')):
            raise SpotBotStoreError("La observación PAPER no corresponde a la estrategia del bot")
        anchor = archived_dca_anchor(config) if config is not None else None
        checked = check_mexc_research(repository, source, validation_id, dca_anchor=anchor)
        self._paper_authorized(kind)
        return checked.binding.exported()

    def _summary(self, bot_id: str) -> dict:
        with self.vault.session() as db:
            row, config, cursor = bot_record(db, bot_id)
            result = {"id":bot_id, "account_id":row.account_id, "policy_id":row.policy_id,
                      "symbol":config["source"]["symbol"], "timeframe":config["source"]["timeframe"],
                      "paper_session_id":config["source"]["session_id"],
                      "strategy_version_id":config["source"]["version_id"],
                      "source_kind":"DCA" if config["source"].get("kind") == "DCA" else "STRATEGY",
                      "dca_limits":config.get("dca_limits"),
                      "research_binding":config.get('research_binding'),
                      "entry_budget_quote":config["entry_budget_quote"],
                      "execution_mode":("CONTINUOUS" if config.get("execution_permission",{}).get("protocol") == "CONTINUOUS_V1"
                                        else "OPERATIONAL" if config.get("execution_permission") else "SESSION"),
                      "execution_permission":config.get("execution_permission"),
                      "stored_status":row.status, "reason":cursor["reason"],
                      "cycle_in_progress":row.cycle_id is not None,
                      "last_decision_id":cursor["last_decision_id"], "created_at":row.created_at.isoformat()}
            history = bot_authorizations(config)
            result["reauthorizations"] = sum("request_id" in item for item in history)
            result["renewals"] = sum("continuity" in item for item in history)
            from app.live_execution.journal import _stored_policy
            result["policy_expires_at"] = _stored_policy(db,row.policy_id).expires_at.isoformat()
            result["last_review"] = history[-1].get("review")
            result["market_tracking"] = "PAPER_LEGACY"
            if config["policy_id"] in config.get("market_stream_seeds",{}):
                result["market_tracking"] = "OWN_REAL"
                from app.live_execution.spot_bot_market import read_market_stream
                try:
                    _,_,pointer = read_market_stream(db,config["policy_id"])
                    result["market_observation"] = {**pointer,"verified":True}
                except SpotBotStoreError:
                    result["market_observation"] = {"verified":False}
        worker = self._worker(bot_id)
        worker_state = worker.state() if worker is not None else {"running":False,"status":"NOT_AUTHORIZED"}
        result["worker"] = worker_state
        if result["execution_permission"] is not None and "permission_remaining_seconds" in worker_state:
            remaining = int((datetime.fromisoformat(result["execution_permission"]["expires_at"])-self.clock()).total_seconds())
            worker_state["permission_remaining_seconds"] = max(0,min(worker_state["permission_remaining_seconds"],remaining))
        result["status"] = ("RUNNING" if result["stored_status"] == "ACTIVE" and worker_state["running"]
                            and not worker_state.get("stop_requested") and worker_state.get("permission_remaining_seconds",0) > 0
                            and worker_state.get("evidence_remaining_seconds",1)>0
                            else "REARM_REQUIRED" if result["stored_status"] == "ACTIVE" else result["stored_status"])
        result["execution_enabled"] = result["status"] == "RUNNING"
        try:
            ledger, _ = owned_spot_bot_ledger(self.vault, bot_id)
            result["position"] = {"units":str(ledger.position.units), "cost_quote":str(ledger.position.cost_quote),
                                  "realized_pnl_quote":str(ledger.realized_pnl_quote), "fill_count":ledger.fill_count,
                                  "verified":True}
            if ledger.manual_sale_count:
                result["position"]["manual_sale_count"] = ledger.manual_sale_count
        except SpotBotStoreError:
            result["position"] = {"units":None,"cost_quote":None,"realized_pnl_quote":None,"fill_count":None,"verified":False}
        return result

    def list(self, account_id: str) -> list[dict]:
        with self.vault.session() as db:
            ids = db.scalars(select(RealSpotBot.id).where(RealSpotBot.account_id == account_id).order_by(
                RealSpotBot.created_at.desc()).limit(100)).all()
        return [self._summary(identifier) for identifier in ids]

    def decisions(self, account_id: str, bot_id: str) -> list[dict]:
        with self.vault.session() as db:
            row, config, _ = bot_record(db, bot_id)
            if row.account_id != account_id:
                raise SpotBotStoreError("El bot no pertenece a esta cuenta")
            rows = db.scalars(select(RealSpotBotDecision).where(RealSpotBotDecision.bot_id == bot_id).order_by(
                RealSpotBotDecision.created_at.desc(), RealSpotBotDecision.id.desc()).limit(100)).all()
            result = []
            for item in rows:
                payload = bot_decision_payload(db,item)
                intent = db.get(RealLiveSpotIntent, item.intent_id)
                legacy = config.get(LEGACY_CANCEL_REVIEWS, {}).get(item.intent_id)
                if legacy is not None:
                    result.append({**payload,"journal_status":LEGACY_CANCEL_STATE,"external_status":"CANCELED",
                                   "observed_fills":0,"legacy_checked_at":legacy["checked_at"]})
                    continue
                if intent is not None and payload.get("request_digest") != intent.request_digest:
                    raise SpotBotStoreError("La decisión no coincide con su pedido")
                observed = db.scalar(select(RealLiveOrderObservation).where(
                    RealLiveOrderObservation.intent_id == item.intent_id).order_by(
                        RealLiveOrderObservation.observed_at.desc()).limit(1))
                result.append({**payload,"journal_status":intent.status if intent else "NOT_RESERVED",
                               "external_status":observed.status if observed else None,
                               "observed_fills":observed.fill_count if observed else None,
                               **({"legacy_fills_recovered":True} if item.intent_id in config.get(LEGACY_FILL_RECOVERIES, {}) else {})})
            return result

    def activate(self, account_id: str, view, trade, *, bot_id: str, draft_id: str, anchor_id: str,
                 paper_session_id: str, entry_budget_quote: Decimal, deadline: float,
                 source_kind="STRATEGY",dca_limits=None,execution_mode="SESSION",continuous_until:datetime|None=None,
                 research_validation_id:str|None=None, research_review_digest:str|None=None) -> dict:
        _money(entry_budget_quote, "entry_budget_quote", positive=True)
        bot_id = str(UUID(bot_id))
        request = {"draft_id":draft_id,"anchor_id":anchor_id,"paper_session_id":paper_session_id}
        if research_validation_id is not None:
            request['research_validation_id'] = str(UUID(research_validation_id))
        if research_review_digest is not None:
            request['research_review_digest'] = research_review_digest
        if execution_mode == "CONTINUOUS":
            if continuous_until is None: raise SpotBotStoreError("Indica hasta cuándo autorizas la continuidad")
            request.update(execution_mode=execution_mode,continuous_until=_utc(continuous_until,"continuous_until").isoformat())
        elif continuous_until is not None: raise SpotBotStoreError("La fecha de continuidad exige su permiso separado")
        elif execution_mode == "OPERATIONAL": request["execution_mode"] = execution_mode
        elif execution_mode != "SESSION": raise SpotBotStoreError("Modo de permiso desconocido")
        if source_kind == "DCA": request.update(source_kind=source_kind,dca_limits=dca_limits)
        elif source_kind != "STRATEGY" or dca_limits is not None: raise SpotBotStoreError("Límites DCA fuera de un bot DCA")
        with self._lock:
            with self.vault.session() as db:
                prior = db.get(RealSpotBot, bot_id)
                if prior is not None:
                    _, config, _ = bot_record(db, bot_id)
                    if (prior.account_id != account_id or config.get("activation_request") != request
                            or Decimal(bot_authorizations(config)[0]["entry_budget_quote"]) != entry_budget_quote):
                        raise SpotBotStoreError("El ID de activación ya se usó con otros datos")
                    return self._summary(bot_id)  # Nunca arranca otra hebra en una repetición.
            if research_validation_id is None:
                raise SpotBotStoreError("Selecciona una validación fuera de muestra antes de activar el bot")
            self._paper_authorized(source_kind)
            from app.portfolio.models import RealMexcAccountLink
            from app.live_execution.journal import _stored_policy
            with self.vault.session() as db:
                draft = _stored_policy(db, draft_id)
                link = db.get(RealMexcAccountLink, draft.account_ref)
                if link is None or link.account_id != account_id:
                    raise SpotBotStoreError("Los límites no corresponden a la cuenta del bot")
            activate_mexc_spot_bot(self.vault, view, trade, self._repository(source_kind),
                draft_id=draft_id, anchor_id=anchor_id, policy_id=str(uuid5(POLICY_NAMESPACE,bot_id)), bot_id=bot_id,
                paper_session_id=paper_session_id, entry_budget_quote=entry_budget_quote, clock=self.clock,
                quote_get=self.quote_get, rules_fetch=self.rules_fetch, activation_request=request,dca_limits=dca_limits,
                execution_mode=execution_mode,continuous_until=continuous_until,
                research_validation_id=research_validation_id,research_review_digest=research_review_digest)

            policy_id = str(uuid5(POLICY_NAMESPACE,bot_id))
            try:
                return self._launch(account_id,bot_id,policy_id,view,trade,deadline)
            except Exception:
                pause_spot_bot(self.vault,bot_id,now=self.clock(),reason="WORKER_START_FAILED",only_if_active=True,expected_policy_id=policy_id)
                raise

    def _launch(self, account_id, bot_id, policy_id, view, trade, deadline, *, extra_authorized=None):
        review_authorized = extra_authorized
        with self.vault.session() as db:
            _,config,_ = bot_record(db,bot_id)
        kind = "DCA" if config["source"].get("kind") == "DCA" else "STRATEGY"
        permission = config.get("execution_permission")
        if time.monotonic() >= deadline: raise MexcBotPermissionError("La revisión humana caducó")
        operational_options = {}
        if permission is not None:
            expires = datetime.fromisoformat(permission["expires_at"])
            approved = datetime.fromisoformat(permission["approved_at"])
            remaining = (expires-self.clock()).total_seconds()
            if remaining <= 0: raise MexcBotPermissionError("La concesión operativa caducó")
            deadline = time.monotonic()+remaining
            repository = self._repository(kind)
            factory = repository.paper.adapter_factory if kind == "DCA" else repository.adapter_factory
            def operational_authorized():
                at = self.clock()
                if not approved <= at < expires: raise MexcBotPermissionError("La concesión operativa caducó o cambió el reloj")
            operational_options = {"execution_permission":permission,"market_candles":factory().get_candles}
            if permission.get("protocol") == "CONTINUOUS_V1":
                from app.live_execution.journal import _stored_policy
                with self.vault.session() as db:
                    operational_options["policy_expires_at"] = _stored_policy(db,policy_id).expires_at
            extra_authorized = operational_authorized
        def exited():
            try:
                pause_spot_bot(self.vault, bot_id, now=self.clock(), reason="WORKER_STOPPED", only_if_active=True,
                              expected_policy_id=worker.state().get("policy_id") or policy_id)
            finally:
                with self._registry_lock:
                    if self._operational.get(bot_id) == permission: self._operational.pop(bot_id,None)
                if permission is not None and self.on_operational_idle is not None: self.on_operational_idle()

        try:
            worker = self.factory(self.vault, view, trade, None if permission is not None else self._repository(kind), bot_id, deadline=deadline,
                quote_get=self.quote_get, rules_fetch=self.rules_fetch, clock=self.clock,
                extra_authorized=extra_authorized or (lambda:self._paper_authorized(kind)), on_exit=exited,
                expected_policy_id=policy_id,**operational_options)
            with self._registry_lock:
                self._workers[bot_id] = worker
                self._accounts[bot_id] = account_id
                if permission is not None: self._operational[bot_id] = permission
                else: self._operational.pop(bot_id,None)
            summary = self._summary(bot_id)  # Antes de que la hebra reclame la bóveda.
            if extra_authorized is not None:
                extra_authorized()
            if review_authorized is not None: review_authorized()
            worker.start()
        except Exception:
            with self._registry_lock:
                if self._operational.get(bot_id) == permission: self._operational.pop(bot_id,None)
            if "worker" in locals(): worker.request_stop()
            pause_spot_bot(self.vault, bot_id, now=self.clock(), reason="WORKER_START_FAILED",
                          expected_policy_id=policy_id)
            raise SpotBotStoreError("No se pudo iniciar el trabajador; el bot quedó pausado") from None
        summary["worker"] = worker.state()
        summary["execution_enabled"] = bool(summary["worker"]["running"])
        summary["status"] = "RUNNING" if summary["execution_enabled"] else "REARM_REQUIRED"
        return summary

    def rearm(self, account_id, bot_id, view, trade, *, request_id, draft_id, anchor_id,
              paper_session_id, entry_budget_quote, deadline, generation,execution_mode="SESSION",continuous_until=None,
              research_validation_id=None,allocation_review_digest=None,research_review_digest=None):
        _money(entry_budget_quote, "entry_budget_quote", positive=True)
        request_id = str(UUID(request_id))
        request = rearm_request(draft_id, anchor_id, paper_session_id, entry_budget_quote,execution_mode,continuous_until,
                                research_validation_id,allocation_review_digest,research_review_digest)
        with self._review_permission(account_id, bot_id) as cancelled, self._lock:
            if cancelled.is_set():
                raise MexcBotPermissionError("Revisión cancelada")
            with self.vault.session() as db:
                row, config, _ = bot_record(db, bot_id)
                if row.account_id != account_id:
                    raise SpotBotStoreError("El bot no pertenece a esta cuenta")
                if prior_rearm(config, request_id, request) is not None:
                    return self._summary(bot_id)  # Un replay no renueva permiso ni inicia hebras.
                if research_validation_id is None:
                    raise SpotBotStoreError("Selecciona una validación fuera de muestra antes de reanudar el bot")
                kind = "DCA" if config["source"].get("kind") == "DCA" else "STRATEGY"
            worker = self._worker(bot_id)
            if worker is not None and worker.state()["running"]:
                raise SpotBotStoreError("Detén el trabajador antes de revisar el bot")
            def authorized():
                if (cancelled.is_set() or time.monotonic() >= deadline
                        or self.vault.session_generation != generation):
                    raise MexcBotPermissionError("Revisión cancelada o permiso REAL caducado")
                self._paper_authorized(kind)

            policy_id = str(uuid5(POLICY_NAMESPACE, bot_id + ":rearm:" + request_id))
            try:
                authorized()
                result = rearm_mexc_spot_bot(self.vault, view, trade, self._repository(kind), bot_id,
                    request_id=request_id, policy_id=policy_id, draft_id=draft_id, anchor_id=anchor_id,
                    paper_session_id=paper_session_id, entry_budget_quote=entry_budget_quote,
                    assert_authorized=authorized, clock=self.clock, quote_get=self.quote_get, rules_fetch=self.rules_fetch,
                    execution_mode=execution_mode,continuous_until=continuous_until,research_validation_id=research_validation_id,
                    allocation_review_digest=allocation_review_digest,research_review_digest=research_review_digest)
                if result["replayed"]:
                    return self._summary(bot_id)
                authorized()
                return self._launch(account_id, bot_id, policy_id, view, trade, deadline, extra_authorized=authorized)
            except Exception:
                # No altera una autorización distinta que haya ganado desde otro proceso.
                pause_spot_bot(self.vault, bot_id, now=self.clock(), reason="REARM_REVIEW_FAILED",
                              only_if_active=True, expected_policy_id=policy_id)
                raise

    def stop(self, account_id: str, bot_id: str) -> dict:
        # Vínculo registrado durante la activación, sin esperar al cerrojo de
        # bóveda que el ciclo puede mantener durante una consulta de red.
        with self._registry_lock:
            known = self._workers.get(bot_id) if self._accounts.get(bot_id) == account_id else None
            pair = self._reviews.get(bot_id)
            review = pair[1] if pair is not None and pair[0] == account_id else None
        if known is not None:
            known.request_stop()
        if review is not None:
            review.set()
        with self._lock:
            with self.vault.session() as db:
                row, _, _ = bot_record(db, bot_id)
                if row.account_id != account_id:
                    raise SpotBotStoreError("El bot no pertenece a esta cuenta")
            worker = self._worker(bot_id)
            if worker is not None:
                worker.request_stop()
            pause_spot_bot(self.vault, bot_id, now=self.clock())
            if worker is not None:
                worker.join(5)
            return self._summary(bot_id)

    def extend_deadline(self, deadline: float) -> None:
        with self._registry_lock:
            workers = [worker for identifier,worker in self._workers.items() if identifier not in self._operational]
        for worker in workers:
            worker.extend_deadline(deadline)

    def operational_status(self) -> dict:
        with self._registry_lock:
            pairs = [(self._workers.get(identifier),permission) for identifier,permission in self._operational.items()]
        active = [permission for worker,permission in pairs if worker is not None and worker.state()["running"]
                  and not worker.state().get("stop_requested") and worker.state().get("permission_remaining_seconds",0)>0
                  and worker.state().get("evidence_remaining_seconds",1)>0
                  and self.clock() < datetime.fromisoformat(permission["expires_at"])]
        return {"active":bool(active),"count":len(active),"until":min((item["expires_at"] for item in active),default=None)}

    def stop_session_workers(self) -> None:
        with self._registry_lock:
            workers = [(identifier,worker) for identifier,worker in self._workers.items() if identifier not in self._operational]
            reviews = [pair[1] for pair in self._reviews.values()]
        for review in reviews: review.set()
        for _,worker in workers: worker.request_stop()
        for identifier,worker in workers:
            worker.join(5)
            if self.vault.unlocked: pause_spot_bot(self.vault,identifier,now=self.clock(),reason="VIEW_PROTECTED",only_if_active=True)

    def stop_all(self, reason: str = "SESSION_REVOKED", *, account_id: str | None = None) -> None:
        # Revocar ANTES de esperar al servicio: una revisión puede estar en red.
        with self._registry_lock:
            workers = [(identifier,worker) for identifier,worker in self._workers.items()
                       if account_id is None or self._accounts.get(identifier) == account_id]
            reviews = [pair[1] for pair in self._reviews.values()
                       if account_id is None or pair[0] == account_id]
        for review in reviews:
            review.set()
        for _, worker in workers:
            worker.request_stop()
        with self._lock:
            for identifier, worker in workers:
                worker.join(5)
                if self.vault.unlocked:
                    pause_spot_bot(self.vault, identifier, now=self.clock(), reason=reason, only_if_active=True)
            if account_id is not None and self.vault.unlocked:
                with self.vault.session() as db:
                    stale = db.scalars(select(RealSpotBot.id).where(
                        RealSpotBot.account_id == account_id, RealSpotBot.status == "ACTIVE")).all()
                for identifier in stale:
                    pause_spot_bot(self.vault, identifier, now=self.clock(), reason=reason, only_if_active=True)
