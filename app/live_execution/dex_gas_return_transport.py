"""One native owner-return POST, separate from the swap transport."""
from datetime import datetime,timezone
from eth_utils import keccak

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_gas_return import GasReturnPlan,verify_return_signature
from app.live_execution.dex_operator_transport import _SenderRpc
from app.live_execution.evm_transaction import quantity,word


class BnbGasReturnSender:
    def __init__(self,plan:GasReturnPlan,*,transport=None,clock=None):
        if type(plan) is not GasReturnPlan:raise DexExecutorError('El emisor necesita una devolución BNB exacta')
        self._plan=plan;self._rpc=_SenderRpc(transport=transport);self._clock=clock or (lambda:datetime.now(timezone.utc));self._used=False

    def __enter__(self):
        # Check chain before the durable POST claim. A failed preliminary read
        # cannot strand a never-sent transfer as an unknown network outcome.
        try:
            self._plan.validate(self._clock())
            if quantity(self._rpc('eth_chainId',[]))!=56:raise DexExecutorError('El RPC no confirmó BNB Chain')
            return self
        except Exception:self._rpc.__exit__(None,None,None);raise

    def __exit__(self,*args):self._rpc.__exit__(*args)

    def send(self,raw:bytes)->str:
        if self._used:raise DexExecutorError('La devolución ya consumió su único POST')
        self._used=True;self._plan.validate(self._clock())
        if type(raw) is not bytes:raise DexExecutorError('Firma de devolución no válida')
        tx_hash='0x'+keccak(raw).hex();verify_return_signature(self._plan,raw,tx_hash)
        reported=word(self._rpc('eth_sendRawTransaction',['0x'+raw.hex()]))
        if reported!=tx_hash:raise DexExecutorError('Comprueba el hash original de la devolución BNB')
        return reported
