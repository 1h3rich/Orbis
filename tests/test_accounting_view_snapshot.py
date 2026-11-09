"""The public accounting capture validates shared receipts within one SQL read."""
from collections import Counter
from decimal import Decimal

import pytest

from app.portfolio import accounting_view as view, economic_acceptance as archive
from app.portfolio import models as m
from app.portfolio.economic_verified_read import KEY
from tests.test_economic_external_group_acceptance import prepared_case


@pytest.mark.parametrize('port', ['CURRENT', 'REVIEW'])
def test_shared_receipts_are_authenticated_once_in_a_complete_account_capture(tmp_path, monkeypatch, port):
    with prepared_case(tmp_path) as case:
        saved = case.confirm()
        calls = Counter()
        original = archive._read_header

        def header(db, identifier):
            calls[identifier] += 1
            return original(db, identifier)

        monkeypatch.setattr(archive, '_read_header', header)
        capture = view.capture_accounting_inputs if port == 'CURRENT' else view.capture_review_baseline
        with case.vault.session() as db:
            inputs = capture(db, case.account)
            assert KEY not in db.info
        projection = view.replay_accounting_view(inputs)
        assert projection.snapshots[case.account].positions['WBNB', 'USDT'].cost_basis == Decimal('99.7')
        assert projection.snapshots[case.peer].positions['WBNB', 'USDT'].cost_basis == Decimal('0.3')
        assert calls == {saved['id']: 1}


@pytest.mark.parametrize('port', ['CURRENT', 'REVIEW'])
def test_sql_change_after_registry_check_is_detected_by_account_capture(tmp_path, monkeypatch, port):
    with prepared_case(tmp_path) as case:
        case.confirm()
        original = view.capture_acceptance

        def changed(db, identifier):
            # A raw write deliberately bypasses the identity map after the
            # registry authenticated this receipt. Only our disposable vault.
            db.connection().exec_driver_sql(
                f'UPDATE {m.RealDexExternalBooking.__tablename__} SET payload_digest=?', ('0' * 64,))
            return original(db, identifier)

        monkeypatch.setattr(view, 'capture_acceptance', changed)
        capture = view.capture_accounting_inputs if port == 'CURRENT' else view.capture_review_baseline
        with case.vault.session() as db:
            with pytest.raises(ValueError):
                capture(db, case.account)
            assert KEY not in db.info
