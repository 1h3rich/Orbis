"""Two genuine pending wallet calls, replaced on ONE disposable chain."""
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
from decimal import Decimal as D
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.live_execution.dex_call_resolution import collect_call_resolution
from app.live_execution.dex_external_order import common_reference,verify_external_order
from app.portfolio import dex_external_observation as observations,dex_external_order as orders
from app.portfolio.domain import EventInput
from app.portfolio.economic_external_capture import capture_external_effects,WalletParticipant
from app.portfolio.economic_external_effects import EffectAllocation,EffectAnnotation
from app.portfolio.economic_external_plan import NewExternalEvent,event_id
from app.portfolio.economic_projection import ExternalOrderMember
from app.portfolio.economic_projection_source import _existing_salt
from app.portfolio.repository import create_account,append_event
from app.portfolio.wallet_links import save_wallet_link
from tests.test_dex_external_observation import external_case,calldata
from tests.test_dex_external_observation_journal import proposal,save
from tests.test_dex_call_resolution import prepare
from tests.dex_evm_fixture import wait_receipt


@contextmanager
def batch_case(path,*,asset=True,later_sale=False,shared_custody=False):
    with external_case(path,'WBNB_TRANSFER'if asset else'TRANSFER')as first,pytest.MonkeyPatch.context()as patch:
        vault,rpc,meta=first.vault,first.rpc,first.meta
        with vault.session()as db:peer=create_account(db,'Segunda wallet','USDT').id
        def clock():
            block=rpc('eth_getBlockByNumber',['latest',False])
            return max(datetime.now(timezone.utc),datetime.fromtimestamp(int(block['timestamp'],16),timezone.utc))+timedelta(seconds=2)
        patch.setattr('tests.test_dex_call_resolution.now',clock)
        peer_meta={**meta,'owner':meta['operator'],'operator':meta['owner']}
        second_plan,second_source,second_before=prepare(vault,peer,peer_meta,rpc,token='WBNB'if asset else'USDT')
        transaction={**second_plan['transaction'],'gasPrice':hex(2*10**9),
            'to':meta['wbnb'if asset else'usdt'],
            'data':calldata('transfer(address,uint256)',['address','uint256'],[meta['owner'],2*10**15])}
        sent=rpc('eth_sendTransaction',[transaction]);wait_receipt(rpc,sent)
        second=SimpleNamespace(vault=vault,account=peer,source=second_source,before=second_before,plan=second_plan,
            meta=peer_meta,rpc=rpc,evidence=collect_call_resolution(rpc,second_source,sent),reference=first.reference,
            now=clock(),hash=sent)
        entries=[]
        for current in(first,second):
            # Both origins remain pending and immutable; observations/certificates
            # describe the actual replacements, never pretend they were swaps.
            saved=save(current,proposal(current))
            with vault.session()as db:
                row,body=observations.observation_record(db,saved['id'])
                certificate=verify_external_order(current.source,row.id,row.payload_digest,body['proof'],current.evidence,
                    reference=current.reference,common=common_reference(_existing_salt(db)),now=clock())
                preview=orders.preview(row,certificate)
            with vault.session()as db:orders.save_order(db,saved['id'],account_id=current.account,proof=certificate,
                preview_digest=preview['preview_digest'],now=clock())
            entries.append(SimpleNamespace(current=current,observation_id=saved['id'],proof=body['proof'],certificate=certificate))
        at=datetime.fromisoformat(entries[0].proof['receipt']['block_at'])
        later=datetime.fromisoformat(entries[1].proof['receipt']['block_at'])
        with vault.session()as db:
            save_wallet_link(db,first.account,meta['owner'],'0x38','Primera');db.commit()
            save_wallet_link(db,first.account if shared_custody else peer,meta['operator'],'0x38','Segunda');db.commit()
        def append(account,item):
            with vault.session()as db:append_event(db,account,str(uuid4()),item)
        append(first.account,EventInput('CONTRIBUTION','USDT',at-timedelta(hours=1),amount=D('1000')))
        if asset:append(first.account,EventInput('BUY','USDT',at-timedelta(minutes=59),asset='WBNB',quantity=D('1'),unit_price=D('100')))
        for account in((first.account,)if shared_custody else(first.account,peer)):
            append(account,EventInput('CONTRIBUTION','BNB',at-timedelta(hours=1),amount=D('1')))
        if later_sale:
            append(peer,EventInput('CONTRIBUTION','USDT',at-timedelta(hours=1),amount=D('1')))
            append(peer,EventInput('BUY','USDT',at-timedelta(minutes=59),asset='WBNB',quantity=D('0.005'),unit_price=D('200')))
            append(peer,EventInput('SELL','USDT',later+timedelta(seconds=1),asset='WBNB',quantity=D('0.002'),unit_price=D('300')))
        inputs=[];declarations=[]
        for index,entry in enumerate(entries):
            account,other=(first.account,peer)if index==0 else(peer,first.account)
            owner,other_address=(meta['owner'],meta['operator'])if index==0 else(meta['operator'],meta['owner'])
            with vault.session()as db:
                captured=capture_external_effects(db,'WALLET_SWAP',entry.current.source.id,context_account_id=account,
                    owner_address=owner,wallets=(WalletParticipant(other,other_address),))
            inputs.append(captured)
            account=captured.root_account_id
            if shared_custody:other=account
            review_id=str(uuid4());gas,out,incoming,pair=(str(uuid4())for _ in range(4))
            stamp=datetime.fromisoformat(entry.proof['receipt']['block_at'])
            amount=D(entry.proof['transfers'][0]['units']).scaleb(-18)
            proposals=(NewExternalEvent(gas,account,EventInput('FEE','BNB',stamp,
                amount=D(entry.proof['receipt']['gas_fee_wei']).scaleb(-18),note='Gas externo observado')),
                NewExternalEvent(out,account,EventInput('TRANSFER_OUT'if asset else'CASH_TRANSFER_OUT','USDT',stamp,
                    note='Traslado propio',**({'asset':'WBNB','quantity':amount,'gross_override':D(0)}if asset else{'amount':amount})),pair),
                NewExternalEvent(incoming,other,EventInput('TRANSFER_IN'if asset else'CASH_TRANSFER_IN','USDT',stamp,
                    note='Traslado propio',**({'asset':'WBNB','quantity':amount,'gross_override':D(0)}if asset else{'amount':amount})),pair))
            if shared_custody:proposals=proposals[:1]
            declarations.append(dict(inputs=captured,review_id=review_id,new_events=proposals,
                members=tuple(ExternalOrderMember(event_id(review_id,key),who,role)for key,who,role in
                    (((gas,account,'GAS'),)if shared_custody else((gas,account,'GAS'),(out,account,'EFFECT'),(incoming,other,'EFFECT')))),
                allocations=(EffectAllocation(('token:'+entry.proof['transfers'][0]['log_ref'],),
                    ()if shared_custody else(event_id(review_id,out),event_id(review_id,incoming)),
                    'NO_LEDGER_CHANGE'if shared_custody else'OWN_TRANSFER','Traslado entre wallets propias'),),
                annotations=(EffectAnnotation('unobserved-effects','MANUAL_COMPLETE','Revisión manual de cobertura restante'),)))
        recorded_at=max(*(item.not_before for item in inputs),*(event.recorded_at for event in inputs[0].events))+timedelta(seconds=1)
        def recapture(db):
            values=[]
            for index,entry in enumerate(entries):
                other=entries[1-index].current
                values.append(capture_external_effects(db,'WALLET_SWAP',entry.current.source.id,
                    context_account_id=entry.current.account,owner_address=entry.current.meta['owner'],
                    wallets=(WalletParticipant(other.account,other.meta['owner']),)))
            return tuple(values)
        yield SimpleNamespace(vault=vault,account=first.account,peer=peer,meta=meta,entries=tuple(entries),inputs=tuple(inputs),
            declarations=tuple(declarations),recorded_at=recorded_at,asset=asset,recapture=recapture)
