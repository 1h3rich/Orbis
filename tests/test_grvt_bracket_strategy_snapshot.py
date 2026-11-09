"""Definición de estrategia sellada antes del POST nativo TESTNET."""

from app.portfolio.vault import SCHEMA_VERSION
import pytest

from app.live_execution.grvt_testnet_bracket_submit import submit_bracket_signal_once
from app.live_execution.grvt_testnet_order import GrvtTestnetOrderClient,BULK_ORDERS_URL
from app.portfolio.vault import RealVault,VaultError
from tests.test_grvt_testnet_bracket_submit import live_group,PASSWORD


def test_snapshot_exists_before_transport_and_survives_locked_sources_and_backup(tmp_path):
    from app.live_execution.grvt_bracket_strategy_snapshot import read_bracket_strategy
    from app.derivatives.paper_access import paper_perp_access
    from app.lab.access import lab_access
    venue,origin,vault,orders,cap,instrument,client,public,recorded=live_group(tmp_path)
    captured=[]
    second=RealVault(tmp_path/'real.db'); second.unlock(PASSWORD)
    def post(url,body,headers):
        if url==BULK_ORDERS_URL:
            captured.append(read_bracket_strategy(second,origin['client_order_id']))
        return client._post(url,body,headers)
    transport=GrvtTestnetOrderClient('fake-testnet-key-'+cap.sub_account_id,http_post=post)
    _,result=submit_bracket_signal_once(venue.paper,venue.lab,vault,transport,orders,cap,cap,instrument,
        session_id=origin['session_id'],action_request_id=origin['action_request_id'],public_post=public)
    assert len(captured)==1 and captured[0]['definition']['stop_loss']=='0.03'
    assert captured[0]['signal_source']==result['signal_source']
    paper_perp_access.lock(venue.paper.path); lab_access.lock(venue.lab.path)
    second.lock(); vault.lock(); reopened=RealVault(tmp_path/'real.db'); reopened.unlock(PASSWORD)
    assert read_bracket_strategy(reopened,origin['client_order_id'])==captured[0]
    reopened.backup(tmp_path/'snapshot-backup.db'); reopened.lock()


def test_migration_preserves_legacy_groups_without_inventing_historical_definitions(tmp_path):
    from app.live_execution.grvt_bracket_strategy_snapshot import read_bracket_strategy
    from tests.test_grvt_testnet_bracket_submit import prepared
    vault=RealVault(tmp_path/'real.db'); vault.unlock(PASSWORD,create=True)
    plan,_,_=prepared(vault)
    with vault.session() as db:
        db.connection().exec_driver_sql('DROP TABLE grvt_testnet_bracket_strategies')
        db.connection().exec_driver_sql('PRAGMA user_version = 42'); db.commit()
    vault.lock(); vault.unlock(PASSWORD)
    assert read_bracket_strategy(vault,plan['parent_client_order_id']) is None
    with vault.session() as db:
        assert db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()==SCHEMA_VERSION
    vault.backup(tmp_path/'migrated-backup.db'); vault.lock()


def test_definition_cannot_be_replaced_after_sending_and_corruption_blocks_backup(tmp_path):
    from app.live_execution.grvt_bracket_strategy_snapshot import read_bracket_strategy,save_bracket_strategy
    from app.live_execution.grvt_testnet_bracket import GrvtTestnetBracketError
    from app.portfolio.models import GrvtTestnetBracketStrategy
    venue,origin,vault,orders,cap,instrument,client,public,recorded=live_group(tmp_path)
    submit_bracket_signal_once(venue.paper,venue.lab,vault,client,orders,cap,cap,instrument,
        session_id=origin['session_id'],action_request_id=origin['action_request_id'],public_post=public)
    saved=read_bracket_strategy(vault,origin['client_order_id'])
    context={key:saved[key] for key in ('definition','timeframe','engine_version')}
    assert save_bracket_strategy(vault,origin['client_order_id'],context)==saved
    with pytest.raises(GrvtTestnetBracketError):
        save_bracket_strategy(vault,origin['client_order_id'],
            {**context,'definition':{**context['definition'],'stop_loss':'0.5'}})
    assert len(recorded['sends'])==1
    with vault.session() as db:
        row=db.get(GrvtTestnetBracketStrategy,origin['client_order_id'])
        row.payload_json=row.payload_json.replace('"stop_loss":"0.03"','"stop_loss":"0.04"')
        db.commit()
    with pytest.raises(GrvtTestnetBracketError): read_bracket_strategy(vault,origin['client_order_id'])
    with pytest.raises(VaultError): vault.backup(tmp_path/'must-not-accept.db')
    vault.lock()
