"""Research records survive cipher backup, not authority or forged changes."""
from datetime import timedelta
import json

import pytest
from sqlalchemy import select

from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_bot_source import DexBotSources
from app.live_execution.dex_bot_worker import DexBotWorker
from app.portfolio import dex_bot_bindings as bindings, dex_bot_journal as bots
from app.portfolio.dex_research import (binding_qualification, authorization_qualification,
    save_authorization_qualification, authorization_digest)
from app.portfolio.models import RealDexBotBinding, RealDexBotRun, RealDexResearchAuthorization
from app.portfolio.models import RealDexResearchBinding
from app.portfolio.vault import RealVault, VaultError, SCHEMA_VERSION
from app.portfolio.wallet_funding import _encoded
from tests.dex_bot_worker_fixture import running_case
from tests.test_dex_deployments import PASSWORD
from tests.test_dex_bot_run_api import http_case, eventually


def qualify(values, run_id):
    vault = values[0]
    source = vault._test_dex_source
    sources = DexBotSources(source.paper)
    with vault.session() as db:
        binding = bindings.binding_record(db, values[-1]['id'])[1]
    review = binding['origin']['review']
    checked = sources.check_research('STRATEGY', review['source'], review['candle_t'], source.validation['id'])
    with vault.session() as db:
        digest = save_authorization_qualification(db, run_id, checked)
        db.commit()
    return digest


def test_cipher_backup_retains_exact_research_and_financial_headers_with_source_locked(tmp_path):
    with running_case(tmp_path, research=True) as (values, run, _, connection, clock, _, market):
        vault = values[0]
        digest = qualify(values, run['id'])
        with vault.session() as db:
            expected = authorization_qualification(db, run['id'], expected_digest=digest)
            financial = bots.run_record(db, run['id'])[1]
            linked = binding_qualification(db, values[-1]['id'])
        assert expected['activation']['candle_t'] == financial['seed'][-1]['t']
        assert expected['activation']['dca_initial_reference'] is None
        assert financial['schema'] == 'orbis.dex.bot.long.v1'
        connection.revoke()
        market.research_source.close()
        destination = vault.backup(tmp_path/'cipher-backup.db')
        assert destination.read_bytes()[:16] != b'SQLite format 3\x00'
        copied = RealVault(destination)
        copied.unlock(PASSWORD)
        try:
            with copied.session() as db:
                assert authorization_qualification(db, run['id'], expected_digest=digest) == expected
                assert binding_qualification(db, values[-1]['id']) == linked
                assert bots.run_record(db, run['id'])[1] == financial
        finally:
            copied.lock()


@pytest.mark.parametrize('fault', ['delete', 'source', 'activation', 'target'])
def test_worker_pins_qualification_and_does_not_trade_after_archive_changes(tmp_path, fault):
    with running_case(tmp_path, research=True) as (values, run, claim, connection, clock, mine, market):
        vault, account = values[:2]
        digest = qualify(values, run['id'])
        sends = []
        def no_sender(plan):
            sends.append(plan)
            raise AssertionError('A changed qualification cannot reach emission')
        worker = DexBotWorker(vault, connection, run_id=run['id'], account_id=account,
            claim=claim, wallet_address=values[4]['owner'], market=market,
            rpc_factory=lambda: __import__('contextlib').nullcontext(values[5]),
            sender_factory=no_sender, clock=lambda: clock[0], research_digest=digest)
        with vault.session() as db:
            record = db.get(RealDexResearchAuthorization, run['id'])
            if fault == 'delete':
                db.delete(record)
            else:
                material = json.loads(record.payload_json)
                if fault == 'source':
                    material['evidence']['paper']['symbol'] = 'ETH/USDT'
                elif fault == 'activation':
                    material['activation']['close'] = '99999'
                else:
                    material['run_header_digest'] = 'f'*64
                record.payload_json, record.payload_digest = _encoded(material)
            db.commit()
        clock[0] += timedelta(seconds=1)
        mine()
        status = worker.run_once()
        assert not status['execution_enabled'] and status['stop_recorded'] and not sends
        with vault.session() as db:
            assert bots.run_record(db, run['id'])[0].status == 'STOPPED'
            with pytest.raises(DexExecutorError):
                authorization_qualification(db, run['id'], expected_digest=digest)
        worker.stop()  # Revocation remains available despite the broken side record.
        if fault != 'delete':
            with pytest.raises(VaultError, match='research'):
                vault.backup(tmp_path/'corrupt-backup.db')


def test_v71_migration_keeps_old_financial_records_and_creates_no_research_or_authority(tmp_path):
    with running_case(tmp_path) as (values, run, _, connection, clock, _, _):
        vault, account = values[:2]
        connection.revoke()
        with vault.session() as db:
            bots.stop_run(db, run['id'], account_id=account, now=clock[0])
        with vault.session() as db:
            before = [(row.id, row.payload_json, row.payload_digest, row.state_json, row.state_digest)
                      for model in (RealDexBotBinding, RealDexBotRun) for row in db.scalars(select(model))]
            db.connection().exec_driver_sql('DROP TABLE real_dex_research_authorizations')
            db.connection().exec_driver_sql('DROP TABLE real_dex_research_bindings')
            db.connection().exec_driver_sql('PRAGMA user_version=71')
            db.commit()
        vault.lock()
        vault.unlock(PASSWORD)
        assert vault.last_migration_backup is not None
        assert RealVault._verify_portfolio_file(vault.last_migration_backup, PASSWORD, expected_version=71)
        with vault.session() as db:
            assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one() == SCHEMA_VERSION
            after = [(row.id, row.payload_json, row.payload_digest, row.state_json, row.state_digest)
                     for model in (RealDexBotBinding, RealDexBotRun) for row in db.scalars(select(model))]
            assert after == before
            assert binding_qualification(db, values[-1]['id']) is None
            assert authorization_qualification(db, run['id']) is None
            assert db.query(RealDexResearchAuthorization).count() == 0


def test_original_changed_during_rpc_never_acquires_signer_or_run(tmp_path, monkeypatch):
    armed = {'value': False, 'source': None}
    def alter():
        if armed['value']:
            source = armed['source']
            with source.lab.connect() as db:
                db.execute("UPDATE validation_runs SET created_at='2025-01-01T00:00:00+00:00' WHERE id=?", (source.validation['id'],))
    with http_case(tmp_path, monkeypatch, before_read=alter) as (client, headers, base, body, values, _, _, hashes, controls):
        source=controls['market'].research_source
        with controls['supervisor'].vault.session() as db:
            db.delete(db.get(RealDexResearchBinding,body['binding_id']))
            db.commit()
        missing=client.post(base,headers=headers,json=body)
        assert missing.status_code==409 and 'reserva' in missing.text and not hashes
        body={**body,'research_validation_id':source.validation['id']}
        armed.update(value=True, source=controls['market'].research_source)
        response = client.post(base, headers=headers, json=body)
        assert response.status_code == 409 and 'investigaci' in response.text, response.text
        assert client.get(base, headers=headers).json() == [] and not hashes
        supervisor = controls['supervisor']
        assert not supervisor.connection.status(account_id=values[1], wallet_address=values[4]['owner'], now=supervisor.now())['connected']


def test_rearm_can_select_new_reserve_without_rewriting_old_permission(tmp_path, monkeypatch):
    from tests.mexc_research_fixture import historical_reserve
    with http_case(tmp_path, monkeypatch) as (client, headers, base, body, values, clock, mine, hashes, controls):
        active = client.post(base, headers=headers, json=body)
        assert active.status_code == 200, active.text
        original = active.json()['research_qualification']
        identifier = body['request_id']
        assert client.post(base+'/'+identifier+'/stop', headers=headers, json={'confirmation':'DETENER_BOT_DEX'}).status_code == 200
        eventually(lambda:client.get(base+'/'+identifier, headers=headers).json(), lambda row:not row['worker']['running'])
        clock[0] += timedelta(seconds=1)
        mine()
        source = controls['market'].research_source
        reserve = historical_reserve(source.lab, source.version['version_id'], before_t=source.dataset['start_t']-60,
                                     symbol='BNB/USDT', timeframe='1m')
        from uuid import uuid4
        rearm = {**body, 'request_id':str(uuid4()), 'permission_mode':'SESSION',
                 'research_validation_id':reserve['id'], 'confirmation':'REARMAR_BOT_DEX'}
        resumed = client.post(base+'/'+identifier+'/rearm', headers=headers, json=rearm)
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()['research_qualification']['evidence']['research']['artifact_id'] == reserve['id']
        assert client.get(base+'/'+identifier, headers=headers).json()['research_qualification'] == original
        current=resumed.json()['id']
        assert client.post(base+'/'+current+'/stop',headers=headers,json={'confirmation':'DETENER_BOT_DEX'}).status_code==200
        eventually(lambda:client.get(base+'/'+current,headers=headers).json(),lambda row:not row['worker']['running'])
        clock[0]+=timedelta(seconds=1)
        mine()
        defaulted={**body,'request_id':str(uuid4()),'permission_mode':'SESSION','confirmation':'REARMAR_BOT_DEX'}
        assert 'research_validation_id' not in defaulted
        again=client.post(base+'/'+current+'/rearm',headers=headers,json=defaulted)
        assert again.status_code==200,again.text
        assert again.json()['research_qualification']['evidence']['research']['artifact_id']==reserve['id']
        assert client.get(base+'/'+identifier,headers=headers).json()['research_qualification']==original
        assert not hashes
