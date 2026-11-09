"""One zero-self BNB cancellation POST, with reads before its durable grant."""
from dataclasses import replace
from datetime import datetime,timezone
from eth_utils import keccak
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_gas_cancellation import CancellationPlan,read_cancellation_state,verify_cancellation_signature
from app.live_execution.dex_operator_transport import _SenderRpc
from app.live_execution.evm_transaction import word


class BnbGasCancellationSender:
    def __init__(self,plan:CancellationPlan,*,transport=None,clock=None):
        if type(plan) is not CancellationPlan:raise DexExecutorError('El emisor necesita una cancelación exacta de valor cero')
        self._plan=plan;self._rpc=_SenderRpc(transport=transport)
        self._clock=clock or (lambda:datetime.now(timezone.utc));self._used=False;self._entered=False

    def __enter__(self):
        try:
            if self._used or self._entered:raise DexExecutorError('El emisor de cancelación ya se utilizó')
            self._plan.validate(self._clock())
            state=read_cancellation_state(self._rpc,self._plan.snapshot.owner,self._plan.snapshot.operator,
                nonce=self._plan.original_nonce,now=self._clock())
            previous=self._plan.snapshot
            if state.block_number<previous.block_number or state.block_number==previous.block_number and state.block_hash!=previous.block_hash:
                raise DexExecutorError('El bloque revisado de cancelación cambió')
            replace(self._plan,snapshot=state).validate(self._clock())
            self._entered=True;return self
        except Exception:self._rpc.__exit__(None,None,None);raise

    def __exit__(self,*args):
        self._entered=False;self._rpc.__exit__(*args)

    def send(self,raw:bytes)->str:
        if not self._entered or self._used:raise DexExecutorError('La cancelación no tiene otro POST disponible')
        self._used=True;self._plan.validate(self._clock())
        if type(raw) is not bytes:raise DexExecutorError('Firma de cancelación no válida')
        tx_hash='0x'+keccak(raw).hex();verify_cancellation_signature(self._plan,raw,tx_hash)
        reported=word(self._rpc('eth_sendRawTransaction',['0x'+raw.hex()]))
        if reported!=tx_hash:raise DexExecutorError('Comprueba el hash original de la cancelación BNB')
        return reported
