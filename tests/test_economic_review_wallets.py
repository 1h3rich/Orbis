"""Multi-wallet review reads on signed owned EVM receipts and real SQLCipher."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4
import json

from eth_account import Account
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app.portfolio.api import create_real_router
from app.portfolio import models as m
from app.live_execution.dex_bot_supervisor import DexBotSupervisor
from app.portfolio.domain import EventInput
from app.portfolio.economic_projection import ProjectionError, ProjectionLimits
from app.portfolio.economic_review_wallets import review_wallets
from app.portfolio.repository import account_snapshot, transfer_cash_between_accounts, create_account, append_event
from app.portfolio.vault import RealVault
from tests.test_economic_acceptance import ready, proposed, confirm
from tests.dex_order_fixture import finalized_wallet_order
from tests.test_dex_operator import write
from tests.test_dex_receipt_order_journal import arguments, review, save
from app.portfolio.wallet_links import save_wallet_link
from app.portfolio import wallet_swap_booking
from tests.test_economic_receipt_source import counts
from tests.test_economic_review_api import confirmation
from tests.test_wallet_funding_api import prove
from tests.test_wallet_swap import PASSWORD


@contextmanager
def multi_case(directory, *, accepted=False, merge=True):
    with ready(directory) as (original, witness):
        vault, context, owner, meta, _, _, _, _ = original
        first = confirm(original, witness, proposed(original, witness)) if accepted else None
        rpc=original[4];alternate={**meta,'owner':meta['operator']};address=alternate['owner']
        write(rpc,meta,meta['usdt'],'mint(address,uint256)',['address','uint256'],[address,10**18])
        write(rpc,alternate,meta['usdt'],'approve(address,uint256)',['address','uint256'],[meta['router'],10**18])
        with vault.session() as db:
            peer=create_account(db,'Otra wallet','USDT').id
            save_wallet_link(db,peer,address,'0x38','Segunda');db.commit()
        for currency,amount in [('USDT','100'),('BNB','0.01')]:
            with vault.session() as db:
                append_event(db,peer,str(uuid4()),EventInput('CONTRIBUTION',currency,
                    datetime.now(timezone.utc)-timedelta(days=1),amount=Decimal(amount)))
        if merge:
            with vault.session() as db:
                transfer_cash_between_accounts(db, owner, peer, str(uuid4()), currency='USDT',
                    amount=Decimal('1'), occurred_at=datetime.now(timezone.utc)+timedelta(seconds=5))
        alternate_case=(*original[:2],peer,alternate,*original[4:])
        source,observed=finalized_wallet_order(alternate_case)
        args=arguments(alternate_case,source,observed)
        save(alternate_case,source,args,review(alternate_case,source,args))
        if not accepted:
            with vault.session() as db:
                proposed_booking=wallet_swap_booking.preview_booking(db,source.id,context_account_id=context,wallet_address=address)
            with vault.session() as db:
                wallet_swap_booking.book_swap(db,source.id,context_account_id=context,wallet_address=address,
                    expected_revision=proposed_booking['revision'],preview_digest=proposed_booking['preview_digest'],now=args['now'])
        other={'source_id':source.id,'gas_fee_wei':json.loads(source.receipt_json)['gas_fee_wei']}
        with vault.session() as db:
            links = {row.account_id: row.id for row in db.scalars(select(m.RealWalletLink))}
        vault.lock()
        app = FastAPI()
        # There is intentionally no usable network transport for catalogue or
        # existing-witness acceptance. Any attempted RPC fails this test.
        calls = []
        @contextmanager
        def no_rpc():
            calls.append('unexpected')
            raise AssertionError('Wallet catalogue must not query a network')
            yield
        start=datetime.now(timezone.utc)
        base=max(start,args['now'],datetime.fromisoformat(first['recorded_at']) if first else start)+timedelta(seconds=2)
        def supervisor(current, connection):
            return DexBotSupervisor(current,connection,clock=lambda:base+(datetime.now(timezone.utc)-start))
        app.include_router(create_real_router(vault.path, dex_rpc_factory=no_rpc,dex_bot_supervisor_factory=supervisor))
        reopened = RealVault(vault.path)
        with TestClient(app) as client:
            headers = {'X-Orbis-Vault-Token': client.post('/real/vault/unlock',
                json={'passphrase': PASSWORD}).json()['token']}
            prove(client, headers, context, Account.from_key('0x'+'11'*32))
            # Separate handle is used solely for owned fixture assertions.
            reopened.unlock(PASSWORD)
            try:
                yield SimpleNamespace(client=client, headers=headers, vault=reopened, context=context,
                    owner=owner, peer=peer, address=address, meta=meta, links=links, calls=calls,
                    other=other, first=first, base=f'/real/accounts/{context}/economic-review')
            finally:
                reopened.lock()
                client.post('/real/vault/lock', headers=headers)


def query(case, **extra):
    return case.client.post(case.base+'/sources', headers=case.headers,
        json={'address': case.meta['owner'], 'include_wallets': True, **extra})


def supplemental(case):
    return [{'context_account_id': case.peer, 'address': case.address}]


def test_private_scope_finds_unprovided_wallet_without_disclosing_its_receipts(tmp_path):
    with multi_case(tmp_path) as case:
        before = counts(case.vault)
        response = query(case)
        assert response.status_code == 200, response.text
        assert response.headers['cache-control'] == 'no-store'
        data = response.json(); scope = data['wallet_scope']
        assert scope['schema'] == 'orbis.portfolio.economic.wallet.scope.v1'
        assert not scope['moves_funds'] and not scope['trading_authority']
        assert {item['id']: item['provided'] for item in scope['wallets']} == {
            case.links[case.owner]: True, case.links[case.peer]: False}
        assert scope['missing_receipts'] == [{'source_kind': 'WALLET_SWAP', 'source_id': case.other['source_id']}]
        assert all(item['wallet_link_id'] == case.links[case.owner] for item in data['sources'])
        assert all(case.other['source_id'] != item['source_id'] for item in data['sources'])
        for address in (case.meta['owner'], case.address):
            assert address.lower() not in response.text.lower()
        assert 'address_digest' not in response.text and 'owner_digest' not in response.text
        assert counts(case.vault) == before and not case.calls
        assert case.client.post(case.base+'/sources', json={'address': case.meta['owner'],
            'include_wallets': True}).status_code == 401


def test_new_wallet_requires_exact_context_proof_and_catalogue_preserves_source_context(tmp_path):
    with multi_case(tmp_path) as case:
        assert query(case, wallets=supplemental(case)).status_code == 409
        secondary = Account.from_key('0x'+'22'*32)
        assert secondary.address.lower() == case.address.lower()
        prove(case.client, case.headers, case.peer, secondary)
        before = counts(case.vault)
        response = query(case, wallets=supplemental(case))
        assert response.status_code == 200, response.text
        data = response.json()
        assert all(item['provided'] for item in data['wallet_scope']['wallets'])
        assert not data['wallet_scope']['missing_receipts']
        row = next(item for item in data['sources'] if item['source_id'] == case.other['source_id'])
        assert row['wallet_account_id'] == case.peer and row['wallet_link_id'] == case.links[case.peer]
        assert row['context_account_id'] == case.context and not row['context_verified']
        prove(case.client, case.headers, case.context, secondary)
        assert next(item for item in query(case, wallets=supplemental(case)).json()['sources']
                    if item['source_id'] == case.other['source_id'])['context_verified']
        assert counts(case.vault) == before and not case.calls


@pytest.mark.parametrize('accepted', [False, True])
def test_two_wallet_existing_review_accepts_atomically_and_recovers_without_addresses(tmp_path, accepted):
    with multi_case(tmp_path, accepted=accepted) as case:
        secondary = Account.from_key('0x'+'22'*32)
        for context in (case.peer, case.context):
            prove(case.client, case.headers, context, secondary)
        assert query(case, wallets=supplemental(case)).status_code == 200
        before = counts(case.vault)
        with case.vault.session() as db:
            originals = {account: account_snapshot(db, account) for account in (case.owner, case.peer)}
        missing = case.client.post(case.base+'/preview', headers=case.headers, json={'address': case.meta['owner']})
        assert missing.status_code == (200 if accepted else 409), missing.text
        body = {'address': case.meta['owner'], 'wallets': supplemental(case)}
        if accepted:
            body['receipts']=[{'source_kind':'WALLET_SWAP','source_id':case.other['source_id'],
                'context_account_id':case.context,'wallet_address':case.address}]
        preview = case.client.post(case.base+'/preview', headers=case.headers, json=body)
        assert preview.status_code == 200, preview.text
        assert preview.json()['mode'] == ('BATCH' if accepted else 'EXISTING')
        assert preview.json()['review']['events_to_create'] == (2 if accepted else 0)
        saved = case.client.post(case.base+'/confirm', headers=case.headers, json=confirmation(body, preview.json()))
        assert saved.status_code == 200, saved.text
        assert saved.json()['created'] and saved.json().get('events_created',0) == (2 if accepted else 0)
        repeated = case.client.post(case.base+'/confirm', headers=case.headers, json=confirmation(body, preview.json()))
        assert repeated.status_code == 200 and not repeated.json()['created']
        assert counts(case.vault) == (before[0]+(2 if accepted else 0),before[1],before[2]+(1 if accepted else 0),before[3]+1)
        with case.vault.session() as db:
            assert account_snapshot(db,case.owner)==originals[case.owner]
            peer=account_snapshot(db,case.peer)
            if accepted:
                assert peer.positions['WBNB','USDT'].quantity==Decimal('0.01')
                assert peer.positions['WBNB','USDT'].cost_basis==Decimal('0.01')
                assert peer.cash['USDT']==Decimal('100.99')
                assert peer.cash['BNB']==Decimal('0.01')-Decimal(case.other['gas_fee_wei'])/10**18
            else:assert peer==originals[case.peer]
        # Fresh vault session invalidates both EOA proofs. Re-prove only root;
        # the accepted peer witness is now sufficient without its address.
        case.client.post('/real/vault/lock', headers=case.headers)
        case.headers = {'X-Orbis-Vault-Token': case.client.post('/real/vault/unlock',
            json={'passphrase': PASSWORD}).json()['token']}
        prove(case.client, case.headers, case.context, Account.from_key('0x'+'11'*32))
        data = query(case).json()
        assert not data['wallet_scope']['missing_receipts']
        assert not next(item for item in data['wallet_scope']['wallets'] if item['account_id'] == case.peer)['provided']
        again = case.client.post(case.base+'/preview', headers=case.headers, json={'address': case.meta['owner']})
        assert again.status_code == 200, again.text
        assert all(item['current'] == item['network'] for item in again.json()['review']['accounts'])
        assert not case.calls


def test_scope_rejects_proven_wallet_outside_transfer_closure_and_strict_inputs(tmp_path):
    with multi_case(tmp_path, merge=False) as case:
        prove(case.client, case.headers, case.peer, Account.from_key('0x'+'22'*32))
        assert query(case, wallets=supplemental(case)).status_code == 409
        assert {item['account_id'] for item in query(case).json()['wallet_scope']['wallets']} == {case.owner}
        assert case.client.post(case.base+'/sources', headers=case.headers, json={
            'address': case.meta['owner'], 'wallets': supplemental(case)}).status_code == 422
        assert query(case, include_wallets='true').status_code == 422
        assert query(case, wallets=supplemental(case)*2).status_code == 422
        assert not case.calls


def test_scope_limits_are_explicit_and_read_matching_never_creates_identity_salt(tmp_path):
    with multi_case(tmp_path) as case:
        with case.vault.session() as db:
            changes = db.connection().connection.driver_connection.total_changes
            with pytest.raises(ProjectionError, match='vínculos'):
                review_wallets(db, (case.owner, case.peer), (case.meta['owner'],),
                              limits=ProjectionLimits(max_accounts=1))
            assert db.connection().connection.driver_connection.total_changes == changes
        legacy = case.client.post(case.base+'/sources', headers=case.headers, json={'address': case.meta['owner']})
        assert legacy.status_code == 200 and 'wallet_scope' not in legacy.json()
        assert all('wallet_link_id' not in row and 'wallet_account_id' not in row for row in legacy.json()['sources'])


def test_receipt_reuse_candidates_belong_to_each_wallet_ledger_not_the_root(tmp_path):
    with multi_case(tmp_path,accepted=True) as case:
        prove(case.client,case.headers,case.peer,Account.from_key('0x'+'22'*32))
        response=query(case,wallets=supplemental(case))
        row=next(item for item in response.json()['sources']if item['source_id']==case.other['source_id'])
        trade=EventInput('BUY','USDT',datetime.fromisoformat(row['occurred_at']),asset='WBNB',
            quantity=Decimal(row['quantity']),unit_price=Decimal('1'))
        with case.vault.session()as db:wrong=append_event(db,case.owner,str(uuid4()),trade).id
        with case.vault.session()as db:right=append_event(db,case.peer,str(uuid4()),trade).id
        before=counts(case.vault)
        response=query(case,wallets=supplemental(case))
        assert response.status_code==200,response.text
        row=next(item for item in response.json()['sources']if item['source_id']==case.other['source_id'])
        assert [item['id']for item in row['trade_candidates']]==[right]
        assert wrong not in str(row['trade_candidates'])
        assert counts(case.vault)==before and not case.calls
