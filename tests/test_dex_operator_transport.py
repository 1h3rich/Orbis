"""Production RPC adapter runs only against verified Ganache or mocked HTTP."""
from datetime import datetime,timezone
import json

import httpx
import pytest
from eth_utils import keccak

from app.live_execution import dex_operator_transport as transport
from app.live_execution.dex_artifact import DexExecutorError
from tests.test_dex_operator_journal import case,prepared_execution
from app.portfolio import dex_operator_journal as journal


def test_production_adapter_sends_once_then_fetches_and_verifies_the_actual_evm_receipt(tmp_path,monkeypatch):
    with case(tmp_path) as (vault,account,meta,rpc,deployment,grant,trade):
        monkeypatch.setenv('ORBIS_BSC_RPC_URL',meta['rpc'])
        session,prepared = prepared_execution(vault,account,grant,trade)
        with transport.BnbOperatorSender(trade) as sender:
            result = prepared.broadcast_once(sender.send,now=datetime.now(timezone.utc))
            assert result['send_result'] == 'HASH_MATCHED'
            sent = sender.transaction_hash
            with pytest.raises(DexExecutorError):sender.send(b'not another request')
        with transport.BnbOperatorReadClient() as reader:
            proof = transport.collect_operator_receipt(reader,meta['owner'],int(deployment['nonce']),sent)
        with vault.session() as db:
            checked = journal.check_intent(db,prepared.identifier,account_id=account,evidence=proof,now=datetime.now(timezone.utc))
            assert checked['status'] == 'FINALIZED' and checked['receipt']['amount_out_units'] == str(10**16)
        session.revoke()


@pytest.mark.parametrize('fault',['chain','timeout','wrong-hash'])
def test_adapter_never_retries_an_uncertain_send_and_keeps_provider_errors_out_of_results(tmp_path,fault):
    with case(tmp_path) as (vault,account,meta,rpc,deployment,grant,trade):
        session,prepared = prepared_execution(vault,account,grant,trade);calls = []
        def handler(request):
            body = json.loads(request.content);calls.append(body['method'])
            if body['method'] == 'eth_chainId':value = '0x1' if fault == 'chain' else '0x38'
            elif fault == 'timeout':raise httpx.ReadTimeout('do not expose URL or raw signature',request=request)
            else:value = '0x'+'ab'*32
            return httpx.Response(200,json={'jsonrpc':'2.0','id':body['id'],'result':value})
        with transport.BnbOperatorSender(trade,transport=httpx.MockTransport(handler)) as sender:
            result = prepared.broadcast_once(sender.send,now=datetime.now(timezone.utc))
            assert result['send_result'] == 'UNKNOWN'
            with pytest.raises(DexExecutorError):sender.send(b'anything')
        assert calls.count('eth_sendRawTransaction') == (0 if fault == 'chain' else 1)
        assert 'do not expose' not in str(result)
        session.revoke()
