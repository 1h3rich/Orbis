"""Repeatable local verification, with persistent evidence and isolated fixtures.

Use --run outside /tmp (for example ~/.local/state/orbis/verification/<run>).
Never unlocks a user vault, starts trading, or restarts the production server.
"""
from __future__ import annotations

import argparse
from collections import Counter,defaultdict
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
PYTHON = str(ROOT/'.venv/bin/python')


def emit(value):
    print(json.dumps(value),flush=True)


def save(path, value):
    path.write_text(json.dumps(value,indent=2))


def environment(directory):
    directory.mkdir(parents=True,exist_ok=True)
    env = os.environ.copy()
    env['ORBIS_DATABASE_URL'] = 'sqlite://'
    for key,filename in (('ORBIS_LAB_PATH','lab.db'),('ORBIS_AUTOMATION_PATH','automation.db'),
            ('ORBIS_PAPER_PERP_PATH','perp.db'),('ORBIS_SPOT_STRATEGY_PATH','spot.db'),('ORBIS_REAL_VAULT_PATH','real.db')):
        env[key] = str(directory/filename)
    return env


def source_manifest():
    paths = set()
    for name in ('app','tests','scripts'):
        for path in (ROOT/name).rglob('*'):
            if path.is_file() and path.suffix in {'.py','.js','.css','.html','.json'}:
                paths.add(path)
    for name in ('src','test','support'):
        paths.update(path for path in (ROOT/'contracts'/name).rglob('*') if path.is_file() and path.suffix in {'.sol','.mjs'})
    for name in ('contracts/compiler.mjs','contracts/package.json','contracts/package-lock.json','requirements.txt'):
        paths.add(ROOT/name)
    return {str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(paths)}


def check_sources(run):
    expected = json.loads((run/'source-manifest.json').read_text())
    current = source_manifest()
    assert current == expected, {'source_changes':[path for path in expected.keys()|current.keys() if expected.get(path) != current.get(path)]}


def xml_cases(path):
    root = ET.parse(path).getroot()
    cases = list(root.iter('testcase'))
    assert cases and not any(case.find(tag) is not None for case in cases for tag in ('failure','error','skipped')),str(path)
    return cases


def normalized(node):
    # This existing parametrization intentionally creates a fresh UUID on import.
    return re.sub(r'(?<=request_id-)[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}(?=\]$)',
                  '<uuid4>',node) if node.startswith('tests/test_mexc_trade_window_audit.py::test_repeated_booking_refuses_changed_ledger_event[') else node


def collect(run):
    value = subprocess.run([PYTHON,'-m','pytest','--collect-only','-q'],cwd=ROOT,
        env=environment(run/'collection-data'),capture_output=True,text=True)
    (run/'collection.log').write_text(value.stdout+value.stderr)
    assert value.returncode == 0,value.stderr[-2500:]
    nodes = [line.strip() for line in value.stdout.splitlines() if line.startswith('tests/') and '::' in line]
    assert nodes and len(nodes) == len(set(nodes))
    return nodes


def targeted(run):
    files = sorted(str(path.relative_to(ROOT)) for path in (ROOT/'tests').glob('test_dex_*.py'))
    files += ['tests/test_evm_native_transfer.py','tests/test_real_api.py','tests/test_real_vault.py','tests/test_wallet_proof.py','tests/test_wallet_swap.py',
              'tests/test_wallet_swap_api.py','tests/test_wallet_funding.py','tests/test_wallet_funding_api.py']
    report,log = run/'targeted.xml',run/'targeted.log'
    with log.open('w') as output:
        result = subprocess.run([PYTHON,'-m','pytest','-q','--tb=short',f'--junitxml={report}',*files],cwd=ROOT,
            env=environment(run/'targeted-data'),stdout=output,stderr=subprocess.STDOUT)
    assert result.returncode == 0,log.read_text()[-12000:]
    result = {'passed':len(xml_cases(report)),'files':len(files),'report':str(report),'log':str(log)}
    save(run/'targeted.json',result)
    return result


def full(run):
    started = time.monotonic();nodes = collect(run);files = defaultdict(list)
    for node in nodes:files[node.split('::')[0]].append(node)
    heavy = {'tests/test_grvt_bot_continuous.py':100,'tests/test_grvt_bot_service.py':50,
        'tests/test_grvt_testnet_strategy_close.py':70,'tests/test_grvt_testnet_loop.py':65,
        'tests/test_grvt_testnet_bracket_submit.py':65,'tests/test_grvt_testnet_oco_finance.py':60,
        'tests/test_grvt_testnet_bracket_finance.py':60,'tests/test_grvt_testnet_bracket_emergency.py':60,
        'tests/test_grvt_strategy_close_api.py':60,'tests/test_grvt_exit_watch_api.py':60,
        'tests/test_grvt_mainnet_bot_service.py':100,'tests/test_grvt_mainnet_bot_api.py':60,
        'tests/test_grvt_mainnet_bot_entry.py':55,'tests/test_grvt_native_recovery_close.py':75,
        'tests/test_grvt_native_partial_close.py':60,'tests/test_grvt_mainnet_recovery_renewal.py':35,
        'tests/test_grvt_mainnet_executions.py':35,'tests/test_grvt_mainnet_exit_limit.py':30,
        'tests/test_dex_operator_journal.py':65,'tests/test_dex_operator.py':48,
        'tests/test_dex_operator_transport.py':12,'tests/test_dex_controls.py':36,
        'tests/test_dex_allowance_journal.py':30,'tests/test_dex_deployment_receipt.py':24,
        'tests/test_dex_bot_binding.py':32,'tests/test_dex_bot_binding_api.py':8,
        'tests/test_dex_bot_worker.py':75,'tests/test_dex_bot_supervisor.py':10,'tests/test_dex_bot_run_api.py':30}
    groups,weights = [[] for _ in range(4)],[0.]*4
    for file in sorted(files,key=lambda name:heavy.get(name,len(files[name])*.65),reverse=True):
        index = min(range(4),key=weights.__getitem__)
        groups[index].append(file);weights[index] += heavy.get(file,len(files[file])*.65)
    workers = []
    for index,group in enumerate(groups):
        report,log = run/f'shard-{index}.xml',run/f'shard-{index}.log'
        output = log.open('w')
        process = subprocess.Popen([PYTHON,'-m','pytest','-q','--tb=short',f'--junitxml={report}',*group],cwd=ROOT,
            env=environment(run/f'worker-{index}'),stdout=output,stderr=subprocess.STDOUT)
        workers.append((process,report,log,output))
    emit({'phase':'full','run':str(run),'cases':len(nodes),'files':len(files),'workers':[item[0].pid for item in workers]})
    pending = set(range(4))
    while pending:
        for index in list(pending):
            process,report,log,output = workers[index]
            if process.poll() is not None:
                output.close();pending.remove(index)
                emit({'shard':index,'returncode':process.returncode,'elapsed':round(time.monotonic()-started,1)})
                if process.returncode:emit({'failure_log':str(log),'tail':log.read_text()[-12000:]})
        if pending:time.sleep(1)
    assert all(item[0].returncode == 0 for item in workers),'Regression shard failed; see its original log'
    combined,reported = ET.Element('testsuites'),[]
    for _,path,_,_ in workers:
        root = ET.parse(path).getroot()
        for suite in list(root):combined.append(suite)
        for case in xml_cases(path):
            parts = case.attrib['classname'].split('.')
            reported.append('/'.join(parts[:2])+'.py::'+'::'.join([*parts[2:],case.attrib['name']]))
    expected,actual = Counter(map(normalized,nodes)),Counter(map(normalized,reported))
    assert expected == actual,{'missing':list((expected-actual).elements())[:10],'extra':list((actual-expected).elements())[:10]}
    report = run/'full.xml';ET.ElementTree(combined).write(report,encoding='utf-8',xml_declaration=True)
    result = {'passed':len(reported),'files':len(files),'failures':0,'errors':0,'skipped':0,
        'case_identity_verified':True,'elapsed':round(time.monotonic()-started,1),'report':str(report)}
    save(run/'full.json',result)
    return result


def contracts(run):
    log = run/'contracts.log';report = run/'contracts.xml'
    with log.open('w') as output:
        for command in (['node','compiler.mjs'],['node','--test','--test-concurrency=1','--test-reporter=junit',
                         '--test-reporter-destination='+str(report),'test/executor.test.mjs']):
            result = subprocess.run(command,cwd=ROOT/'contracts',stdout=output,stderr=subprocess.STDOUT)
            assert result.returncode == 0,log.read_text()[-10000:]
    from app.live_execution.dex_artifact import ARTIFACT_SHA256,SOURCE_SHA256,executor_artifact
    executor_artifact()
    for name in ('contracts/artifacts/BoundedSwapExecutor.json','app/live_execution/artifacts/bnb_executor_v01.json'):
        assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest() == ARTIFACT_SHA256
    result = {'passed':len(xml_cases(report)),'report':str(report),'artifact_sha256':ARTIFACT_SHA256,
              'source_sha256':SOURCE_SHA256,'compiled_artifact_matches_application':True}
    save(run/'contracts.json',result)
    return result


def browser(run,*,selected=None):
    scenarios = [('manual_paper_protection','manual_paper','manual_paper'),
        ('manual_paper_creation','manual_paper','manual_paper'),
        ('perp_bot_activation','grvt_perp_activation','grvt_perp_activation'),
        ('perp_optimized_real_review','grvt_perp_activation','grvt_perp_activation'),
        ('perp_live_review','perp_live_review','perp_live_review'),('stalled_read','grvt_mainnet_exit_limit','grvt_mainnet_exit_limit'),
        ('exit_limit','grvt_mainnet_exit_limit','grvt_mainnet_exit_limit'),
        ('grvt_limit_journey','grvt_limit','grvt_limit'),
        ('grvt_partial_limit_journey','grvt_limit','grvt_limit'),
        ('chart','grvt_mainnet_bot','grvt_mainnet_chart'),('outcomes','grvt_mainnet_bot','grvt_mainnet_outcomes'),
        ('mexc','mexc_roundtrip','mexc_roundtrip'),('mexc_manual','mexc_roundtrip','mexc_roundtrip'),('mexc_renewed_view','mexc_roundtrip','mexc_roundtrip'),('mexc_renewed_transfers','mexc_roundtrip','mexc_roundtrip'),('mexc_operational_view_renewal','mexc_roundtrip','mexc_roundtrip'),('mexc_funding_nonce','mexc_roundtrip','mexc_roundtrip'),('mexc_funding_closure','mexc_roundtrip','mexc_roundtrip'),('mexc_funding_bnb','mexc_roundtrip','mexc_roundtrip'),('mexc_withdrawal_bnb','mexc_roundtrip','mexc_roundtrip'),
        ('mexc_partial_manual','mexc_roundtrip','mexc_roundtrip'),
        ('mexc_manual_recovery','mexc_execution','mexc_execution'),('mexc_existing_balance','mexc_execution','mexc_execution'),
        ('mexc_external_deposits','mexc_external_deposits','mexc_external_deposits'),
        ('mexc_dca','mexc_dca','mexc_dca'),('mexc_dca_average','mexc_dca','mexc_dca'),('mexc_dca_volume','mexc_dca','mexc_dca'),('renewal','grvt_mainnet_renewal','grvt_mainnet_renewal'),
        ('mexc_bot_legacy','mexc_bot','mexc_bot_legacy'),
        ('mexc_bot_legacy_canceled','mexc_bot','mexc_bot_legacy'),
        ('mexc_bot_legacy_fills','mexc_bot','mexc_bot_legacy'),
        ('mexc_bot_recovery_unsent','mexc_bot','mexc_bot_recovery'),('mexc_bot_recovery_timeout','mexc_bot','mexc_bot_recovery'),
        ('manual','grvt_mainnet','grvt_mainnet'),('grvt_view_ioc','grvt_mainnet','grvt_mainnet_view_ioc'),
        ('grvt_view_ioc_history','grvt_mainnet','grvt_mainnet_view_ioc'),
        ('bot','grvt_mainnet_bot','grvt_mainnet_bot'),
        ('testnet','grvt_testnet_loop','grvt_testnet_loop'),
        ('grvt_bracket_journey','grvt_bracket','grvt_bracket'),('grvt_market_sample','grvt_bracket','grvt_market_sample'),
        ('grvt_testnet_bot_chart','grvt_bracket','grvt_bot'),('grvt_testnet_bot_chart_partial','grvt_bracket','grvt_bot'),
        ('dex_preparation','dex_bot_preparation','dex_bot_preparation'),
        ('dex_binding','dex_bot_binding','dex_bot_binding'),('dex_owner','dex_owner','dex_owner'),('dex_operator','dex_owner','dex_operator'),
        ('dex_gas','dex_gas','dex_gas'),('dex_runs','dex_runs','dex_runs'),
        ('dex_operational','dex_runs','dex_operational'),('dex_rearm','dex_runs','dex_rearm'),
        ('dex_renewal','dex_runs','dex_renewal'),('dex_dca','dex_runs','dex_dca'),
        ('dex_dca_average','dex_runs','dex_dca'),
        ('dex_dca_volume','dex_runs','dex_dca'),
        ('dex_recovered_long','dex_runs','dex_recovered_renewal'),('dex_recovered_dca','dex_runs','dex_recovered_renewal'),
        ('dex_return','dex_runs','dex_return'),('dex_chart','dex_runs','dex_chart'),('mexc_bot_chart','mexc_bot','mexc_bot'),('mexc_bot_cash_rearm','mexc_bot','mexc_bot'),('mexc_bot_manual_rearm','mexc_bot','mexc_bot'),('mexc_bot_sale_rearm','mexc_bot','mexc_bot'),('mexc_bot_assigned_sale','mexc_bot','mexc_bot'),('dex_booking','dex_runs','dex_booking'),('dex_order_proof','dex_runs','dex_booking'),('dex_cancel','dex_owner','dex_cancel'),('native_resolution','dex_gas','native_resolution'),('dex_cancellation','dex_cancellation','dex_cancellation'),('dex_wallet_cancellation','dex_wallet_cancellation','dex_wallet_cancellation'),
        ('call_wallet_executed','dex_call','dex_call'),('call_wallet_cancelled','dex_call','dex_call'),
        ('call_operator_executed','dex_call','dex_call'),('call_operator_cancelled','dex_call','dex_call'),
        ('wallet_order_proof','dex_call','wallet_order'),
        ('accounting_view','accounting_view','accounting_view'),
        ('economic_review','economic_review','economic_review'),
        ('external_recovery_batch','external_review','external_review'),
        ('external_recovery_capture','external_review','external_capture'),
        ('economic_multi_wallet','economic_review','economic_multi_wallet'),
        ('economic_order_review','economic_review','economic_order'),
        ('economic_order_existing','economic_review','economic_order'),
        ('economic_gas_review','economic_gas','economic_gas'),
        ('economic_gas_existing','economic_gas','economic_gas'),
        ('wallet_manual','wallet_swap','wallet_swap'),('wallet_existing','wallet_swap','wallet_swap'),
        ('wallet_funding_contribution','wallet_funding','wallet_funding'),
        ('wallet_funding_internal','wallet_funding','wallet_funding'),
        ('wallet_pending_timeout','wallet_swap','wallet_pending'),('wallet_pending_clear','wallet_swap','wallet_pending'),
        ('wallet_pending_remount','wallet_swap','wallet_pending'),('wallet_pending_late_reject','wallet_swap','wallet_pending'),
        ('wallet_pending_wallet_change','wallet_swap','wallet_pending'),('wallet_pending_lock_unlock','wallet_swap','wallet_pending'),
        ('basic_journey','basic','basic'),('perp_data_journey','perp_data','perp_data'),('perp_experiment_journey','perp_data','perp_experiment'),('perp_validation_journey','perp_validation','perp_validation'),('perp_observed_journey','perp_observed','perp_observed'),('manual_perp_journey','paper_perp','paper_perp'),('storage_protection_journey','storage_protection','storage_protection'),
        ('perp_limit_journey','perp_data','perp_experiment'),
        ('perp_rule_comparison_journey','perp_data','perp_experiment'),
        ('perp_run_comparison_journey','perp_data','perp_experiment'),
        ('perp_partial_journey','perp_data','perp_experiment'),
        ('perp_limit_validation_journey','perp_validation','perp_validation'),
        ('perp_optimization_journey','perp_validation','perp_optimization'),
        ('perp_optimization_variant_journey','perp_validation','perp_optimization'),
        ('perp_optimization_paper_journey','perp_observed','perp_optimization_paper'),
        ('perp_limit_observed_journey','perp_observed','perp_limit_observed'),
        ('perp_partial_observed_journey','perp_observed','perp_limit_observed'),
        ('portfolio_dca_journey','portfolio_dca','portfolio_dca'),
        ('dca_paper_observation','paper_dca','paper_dca'),
        ('dca_sma_journey','paper_dca','dca_sma'),
        ('dca_average_journey','paper_dca','dca_average'),
        ('dca_volume_journey','paper_dca','dca_volume'),
        ('dca_paper_reserve','paper_dca','dca_paper_reserve'),
        ('dca_import_journey','paper_dca','dca_import'),
        ('manual_portfolio','application','real'),
        ('lab_rsi_divergence','application','lab_rsi_divergence'),
        ('lab_full_window','lab_marker_window','lab_full_window'),
        ('lab_candle_window','lab_marker_window','lab_candle_window_live'),
        ('lab_curve_window','lab_marker_window','lab_curve_window_live'),
        ('lab_marker_window','lab_marker_window','lab_marker_window_live'),
        ('lab_optimization_journey','application','lab_optimization'),
        ('lab_walk_forward_journey','application','lab_walk_forward'),
        ('lab_export','application','lab_export'),
        ('lab_ai_archive','lab_ai_archive','lab_ai_archive'),
        ('lab_ai_comparison','lab_ai_archive','lab_ai_comparison'),
        ('market_alerts','application','market_alerts'),
        ('home_workspaces','application','home_workspaces'),
        ('estudio_long_range','application','estudio_long_range'),
        ('dca_validation_journey','application','dca_validation'),
        ('lab_spot_handoff','spot_strategy','lab_paper_handoff'),
        ('spot_strategy_journey','spot_strategy','spot_strategy'),
        ('lab_grvt_handoff','lab_grvt','lab_grvt_handoff')]
    if selected:
        assert set(selected)<={item[0] for item in scenarios},'Unknown browser scenario'
        scenarios=[item for item in scenarios if item[0] in selected]
    evidence = {}
    for name,server,flow in scenarios:
        two_process = name in ('manual_paper_protection','manual_paper_creation','basic_journey','portfolio_dca_journey','dca_validation_journey','perp_data_journey','perp_experiment_journey','perp_rule_comparison_journey','perp_run_comparison_journey','perp_limit_journey','perp_partial_journey','perp_validation_journey','perp_limit_validation_journey','perp_optimization_journey','perp_optimization_variant_journey','perp_optimization_paper_journey','perp_observed_journey','perp_limit_observed_journey','perp_partial_observed_journey','storage_protection_journey')
        # The original testnet flow intentionally uses this fixed local port.
        port = 8799 if name == 'testnet' else 8801
        directory = run/('browser-'+name);directory.mkdir(exist_ok=True)
        env = environment(directory/'data')
        if server=='lab_marker_window':env['ORBIS_TEST_LAB_WINDOW']=name
        if name in ('perp_limit_journey','perp_limit_validation_journey','perp_limit_observed_journey','perp_partial_journey','perp_partial_observed_journey'):env['ORBIS_TEST_PERP_ENTRY']='LIMIT'
        if name=='perp_partial_journey':env['ORBIS_TEST_PERP_PARTIAL']='1'
        if name=='perp_rule_comparison_journey':env['ORBIS_TEST_PERP_RULE_COMPARISON']='1'
        if name=='perp_run_comparison_journey':env['ORBIS_TEST_PERP_RUN_COMPARISON']='1'
        if name=='perp_optimization_variant_journey':env['ORBIS_TEST_PERP_OPTIMIZATION_VARIANT']='1'
        if name=='perp_optimized_real_review':env['ORBIS_TEST_GRVT_PERP_OPTIMIZED']='1'
        if name=='perp_partial_observed_journey':env['ORBIS_TEST_PERP_OBSERVED_PARTIAL']='1'
        if name=='grvt_partial_limit_journey':env['ORBIS_TEST_GRVT_LIMIT_PARTIAL']='1'
        if name=='grvt_market_sample':env['ORBIS_TEST_GRVT_MARKET_SAMPLE']='1'
        if name in ('grvt_view_ioc','grvt_view_ioc_history'):env['ORBIS_TEST_GRVT_VIEW_IOC']='1'
        if name=='grvt_view_ioc_history':env['ORBIS_TEST_GRVT_VIEW_IOC_HISTORY']='1'
        if name=='dca_sma_journey':env['ORBIS_TEST_DCA_SMA']='1'
        if name=='dca_average_journey':env['ORBIS_TEST_DCA_AVERAGE']='1'
        if name=='dca_volume_journey':env['ORBIS_TEST_DCA_VOLUME']='1'
        if name=='dca_paper_reserve':env['ORBIS_TEST_DCA_RESERVE']='1'
        if name in ('grvt_testnet_bot_chart','grvt_testnet_bot_chart_partial'):
            env.update(ORBIS_TEST_AUTOMATIC_BOT='1',ORBIS_TEST_EXIT_WATCH='1')
        if name=='grvt_testnet_bot_chart_partial':
            env.update(ORBIS_TEST_BOUNDED_BOT='1',ORBIS_TEST_NATIVE_PARTIAL='1')
        if server=='perp_observed':env['ORBIS_PAPER_PERP_PATH']=str(directory/'data/paper.db')
        if server == 'application':
            env['ORBIS_DATABASE_URL'] = 'sqlite:///'+str(directory/'data/legacy.db')
        env['ORBIS_TEST_BASE_URL'] = 'http://127.0.0.1:'+str(port)
        env['ORBIS_TEST_GRVT_DIRECTORY'] = str(directory)
        env['ORBIS_TEST_MEXC_DIRECTORY'] = str(directory)
        env['ORBIS_TEST_ROUNDTRIP_DIRECTORY'] = str(directory)
        if server=='wallet_funding':
            env['ORBIS_TEST_FUNDING_DIRECTORY'] = str(directory/'data')
            env['ORBIS_TEST_FUNDING_INTERNAL'] = '1' if name=='wallet_funding_internal' else '0'
        if name in ('mexc_dca', 'mexc_dca_average', 'mexc_dca_volume'):env['ORBIS_TEST_DCA_DIRECTORY']=str(directory/'data')
        if name=='mexc_dca_average':env['ORBIS_TEST_MEXC_DCA_AVERAGE']='1'
        if name=='mexc_dca_volume':env['ORBIS_TEST_MEXC_DCA_VOLUME']='1'
        if name in ('mexc_manual','mexc_renewed_view','mexc_renewed_transfers','mexc_operational_view_renewal','mexc_funding_nonce','mexc_funding_closure','mexc_funding_bnb','mexc_withdrawal_bnb'):env['ORBIS_TEST_MEXC_MANUAL']='1'
        if name=='mexc_partial_manual':env.update(ORBIS_TEST_MEXC_MANUAL='1',ORBIS_TEST_MEXC_PARTIAL='1')
        if name=='mexc_renewed_view':env['ORBIS_TEST_MEXC_VIEW_ROTATION']='1'
        if name=='mexc_renewed_transfers':env['ORBIS_TEST_MEXC_TRANSFER_VIEW_ROTATION']='1'
        if name=='mexc_operational_view_renewal':env['ORBIS_TEST_MEXC_OPERATIONAL_VIEW_RENEWAL']='1'
        if name=='mexc_funding_nonce':env['ORBIS_TEST_FUNDING_NONCE']='1'
        if name=='mexc_funding_closure':env['ORBIS_TEST_FUNDING_CLOSURE']='1'
        if name in ('mexc_funding_bnb','mexc_renewed_transfers'):env['ORBIS_TEST_FUNDING_BNB']='1'
        if name=='mexc_withdrawal_bnb':env['ORBIS_TEST_WITHDRAW_BNB']='1'
        if name=='mexc_existing_balance':env['ORBIS_TEST_MEXC_EXISTING']='1'
        if name=='mexc_bot_cash_rearm':env['ORBIS_TEST_MEXC_CASH_REARM']='1'
        if name=='mexc_bot_manual_rearm':env['ORBIS_TEST_MEXC_MANUAL_REARM']='1'
        if name in ('mexc_bot_sale_rearm','mexc_bot_assigned_sale'):env['ORBIS_TEST_MEXC_SALE_REARM']='1'
        if name=='mexc_bot_assigned_sale':env['ORBIS_TEST_MEXC_ASSIGNED_SALE']='1'
        if name=='mexc_bot_legacy_canceled':env['ORBIS_TEST_MEXC_LEGACY_CANCELED']='1'
        if name=='mexc_bot_legacy_fills':env['ORBIS_TEST_MEXC_LEGACY_RECOVER_FILLS']='1'
        if name in ('mexc_bot_recovery_unsent','mexc_bot_recovery_timeout'):
            env['ORBIS_TEST_BOT_RECOVERY_MODE']='before_submit' if name=='mexc_bot_recovery_unsent' else 'timeout'
        env['ORBIS_TEST_EXECUTION_DIRECTORY'] = str(directory)
        env['ORBIS_TEST_DEX_DIRECTORY'] = str(directory)
        env['ORBIS_TEST_SWAP_DIRECTORY'] = str(directory)
        if name in ('external_recovery_batch','external_recovery_capture'):
            env['ORBIS_TEST_EXTERNAL_MODE']='CAPTURE' if name=='external_recovery_capture' else 'BATCH'
        if server=='manual_paper':env['ORBIS_TEST_MANUAL_MODE']='protect' if name=='manual_paper_protection' else 'create'
        if two_process:
            env['ORBIS_BASIC_FIXTURE'] = '1'
            env['ORBIS_BASIC_DIRECTORY'] = str(directory/'data')
            assert not any((directory/'data'/file).exists() for file in ('lab.db','spot.db','automation.db','real.db')),'Basic journey requires empty owned data'
        if name in ('wallet_manual','wallet_existing'):
            env['ORBIS_TEST_SWAP_BOOKING']='1'
            env['ORBIS_TEST_SWAP_BOOKING_EXISTING']='1' if name=='wallet_existing' else '0'
        if name.startswith('wallet_pending_'):
            env['ORBIS_TEST_WALLET_PENDING_CASE']=name.removeprefix('wallet_pending_')
        if name == 'stalled_read':env['ORBIS_TEST_STALLED_READ'] = '1'
        if name == 'chart':env['ORBIS_TEST_GRVT_CHART'] = '1'
        if name == 'dex_operational':env['ORBIS_TEST_DEX_OPERATIONAL'] = '1'
        if name == 'dex_renewal':env['ORBIS_TEST_DEX_RENEWAL'] = '1'
        if name in ('dex_dca','dex_dca_average','dex_dca_volume'):
            env['ORBIS_TEST_DEX_DCA']='1';env['ORBIS_TEST_DEX_RENEWAL']='1'
        if name=='dex_dca_average':env['ORBIS_TEST_DEX_DCA_AVERAGE']='1'
        if name=='dex_dca_volume':env['ORBIS_TEST_DEX_DCA_VOLUME']='1'
        if name in ('dex_recovered_long','dex_recovered_dca'):
            env['ORBIS_TEST_DEX_RECOVERED_RENEWAL']='1';env['ORBIS_TEST_DEX_RENEWAL']='1'
            if name=='dex_recovered_dca':env['ORBIS_TEST_DEX_DCA']='1'
        if name == 'dex_cancel':env['ORBIS_TEST_DEX_CANCEL']='1'
        if name == 'native_resolution':env['ORBIS_TEST_NATIVE_RESOLUTION']='1'
        if name == 'dex_return':env['ORBIS_TEST_DEX_RETURN']='1'
        if name == 'dex_chart':env['ORBIS_TEST_DEX_CHART']='1'
        if name in ('dex_booking','dex_order_proof'):env['ORBIS_TEST_DEX_BOOKING']='1';env['ORBIS_TEST_DEX_CHART']='1'
        if name=='dex_order_proof':env['ORBIS_TEST_DEX_ORDER_PROOF']='1'
        if name.startswith('call_'):
            env['ORBIS_TEST_CALL_KIND']='OPERATOR' if name.startswith('call_operator_') else 'WALLET_SWAP'
            env['ORBIS_TEST_CALL_OUTCOME']='CANCELLED' if name.endswith('_cancelled') else 'EXECUTED'
        if name=='wallet_order_proof':
            env.update(ORBIS_TEST_CALL_KIND='WALLET_SWAP',ORBIS_TEST_CALL_OUTCOME='EXECUTED',ORBIS_TEST_ORDER_WALLET_ORIGINAL='1')
        if name=='economic_gas_existing':env['ORBIS_TEST_ECONOMIC_GAS_BOOKED']='1'
        if name=='economic_multi_wallet':env['ORBIS_TEST_ECONOMIC_MULTI']='1'
        if name in ('economic_order_review','economic_order_existing'):
            env['ORBIS_TEST_ECONOMIC_ORDER']='EXISTING'if name=='economic_order_existing'else 'BATCH'
        with socket.socket() as sock:assert sock.connect_ex(('127.0.0.1',port)) != 0,'Fixture port occupied'
        started = time.monotonic();stages = []
        for stage in (('create','reopen') if two_process else ('main',)):
            process = None
            suffix = '-reopen' if stage == 'reopen' else ''
            server_log,flow_log = directory/('server'+suffix+'.log'),directory/('flow'+suffix+'.log')
            stage_flow = flow+'_reopen' if stage == 'reopen' else flow
            if two_process:env['ORBIS_BASIC_PHASE'] = stage
            with server_log.open('w') as output:
                try:
                    module = 'app.main:app' if server == 'application' else 'tests.browser_'+server+'_server:app'
                    if server in ('storage_protection','manual_paper'):
                        if stage == 'create':
                            from tests.test_storage_protection import prepared
                            prepared(directory/'data')
                            import sqlite3
                            if server=='storage_protection' or env['ORBIS_TEST_MANUAL_MODE']=='protect':
                                with sqlite3.connect(directory/'data/legacy.db') as own_db:
                                    own_db.execute('CREATE TABLE owned_fixture (id INTEGER PRIMARY KEY)')
                        env.update({'ORBIS_DATABASE_URL':'sqlite:///'+str(directory/'data/legacy.db'),
                            'ORBIS_AUTOMATION_PATH':str(directory/'data/dca.db'),
                            'ORBIS_PAPER_PERP_PATH':str(directory/'data/grvt.db')})

                    server_env,pass_fds = env,()
                    if server == 'application':
                        from app.database.legacy_runtime import create_new_encrypted_legacy_database
                        legacy_path = (directory/'data/legacy.db').resolve()
                        assert legacy_path.is_relative_to(directory.resolve())
                        password = 'owned manual portfolio legacy password'
                        if stage == 'reopen':
                            assert legacy_path.is_file()
                        else:
                            assert not legacy_path.exists()
                            create_new_encrypted_legacy_database(legacy_path,password)
                        read_fd,write_fd = os.pipe()
                        with os.fdopen(write_fd,'wb') as key_output:key_output.write(password.encode())
                        server_env = {**env,'ORBIS_DATABASE_KEY_FD':str(read_fd)}
                        pass_fds = (read_fd,)
                    try:
                        command=([PYTHON,'-m','tests.browser_'+server+'_launcher','--port',str(port),'--no-browser']
                            if server in ('storage_protection','manual_paper') else [PYTHON,'-m','uvicorn',module,'--host','127.0.0.1','--port',str(port)])
                        process = subprocess.Popen(command,cwd=ROOT,env=server_env,
                            pass_fds=pass_fds,stdout=output,stderr=subprocess.STDOUT)
                    finally:
                        for fd in pass_fds:os.close(fd)
                    deadline = time.monotonic()+30
                    while True:
                        assert process.poll() is None,server_log.read_text()[-4000:]
                        with socket.socket() as sock:
                            if sock.connect_ex(('127.0.0.1',port)) == 0:break
                        assert time.monotonic() < deadline,'Fixture startup timeout'
                        time.sleep(.1)
                    with flow_log.open('w') as flow_output:
                        flow_module = 'tests.browser_'+stage_flow
                        if not stage_flow.endswith('_live'):
                            flow_module += '_flow'
                        result = subprocess.run([PYTHON,'-m',flow_module],cwd=ROOT,env=env,
                            stdout=flow_output,stderr=subprocess.STDOUT)
                    stages.append({'stage':stage,'ok':result.returncode == 0,'exit':result.returncode,
                                   'log':str(flow_log),'server_pid':process.pid})
                finally:
                    if process is not None and process.poll() is None:
                        process.terminate()
                        try:process.wait(timeout=10)
                        except subprocess.TimeoutExpired:process.kill();process.wait()
            if result.returncode:break
        item = {'ok':all(stage['ok'] for stage in stages),'exit':stages[-1]['exit'],
                'log':str(flow_log),'seconds':round(time.monotonic()-started,1),'stages':stages}
        evidence[name] = item;save(run/'browser.json',evidence);emit({name:item})
        assert item['ok'],flow_log.read_text()[-12000:]
    return {'passed':len(evidence),'report':str(run/'browser.json')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('--phase',choices=['manifest','targeted','full','contracts','browser'],required=True)
    parser.add_argument('--browser-scenario',action='append',help='Run only a named browser scenario; repeat to select several')
    args = parser.parse_args();run = args.run.resolve();run.mkdir(parents=True,exist_ok=True)
    if args.phase == 'manifest':
        result = source_manifest();save(run/'source-manifest.json',result);emit({'files':len(result),'run':str(run)});return
    check_sources(run)
    save(run/(args.phase+'-source-manifest.json'),source_manifest())
    assert not args.browser_scenario or args.phase=='browser','Scenario selection requires the browser phase'
    result = browser(run,selected=args.browser_scenario) if args.phase=='browser' else globals()[args.phase](run)
    check_sources(run)
    emit({'phase':args.phase,'completed_at':datetime.now(timezone.utc).isoformat(),**result})


if __name__ == '__main__':
    sys.path.insert(0,str(ROOT))
    main()
