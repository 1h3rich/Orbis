"""Full private HTTP save/recovery, wallet control and revocation boundaries."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import select, func

from app.portfolio.models import RealDexExternalObservation
from tests.test_dex_external_observation_api import api_case, owner
from tests.test_wallet_swap import PASSWORD
from tests.test_economic_receipt_source import counts


def review(case):
    owner(case)
    result = case.client.post(case.url, headers=case.headers, json=case.body)
    assert result.status_code == 200, result.text
    data = result.json()
    return {**case.body, 'preview_digest': data['preview_digest'], 'source_state_digest': data['source_state_digest'],
            'confirmation': 'GUARDAR_OBSERVACION_EXTERNA'}


def test_actual_http_save_lost_reply_retry_and_read_after_unlock_without_wallet_or_rpc(tmp_path):
    with api_case(tmp_path) as case:
        body = review(case); before = counts(case.vault)
        result = case.client.post(case.url+'/save', headers=case.headers, json=body)
        assert result.status_code == 200, result.text
        data = result.json(); assert data['saved'] and data['nonce_consumed'] and data['status'] == 'EFFECTS_PENDING'
        assert not data['resolves_intent'] and not data['accounting_complete'] and not data['trading_authority']
        assert counts(case.vault) == before
        case.gate['failure'] = True; reads = len(case.reads)
        retried = case.client.post(case.url+'/save', headers=case.headers, json=body)
        assert retried.status_code == 200 and retried.json() == data
        assert len(case.reads) == reads
        assert case.client.post('/real/vault/lock', headers=case.headers).status_code == 200
        token = case.client.post('/real/vault/unlock', json={'passphrase': PASSWORD}).json()['token']
        assert case.client.get(case.url+'/saved', headers=case.headers).status_code == 401
        case.headers = {'X-Orbis-Vault-Token': token}
        restored = case.client.get(case.url+'/saved', headers=case.headers)
        assert restored.status_code == 200 and restored.json() == data
        assert restored.headers['cache-control'] == 'no-store'
        assert len(case.reads) == reads
        with case.vault.session() as db:
            assert db.scalar(select(func.count()).select_from(RealDexExternalObservation)) == 1
        for private in (case.meta['owner'], case.meta['operator'], case.hash):
            assert private.lower() not in restored.text.lower()


def test_save_requires_exact_review_wallet_origin_and_confirmation_and_cannot_leak_other_account(tmp_path):
    with api_case(tmp_path) as case:
        body = review(case); reads = len(case.reads)
        assert case.client.post(case.url+'/save', json=body).status_code == 401
        assert case.client.post(case.url+'/save', headers=case.headers,
            json={**body, 'address': case.meta['operator']}).status_code == 409
        for changes in ({'confirmation': 'RESOLVER_LLAMADA_DEX'}, {'proof': {}}, {'accept_changes': True}):
            assert case.client.post(case.url+'/save', headers=case.headers, json={**body, **changes}).status_code == 422
        assert case.client.post(case.url+'/save', headers={**case.headers, 'Origin': 'https://outside.invalid'}, json=body).status_code == 403
        assert len(case.reads) == reads
        missing = case.client.get(case.url+'/saved', headers=case.headers)
        assert missing.status_code == 200 and missing.json()['saved'] is False
        assert case.client.post(case.url+'/save', headers=case.headers, json={**body, 'preview_digest': '0'*64}).status_code == 409
        with case.vault.session() as db:
            assert not list(db.scalars(select(RealDexExternalObservation)))
        valid = case.client.post(case.url+'/save', headers=case.headers, json=body)
        assert valid.status_code == 200, valid.text
        other = case.url.replace(case.account, str(uuid4()))
        assert case.client.get(other+'/saved', headers=case.headers).status_code == 409
        assert case.client.post(case.url+'/save', headers=case.headers,
            json={**body, 'source_state_digest': '0'*64}).status_code == 409
        assert case.client.post(case.url, headers=case.headers, json=case.body).status_code == 409


@pytest.mark.parametrize('change', ['LOCK', 'SOURCE'])
def test_save_during_rpc_discards_late_result_and_never_creates_journal(tmp_path, change):
    from app.portfolio import wallet_swap as swaps
    with api_case(tmp_path) as case:
        body = review(case); case.gate['block'] = True
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(case.client.post, case.url+'/save', headers=case.headers, json=body)
            assert case.entered.wait(5)
            if change == 'LOCK':
                assert case.client.post('/real/vault/lock', headers=case.headers).status_code == 200
            else:
                with case.vault.session() as db:
                    swaps.report_swap(db, case.plan['id'], account_id=case.account, tx_hash=case.hash, now=case.now)
            case.release.set(); response = future.result(timeout=10)
        assert response.status_code == (401 if change == 'LOCK' else 409), response.text
        assert 'observation_ref' not in response.text
        if change == 'LOCK':
            case.headers = {'X-Orbis-Vault-Token': case.client.post('/real/vault/unlock', json={'passphrase': PASSWORD}).json()['token']}
        with case.vault.session() as db:
            assert not list(db.scalars(select(RealDexExternalObservation)))


def test_save_crypto_is_detached_and_a_replaced_vault_session_cannot_commit(tmp_path, monkeypatch):
    from app.portfolio import dex_external_observation_api as module
    with api_case(tmp_path) as case:
        body = review(case); original = module.verify_external_call
        with ThreadPoolExecutor(max_workers=1) as pool:
            def checked(*args, **kwargs):
                proof = original(*args, **kwargs)
                assert pool.submit(case.client.post, '/real/vault/lock', headers=case.headers).result(timeout=5).status_code == 200
                replacement = pool.submit(case.client.post, '/real/vault/unlock', json={'passphrase': PASSWORD}).result(timeout=5)
                assert replacement.status_code == 200
                case.replacement = {'X-Orbis-Vault-Token': replacement.json()['token']}
                return proof
            monkeypatch.setattr(module, 'verify_external_call', checked)
            response = case.client.post(case.url+'/save', headers=case.headers, json=body)
        assert response.status_code == 401, response.text
        case.headers = case.replacement
        with case.vault.session() as db:
            assert not list(db.scalars(select(RealDexExternalObservation)))


def test_two_http_confirmations_use_one_record_and_failures_roll_back(tmp_path, monkeypatch):
    from sqlalchemy.orm import Session
    with api_case(tmp_path) as case:
        body = review(case)
        with monkeypatch.context() as patch:
            patch.setattr(Session, 'commit', lambda db: (_ for _ in ()).throw(ValueError('owned write failure')))
            response = case.client.post(case.url+'/save', headers=case.headers, json=body)
            assert response.status_code == 409
        with case.vault.session() as db:
            assert not list(db.scalars(select(RealDexExternalObservation)))
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(case.client.post, case.url+'/save', headers=case.headers, json=body)
            b = pool.submit(case.client.post, case.url+'/save', headers=case.headers, json=body)
            first, second = a.result(timeout=15), b.result(timeout=15)
        assert first.status_code == second.status_code == 200, (first.text, second.text)
        assert first.json() == second.json()
        with case.vault.session() as db:
            assert db.scalar(select(func.count()).select_from(RealDexExternalObservation)) == 1
