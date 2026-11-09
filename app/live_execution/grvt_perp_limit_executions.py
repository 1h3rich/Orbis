"""Read-only graph projection of the verified LIMIT receipt and exit audits."""
from app.live_execution.grvt_mainnet_executions import fill_executions
from app.live_execution.grvt_mainnet_journal import encode


def limit_executions_view(saved: dict) -> dict:
    claim,events = saved['claim'],saved['events']
    cid = claim['client_order_id']
    research = claim['preflight']['plan']['source']['terms']['research']
    symbol = research['symbol']
    entry = events['receipt']
    audit = events['exit_audits'][-1] if events['exit_audits'] else events['exit_prefix']
    rows = entry['fills'] if entry is not None else []
    at = events['observation_at_ns'] if entry is not None else claim['claimed_at_ns']
    if audit is not None:
        rows = audit['source']['histories']['fill_history']['rows']
        at = audit['at_ns']
    roles = {cid:'ENTRY'}
    roles.update((item['claim']['client_order_id'],'REDUCTION') for item in events['reductions'])
    executions = fill_executions(rows,roles)
    decisions = {item['claim']['client_order_id']:item['claim']['decision'] for item in events['reductions']
                 if 'decision' in item['claim']}
    for fill in executions:
        decision = decisions.get(fill['client_order_id'])
        if decision is not None:
            fill.update(target_index=decision['target_index'],reduction_reason=decision['reason'])
    settled = events['position_settled'] or events['empty_settled']
    net = audit['result']['net_change_usdt'] if events['position_settled'] else '0' if events['empty_settled'] else None
    result = {'environment':'REAL','network':'MAINNET','provider':'grvt','parent_client_order_id':cid,
              'sub_account_id':saved['sub_account_id'],'symbol':symbol,'timeframe':research['timeframe'],
              'base_asset':symbol.split('/',1)[0],'quote_asset':'USDT',
              'evidence':'OWN_VERIFIED_FILLS' if rows else 'NO_VERIFIED_FILLS','as_of_ns':str(at),
              'authorization':False,'financially_reconciled':settled,'net_realized_pnl_usdt':net,
              'receipt_digest':events['head'] if settled else None,'executions':executions,'total':len(executions)}
    return {**result,'revision':encode(result)[1]}
