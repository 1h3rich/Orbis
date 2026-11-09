"""Authenticated multi-wallet effects preview on signed Anvil + SQLCipher.

These are proposed originals, not saved bookkeeping or a mainnet pilot.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from uuid import uuid4
import json

from eth_account import Account
import pytest
from sqlalchemy import select

from app.portfolio import models as m
from app.portfolio.domain import EventInput
from app.portfolio.economic_external_capture import capture_external_effects, WalletParticipant
from app.portfolio.economic_external_plan import event_id
from app.portfolio.repository import create_account, append_event
from app.portfolio.wallet_links import save_wallet_link
from tests.test_dex_external_observation_api import api_case, owner
from tests.test_dex_external_order_api import ready, order
from tests.test_economic_receipt_source import counts
from tests.test_wallet_funding_api import prove


def key(): return str(uuid4())


@contextmanager
def effects_case(path, *, mode='TRANSFER', paired=True):
    with api_case(path, mode) as c:
        owner(c); order_url = ready(c)
        response = c.client.post(order_url+'/save', headers=c.headers, json=order(c, order_url))
        assert response.status_code == 200, response.text
        observed = c.client.get(c.url+'/saved', headers=c.headers).json()['observation']
        at = datetime.fromisoformat(observed['receipt']['block_at'])
        with c.vault.session() as db:
            ledger = create_account(db, 'Wallet principal', 'USDT').id
            peer = create_account(db, 'Wallet secundaria', 'USDT').id
            save_wallet_link(db, ledger, c.meta['owner'], '0x38', 'Principal'); db.commit()
            save_wallet_link(db, peer, c.meta['operator'], '0x38', 'Secundaria'); db.commit()
        for account, currency, amount in ((ledger, 'USDT', '10'), (ledger, 'BNB', '1'), (peer, 'USDT', '3')):
            with c.vault.session() as db:
                append_event(db, account, key(), EventInput('CONTRIBUTION', currency, at-timedelta(hours=1), amount=D(amount)))
        gas = key(); out = key(); incoming = key(); pair = key()
        body = {'address': c.meta['owner'], 'review_id': key(),
            'new_events': [{'key': gas, 'account_id': ledger, 'kind': 'FEE', 'quote_currency': 'BNB',
                'amount': str(D(observed['receipt']['gas_fee_wei']).scaleb(-18)), 'note': 'Gas externo observado'}],
            'members': [{'event': {'new_key': gas}, 'role': 'GAS'}],
            'annotations': [{'reference': 'unobserved-effects', 'disposition': 'MANUAL_COMPLETE', 'note': 'Revisión manual de cobertura restante'}]}
        if mode == 'TRANSFER':
            amount = str(D(observed['transfers'][0]['units']).scaleb(-18))
            body['new_events'].append({'key': out, 'account_id': ledger,
                'kind': 'CASH_TRANSFER_OUT' if paired else 'WITHDRAWAL', 'quote_currency': 'USDT',
                'amount': amount, **({'pair_key': pair} if paired else {})})
            body['members'].append({'event': {'new_key': out}, 'role': 'EFFECT'})
            refs = [{'new_key': out}]
            if paired:
                body['new_events'].append({'key': incoming, 'account_id': peer, 'kind': 'CASH_TRANSFER_IN',
                    'quote_currency': 'USDT', 'amount': amount, 'pair_key': pair})
                body['members'].append({'event': {'new_key': incoming}, 'role': 'EFFECT'})
                refs.append({'new_key': incoming})
                prove(c.client, c.headers, peer, Account.from_key('0x'+'22'*32))
                body['wallets'] = [{'context_account_id': peer, 'address': c.meta['operator']}]
            body['allocations'] = [{'effect_refs': ['token:'+observed['transfers'][0]['log_ref']],
                'events': refs, 'classification': 'OWN_TRANSFER' if paired else 'EXTERNAL_FLOW',
                'description': 'Traslado entre wallets propias' if paired else 'Salida declarada a tercero'}]
        elif mode == 'APPROVAL':
            body['annotations'].append({'reference': 'approval:'+observed['approvals'][0]['log_ref'],
                'disposition': 'PERMISSION_REVIEWED', 'note': 'Permiso revisado'})
        yield SimpleNamespace(**vars(c), ledger=ledger, peer=peer, at=at, observed=observed, body_review=body,
            effects_url=c.url.removesuffix('/external-observation')+'/external-effects/preview')


def post(c, body=None):
    return c.client.post(c.effects_url, headers=c.headers, json=body or c.body_review)


def test_disconnected_owned_wallet_histories_join_without_capital_profit_or_writes(tmp_path):
    with effects_case(tmp_path) as c:
        before = counts(c.vault); reads = len(c.reads); c.gate['failure'] = True
        result = post(c); assert result.status_code == 200, result.text
        data = result.json(); rows = {item['account_id']: item for item in data['accounts']}
        assert data['root_account_id'] == c.ledger != c.account
        assert set(rows) == {c.ledger, c.peer}
        assert rows[c.ledger]['current']['cash']['USDT'] == '10'
        assert D(rows[c.ledger]['proposed']['cash']['USDT']) == D('9.995')
        assert D(rows[c.peer]['proposed']['cash']['USDT']) == D('3.005')
        for account, amount in ((c.ledger, '10'), (c.peer, '3')):
            assert rows[account]['proposed']['contributions']['USDT'] == amount
            assert rows[account]['proposed']['withdrawals'] == {}
            assert D(rows[account]['proposed']['realized_pnl']['USDT']) == 0
        assert D(rows[c.ledger]['proposed']['realized_pnl']['BNB']) == -D(c.observed['receipt']['gas_fee_wei']).scaleb(-18)
        assert D(rows[c.ledger]['proposed']['cash']['BNB']) == 1-D(c.observed['receipt']['gas_fee_wei']).scaleb(-18)
        assert data['events_to_create'] == 3 and data['manual_review_complete']
        assert data['acceptance_available']and data['can_accept']
        for flag in ('saved', 'accounting_acceptance_active', 'effects_proven_complete',
                'strategy_executed', 'resolves_intent', 'moves_funds', 'trading_authority'): assert data[flag] is False
        assert result.headers['cache-control'] == 'no-store'
        for raw in (c.meta['owner'], c.meta['operator'], c.hash): assert raw.lower() not in result.text.lower()
        assert counts(c.vault) == before and len(c.reads) == reads
        repeat = post(c, {**c.body_review, 'recorded_at': data['recorded_at']})
        assert repeat.status_code == 200 and repeat.json() == data
        assert c.client.get(c.url+'/saved', headers=c.headers).json()['status'] == 'EFFECTS_PENDING'


def test_every_peer_needs_session_proof_exact_context_and_real_local_wallet_link(tmp_path):
    with effects_case(tmp_path) as c:
        body = deepcopy(c.body_review)
        body['wallets'][0]['context_account_id'] = c.account
        assert post(c, body).status_code == 409
        with c.vault.session() as db:
            row = db.scalar(select(m.RealWalletLink).where(m.RealWalletLink.account_id == c.peer)); db.delete(row); db.commit()
        assert post(c).status_code == 409
        assert c.client.post(c.effects_url, json=c.body_review).status_code == 401
        assert c.client.post(c.effects_url, headers={**c.headers, 'Origin': 'https://outside.invalid'}, json=c.body_review).status_code == 403


@pytest.mark.parametrize('change', ['FLOAT', 'EVIDENCE', 'ROLE', 'PARTIAL_PAIR', 'GAS', 'OMITTED_FLOW', 'UNASSIGNED', 'PRECISION'])
def test_external_contract_rejects_inexact_or_incomplete_declarations_without_writes(tmp_path, change):
    with effects_case(tmp_path) as c:
        body = deepcopy(c.body_review); before = counts(c.vault)
        if change == 'FLOAT': body['new_events'][0]['amount'] = 0.1
        if change == 'EVIDENCE': body['proof'] = {}
        if change == 'ROLE': body['wallets'][0]['role'] = 'OWNER'
        if change == 'PARTIAL_PAIR': body['allocations'][0]['events'].pop()
        if change == 'GAS': body['new_events'][0]['amount'] = '0.0001'
        if change == 'OMITTED_FLOW': body['allocations'] = []
        if change == 'UNASSIGNED': body['new_events'].append({'key': key(), 'account_id': c.ledger, 'kind': 'CONTRIBUTION', 'quote_currency': 'USDT', 'amount': '1'})
        if change == 'PRECISION': body['new_events'][1]['amount'] = '0.0050000000000000001'
        response = post(c, body)
        assert response.status_code == (422 if change in ('FLOAT', 'EVIDENCE', 'ROLE') else 409), response.text
        assert counts(c.vault) == before


@pytest.mark.parametrize('change', ['LOCK', 'LINK', 'HISTORY', 'HEAD_NAME'])
def test_financial_replay_releases_leases_and_revalidation_discards_stale_results(tmp_path, monkeypatch, change):
    from app.portfolio import economic_external_api as module
    with effects_case(tmp_path) as c:
        original = module.prepare_external_effects
        def replay(*args, **kwargs):
            result = original(*args, **kwargs)
            if change == 'LOCK':
                with ThreadPoolExecutor(max_workers=1) as pool:
                    assert pool.submit(c.client.post, '/real/vault/lock', headers=c.headers).result(timeout=5).status_code == 200
            else:
                with c.vault.session() as db:
                    if change == 'LINK':
                        row = db.scalar(select(m.RealWalletLink).where(m.RealWalletLink.account_id == c.peer)); db.delete(row); db.commit()
                    elif change == 'HEAD_NAME': db.get(m.RealAccount, c.peer).name = 'Nombre editado'; db.commit()
                    else: append_event(db, c.peer, key(), EventInput('CONTRIBUTION', 'USDT', c.at-timedelta(minutes=30), amount=D('1')))
            return result
        monkeypatch.setattr(module, 'prepare_external_effects', replay)
        response = post(c)
        assert response.status_code == (401 if change == 'LOCK' else 409), response.text
        assert 'proposed' not in response.text and 'preview_digest' not in response.text


def test_approval_without_trade_and_unknown_coverage_stay_distinct(tmp_path):
    with effects_case(tmp_path, mode='APPROVAL') as c:
        data = post(c); assert data.status_code == 200, data.text
        assert data.json()['events_to_create'] == 1 and data.json()['manual_review_complete']
        body = deepcopy(c.body_review); body['annotations'][0]['disposition'] = 'UNKNOWN'
        data = post(c, body); assert data.status_code == 200 and not data.json()['manual_review_complete']
        assert data.json()['accounts'][0]['proposed']['positions'] == []


def test_capture_is_detached_private_and_does_not_create_a_new_identity(tmp_path):
    with effects_case(tmp_path) as c:
        with c.vault.session() as db:
            before = db.connection().connection.driver_connection.total_changes
            inputs = capture_external_effects(db, 'WALLET_SWAP', c.source.id, context_account_id=c.account,
                owner_address=c.meta['owner'], wallets=(WalletParticipant(c.peer, c.meta['operator']),))
            assert db.connection().connection.driver_connection.total_changes == before
        from app.portfolio.economic_external_api import ExternalReview, _prepare
        body = ExternalReview(**c.body_review); c.vault.lock()
        plan = _prepare(inputs, body, c.now+timedelta(seconds=5))
        assert len(plan.created_events) == 3 and plan.review.body['manual_review_complete']
        text = repr(inputs).lower()+plan.body_json.lower()
        for raw in (c.meta['owner'], c.meta['operator'], c.hash): assert raw.lower() not in text
        ids = {event.id for event in plan.created_events}
        assert ids == {event_id(body.review_id, item.key) for item in body.new_events}


def test_new_asset_pair_uses_actual_inventory_basis_without_changing_existing_originals(tmp_path):
    with effects_case(tmp_path, mode='WBNB_TRANSFER') as c:
        with c.vault.session() as db:
            original = append_event(db, c.ledger, key(), EventInput('BUY', 'USDT', c.at-timedelta(minutes=30),
                asset='WBNB', quantity=D('0.1'), unit_price=D('100')))
        prove(c.client, c.headers, c.peer, Account.from_key('0x'+'22'*32))
        body = deepcopy(c.body_review); pair = key(); out = key(); incoming = key()
        body['wallets'] = [{'context_account_id': c.peer, 'address': c.meta['operator']}]
        for identifier, account, kind in ((out, c.ledger, 'TRANSFER_OUT'), (incoming, c.peer, 'TRANSFER_IN')):
            body['new_events'].append({'key': identifier, 'account_id': account, 'kind': kind,
                'quote_currency': 'USDT', 'asset': 'WBNB', 'quantity': '0.005', 'pair_key': pair})
            body['members'].append({'event': {'new_key': identifier}, 'role': 'EFFECT'})
        body['allocations'] = [{'effect_refs': ['token:'+c.observed['transfers'][0]['log_ref']],
            'events': [{'new_key': out}, {'new_key': incoming}], 'classification': 'OWN_TRANSFER', 'description': 'Traslado de unidades propias'}]
        before = counts(c.vault); result = post(c, body); assert result.status_code == 200, result.text
        data = result.json(); rows = {item['account_id']: item for item in data['accounts']}
        left = rows[c.ledger]['proposed']['positions'][0]; right = rows[c.peer]['proposed']['positions'][0]
        assert (D(left['quantity']), D(left['cost_basis'])) == (D('0.095'), D('9.5'))
        assert (D(right['quantity']), D(right['cost_basis'])) == (D('0.005'), D('0.5'))
        assert {D(item['gross_amount']) for item in data['created_events'] if item['kind'].startswith('TRANSFER_')} == {D('0.5')}
        assert counts(c.vault) == before
        with c.vault.session() as db:
            row = db.get(m.RealEvent, original.id)
            assert row.gross_amount == D('10') and row.quantity == D('0.1')


def test_existing_gas_is_reused_and_similar_new_proposal_requires_explicit_override(tmp_path):
    with effects_case(tmp_path, mode='APPROVAL') as c:
        body = deepcopy(c.body_review); gas = body['new_events'][0]
        with c.vault.session() as db:
            existing = append_event(db, c.ledger, key(), EventInput('FEE', 'BNB', c.at,
                amount=D(gas['amount']), note='Gas ya registrado'))
        assert post(c, body).status_code == 409
        body['new_events'] = []; body['members'] = [{'event': {'existing_id': existing.id}, 'role': 'GAS'}]
        result = post(c, body); assert result.status_code == 200, result.text
        assert result.json()['events_to_create'] == 0
        row = result.json()['accounts'][0]
        assert row['current'] == row['proposed'] and not row['changed']


def test_review_stamp_cannot_precede_captured_originals_or_claim_future_registration(tmp_path):
    with effects_case(tmp_path, mode='APPROVAL') as c:
        early = post(c, {**c.body_review, 'recorded_at': c.at.isoformat()})
        assert early.status_code == 409
        future = post(c, {**c.body_review, 'recorded_at': (datetime.now(c.at.tzinfo)+timedelta(days=1)).isoformat()})
        assert future.status_code == 409


def test_http_financial_confirm_retry_and_reopen_without_wallet_proof_or_rpc(tmp_path):
    from tests.test_wallet_swap import PASSWORD
    from app.portfolio.repository import account_snapshot
    with effects_case(tmp_path)as c:
        before=counts(c.vault);calls=len(c.reads)
        preview=post(c);assert preview.status_code==200,preview.text
        data=preview.json();url=c.effects_url.removesuffix('/preview')
        body={**c.body_review,'recorded_at':data['recorded_at'],'preview_digest':data['preview_digest'],
            'confirmation':'ACEPTAR_EFECTOS_EXTERNOS'}
        wrong=c.client.post(url+'/confirm',headers=c.headers,json={**body,'preview_digest':'0'*64})
        assert wrong.status_code==409 and counts(c.vault)==before
        response=c.client.post(url+'/confirm',headers=c.headers,json=body);assert response.status_code==200,response.text
        saved=response.json();assert saved['created']and saved['events_created']==3 and saved['is_current']
        repeated=c.client.post(url+'/confirm',headers=c.headers,json=body);assert repeated.status_code==200,repeated.text
        assert repeated.json()['id']==saved['id']and not repeated.json()['created']and repeated.json()['events_created']==0
        with c.vault.session()as db:assert account_snapshot(db,c.peer).cash['USDT']==D('3.005')
        assert c.client.post('/real/vault/lock',headers=c.headers).status_code==200
        token=c.client.post('/real/vault/unlock',json={'passphrase':PASSWORD}).json()['token']
        recovered=c.client.get(url,headers={'X-Orbis-Vault-Token':token});assert recovered.status_code==200,recovered.text
        assert recovered.json()['id']==saved['id']and not recovered.json()['created']
        assert len(c.reads)==calls and recovered.headers['cache-control']=='no-store'


def test_explicit_manual_trade_gross_equal_to_price_product_reopens_canonically(tmp_path):
    from app.portfolio.repository import account_snapshot
    with effects_case(tmp_path,mode='APPROVAL')as c:
        body=deepcopy(c.body_review);trade=key();manual=key()
        body['new_events'].append({'key':trade,'account_id':c.ledger,'kind':'BUY','quote_currency':'USDT',
            'asset':'WBNB','quantity':'0.01','unit_price':'10','gross_amount':'0.1000','note':'Efecto declarado manualmente'})
        body['members'].append({'event':{'new_key':trade},'role':'EFFECT'})
        body['manual_effects']=[{'id':manual,'annotation_ref':'unobserved-effects','reason':'Interpretación manual independiente del permiso',
            'deltas':[{'account_id':c.ledger,'asset':'USDT','units':'-100000000000000000'},
                {'account_id':c.ledger,'asset':'WBNB','units':'10000000000000000'}]}]
        body['allocations']=[{'effect_refs':['manual:'+manual],'events':[{'new_key':trade}],
            'classification':'TRADE','description':'Compraventa declarada por el usuario'}]
        preview=post(c,body);assert preview.status_code==200,preview.text
        data=preview.json();assert data['can_accept']and not data['effects_proven_complete']
        saved=c.client.post(c.effects_url.removesuffix('/preview')+'/confirm',headers=c.headers,json={**body,
            'recorded_at':data['recorded_at'],'preview_digest':data['preview_digest'],'confirmation':'ACEPTAR_EFECTOS_EXTERNOS'})
        assert saved.status_code==200,saved.text
        assert not saved.json()['strategy_executed']
        with c.vault.session()as db:
            assert account_snapshot(db,c.ledger).positions['WBNB','USDT'].cost_basis==D('0.1')
        c.vault.backup(tmp_path/'canonical-manual-trade.db')


@pytest.mark.parametrize('separate_operator_custody', [False, True])
def test_actual_operator_delegation_pays_gas_without_inventing_principal_or_restarting(tmp_path, separate_operator_custody):
    from datetime import timezone
    from threading import Event
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.portfolio.api import create_real_router
    from app.portfolio import dex_operator_journal as operators
    from app.live_execution.dex_bot_supervisor import DexBotSupervisor
    from tests.test_dex_call_resolution_operator import case, lower_price, pending
    from tests.test_dex_deployments import PASSWORD
    with case(tmp_path, evm_backend='anvil') as values:
        vault, context, meta, rpc, deployment, grant, _ = values
        source, original, _ = pending(values, lower_price(meta, rpc, int(deployment['nonce'])))
        sent = rpc('eth_sendTransaction', [{'from': meta['operator'], 'to': meta['owner'], 'nonce': hex(source.nonce),
            'data': '0x', 'value': hex(10**15), 'gas': hex(21000), 'gasPrice': hex(2*10**9)}])
        rpc('miner_start', [1]); rpc('evm_mine', [])
        at = datetime.fromtimestamp(int(rpc('eth_getBlockByNumber', ['latest', False])['timestamp'], 16), timezone.utc)
        with vault.session() as db:
            ledger = create_account(db, 'Custodia principal', 'USDT').id
            save_wallet_link(db, ledger, meta['owner'], '0x38', 'Principal'); db.commit()
            signer_ledger = create_account(db, 'Custodia operador', 'USDT').id if separate_operator_custody else ledger
            if separate_operator_custody:
                save_wallet_link(db, signer_ledger, meta['operator'], '0x38', 'Operador'); db.commit()
        for target in {ledger, signer_ledger}:
            with vault.session() as db: append_event(db, target, key(), EventInput('CONTRIBUTION', 'BNB', at-timedelta(hours=1), amount=D(1)))
        def clock(): return max(datetime.now(timezone.utc), at)+timedelta(seconds=2)
        with vault.session() as db: operators.stop_grant(db, grant['id'], account_id=context, now=clock())
        vault.lock(); holder = {}; reads = []
        def supervisor(v, connection):
            holder['vault'] = v; holder['supervisor'] = DexBotSupervisor(v, connection, clock=clock)
            return holder['supervisor']
        @contextmanager
        def reader():
            def call(method, params):
                assert 'send' not in method.lower() and 'sign' not in method.lower()
                reads.append(method); return rpc(method, params)
            yield call
        app = FastAPI(); app.include_router(create_real_router(vault.path, dex_rpc_factory=reader, dex_bot_supervisor_factory=supervisor))
        with TestClient(app) as client:
            headers = {'X-Orbis-Vault-Token': client.post('/real/vault/unlock', json={'passphrase': PASSWORD}).json()['token']}
            prove(client, headers, context, Account.from_key('0x'+'11'*32))
            base = f'/real/accounts/{context}/dex-call-resolutions/OPERATOR/{source.id}'
            raw = {'address': meta['owner'], 'tx_hash': sent}
            checked = client.post(base+'/external-observation', headers=headers, json=raw); assert checked.status_code == 200, checked.text
            body = {**raw, 'preview_digest': checked.json()['preview_digest'], 'source_state_digest': checked.json()['source_state_digest'],
                'confirmation': 'GUARDAR_OBSERVACION_EXTERNA'}
            observed = client.post(base+'/external-observation/save', headers=headers, json=body); assert observed.status_code == 200, observed.text
            checked = client.post(base+'/external-order/preview', headers=headers, json=raw); assert checked.status_code == 200, checked.text
            saved = client.post(base+'/external-order/save', headers=headers, json={**raw,
                'preview_digest': checked.json()['preview_digest'], 'confirmation': 'GUARDAR_ORDEN_EXTERNA'})
            assert saved.status_code == 200, saved.text
            gas = key(); proof = observed.json()['observation']
            body = {'address': meta['owner'], 'review_id': key(), 'new_events': [
                {'key': gas, 'account_id': signer_ledger, 'kind': 'FEE', 'quote_currency': 'BNB', 'amount': '0.000042', 'note': 'Gas externo observado'}],
                'members': [{'event': {'new_key': gas}, 'role': 'GAS'}],
                'annotations': [{'reference': 'unobserved-effects', 'disposition': 'MANUAL_COMPLETE', 'note': 'Revisión manual de efectos restantes'}],
                'allocations': [{'effect_refs': ['native:'+proof['call_ref']], 'events': [], 'classification': 'NO_LEDGER_CHANGE',
                    'description': 'Ambos actores bajo la misma custodia'}]}
            if separate_operator_custody:
                prove(client, headers, signer_ledger, Account.from_key('0x'+'22'*32))
                body['wallets'] = [{'context_account_id': signer_ledger, 'address': meta['operator']}]
                out = key(); incoming = key(); pair = key()
                for identifier, target, kind in ((out, signer_ledger, 'CASH_TRANSFER_OUT'), (incoming, ledger, 'CASH_TRANSFER_IN')):
                    body['new_events'].append({'key': identifier, 'account_id': target, 'kind': kind, 'quote_currency': 'BNB',
                        'amount': '0.001', 'pair_key': pair})
                    body['members'].append({'event': {'new_key': identifier}, 'role': 'EFFECT'})
                body['allocations'][0].update(events=[{'new_key': out}, {'new_key': incoming}], classification='OWN_TRANSFER', description='Retorno a wallet propia')
            url = base+'/external-effects/preview'; count = len(reads); before = counts(holder['vault'])
            holder['supervisor']._starting['owned-test-reservation'] = Event()
            assert client.post(url, headers=headers, json=body).status_code == 409
            holder['supervisor']._starting.clear()
            result = client.post(url, headers=headers, json=body); assert result.status_code == 200, result.text
            rows = {item['account_id']: item['proposed'] for item in result.json()['accounts']}
            if separate_operator_custody:
                assert D(rows[ledger]['cash']['BNB']) == D('1.001')
                assert D(rows[signer_ledger]['cash']['BNB']) == D('0.998958')
            else: assert D(rows[ledger]['cash']['BNB']) == D('0.999958')
            assert not result.json()['strategy_executed'] and not result.json()['resolves_intent']
            assert len(reads) == count and counts(holder['vault']) == before
            confirmed=client.post(base+'/external-effects/confirm',headers=headers,json={**body,
                'recorded_at':result.json()['recorded_at'],'preview_digest':result.json()['preview_digest'],
                'confirmation':'ACEPTAR_EFECTOS_EXTERNOS'})
            assert confirmed.status_code==200,confirmed.text
            assert confirmed.json()['created']and confirmed.json()['external_review_accepted']
            assert confirmed.json()['events_created']==(3 if separate_operator_custody else 1)
            assert not confirmed.json()['strategy_executed']and not confirmed.json()['resolves_intent']
            with holder['vault'].session() as db:
                row, _, _ = operators.intent_record(db, source.id)
                assert (row.payload_json, row.state_json, row.updated_at) == original
                status = operators.grant_view(db, grant['id'], now=clock())
                assert status['status'] == 'STOPPED' and not status['unresolved_outcomes'] and status['gas_spent_wei'] == str(21000*2*10**9)
                assert status['external_outcomes_reviewed']==1
            assert client.post('/real/vault/lock', headers=headers).status_code == 200
