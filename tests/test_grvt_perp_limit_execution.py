"""Owned vault and signatures with a simulated exchange at the HTTP boundary."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
from threading import Barrier

import httpx
import pytest

from app.live_execution import grvt_perp_limit_execution as execution
from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError
from app.live_execution.grvt_mainnet_journal import encode, require_new_mainnet_bracket, stop_mainnet_subaccount
from app.live_execution.grvt_mainnet_transport import BULK_URL
from app.live_execution.grvt_perp_limit_journal import read_limit_attempt
from app.live_execution.grvt_perp_limit_plan import PerpLimitEntryPlan
from app.live_execution.grvt_perp_limit_preflight import collect_limit_preflight
from app.live_execution.grvt_perp_limit_receipt import LimitObservationPending
from app.portfolio.models import GrvtMainnetLimitAttempt, GrvtMainnetLimitEvent
from app.portfolio.vault import RealVault, SCHEMA_VERSION, VaultError
from tests.grvt_perp_mainnet_fixture import perp_case
from tests.test_grvt_perp_limit_entry import CID, NS, completed_limit_trial, fill
from tests.test_grvt_testnet_bracket_submit import PASSWORD
from tests.test_grvt_testnet_signer import KEY, SIGNER


SUB = '123456'
VENUE_ORDER_ID = '0x884'


def cold(path):
    vault = RealVault(path)
    vault.unlock(PASSWORD)
    return vault


def enable_order_history(case, *ids):
    case.settings['order_lookup_errors'].update({cid:(404,1004) for cid in ids})

    def history(body):
        if not body['cursor']:
            return {'result':[],'next':'older'}
        assert body['cursor'] == 'older'
        return {'result':[deepcopy(row) for row in case.settings['orders'].values()
                          if int(body['start_time']) < int(row['metadata']['create_time']) <= int(body['end_time'])],
                'next':''}

    case.settings['order_history'] = history


def setup(tmp_path, monkeypatch, *, lose_submit=False, lose_cancel=False, direction='LONG', partial=False, optimized=False):
    _, trial, terms, seed, records, clock, _ = completed_limit_trial(tmp_path,monkeypatch,direction=direction,partial=partial,optimized=optimized)
    case = perp_case(tmp_path)
    case.clock[0] = clock['now']
    calls = []
    base_factory = case.factory

    def factory():
        client = base_factory()
        previous = client._post

        def post(url, body, headers):
            if url != BULK_URL:
                return previous(url,body,headers)
            calls.append(deepcopy(body))
            assert body['sub_account_id'] == SUB and body['client_order_i_ds'] == []
            probe = cold(case.vault.path)
            try:
                saved = read_limit_attempt(probe,CID)
            finally:
                probe.lock()
            at = str(case.clock[0])
            if body['orders']:
                assert saved['events']['send_started'] and saved['events']['ack'] is None
                assert body['order_i_ds'] == [] and len(body['orders']) == 1
                wire = body['orders'][0]
                assert wire['time_in_force'] == 'GOOD_TILL_TIME' and wire['metadata'] == {'client_order_id':CID}
                assert encode(wire)[1] == saved['claim']['wire_digest']
                raw = {**deepcopy(wire),'order_id':VENUE_ORDER_ID,'state':{'status':'OPEN',
                    'book_size':[wire['legs'][0]['size']],'traded_size':['0'],
                    'update_time':at,'reject_reason':'UNSPECIFIED'}}
                raw['metadata']['create_time'] = at
                case.settings['orders'][CID] = raw
                if lose_submit:
                    raise httpx.ReadTimeout('Owned order response lost')
                result = {'orders':[raw],'cancel_acks':[]}
            else:
                assert saved['events']['cancel_claimed'] and body['order_i_ds'] == [VENUE_ORDER_ID]
                row = case.settings['orders'][CID]
                row['state'] = {'status':'CANCELLED','book_size':['0'],'traded_size':['0.5'],
                                'update_time':at,'reject_reason':'CLIENT_CANCEL'}
                price = '99.9' if direction == 'LONG' else '100.1'
                case.settings['histories']['fill_history'].append(fill(direction,trade_id='during-cancel',
                    size='0.2',price=price,order_id=VENUE_ORDER_ID,event_time=at))
                if lose_cancel:
                    raise httpx.ReadTimeout('Owned cancel response lost')
                result = {'orders':[],'cancel_acks':[{'ack':True}]}
            return httpx.Response(200,json={'result':result},request=httpx.Request('POST',url))

        client._post = post
        return client

    session = {'client_factory':factory,'require_permission':lambda:None,'clock_ns':lambda:case.clock[0]}
    send = {**session,'origin':trial,'budget':terms.budget,'seed':seed,'records':records,
            'max_pending_seconds':30,'entry_deadline_ns':str(case.clock[0]+30*NS),
            'private_key':KEY,'expected_signer':SIGNER,'public_post':case.public,'client_order_id':CID,
            'confirmation':'ENVIAR_ENTRADA_LIMIT_GRVT_MAINNET'}
    if partial:
        send.pop('budget')
        send.pop('max_pending_seconds')
        send.update(terms=terms,confirmation='ENVIAR_ENTRADA_LIMIT_PARCIALES_GRVT_MAINNET')
    return case,calls,session,send


@pytest.mark.parametrize('lost', [False,True])
def test_durable_submit_cancel_and_post_cancel_partial_recovery(tmp_path, monkeypatch, lost):
    case,calls,session,send = setup(tmp_path,monkeypatch,lose_submit=lost,lose_cancel=lost)
    if lost:
        with pytest.raises(GrvtMainnetError,match='incierto'):
            execution.submit_limit_entry_once(case.vault,**send)
    else:
        ack = execution.submit_limit_entry_once(case.vault,**send)
        assert ack['events']['ack'] == {'order_id':VENUE_ORDER_ID}
        assert ack['events']['receipt'] is None
    case.vault.lock()
    vault = cold(case.vault.path)
    saved = read_limit_attempt(vault,CID)
    assert not saved['authorization'] and saved['slot_reserved']
    assert bool(saved['events']['ack']) != lost
    if lost:
        enable_order_history(case,CID)
    assert 'wire' not in saved['claim'] and 'private_key' not in saved['claim']
    with pytest.raises(GrvtMainnetError,match='reservado'):
        execution.submit_limit_entry_once(vault,**send)
    with vault.session() as db, pytest.raises(GrvtMainnetError,match='reservado'):
        require_new_mainnet_bracket(db,SUB,str(int(CID)+10))
    case.clock[0] += 2*NS
    receipt = execution.observe_limit_attempt(vault,CID,**session)['events']['receipt']
    assert receipt['filled_size'] == '0' and receipt['status'] == 'OPEN'

    case.clock[0] += NS
    wire = case.settings['orders'][CID]
    remaining = Decimal(wire['legs'][0]['size']) - Decimal('0.3')
    wire['state'].update(traded_size=['0.3'],book_size=[str(remaining)],update_time=str(case.clock[0]))
    case.settings['histories']['fill_history'] = [
        fill(size='0.3',price='99.9',order_id=VENUE_ORDER_ID,event_time=str(case.clock[0]))]
    case.clock[0] += NS
    cancel = {**session,'confirmation':'CANCELAR_ENTRADA_LIMIT_GRVT_MAINNET'}
    if lost:
        with pytest.raises(GrvtMainnetError,match='incierta'):
            execution.cancel_limit_attempt_once(vault,CID,**cancel)
    else:
        result = execution.cancel_limit_attempt_once(vault,CID,**cancel)
        assert result['events']['cancel_acknowledged']
        assert result['events']['receipt']['filled_size'] == '0.3'
        assert not result['events']['receipt']['terminal']
    vault.lock()
    vault = cold(case.vault.path)
    duplicate = execution.cancel_limit_attempt_once(vault,CID,**cancel)
    assert duplicate['events']['cancel_claimed'] and len(calls) == 2
    case.clock[0] += NS
    final = execution.observe_limit_attempt(vault,CID,**session)
    receipt = final['events']['receipt']
    assert receipt['terminal'] and receipt['phase'] == 'ENTRY_TERMINAL_WITH_FILLS'
    assert receipt['filled_size'] == '0.5' and receipt['entry_notional'] == '49.95'
    assert final['slot_reserved'] and not final['authorization']
    requests = [body for name,body in case.settings['history_requests'] if name == 'order_history']
    if lost:
        assert [body['cursor'] for body in requests] == ['', 'older']*9
        for body in requests:
            assert body['sub_account_id'] == SUB and body['limit'] == 500
            assert (body['kind'],body['base'],body['quote']) == (['PERPETUAL'],['BTC'],['USDT'])
            assert int(body['start_time']) == saved['claim']['issued_at_ns']-1
            assert int(body['end_time']) <= int(saved['claim']['preflight']['plan']['result']['expires_at_ns'])
    else:
        assert not requests
    if not lost:
        vault.lock()
        vault = cold(case.vault.path)
        assert read_limit_attempt(vault,CID) == final
        vault.backup(tmp_path/'limit-encrypted-copy.db')
        with vault.session() as db:
            attempt = db.get(GrvtMainnetLimitAttempt,CID)
            event = db.get(GrvtMainnetLimitEvent,(CID,attempt.event_count))
            edited = json.loads(event.payload_json)
            edited['detail']['result']['filled_size'] = '0'
            event.payload_json,event.payload_digest = encode(edited)
            attempt.event_head = event.payload_digest
            db.commit()
        with pytest.raises(GrvtMainnetError,match='verificable'):
            read_limit_attempt(vault,CID)
        with pytest.raises(VaultError,match='MAINNET'):
            vault.backup(tmp_path/'invalid-limit-copy.db')
    vault.lock()


@pytest.mark.parametrize('failure', ['absent','duplicate','cursor','wrong_sub','tampered','unauthorized','revoked'])
def test_unproven_order_history_keeps_original_reservation(tmp_path, monkeypatch, failure):
    case,calls,session,send = setup(tmp_path,monkeypatch,lose_submit=True)
    with pytest.raises(GrvtMainnetError,match='incierto'):
        execution.submit_limit_entry_once(case.vault,**send)
    saved = read_limit_attempt(case.vault,CID)
    case.clock[0] += 45*NS
    enable_order_history(case,CID)
    original = case.settings['order_history']
    revoked = False

    def history(body):
        nonlocal revoked
        page = original(body)
        if failure == 'absent':
            page['result'] = []
        if failure == 'cursor' and body['cursor']:
            page['next'] = 'older'
        if failure == 'revoked':
            revoked = True
        if page['result']:
            row = page['result'][0]
            if failure == 'wrong_sub':
                row['sub_account_id'] = '234567'
            elif failure == 'duplicate':
                page['result'].append({**row,'order_id':'0x999'})
            elif failure == 'tampered':
                row['signature']['nonce'] ^= 1
        return page

    def permission():
        if revoked:
            raise GrvtMainnetError('Permiso de prueba revocado')

    case.settings['order_history'] = history
    if failure == 'unauthorized':
        case.settings['order_lookup_errors'][CID] = (401,1008)
    error = LimitObservationPending if failure in ('absent','unauthorized') else GrvtMainnetError
    with pytest.raises(error):
        execution.observe_limit_attempt(case.vault,CID,**{**session,'require_permission':permission})
    assert read_limit_attempt(case.vault,CID) == saved
    assert saved['slot_reserved'] and saved['events']['receipt'] is None and len(calls) == 1
    requests = [body for name,body in case.settings['history_requests'] if name == 'order_history']
    assert len(requests) == {'unauthorized':0,'revoked':1}.get(failure,2)
    expiry = saved['claim']['preflight']['plan']['result']['expires_at_ns']
    assert all(body['end_time'] == expiry for body in requests)
    case.vault.lock()


def test_stop_after_claim_refuses_send_and_keeps_owned_attempt(tmp_path, monkeypatch):
    case,calls,_,send = setup(tmp_path,monkeypatch)
    original = execution.claim_limit_attempt

    def claim_then_stop(vault, *args, **kwargs):
        digest = original(vault,*args,**kwargs)
        stop_mainnet_subaccount(vault,SUB)
        return digest

    monkeypatch.setattr(execution,'claim_limit_attempt',claim_then_stop)
    with pytest.raises(GrvtMainnetError,match='revocado'):
        execution.submit_limit_entry_once(case.vault,**send)
    saved = read_limit_attempt(case.vault,CID)
    assert not calls and not saved['events']['send_started'] and saved['slot_reserved']
    case.vault.lock()


def test_terminal_zero_fill_releases_only_after_stable_flat_account(tmp_path, monkeypatch):
    case,calls,session,send = setup(tmp_path,monkeypatch)
    execution.submit_limit_entry_once(case.vault,**send)
    case.clock[0] += 31*NS
    row = case.settings['orders'][CID]
    row['state'].update(status='CANCELLED',reject_reason='EXPIRED',book_size=['0'],update_time=str(case.clock[0]))
    case.settings.update(account_balance='99',account_equity='99')
    with pytest.raises(GrvtMainnetError,match='verificable'):
        execution.settle_empty_limit_attempt(case.vault,CID,**session)
    assert read_limit_attempt(case.vault,CID)['slot_reserved']
    case.settings.update(account_balance='100',account_equity='100')
    case.clock[0] += NS
    settled = execution.settle_empty_limit_attempt(case.vault,CID,**session)
    assert settled['events']['empty_settled'] and not settled['slot_reserved']
    case.vault.lock()
    vault = cold(case.vault.path)
    assert read_limit_attempt(vault,CID) == settled
    with vault.session() as db:
        require_new_mainnet_bracket(db,SUB,str(int(CID)+10))
        with pytest.raises(GrvtMainnetError,match='reservado'):
            require_new_mainnet_bracket(db,SUB,CID)
    assert execution.settle_empty_limit_attempt(vault,CID,**session) == settled
    assert len(calls) == 1
    vault.lock()


def test_additive_limit_migration_from_79_verifies_backup(tmp_path):
    path = tmp_path/'schema79.db'
    vault = RealVault(path)
    vault.unlock(PASSWORD,create=True)
    with vault.session() as db:
        conn = db.connection()
        for table in ('grvt_mainnet_limit_reductions','grvt_mainnet_limit_events','grvt_mainnet_limit_slots',
                      'grvt_mainnet_limit_attempts'):
            conn.exec_driver_sql(f'DROP TABLE {table}')
        conn.exec_driver_sql('PRAGMA user_version = 79')
        db.commit()
    vault.lock()
    vault.unlock(PASSWORD)
    backup = vault.last_migration_backup
    assert backup.is_file() and vault.verify_managed_backup(backup)['verified']
    with vault.session() as db:
        assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one() == SCHEMA_VERSION
        assert db.query(GrvtMainnetLimitAttempt).count() == 0
    vault.lock()


def test_concurrent_claims_serialize_across_two_vault_handles(tmp_path, monkeypatch):
    case,calls,session,send = setup(tmp_path,monkeypatch)
    vaults = [case.vault, cold(case.vault.path)]
    terms = send['origin'].terms(send['budget'],max_pending_seconds=30)
    clients = [session['client_factory']() for _ in vaults]
    proof = collect_limit_preflight(clients[0],terms,send['seed'],send['records'],
        entry_deadline_ns=send['entry_deadline_ns'],public_post=case.public,clock_ns=session['clock_ns'])
    plan = PerpLimitEntryPlan.model_validate(proof['plan'])
    barrier = Barrier(2)

    def claim(index):
        cid = str(int(CID)+index*10)
        contract = execution._sign(plan,cid,key=KEY,expected_signer=SIGNER,at=case.clock[0])
        barrier.wait(timeout=15)
        try:
            execution.claim_limit_attempt(vaults[index],contract,proof,send['seed'],send['records'],
                client=clients[index],checked=session['clock_ns'])
            return cid
        except GrvtMainnetError as exc:
            assert 'reservado' in str(exc)
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim,range(2)))
    winners = [cid for cid in outcomes if cid]
    assert len(winners) == 1 and not calls
    assert not read_limit_attempt(case.vault,winners[0])['events']['send_started']
    for vault in vaults:
        vault.lock()
