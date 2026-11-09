"""Reserva cifrada de entrada y salidas vinculadas, sin claves de usuario."""

from dataclasses import replace
from decimal import Decimal

from app.portfolio.vault import SCHEMA_VERSION
import pytest

from app.portfolio.vault import RealVault
from app.live_execution.grvt_testnet_preflight import client_id_for_signal
from tests.test_grvt_testnet_signer import NOW,KEY,SIGNER,limits,metadata
from tests.test_grvt_testnet_bracket import plan_for


PASSWORD = 'bracket isolated test passphrase'


def source():
    cid = client_id_for_signal('paper-session','paper-entry')
    return {'kind':'GRVT_AUTO_PAPER','session_id':'paper-session','action_request_id':'paper-entry',
        'action':'OPEN','direction':'LONG','version_id':'version-1','dataset_id':'dataset-1',
        'dataset_hash':'a'*64,'client_order_id':cid}


def signed_orders():
    from app.live_execution.grvt_testnet_bracket import compile_testnet_bracket,sign_testnet_bracket
    from tests.test_grvt_testnet_risk import state,rules
    from app.live_execution.grvt_testnet_quote import GrvtTestnetQuote
    entry = replace(plan_for().entry,client_order_id=source()['client_order_id'])
    quote = GrvtTestnetQuote(entry.instrument,NOW,NOW,Decimal('49999'),Decimal('50001'),
                            Decimal('10'),Decimal('10'),last_price=Decimal('50000'))
    draft = compile_testnet_bracket(entry,state(),rules(),metadata(),limits(),limits(),quote,
        stop_loss=Decimal('49000'),take_profit=Decimal('53000'),max_spread_bps=25,
        max_slippage_bps=25,exit_nonces=(43,44),exit_expiration_ns=NOW+3_600_000_000_000,now_ns=NOW)
    return sign_testnet_bracket(draft,limits(),limits(),metadata(),dedicated_private_key=KEY,
        expected_signer=SIGNER,now_ns=NOW)


def test_three_reserved_ids_survive_unlock_and_block_the_old_single_order_path(tmp_path):
    from app.live_execution.grvt_testnet_bracket_journal import prepare_bracket,read_bracket
    from app.live_execution.grvt_testnet_journal import require_new_testnet_submission,GrvtTestnetJournalError
    vault = RealVault(tmp_path/'real.db'); vault.unlock(PASSWORD,create=True)
    orders = signed_orders()
    group = prepare_bracket(vault,orders,limits(),limits(),metadata(),signal_source=source(),now_ns=NOW)
    assert group['status'] == 'PREPARED' and len(group['legs']) == 3
    assert group['signal_source'] == source() and group['can_submit'] is False
    assert group['protection_guaranteed'] is False
    for secret in [KEY.hex(),SIGNER,*[order['signature']['r'] for order in orders]]:
        assert secret not in str(group)
    vault.lock(); vault = RealVault(tmp_path/'real.db'); vault.unlock(PASSWORD)
    assert read_bracket(vault,source()['client_order_id']) == group
    with pytest.raises(GrvtTestnetJournalError):
        require_new_testnet_submission(vault,'123456',str(2**63+12345))
    vault.backup(tmp_path/'backup.db')
    vault.lock()


def test_discarding_an_unsent_group_releases_scope_but_not_any_of_its_three_ids(tmp_path):
    from app.live_execution.grvt_testnet_bracket_journal import prepare_bracket,abandon_bracket
    from app.live_execution.grvt_testnet_journal import require_new_testnet_submission,GrvtTestnetJournalError
    vault = RealVault(tmp_path/'real.db'); vault.unlock(PASSWORD,create=True)
    orders = signed_orders()
    prepare_bracket(vault,orders,limits(),limits(),metadata(),signal_source=source(),now_ns=NOW)
    assert abandon_bracket(vault,source()['client_order_id'],now_ns=NOW+1_000_000_000)['status'] == 'ABANDONED'
    require_new_testnet_submission(vault,'123456',str(2**63+12345))
    for order in orders:
        with pytest.raises(GrvtTestnetJournalError):
            require_new_testnet_submission(vault,'123456',order['metadata']['client_order_id'])
    vault.backup(tmp_path/'discarded-backup.db')
    vault.lock()


def test_reviewed_price_policy_is_part_of_the_immutable_preparation(tmp_path):
    from app.live_execution.grvt_testnet_bracket_journal import prepare_bracket
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    vault = RealVault(tmp_path/'real.db'); vault.unlock(PASSWORD,create=True)
    orders = signed_orders()
    group = prepare_bracket(vault,orders,limits(),limits(),metadata(),signal_source=source(),now_ns=NOW,
        max_spread_bps=25,max_slippage_bps=25)
    assert group['price_policy'] == {'max_spread_bps':25,'max_slippage_bps':25}
    with pytest.raises(GrvtTestnetBracketError):
        prepare_bracket(vault,orders,limits(),limits(),metadata(),signal_source=source(),now_ns=NOW+1,
            max_spread_bps=25,max_slippage_bps=100)
    vault.lock()


def test_v36_upgrade_keeps_existing_entry_receipt_and_encrypted_preimage(tmp_path):
    from tests.test_grvt_testnet_oco import ready
    from app.live_execution.grvt_testnet_recovery import read_testnet_recovery
    old = ready(tmp_path)
    before = read_testnet_recovery(old,'123456')
    with old.session() as db:
        db.connection().exec_driver_sql('DROP TABLE grvt_testnet_bracket_legs')
        db.connection().exec_driver_sql('DROP TABLE grvt_testnet_bracket_groups')
        db.connection().exec_driver_sql('PRAGMA user_version = 36'); db.commit()
    old.lock()
    reopened = RealVault(tmp_path/'real.db'); reopened.unlock('long test passphrase')
    assert read_testnet_recovery(reopened,'123456') == before
    assert reopened.last_migration_backup and reopened.last_migration_backup.exists()
    assert reopened.last_migration_backup.read_bytes()[:16] != b'SQLite format 3\x00'
    with reopened.session() as db:
        assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one() == SCHEMA_VERSION
    reopened.backup(tmp_path/'v37-migrated-backup.db')
    reopened.lock()


def test_two_connections_prepare_one_group_without_losing_reserved_ids(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from app.live_execution.grvt_testnet_bracket_journal import prepare_bracket,read_bracket
    first = RealVault(tmp_path/'real.db'); first.unlock(PASSWORD,create=True)
    second = RealVault(tmp_path/'real.db'); second.unlock(PASSWORD)
    orders = signed_orders()
    def prepare(vault):
        return prepare_bracket(vault,orders,limits(),limits(),metadata(),signal_source=source(),now_ns=NOW)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(prepare,(first,second)))
    assert outcomes[0] == outcomes[1] == read_bracket(first,source()['client_order_id'])
    first.backup(tmp_path/'concurrent-backup.db')
    first.lock(); second.lock()


@pytest.mark.parametrize('fault',['size','nonce','asset_id_type','side','price_policy_type','price_cap','parent_trigger'])
def test_rehashed_corruption_is_rejected_by_reads_and_backup_validation(tmp_path,fault):
    import hashlib
    import json
    from app.live_execution.grvt_testnet_bracket_journal import prepare_bracket,read_bracket
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    from app.portfolio.models import GrvtTestnetBracketGroup
    from app.portfolio.vault import VaultError
    vault = RealVault(tmp_path/'real.db'); vault.unlock(PASSWORD,create=True)
    prepare_bracket(vault,signed_orders(),limits(),limits(),metadata(),signal_source=source(),now_ns=NOW)
    with vault.session() as db:
        row = db.get(GrvtTestnetBracketGroup,source()['client_order_id']); payload = json.loads(row.payload_json)
        if fault == 'size': payload['legs'][1]['size'] = '0.02'
        if fault == 'nonce': payload['legs'][1]['nonce'] = payload['legs'][0]['nonce']
        if fault == 'asset_id_type': payload['instrument_terms']['asset_id'] = int(payload['instrument_terms']['asset_id'])
        if fault == 'side': payload['legs'][0]['side'] = 'SELL'
        if fault == 'price_policy_type': payload['price_policy']['max_spread_bps'] = True
        if fault == 'price_cap': payload['entry_cap'] = '100'
        if fault == 'parent_trigger': payload['legs'][0]['trigger_price'] = '50000'
        row.payload_json = json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False)
        row.payload_digest = hashlib.sha256(row.payload_json.encode('ascii')).hexdigest(); db.commit()
    with pytest.raises(GrvtTestnetBracketError): read_bracket(vault,source()['client_order_id'])
    with pytest.raises(VaultError): vault.backup(tmp_path/'bad-backup.db')
    vault.lock()


def test_missing_v37_group_table_is_not_interpreted_as_no_pending_activity(tmp_path):
    from app.live_execution.grvt_testnet_journal import require_new_testnet_submission,GrvtTestnetJournalError
    vault = RealVault(tmp_path/'real.db'); vault.unlock(PASSWORD,create=True)
    with vault.session() as db:
        db.connection().exec_driver_sql('DROP TABLE grvt_testnet_bracket_groups'); db.commit()
    with pytest.raises(GrvtTestnetJournalError):
        require_new_testnet_submission(vault,'123456',str(2**63+12345))
    vault.lock()
