"""Continuación efímera de decisión/reserva creadas en el mismo commit.

No se serializa ni reconstruye desde la base. No sustituye View/Trade,
preflight, autorización del worker o marca durable del único POST.
"""

from threading import Lock
from weakref import ref

from app.live_execution.journal import RecordedIntent,intent_request_digest
from app.live_execution.policy import LiveSpotIntent
from app.portfolio.models import RealLiveSpotIntent,RealSpotBotDecision


class PreparedSpotBotOrder:
    def __init__(self,*,vault,bot_id: str,cycle_id: str,generation: int,decision_id: str,
                 decision_digest: str,request_digest: str,recorded: RecordedIntent):
        self.bot_id,self.cycle_id,self.generation = bot_id,cycle_id,generation
        self.decision_id,self.decision_digest = decision_id,decision_digest
        self.request_digest,self.recorded = request_digest,recorded
        self._vault = ref(vault)
        self._taken,self._lock = False,Lock()

    def __copy__(self):
        raise TypeError("Una continuación de envío no se copia")

    def __deepcopy__(self,memo):
        raise TypeError("Una continuación de envío no se copia")

    def __reduce_ex__(self,protocol):
        raise TypeError("Una continuación de envío no se serializa")

    def take(self,vault,intent: LiveSpotIntent,decision_id: str) -> None:
        from app.live_execution.spot_bot_store import SpotBotStoreError,validate_bot_order_scope,bot_record,decoded
        if self._vault() is not vault:
            raise SpotBotStoreError("La preparación pertenece a otra instancia de bóveda")
        with vault.guard_session_generation(self.generation),self._lock:
            if (self._taken or self.recorded.status != "RESERVED" or decision_id != self.decision_id
                    or self.recorded.intent_id != intent.intent_id or intent_request_digest(intent) != self.request_digest):
                raise SpotBotStoreError("La preparación del bot no concede otro intento de envío")
            with vault.session() as db:
                bot,_,_ = bot_record(db,self.bot_id)
                row = db.get(RealLiveSpotIntent,intent.intent_id)
                decision = db.get(RealSpotBotDecision,decision_id)
                if (bot.cycle_id != self.cycle_id or bot.status != "ACTIVE" or row is None
                        or row.status != "RESERVED" or row.reason != "AUTHORIZED_FOR_RESERVATION"
                        or row.request_digest != self.request_digest or decision is None or decision.bot_id != self.bot_id
                        or decision.intent_id != intent.intent_id or decision.payload_digest != self.decision_digest):
                    raise SpotBotStoreError("La preparación, ciclo o reserva del bot cambió")
                if decoded(decision.payload_json,decision.payload_digest).get("preparation_cycle_id") != self.cycle_id:
                    raise SpotBotStoreError("La decisión no corresponde al ciclo de preparación")
                validate_bot_order_scope(vault,intent,decision_id=decision_id,db=db)
            self._taken = True
