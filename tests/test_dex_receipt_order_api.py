"""Protected block discovery over owned EVM receipts; no API signing/sending."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import Event
from types import SimpleNamespace
import json

import pytest
from eth_account import Account
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from app.live_execution.dex_bot_supervisor import DexBotSupervisor
from app.portfolio.api import create_real_router
from app.portfolio.dex_receipt_order_source import order_source
from app.portfolio import dex_receipt_order as orders
from app.portfolio.models import RealDexReceiptOrderProof, RealDexCallResolution
from app.portfolio.repository import list_original_events, account_book_revision
from app.portfolio.wallet_funding import _encoded
from tests.dex_order_fixture import same_block_case
from tests.test_dex_call_booking import resolved_case
from tests.test_wallet_funding_api import prove
from tests.test_wallet_swap import PASSWORD
from tests.test_dex_deployments import PASSWORD as OPERATOR_PASSWORD


@contextmanager
def http_case(directory, monkeypatch, kind='CALL_RESOLUTION', *, typed=False):
    manager = resolved_case(directory, typed=typed) if kind == 'CALL_RESOLUTION' else same_block_case(directory, monkeypatch)
    with manager as fixture:
        if kind == 'CALL_RESOLUTION':
            vault, account, ledger, meta, rpc, _, _, resolved = fixture
            source_id, password = resolved['id'], PASSWORD
        else:
            vault, account, ledger, meta, rpc = fixture.vault, fixture.account, fixture.ledger_account, fixture.meta, fixture.rpc
            source_id = next(item.id for item in fixture.sources if item.kind == kind)
            password = OPERATOR_PASSWORD
        with vault.session() as db:
            source = order_source(db, kind, source_id, wallet_address=meta['owner'])
            before = list_original_events(db, ledger)
            revision = account_book_revision(db, ledger)
        vault.lock()
        calls, holder = [], {}
        gate = {'phase': None, 'entered': Event(), 'release': Event(), 'failure': False, 'corrupt': None, 'contexts': 0}
        @contextmanager
        def reader():
            if gate.get('factory_failure'):
                raise ValueError('private-rpc-endpoint-secret')
            gate['contexts'] += 1
            phase = gate['contexts']
            def read(method, params):
                assert not any(value in method.lower() for value in ('send', 'sign')), method
                calls.append((method, params))
                if gate['phase'] == phase:
                    gate['phase'] = None
                    gate['entered'].set()
                    assert gate['release'].wait(10)
                if gate['failure']:
                    raise RuntimeError('private-rpc-endpoint-secret')
                result = rpc(method, params)
                if gate['corrupt'] and method == 'eth_getBlockByNumber' and params[0].startswith('0x'):
                    result = deepcopy(result)
                    if gate['corrupt'] == 'duplicate':
                        result['transactions'] = [result['transactions'][0]] * 2
                    else:
                        result['hash'] = '0x' + 'f' * 64
                return result
            yield read
        def supervisor(current, connection):
            holder['vault'] = current
            return DexBotSupervisor(current, connection,
                clock=lambda: max(datetime.now(timezone.utc), source.updated_at + timedelta(seconds=1)))
        app = FastAPI()
        app.include_router(create_real_router(vault.path, dex_rpc_factory=reader, dex_bot_supervisor_factory=supervisor))
        with TestClient(app) as client:
            headers = {'X-Orbis-Vault-Token': client.post('/real/vault/unlock', json={'passphrase': password}).json()['token']}
            info = SimpleNamespace(client=client, headers=headers, account=account, ledger=ledger,
                meta=meta, source=source, source_id=source_id, kind=kind, vault=holder['vault'],
                password=password, calls=calls, gate=gate, before=before, revision=revision,
                url=f'/real/accounts/{account}/dex-receipts/{kind}/{source_id}/order-proof')
            try:
                yield info
            finally:
                client.post('/real/vault/lock', headers=info.headers)


def verify_owner(case):
    prove(case.client, case.headers, case.account, Account.from_key('0x' + '11' * 32))


def test_passive_block_collection_does_not_extend_unlock_but_manual_collection_does(tmp_path,monkeypatch):
    from app.portfolio.api import RealAccess
    with http_case(tmp_path,monkeypatch)as case:
        verify_owner(case);touched=[];original=RealAccess._touch
        def touch(access):
            touched.append(True);original(access)
        monkeypatch.setattr(RealAccess,'_touch',touch)
        response=case.client.post(case.url,headers={**case.headers,'X-Orbis-Passive':'1'},json={'address':case.meta['owner']})
        assert response.status_code==200,response.text
        assert response.json()['created']and not touched
        repeated=case.client.post(case.url,headers=case.headers,json={'address':case.meta['owner']})
        assert repeated.status_code==200 and not repeated.json()['created']and touched


@pytest.mark.parametrize('kind,typed', [('CALL_RESOLUTION', False), ('CALL_RESOLUTION', True), ('WALLET_SWAP', False), ('OPERATOR', False)])
def test_http_discovers_and_saves_exact_order_once_without_hash_input_or_ledger_changes(tmp_path, monkeypatch, kind, typed):
    with http_case(tmp_path, monkeypatch, kind, typed=typed) as case:
        body = {'address': case.meta['owner']}
        assert case.client.post(case.url, json=body).status_code == 401
        assert case.client.post(case.url, headers=case.headers, json=body).status_code == 409
        assert not case.calls
        verify_owner(case)
        headers = {**case.headers, 'X-Orbis-Wallet': case.meta['owner']}
        status = case.client.get(case.url, headers=headers)
        assert status.status_code == 200 and not status.json()['saved'] and not case.calls
        assert case.client.post(case.url, headers={**case.headers, 'Origin': 'https://outside.invalid'}, json=body).status_code == 403
        assert case.client.post(case.url, headers=case.headers, json={**body, 'transaction_index': 7}).status_code == 422
        prove(case.client, case.headers, case.ledger, Account.from_key('0x' + '11' * 32))
        wrong_context = case.url.replace('/accounts/' + case.account + '/', '/accounts/' + case.ledger + '/')
        assert case.client.post(wrong_context, headers=case.headers, json=body).status_code == 409
        other = Account.from_key('0x' + '33' * 32)
        prove(case.client, case.headers, case.account, other)
        assert case.client.post(case.url, headers=case.headers, json={'address': other.address}).status_code == 409
        assert not case.calls
        first = case.client.post(case.url, headers=case.headers, json=body)
        assert first.status_code == 200, first.text
        saved = first.json()
        assert saved['created'] and saved['saved'] and saved['source_kind'] == kind
        assert saved['proof']['parent_kind'] == ('OPERATOR' if kind == 'OPERATOR' else 'WALLET_SWAP')
        assert first.headers['cache-control'] == 'no-store'
        assert not saved['trading_authority'] and not saved['applied_to_ledger']
        count = len(case.calls)
        for method in ('get', 'post'):
            response = case.client.get(case.url, headers=headers) if method == 'get' else case.client.post(case.url, headers=case.headers, json=body)
            assert response.status_code == 200 and response.json()['id'] == saved['id']
            assert not response.json()['created'] and len(case.calls) == count
        with case.vault.session() as db:
            record = orders.order_record(db, saved['id'])
            assert record[0].transaction_index == saved['transaction_index']
            assert list_original_events(db, case.ledger) == case.before
            assert account_book_revision(db, case.ledger) == case.revision
            reopened = order_source(db, kind, case.source_id, wallet_address=case.meta['owner'])
            assert reopened == case.source
            raw = record[0].payload_json
        for address in (case.meta['owner'], case.meta['operator']):
            assert address.lower() not in raw.lower() and address.lower() not in first.text.lower()
        observed_hashes = [params[0] for method, params in case.calls if method == 'eth_getTransactionReceipt']
        assert len(observed_hashes) == 1
        assert observed_hashes[0].lower() not in raw.lower() and observed_hashes[0].lower() not in first.text.lower()
        case.client.post('/real/vault/lock', headers=case.headers)
        assert case.client.get(case.url, headers=headers).status_code == 401
        case.headers = {'X-Orbis-Vault-Token': case.client.post('/real/vault/unlock', json={'passphrase': case.password}).json()['token']}
        assert case.client.post(case.url, headers=case.headers, json=body).status_code == 409
        verify_owner(case)
        case.gate['failure'] = True
        assert case.client.post(case.url, headers=case.headers, json=body).json()['id'] == saved['id']
        assert len(case.calls) == count


@pytest.mark.parametrize('phase', [1, 2])
def test_vault_lock_during_each_rpc_phase_is_prompt_and_saves_no_proof(tmp_path, monkeypatch, phase):
    with http_case(tmp_path, monkeypatch) as case:
        verify_owner(case)
        case.gate['phase'] = phase
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(case.client.post, case.url, headers=case.headers, json={'address': case.meta['owner']})
            assert case.gate['entered'].wait(5)
            locked = case.client.post('/real/vault/lock', headers=case.headers)
            case.gate['release'].set()
            assert locked.status_code == 200
            response = future.result(timeout=10)
        assert response.status_code == 401
        case.headers = {'X-Orbis-Vault-Token': case.client.post('/real/vault/unlock', json={'passphrase': case.password}).json()['token']}
        with case.vault.session() as db:
            assert db.scalar(select(func.count()).select_from(RealDexReceiptOrderProof)) == 0
            assert list_original_events(db, case.ledger) == case.before


def test_changed_original_after_discovery_is_not_saved_from_a_warm_source(tmp_path, monkeypatch):
    with http_case(tmp_path, monkeypatch) as case:
        verify_owner(case)
        case.gate['phase'] = 2
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(case.client.post, case.url, headers=case.headers, json={'address': case.meta['owner']})
            assert case.gate['entered'].wait(5)
            with case.vault.session() as db:
                row = db.get(RealDexCallResolution, case.source_id)
                payload = json.loads(row.payload_json)
                row.payload_json, row.payload_digest = _encoded({**payload, 'nonce': '999'})
                db.commit()
            case.gate['release'].set()
            response = future.result(timeout=10)
        assert response.status_code == 409
        with case.vault.session() as db:
            assert db.scalar(select(func.count()).select_from(RealDexReceiptOrderProof)) == 0


@pytest.mark.parametrize('fault', ['duplicate', 'wrong_block', 'network', 'factory'])
def test_invalid_block_and_private_transport_errors_leave_no_order_record(tmp_path, monkeypatch, fault):
    with http_case(tmp_path, monkeypatch) as case:
        verify_owner(case)
        case.gate['failure'] = fault == 'network'
        case.gate['factory_failure'] = fault == 'factory'
        case.gate['corrupt'] = None if fault in ('network', 'factory') else fault
        response = case.client.post(case.url, headers=case.headers, json={'address': case.meta['owner']})
        assert response.status_code == (503 if fault in ('network', 'factory') else 409)
        assert 'private-rpc-endpoint-secret' not in response.text
        assert response.headers['cache-control'] == 'no-store'
        with case.vault.session() as db:
            assert db.scalar(select(func.count()).select_from(RealDexReceiptOrderProof)) == 0


def test_concurrent_http_checks_save_one_witness_and_no_ledger_event(tmp_path, monkeypatch):
    with http_case(tmp_path, monkeypatch) as case:
        verify_owner(case)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: case.client.post(case.url, headers=case.headers,
                json={'address': case.meta['owner']}), range(2)))
        assert all(result.status_code == 200 for result in results), [result.text for result in results]
        assert sorted(result.json()['created'] for result in results) == [False, True]
        assert len({result.json()['id'] for result in results}) == 1
        with case.vault.session() as db:
            assert db.scalar(select(func.count()).select_from(RealDexReceiptOrderProof)) == 1
            assert list_original_events(db, case.ledger) == case.before
