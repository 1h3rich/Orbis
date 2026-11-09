"""Genuine pending bot send, signed replacement and sealed result in owned EVM."""
from contextlib import contextmanager, nullcontext
from datetime import timedelta

from eth_account import Account
from eth_utils import to_checksum_address
from sqlalchemy import select

from app.live_execution.dex_bot_worker import DexBotWorker
from app.live_execution.dex_call_resolution import collect_call_resolution
from app.live_execution.dex_state import read_executor_state
from app.portfolio import dex_call_resolution as resolutions
from app.portfolio.models import RealDexOperatorIntent
from tests.dex_bot_worker_fixture import running_case
from tests.dex_dca_worker_fixture import dca_running_case


def make_worker(case, run_id, claim):
    values, _, _, connection, clock, _, market = case
    return DexBotWorker(values[0], connection, run_id=run_id, account_id=values[1],
        claim=claim, wallet_address=values[4]['owner'], market=market,
        rpc_factory=lambda:nullcontext(values[5]), clock=lambda:clock[0])


@contextmanager
def recovered_case(directory, monkeypatch, *, dca=False, outcome='EXECUTED', research=False,
                   gas_price=1200000000, extra_native_wei=0):
    factory = dca_running_case if dca else running_case
    with factory(directory, evm_backend='anvil', policy_changes={'max_gas_price':str(2*10**9)},research=research) as case:
        values, started, claim, _, clock, mine, _ = case
        vault, account, manager, draft, meta, rpc, deployment, binding = values
        monkeypatch.setenv('ORBIS_BSC_RPC_URL', meta['rpc'])
        worker = make_worker(case, started['id'], claim)
        try:
            if extra_native_wei:
                assert rpc('orbis_fixture_identity')=={'id':meta['fixtureId'],'chain':56}
                sent=rpc('eth_sendTransaction',[{'from':meta['owner'],'to':draft['operator_address'],
                    'value':hex(extra_native_wei),'gas':'0x5208','gasPrice':hex(10**9)}])
                assert rpc('eth_getTransactionReceipt',[sent])['status']=='0x1'
            clock[0] += timedelta(seconds=1)
            mine()
            rpc('miner_stop', [])
            result = worker.run_once()
            assert result['pending_receipt'] and result['last_action'] == 'BUY', result
            with vault.session() as db:
                raw = db.scalar(select(RealDexOperatorIntent).where(RealDexOperatorIntent.grant_id==binding['grant_id']))
                parent, terms, _, source = resolutions.source_record(db, 'OPERATOR', raw.id, owner=meta['owner'])
                before = (parent.payload_json, parent.state_json, parent.updated_at)
            call = source.expected_call()
            fields = {'to':to_checksum_address(draft['operator_address'] if outcome=='CANCELLED' else call['to']),
                'data':'0x' if outcome=='CANCELLED' else call['data'], 'value':0, 'chainId':56,
                'nonce':source.nonce, 'gas':21000 if outcome=='CANCELLED' else int(terms['gas'])+1000,
                'gasPrice':gas_price}
            if outcome=='REVERTED':
                fields.update(data='0xdeadbeef', gas=60000)
            with manager._copy(draft['id'], account, meta['owner'], clock[0]) as (_, key):
                signed = Account.sign_transaction(fields, bytes(key))
            replacement = rpc('eth_sendRawTransaction', ['0x'+signed.raw_transaction.hex()])
            clock[0] += timedelta(seconds=2)
            mine()
            rpc('miner_start', [1])
            worker.stop()
            clock[0] += timedelta(seconds=1)
            scope = dict(account_id=account, wallet_address=meta['owner'], accept_changes=True,
                evidence=collect_call_resolution(rpc, source, replacement), now=clock[0])
            with vault.session() as db:
                preview = resolutions.preview_resolution(db, 'OPERATOR', parent.id, **scope)
            with vault.session() as db:
                done = resolutions.confirm_resolution(db, 'OPERATOR', parent.id,
                    preview_digest=preview['preview_digest'], source_state_digest=preview['source_state_digest'], **scope)
            assert done['outcome']==outcome
            def snapshot():
                return read_executor_state(rpc, meta['owner'], int(deployment['nonce']), 500,
                    candidate_operator=draft['operator_address'], now=clock[0], block_tag='finalized')
            yield {'case':case, 'source_id':parent.id, 'original':before, 'result':done,
                   'accepted_at':scope['now'], 'snapshot':snapshot}
        finally:
            worker.stop()
