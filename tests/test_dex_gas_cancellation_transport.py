"""Bounded real RPC adapter; reads may fail before any cancellation grant."""
import json
import httpx
import pytest
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_gas_cancellation_transport import BnbGasCancellationSender
from app.portfolio import dex_gas_cancellation as journal
from tests.dex_gas_cancellation_fixture import pending_return,cancellation_review,cancellation_plan
from tests.test_dex_gas_cancellation_outbox import execution
from tests.test_dex_gas_funding import now


@pytest.mark.parametrize('fault',['none','chain','nonce','queue','balance','code','timeout','wrong-hash'])
def test_sender_rechecks_unconsumed_nonce_and_balance_before_grant_and_never_retries(tmp_path,fault):
    with pending_return(tmp_path) as (values,original,_,_):
        vault,account,_,draft,_,rpc,_,_=values
        review=cancellation_review(values,original);plan=cancellation_plan(values,review)
        packet=execution(values,review,plan);calls=[];operator=draft['operator_address']
        def handler(request):
            body=json.loads(request.content);method,params=body['method'],body['params'];calls.append(method)
            if method=='eth_sendRawTransaction':
                if fault=='timeout':raise httpx.ReadTimeout('private provider diagnostics',request=request)
                value='0x'+'ab'*32 if fault=='wrong-hash' else rpc(method,params)
            elif fault=='chain' and method=='eth_chainId':value='0x1'
            elif fault=='nonce' and method=='eth_getTransactionCount' and params[0]==operator:value=hex(plan.original_nonce+1)
            elif fault=='queue' and method=='eth_getTransactionCount' and params[1]=='pending':value=hex(plan.original_nonce+2)
            elif fault=='balance' and method=='eth_getBalance':value='0x1'
            elif fault=='code' and method=='eth_getCode' and params[0]==operator:value='0x00'
            else:value=rpc(method,params)
            return httpx.Response(200,json={'jsonrpc':'2.0','id':body['id'],'result':value})
        sender=BnbGasCancellationSender(plan,transport=httpx.MockTransport(handler),clock=now)
        if fault in {'chain','nonce','queue','balance','code'}:
            with pytest.raises(DexExecutorError):
                with sender as transport:packet.broadcast_once(transport.send,now=now())
            with vault.session() as db:assert journal.view_cancellation(db,review['id'],now=now())['status']=='SIGNED'
            assert calls.count('eth_sendRawTransaction')==0
        else:
            with sender as transport:
                result=packet.broadcast_once(transport.send,now=now())
                assert result['send_result']==('HASH_MATCHED' if fault=='none' else 'UNKNOWN')
                with pytest.raises(DexExecutorError):transport.send(b'no retry')
            assert calls.count('eth_sendRawTransaction')==1
            assert 'private provider' not in str(result)
        with pytest.raises(DexExecutorError):sender.send(b'not entered')
