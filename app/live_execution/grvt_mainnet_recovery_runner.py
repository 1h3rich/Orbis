"""Cierre síncrono MAINNET acotado; no conserva claves ni arranca trabajos."""
from decimal import Decimal
from app.live_execution.grvt_mainnet_bracket import GrvtMainnetError

def run_mainnet_recovery(vault,parent,*,checked,factory,key,signer,public,clock,waiter,stop,on_status):
    result='PREPARING'
    def status(value):
        nonlocal result
        result=value;on_status(value)
    from app.live_execution.grvt_mainnet_recovery import cancel_mainnet_bracket_once,audit_recovery_position,read_mainnet_recovery
    from app.live_execution.grvt_mainnet_reduction import submit_recovery_reduction_once,observe_recovery_reductions,ready_snapshots
    from app.live_execution.grvt_mainnet_finance import audit_mainnet_bracket,reconcile_mainnet_bracket
    def check():
        if stop.is_set():raise GrvtMainnetError('Cierre MAINNET detenido')
        checked()
    def wait(seconds):return waiter(stop,seconds)
    try:
        while True:
            check();view=read_mainnet_recovery(vault,parent)
            if clock()>=view['request']['deadline_ns']:status('EXPIRED');break
            status('CANCELLING')
            view=cancel_mainnet_bracket_once(vault,parent,client_factory=factory,require_permission=check,clock_ns=clock)
            if not view['native_orders_terminal']:wait(2);continue
            if view['reductions']:
                status('RECOVERING')
                try:view=observe_recovery_reductions(vault,parent,client_factory=factory,require_permission=check,clock_ns=clock)
                except GrvtMainnetError:wait(2);continue
                if any(item['state'] is None or item['state']['status'] not in ('FILLED','CANCELLED','REJECTED') for item in view['reductions']):
                    wait(2);continue
            status('AUDITING')
            snapshot=audit_recovery_position(vault,parent,client_factory=factory,require_permission=check,clock_ns=clock)
            if Decimal(snapshot['details']['result']['remaining_size'])==0:
                status('VERIFYING')
                audit_mainnet_bracket(vault,parent,client_factory=factory,require_permission=check,clock_ns=clock)
                try:check();reconcile_mainnet_bracket(vault,parent,now_ns=clock())
                except GrvtMainnetError:wait(5.1);continue
                status('COMPLETE');break
            view=read_mainnet_recovery(vault,parent)
            if len(view['reductions'])>=view['request']['max_attempts']:status('ATTEMPT_LIMIT');break
            try:ready_snapshots(view['events'],view['reductions'],now_ns=clock(),after_sequence=view['request'].get('grant_sequence',0))
            except GrvtMainnetError:wait(5.1);continue
            status('REDUCING')
            submit_recovery_reduction_once(vault,parent,client_factory=factory,private_key=key,expected_signer=signer,
                require_permission=check,public_post=public,clock_ns=clock)
            wait(1)
    except Exception:status('STOPPED' if stop.is_set() else 'REVIEW')

    return result
