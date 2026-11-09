"""Dedicated operator creation and encrypted backup, without funds or signing."""
from datetime import datetime,timezone
from uuid import uuid4
import json
from eth_account import Account
from app.live_execution.dex_operator_drafts import DexOperatorDrafts
from tests.test_dex_deployments import setup

BACKUP_PASSWORD='disposable operator backup password'


def test_funding_recipient_requires_the_current_verified_draft(tmp_path):
    import pytest
    from app.live_execution.dex_artifact import DexExecutorError
    vault,account,now=setup(tmp_path);owner=Account.from_key('0x'+'11'*32).address
    manager=DexOperatorDrafts(vault)
    try:
        created=manager.create(identifier=str(uuid4()),account_id=account,wallet_address=owner,now=now)
        args={'identifier':created['id'],'account_id':account,'wallet_address':owner}
        with pytest.raises(DexExecutorError):manager.require_verified(**args,now=datetime.now(timezone.utc))
        backup=manager.export_backup(**args,password=BACKUP_PASSWORD,now=datetime.now(timezone.utc))
        manager.verify_backup(**args,backup=backup,password=BACKUP_PASSWORD,now=datetime.now(timezone.utc))
        assert manager.require_verified(**args,now=datetime.now(timezone.utc))==created['operator_address']
        for changed in ({'identifier':str(uuid4())},{'account_id':str(uuid4())},{'wallet_address':Account.from_key('0x'+'22'*32).address}):
            with pytest.raises(DexExecutorError):manager.require_verified(**{**args,**changed},now=datetime.now(timezone.utc))
        manager.revoke()
        with pytest.raises(DexExecutorError):manager.require_verified(**args,now=datetime.now(timezone.utc))
    finally:manager.revoke();vault.lock()


def test_new_operator_is_separate_and_requires_a_successfully_decrypted_backup(tmp_path):
    vault,account,now=setup(tmp_path)
    owner=Account.from_key('0x'+'11'*32).address
    manager=DexOperatorDrafts(vault)
    try:
        created=manager.create(identifier=str(uuid4()),account_id=account,wallet_address=owner,now=now)
        assert created['exists'] and created['credential_storage']=='MEMORY_ONLY'
        assert created['operator_address'].lower()!=owner.lower() and not created['backup_verified']
        assert not created['automatic_execution']
        args={'identifier':created['id'],'account_id':account,'wallet_address':owner}
        backup=manager.export_backup(**args,password=BACKUP_PASSWORD,now=datetime.now(timezone.utc))
        assert backup['schema']=='orbis.dex.operator.backup.v1' and backup['chain_id']=='0x38'
        assert owner.lower() not in json.dumps(backup).lower()
        recovered=Account.from_key(Account.decrypt(backup['keyfile'],BACKUP_PASSWORD))
        assert recovered.address.lower()==created['operator_address']
        assert not manager.status(account_id=account,wallet_address=owner,now=datetime.now(timezone.utc))['backup_verified']
        verified=manager.verify_backup(**args,backup=backup,password=BACKUP_PASSWORD,now=datetime.now(timezone.utc))
        assert verified['backup_verified'] and not verified['automatic_execution']
        assert manager.status(account_id=account,wallet_address=owner,now=datetime.now(timezone.utc))['backup_verified']
    finally:manager.revoke();vault.lock()


def test_creation_replay_cannot_change_its_lifetime_or_rotate_a_live_operator(tmp_path):
    import pytest
    from app.live_execution.dex_artifact import DexExecutorError
    vault,account,now=setup(tmp_path);owner=Account.from_key('0x'+'11'*32).address;clock=[100.0]
    manager=DexOperatorDrafts(vault,monotonic=lambda:clock[0]);identifier=str(uuid4())
    try:
        values={'identifier':identifier,'account_id':account,'wallet_address':owner,'now':now,'ttl_seconds':60}
        original=manager.create(**values)
        assert manager.create(**values)==original
        with pytest.raises(DexExecutorError):manager.create(**{**values,'ttl_seconds':900})
        with pytest.raises(DexExecutorError):manager.create(**{**values,'identifier':str(uuid4())})
        assert manager.status(account_id=account,wallet_address=owner,now=now)['operator_address']==original['operator_address']
        clock[0]+=61
        assert not manager.status(account_id=account,wallet_address=owner,now=now)['exists']
        assert manager.create(**{**values,'identifier':str(uuid4())})['operator_address']!=original['operator_address']
        vault.lock()
        assert manager.status(account_id=account,wallet_address=owner,now=now)['credential_storage']=='NONE'
    finally:manager.revoke();vault.lock()


def test_encrypted_copy_restores_after_restart_only_for_its_original_owner(tmp_path):
    import pytest
    from app.live_execution.dex_artifact import DexExecutorError
    from tests.test_dex_deployments import PASSWORD
    vault,account,now=setup(tmp_path);owner=Account.from_key('0x'+'11'*32).address
    manager=DexOperatorDrafts(vault)
    try:
        created=manager.create(identifier=str(uuid4()),account_id=account,wallet_address=owner,now=now)
        backup=manager.export_backup(identifier=created['id'],account_id=account,wallet_address=owner,password=BACKUP_PASSWORD,now=datetime.now(timezone.utc))
        manager.revoke();vault.lock();vault.unlock(PASSWORD)
        args={'identifier':str(uuid4()),'account_id':account,'wallet_address':owner,'backup':backup,
            'password':BACKUP_PASSWORD,'now':datetime.now(timezone.utc)}
        with pytest.raises(DexExecutorError):manager.restore_backup(**{**args,'wallet_address':Account.from_key('0x'+'22'*32).address})
        assert not manager.status(account_id=account,wallet_address=owner,now=datetime.now(timezone.utc))['exists']
        restored=manager.restore_backup(**args)
        assert restored['operator_address']==created['operator_address'] and restored['backup_verified']
        assert restored['id']==args['identifier'] and not restored['automatic_execution']
        replay=manager.restore_backup(**{**args,'now':datetime.now(timezone.utc)})
        assert replay['id']==restored['id'] and replay['expires_at']==restored['expires_at']
    finally:manager.revoke();vault.lock()


def test_cancel_during_restore_does_not_install_late_keys_or_hold_the_vault_lock(tmp_path,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    import pytest
    from app.live_execution.dex_artifact import DexExecutorError
    vault,account,now=setup(tmp_path);owner=Account.from_key('0x'+'11'*32).address
    manager=DexOperatorDrafts(vault);entered=Event();release=Event()
    try:
        created=manager.create(identifier=str(uuid4()),account_id=account,wallet_address=owner,now=now)
        backup=manager.export_backup(identifier=created['id'],account_id=account,wallet_address=owner,password=BACKUP_PASSWORD,now=datetime.now(timezone.utc));manager.revoke()
        real_decrypt=Account.decrypt
        def delayed(*args,**kwargs):entered.set();assert release.wait(5);return real_decrypt(*args,**kwargs)
        monkeypatch.setattr(Account,'decrypt',delayed)
        with ThreadPoolExecutor(max_workers=1) as workers:
            future=workers.submit(manager.restore_backup,identifier=str(uuid4()),account_id=account,wallet_address=owner,
                backup=backup,password=BACKUP_PASSWORD,now=datetime.now(timezone.utc))
            try:
                assert entered.wait(3)
                # Both operations must finish while the expensive cipher job is paused.
                assert vault.session_generation is not None
                manager.revoke()
                assert not manager.status(account_id=account,wallet_address=owner,now=datetime.now(timezone.utc))['exists']
            finally:release.set()
            with pytest.raises(DexExecutorError):future.result(timeout=5)
        assert not manager.status(account_id=account,wallet_address=owner,now=datetime.now(timezone.utc))['exists']
    finally:release.set();manager.revoke();vault.lock()


def test_late_restore_from_a_revoked_session_cannot_erase_the_new_sessions_operator(tmp_path,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    import pytest
    from app.live_execution.dex_artifact import DexExecutorError
    from tests.test_dex_deployments import PASSWORD
    vault,account,now=setup(tmp_path);owner=Account.from_key('0x'+'11'*32).address
    manager=DexOperatorDrafts(vault);entered=Event();release=Event()
    try:
        first=manager.create(identifier=str(uuid4()),account_id=account,wallet_address=owner,now=now)
        backup=manager.export_backup(identifier=first['id'],account_id=account,wallet_address=owner,password=BACKUP_PASSWORD,now=datetime.now(timezone.utc));manager.revoke()
        real_decrypt=Account.decrypt
        def delayed(*args,**kwargs):entered.set();assert release.wait(5);return real_decrypt(*args,**kwargs)
        monkeypatch.setattr(Account,'decrypt',delayed)
        with ThreadPoolExecutor(max_workers=1) as workers:
            future=workers.submit(manager.restore_backup,identifier=str(uuid4()),account_id=account,wallet_address=owner,backup=backup,password=BACKUP_PASSWORD,now=datetime.now(timezone.utc))
            try:
                assert entered.wait(3)
                vault.lock();vault.unlock(PASSWORD)
                new=manager.create(identifier=str(uuid4()),account_id=account,wallet_address=owner,now=datetime.now(timezone.utc))
            finally:release.set()
            with pytest.raises(DexExecutorError):future.result(timeout=5)
        status=manager.status(account_id=account,wallet_address=owner,now=datetime.now(timezone.utc))
        assert status['exists'] and status['id']==new['id'] and status['operator_address']==new['operator_address']
    finally:release.set();manager.revoke();vault.lock()


def test_expired_or_discarded_request_id_never_silently_generates_a_different_key(tmp_path):
    import pytest
    from app.live_execution.dex_artifact import DexExecutorError
    vault,account,now=setup(tmp_path);owner=Account.from_key('0x'+'11'*32).address;clock=[100.0]
    manager=DexOperatorDrafts(vault,monotonic=lambda:clock[0])
    try:
        args={'identifier':str(uuid4()),'account_id':account,'wallet_address':owner,'now':now,'ttl_seconds':30}
        manager.create(**args);clock[0]+=31
        with pytest.raises(DexExecutorError):manager.create(**args)
        assert not manager.status(account_id=account,wallet_address=owner,now=now)['exists']
        newer={**args,'identifier':str(uuid4())};manager.create(**newer);manager.revoke()
        with pytest.raises(DexExecutorError):manager.create(**newer)
    finally:manager.revoke();vault.lock()


def test_untrusted_backup_parameters_are_rejected_before_expensive_decryption(tmp_path,monkeypatch):
    from copy import deepcopy
    import pytest
    from app.live_execution.dex_artifact import DexExecutorError
    vault,account,now=setup(tmp_path);owner=Account.from_key('0x'+'11'*32).address
    manager=DexOperatorDrafts(vault)
    try:
        state=manager.create(identifier=str(uuid4()),account_id=account,wallet_address=owner,now=now)
        args={'identifier':state['id'],'account_id':account,'wallet_address':owner,'now':datetime.now(timezone.utc)}
        backup=manager.export_backup(**args,password=BACKUP_PASSWORD)
        called=[];original=Account.decrypt
        def observe(*a,**k):called.append(True);return original(*a,**k)
        monkeypatch.setattr(Account,'decrypt',observe)
        for field,value in [('n',2**40),('n',True),('dklen',2**30),('r',0),('p',10000)]:
            wrong=deepcopy(backup);wrong['keyfile']['crypto']['kdfparams'][field]=value
            with pytest.raises(DexExecutorError):manager.verify_backup(**args,backup=wrong,password=BACKUP_PASSWORD)
        assert not called
        with pytest.raises(DexExecutorError) as failed:manager.verify_backup(**args,backup=backup,password='incorrect backup password')
        assert 'incorrect backup password' not in str(failed.value)
        assert not manager.status(account_id=account,wallet_address=owner,now=datetime.now(timezone.utc))['backup_verified']
    finally:manager.revoke();vault.lock()
