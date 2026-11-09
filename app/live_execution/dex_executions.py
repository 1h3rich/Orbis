"""Read-only DEX execution projection from verified causal receipt families."""
from datetime import datetime
from decimal import Context,Decimal,ROUND_HALF_EVEN,localcontext
import re
from uuid import UUID

from sqlalchemy import select
from app.lab.store import content_hash
from app.live_execution.dex_artifact import DexExecutorError
from app.live_execution.dex_dca_plan import ENGINE as DCA_ENGINE
from app.portfolio import dex_bot_journal as bots,dex_bot_bindings as bindings,dex_operator_journal as operator
from app.portfolio.dex_verified_read import scope
from app.portfolio.models import RealDexBotDecision,RealDexOperatorIntent,RealDexResearchAuthorization
from app.portfolio.wallet_funding import _decoded,_time

from app.live_execution.dex_versions import DCA_POLICY_OUTCOME,EXTERNAL_DCA_ENGINES
DCA_ENGINES=(DCA_ENGINE,'orbis.dex.bot.dca.v2',DCA_POLICY_OUTCOME,*EXTERNAL_DCA_ENGINES)

PRICE_PRECISION=80
MAX_EXECUTIONS=100000


def _units(value:str|int)->str:
    number=int(value);whole,fraction=divmod(abs(number),10**18)
    tail=str(fraction).zfill(18).rstrip('0')
    return ('-' if number<0 else '')+str(whole)+('.'+tail if tail else '')


def _price(gross:str,quantity:str)->str:
    with localcontext(Context(prec=PRICE_PRECISION,rounding=ROUND_HALF_EVEN)):
        value=Decimal(gross)/Decimal(quantity)
        return format(value,'f').rstrip('0').rstrip('.') if '.' in format(value,'f') else format(value,'f')


def read_executions(db,*,account_id:str,run_id:str)->dict:
    account_id,run_id=str(UUID(account_id)),str(UUID(run_id))
    with scope(db):
        selected,terms,_,archive=bots.run_record(db,run_id)
        if selected.account_id!=account_id:raise DexExecutorError('Las ejecuciones DEX pertenecen a otra cuenta')
        if terms['schema'] in DCA_ENGINES:
            from app.portfolio.dex_dca_journal import _ordered as ordered
        else:
            from app.portfolio.dex_bot_continuity import ordered_predecessors as ordered
        family=ordered(db,run_id);source=archive['review']['source']
        binding=bindings.binding_record(db,selected.binding_id)[1]
        result=[];witnesses=[];identities=set();pending=reverted=gas=cancelled=cancelled_gas=0;has_resolutions=False
        external_count=external_gas=0
        version=db.connection().exec_driver_sql('PRAGMA user_version').scalar_one()
        external_support=version>=78
        outcomes=version>=71
        for identifier in family:
            row,definition,_,history=bots.run_record(db,identifier)
            if row.account_id!=account_id or history['review']['source']!=source:raise DexExecutorError('El linaje de ejecuciones cambió de cuenta o fuente')
            # Join the indexed immutable decision digest. A long history of
            # WAIT decisions must not create an unbounded SQL IN parameter list.
            query=(select(RealDexOperatorIntent,RealDexBotDecision).join(RealDexBotDecision,
                RealDexBotDecision.payload_digest==RealDexOperatorIntent.decision_ref).where(
                    RealDexOperatorIntent.grant_id==row.grant_id,RealDexBotDecision.run_id==identifier)
                .order_by(RealDexOperatorIntent.created_at,RealDexOperatorIntent.id))
            for raw,event in db.execute(query):
                item,recipe,state=operator.intent_record(db,raw.id);decision=_decoded(event.payload_json,event.payload_digest)
                if recipe['action']=='STOP':continue
                if (item.id in identities or item.account_id!=account_id or item.grant_id!=row.grant_id
                        or recipe['action']!=decision['plan']['action'] or decision['execution'] is None
                        or any(recipe.get(key)!=value for key,value in decision['execution'].items())):
                    raise DexExecutorError('La ejecución DEX no corresponde a su decisión propia')
                identities.add(item.id);witnesses.append({'intent':item.id,'payload':item.payload_digest,'state':item.state_digest,
                    'decision':recipe['decision_ref'],'run':row.id,'run_payload':row.payload_digest})
                if len(identities)>MAX_EXECUTIONS:raise DexExecutorError('Demasiadas ejecuciones para este gráfico DEX')
                effective=item.status;receipt=state['receipt'];resolved=None
                if outcomes:
                    from app.portfolio.dex_operator_outcomes import operator_outcome
                    fact=operator_outcome(db,item.id,include_external=external_support)
                    effective='FINALIZED' if fact.outcome=='EXECUTED' else fact.outcome
                    if fact.external_witness_json is not None:
                        witnesses[-1]['external_review']=fact.witness()
                        external_count+=1;external_gas+=fact.gas_fee_wei
                        continue
                    if fact.resolution_id is not None:
                        resolved=fact.witness();has_resolutions=True
                        witnesses[-1]['resolution']=resolved
                        receipt={**fact.receipt,**(fact.effect or {})}
                if effective=='ABANDONED_UNSENT':continue
                if effective=='CANCELLED':cancelled+=1;cancelled_gas+=int(receipt['gas_fee_wei']);continue
                if effective=='REVERTED':reverted+=1;gas+=int(receipt['gas_fee_wei']);continue
                if effective!='FINALIZED':pending+=1;continue
                buy=recipe['action']=='BUY'
                quantity=_units(receipt['amount_out_units'] if buy else recipe['trade']['amount'])
                gross=_units(recipe['trade']['amount'] if buy else receipt['amount_out_units'])
                at=_time(datetime.fromisoformat(receipt['block_at']))
                booked=False;event_id=None;booking_id=None
                if version>=66:
                    from app.portfolio.dex_receipt_booking import booking_record,booking_id as receipt_booking_id
                    record=booking_record(db,receipt_booking_id('CALL_RESOLUTION',resolved['resolution_id']) if resolved else receipt_booking_id('OPERATOR',item.id))
                    if record is not None:
                        booked=True;booking_id=record[0].id
                        event_id=next(ref['id'] for ref in record[1]['events'] if ref['role']=='TRADE')

                result.append({'fill_id':item.id,'intent_id':item.id,'run_id':row.id,'binding_id':row.binding_id,
                    **({'receipt_kind':'CALL_RESOLUTION','resolution_id':resolved['resolution_id']} if resolved else {}),
                    'financially_booked':booked,'booking_id':booking_id,'event_id':event_id,
                    'strategy_version_id':source['version_id'],'side':recipe['action'],'role':'ENTRY' if buy else 'EXIT',
                    'quantity_base':quantity,'gross_quote':gross,'price':_price(gross,quantity),
                    'fee':_units(receipt['gas_fee_wei']),'fee_asset':'BNB','gas_fee_wei':receipt['gas_fee_wei'],
                    'occurred_at':at.isoformat(),'time_ms':int(at.timestamp())*1000+at.microsecond//1000,
                    'block_number':receipt['block_number'],'block_hash':receipt['block_hash'],'reason':decision['plan']['reason'],
                    'signal_candle_t':decision['plan']['candle_t'],'decision_at':decision['at'],
                    'reference_price':decision['quote']['ask' if buy else 'bid'],
                    'reference_at':decision['quote']['event_at'],'reference_observed_at':decision['quote']['observed_at'],
                    'activation_kind':decision['plan'].get('dca',{}).get('kind'),
                    'activation_key':decision['plan'].get('dca',{}).get('key'),
                    **({'volume':decision['plan']['evidence']['volume']} if 'volume' in decision['plan']['evidence'] else {}),
                    'partial':not buy and int(receipt['owned_wbnb'])>0,'position_after_base':_units(receipt['owned_wbnb']),
                    'realized_pnl_quote':_units(receipt['realized_pnl_usdt']) if not buy else None})
        result.sort(key=lambda item:(item['time_ms'],int(item['block_number']),item['fill_id']))
        value={'environment':'REAL','provider':'uniswap-v3','chain_id':'0x38','account_id':account_id,'run_id':run_id,
            'root_binding_id':binding.get('root_binding_id',selected.binding_id),'family_run_ids':family,
            'symbol':'BNB/USDT','timeframe':source['timeframe'],'base_asset':'WBNB','quote_asset':'USDT',
            'reference_provider':'mexc','evidence':'OWN_FINALIZED_DEX_RECEIPTS','price_precision':PRICE_PRECISION,
            'financially_booked':bool(result) and all(fill['financially_booked'] for fill in result),
            'booked_count':sum(fill['financially_booked'] for fill in result),'authorization':False,'total':len(result),'executions':result,
            'pending_count':pending,'reverted_count':reverted,'reverted_gas_wei':str(gas)}
        if has_resolutions:value.update(cancelled_count=cancelled,cancelled_gas_wei=str(cancelled_gas))
        if external_count:value.update(external_reviewed_count=external_count,external_gas_wei=str(external_gas),
            external_reviews_are_fills=False)
        # WAIT decisions do not change economic data. Pagination is invalidated
        # by actual intent/receipt changes, not by every polling heartbeat.
        revision_material={'value':value,'witnesses':witnesses}
        if version>=72 and result:
            # Pin the independent LAB qualification for each authorization
            # that produced an exportable swap, without exposing its ID.
            research_digests={}
            for identifier in sorted({fill['run_id'] for fill in result}):
                record=db.get(RealDexResearchAuthorization,identifier)
                research_digests[identifier]=record.payload_digest if record is not None else None
            revision_material['research_authorizations']=research_digests
        return {**value,'revision':content_hash(revision_material)}


def executions_page(value:dict,*,limit:int=500,offset:int=0,revision:str|None=None)->dict:
    if (type(limit) is not int or not 1<=limit<=500 or type(offset) is not int or not 0<=offset<=MAX_EXECUTIONS
            or offset>0 and revision is None or revision is not None and (type(revision) is not str or not re.fullmatch(r'[0-9a-f]{64}',revision))):
        raise DexExecutorError('Paginación de ejecuciones DEX no válida')
    if revision is not None and revision!=value['revision']:raise DexExecutorError('Las ejecuciones DEX cambiaron; actualiza el gráfico')
    if offset>value['total']:raise DexExecutorError('Tramo de ejecuciones DEX fuera de la instantánea')
    end=min(offset+limit,value['total'])
    return {**value,'executions':value['executions'][offset:end],'offset':offset,'next_offset':end if end<value['total'] else None}
