"""First-use DCA and manual REAL ledger in an exclusively owned installation."""

import csv
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import time

from playwright.sync_api import expect, sync_playwright

BASE = os.environ['ORBIS_TEST_BASE_URL']
DATA = Path(os.environ['ORBIS_BASIC_DIRECTORY'])
OUT = DATA.parent
PASSWORD = 'owned portfolio dca journey password'
assert os.environ.get('ORBIS_BASIC_FIXTURE') == '1'
assert os.environ['ORBIS_BASIC_PHASE'] == 'create'
assert all(not (DATA / name).exists() for name in ('lab.db', 'automation.db', 'real.db'))


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).strftime('%Y-%m-%dT%H:%M')


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 1000}, timezone_id='UTC')
    errors, writes = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('dialog', lambda dialog: dialog.accept())
    page.on('request', lambda request: writes.append(request.url)
            if request.method in ('POST', 'PUT', 'DELETE') else None)
    page.route('**/market/**', lambda route: route.fulfill(status=503, json={}))
    try:
        fixture = page.request.get(BASE + '/__test/state').json()
        assert fixture['fixture_id'] == OUT.name and fixture['worker']['running']
        page.goto(BASE + '/ui/#/config')
        page.wait_for_load_state('networkidle')
        for selector, endpoint in [('#cfg-lab-create-form', '/lab/create'),
                                   ('#cfg-automation-create-form', '/automation/dca/create')]:
            form = page.locator(selector)
            form.evaluate("el=>{for(let p=el.parentElement;p;p=p.parentElement){if(p.tagName==='DETAILS')p.open=true;}}")
            expect(form).to_be_visible()
            expect(form.locator('[name=session_seconds]')).to_have_value('900')
            form.locator('[name=session_seconds]').select_option('604800')
            form.locator('[name="passphrase"]').fill(PASSWORD)
            with page.expect_response(BASE + endpoint) as response:
                form.locator('button[type="submit"]').click()
            assert response.value.status == 200, response.value.text()
            assert response.value.json()['session_seconds'] == 604800
            expect(form.locator('[name=passphrase]')).to_have_value('')
        page.goto(BASE + '/ui/#/laboratorio')
        page.wait_for_load_state('networkidle')
        download = page.locator('#lab-download')
        download.locator('[name="provider"]').select_option('mexc')
        download.locator('[name="symbol"]').fill('BTC/USDT')
        download.locator('[name="timeframe"]').select_option('1d')
        download.locator('[name="start"]').fill(stamp(fixture['start']))
        download.locator('[name="end"]').fill(stamp(fixture['end']))
        with page.expect_response('**/lab/datasets/download') as response:
            download.locator('button[type="submit"]').click()
        assert response.value.status == 200, response.value.text()
        dataset = response.value.json()
        assert dataset['count'] == 6 and dataset['verified_public']

        page.goto(BASE + '/ui/#/dca')
        page.wait_for_load_state('networkidle')
        form = page.locator('#dca-form')
        form.locator('.dca-options summary').click()
        form.locator('[name="frequency"]').select_option('daily')
        for name, value in {'name': 'Plan desde cero', 'amount': '100', 'budget_total': '500',
                            'monthly_limit': '150', 'max_exposure': '1', 'fee_rate': '0',
                            'slippage_rate': '0', 'drop1': '0.1', 'extra1': '50',
                            'drop2': '', 'extra2': '', 'max_extra_activations': '1'}.items():
            form.locator('[name="' + name + '"]').fill(value)
        with page.expect_response(lambda r: r.url == BASE + '/automation/dca/rules' and r.request.method == 'POST') as response:
            page.locator('#dca-save').click()
        assert response.value.status == 200, response.value.text()
        rule = response.value.json()
        expect(page.locator('#dca-version')).to_have_value(rule['version_id'])
        with page.expect_response(lambda r: r.url == BASE + '/automation/dca/runs' and r.request.method == 'POST') as response:
            page.locator('#dca-run').click()
        assert response.value.status == 200, response.value.text()
        run = response.value.json()
        for name in ('DCA fijo', 'DCA condicionado', 'Compra inicial'):
            expect(page.locator('#dca-report')).to_contain_text(name)
        page.locator('#dca-report .lab-verify button').click()
        expect(page.locator('#dca-report .lab-verify')).to_have_attribute('data-state', 'verified')
        page.locator('#dca-paper-dataset').select_option(dataset['id'])
        expect(page.locator('#dca-paper-run')).to_have_value(run['id'])
        assert page.request.get(BASE + '/automation/dca/paper/sessions').json() == []
        with page.expect_response(lambda r: r.url == BASE + '/automation/dca/paper/sessions' and r.request.method == 'POST') as response:
            page.locator('#dca-paper-start').click()
        assert response.value.status == 201, response.value.text()
        paper = response.value.json()
        identifier = paper['id']
        assert paper['source_run_id'] == run['id'] and Decimal(paper['state']['cash']) == 500

        def wait_tick(tick):
            deadline = time.monotonic() + 15
            while True:
                value = page.request.get(BASE + '/automation/dca/paper/sessions/' + identifier).json()
                if value['tick_index'] == tick:
                    return value
                assert time.monotonic() < deadline, value
                page.wait_for_timeout(100)

        for tick in (1, 2):
            advanced = page.request.post(BASE + '/__test/advance', data={'fixture_id': OUT.name, 'tick': tick})
            assert advanced.status == 200, advanced.text()
            observed = wait_tick(tick)
            assert Decimal(observed['state']['spent']) == 150
            assert Decimal(observed['state']['cash']) == 350
            assert Decimal(observed['state']['units']) == 2
            for endpoint in ('/lab/status', '/automation/dca/status'):
                lifetime = page.request.get(BASE + endpoint).json()
                assert lifetime['session_seconds'] == 604800 and lifetime['unlocked']
                assert 0 < lifetime['remaining_seconds'] < 604800 - 900
        page.locator('#dca-paper-session').select_option(identifier)
        expect(page.locator('#dca-paper-report')).to_contain_text('BUY')
        page.get_by_role('button', name='Preparar MEXC REAL').click()
        page.wait_for_url('**/#/real')
        expect(page.get_by_text('Prueba PAPER seleccionada para MEXC.')).to_be_visible()
        assert identifier not in page.url
        page.go_back()
        page.locator('#dca-paper-session').select_option(identifier)
        expect(page.locator('#dca-paper-report')).to_contain_text('BUY')
        page.locator('#dca-paper-stop').click()
        expect(page.locator('#dca-paper-report .statusbadge')).to_contain_text('STOPPED')
        paper = page.request.get(BASE + '/automation/dca/paper/sessions/' + identifier).json()
        assert paper['status'] == 'STOPPED'
        assert paper['state']['extra_count'] == 1
        assert Decimal(paper['state']['monthly_spent']['2025-01']) == 150
        assert [event['trigger_kind'] for event in paper['events'] if event['kind'] == 'BUY'] == ['BASE', 'DIP']
        assert [event['reason'] for event in paper['events'] if event['kind'] == 'SKIP'] == ['BUDGET_OR_EXPOSURE_LIMIT']
        page.screenshot(path=str(OUT / 'dca-desktop.png'), full_page=True)

        # This is a manual record in an owned vault, not an exchange order.
        page.goto(BASE + '/ui/#/real')
        page.wait_for_load_state('networkidle')
        page.locator('#real-passphrase').fill(PASSWORD)
        page.locator('#real-passphrase-confirm').fill(PASSWORD)
        with page.expect_response(BASE + '/real/vault/create') as response:
            page.get_by_role('button', name='Crear bóveda', exact=True).click()
        assert response.value.status == 200, response.value.text()
        headers = {'X-Orbis-Vault-Token': response.value.json()['token']}
        page.locator('#real-new-account [name="name"]').fill('Cartera manual de prueba')
        with page.expect_response(lambda r: r.url == BASE + '/real/accounts' and r.request.method == 'POST') as response:
            page.locator('#real-new-account button[type="submit"]').click()
        assert response.value.status == 200, response.value.text()
        account = response.value.json()
        expect(page.locator('#real-account')).to_have_value(account['id'])
        events_url = BASE + '/real/accounts/' + account['id'] + '/events'

        def record(kind, values, minute, status=200):
            page.locator('#real-kind').select_option(kind)
            form = page.locator('#real-event-form')
            for name, value in values.items():
                form.locator('[name="' + name + '"]').fill(value)
            form.locator('[name="occurred_at"]').fill('2025-01-04T01:' + str(minute).zfill(2))
            with page.expect_response(lambda r: r.url == events_url and r.request.method == 'POST') as response:
                form.locator('button[type="submit"]').click()
            assert response.value.status == status, response.value.text()
            if kind in ('BUY', 'SELL', 'CONTRIBUTION', 'WITHDRAWAL'):
                assert response.value.request.post_data_json['note'] is None

        record('CONTRIBUTION', {'amount': '1000'}, 0)
        # Rejected notes stay editable on their own movement. Switching to a
        # trade hides that note; it must not silently block a valid purchase.
        record('TRANSFER', {'asset': 'BTC', 'quantity': '0.1',
                            'note': '0x' + '1234' * 10}, 0, 422)
        expect(page.locator('#real-event-form [name="note"]')).to_have_value('0x' + '1234' * 10)
        record('BUY', {'asset': 'BTC', 'quantity': '1', 'unit_price': '100', 'fee': '1'}, 1)
        record('BUY', {'asset': 'BTC', 'quantity': '1', 'unit_price': '200', 'fee': '1'}, 2)
        record('SELL', {'asset': 'BTC', 'quantity': '0.5', 'unit_price': '400', 'fee': '1'}, 3)
        record('BUY', {'asset': 'ETH', 'quantity': '2', 'unit_price': '50', 'fee': '0.5'}, 4)
        record('TRANSFER', {'asset': 'BTC', 'quantity': '0.1',
                            'note': '0x' + '1234' * 10}, 4, 422)
        record('WITHDRAWAL', {'amount': '25'}, 5)
        record('SELL', {'asset': 'ETH', 'quantity': '3', 'unit_price': '50', 'fee': '0'}, 6, 422)
        record('TRANSFER', {'asset': 'BTC', 'quantity': '0.1',
                            'note': '0x' + '1234' * 10}, 7, 422)
        snapshot = page.request.get(BASE + '/real/accounts/' + account['id'] + '/snapshot', headers=headers).json()
        events = page.request.get(events_url, headers=headers).json()
        assert len(events) == 6
        assert Decimal(snapshot['cash']['EUR']) == Decimal('771.5'), snapshot
        positions = {row['asset']: row for row in snapshot['positions']}
        for asset, quantity, average, cost, realized in [('BTC', '1.5', '151', '226.5', '123.5'),
                                                         ('ETH', '2', '50.25', '100.5', '0')]:
            position = positions[asset]
            for field, expected in [('quantity', quantity), ('average_cost', average),
                                    ('cost_basis', cost), ('realized_pnl', realized)]:
                assert Decimal(position[field]) == Decimal(expected), (field, position)
        assert Decimal(snapshot['contributions']['EUR']) == 1000
        assert Decimal(snapshot['withdrawals']['EUR']) == 25
        assert Decimal(snapshot['realized_pnl']['EUR']) == Decimal('123.5')
        assert Decimal(positions['BTC']['fees']) == 3 and Decimal(positions['ETH']['fees']) == Decimal('0.5')
        assert all(not event['note'] for event in events)
        assert page.request.get(BASE + '/automation/dca/paper/sessions/' + identifier).json() == paper
        with page.expect_download() as downloaded:
            page.locator('#real-export-csv').click()
        csv_text = Path(downloaded.value.path()).read_text(encoding='utf-8-sig')
        (OUT / 'portfolio.csv').write_text(csv_text)
        rows = list(csv.DictReader(csv_text.splitlines()))
        assert len(rows) == 6 and {row['environment'] for row in rows} == {'REAL'}
        page.locator('#real-csv-import summary').click()
        page.locator('#real-csv-file').set_input_files(str(OUT / 'portfolio.csv'))
        page.locator('#real-csv-check').click()
        expect(page.locator('#real-csv-import')).to_contain_text('0 nuevos · 6 ya presentes')
        assert len(page.request.get(events_url, headers=headers).json()) == 6
        assert PASSWORD not in str(page.evaluate('Object.values(localStorage)'))
        for filename in ('lab.db', 'automation.db', 'real.db'):
            assert (DATA / filename).read_bytes()[:16] != b'SQLite format 3\x00'
        assert not any(any(fragment in url for fragment in ('/exchanges/', '/mexc-', '/dex-', '/wallet-')) for url in writes), writes
        assert not errors, errors
        page.screenshot(path=str(OUT / 'portfolio-desktop.png'), full_page=True)
        page.locator('#real-lock').click()
        expect(page.locator('#real-passphrase')).to_be_visible()
        (OUT / 'result.json').write_text(json.dumps({'passed': True, 'server': fixture['server'],
            'dataset': dataset, 'rule': rule, 'run': run, 'paper': paper, 'account': account,
            'snapshot': snapshot, 'events': events, 'errors': errors}, indent=2))
    except Exception:
        page.screenshot(path=str(OUT / 'failure.png'), full_page=True)
        (OUT / 'failure-dom.yml').write_text(page.locator('body').aria_snapshot())
        (OUT / 'failure-state.json').write_text(json.dumps(page.request.get(BASE + '/__test/state').json(), indent=2))
        raise
    finally:
        browser.close()

print('First-use DCA budget/PAPER and multiactive manual portfolio passed in owned encrypted bases')
