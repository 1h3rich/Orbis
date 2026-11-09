"""Contratos MAINNET con firma real y claves públicas de prueba."""

from decimal import Decimal
from eth_account import Account
from eth_account.messages import encode_typed_data

from tests.test_grvt_testnet_signer import KEY,SIGNER

NOW=1_780_000_000_000_000_000


def test_mainnet_long_signs_a_fok_entry_and_reduce_only_native_exits_in_chain_325():
    from app.live_execution.grvt_mainnet_bracket import (
        MainnetInstrument,MainnetBracketDraft,sign_mainnet_bracket,verify_mainnet_bracket)
    instrument=MainnetInstrument('BTC_USDT_Perp',123456789,3,Decimal('0.01'),Decimal('0.001'),Decimal('10'))
    draft=MainnetBracketDraft('123456','BTC_USDT_Perp','LONG',Decimal('0.5'),Decimal('100'),
        Decimal('95'),Decimal('110'),Decimal('100'),Decimal('100'),3600,25,25)
    orders=sign_mainnet_bracket(draft,instrument,parent_client_order_id=str(2**63+100),
        nonces=(11,12,13),private_key=KEY,expected_signer=SIGNER,now_ns=NOW)
    assert len(orders)==3 and len({row['metadata']['client_order_id'] for row in orders})==3
    assert orders[0]['time_in_force']=='FILL_OR_KILL' and orders[0]['reduce_only'] is False
    assert [row['metadata']['trigger']['trigger_type'] for row in orders[1:]]==['STOP_LOSS','TAKE_PROFIT']
    assert all(row['reduce_only'] is True and row['time_in_force']=='GOOD_TILL_TIME' for row in orders[1:])
    assert all(row['signature']['chain_id']=='325' for row in orders)
    assert orders[0]['legs'][0]['is_buying_asset'] is True
    assert all(row['legs'][0]['is_buying_asset'] is False for row in orders[1:])
    assert verify_mainnet_bracket(orders,draft,instrument,issued_at_ns=NOW,now_ns=NOW)==SIGNER
    # Estructura independiente, contrastada con el SDK oficial de GRVT.
    types={'Order':[{'name':name,'type':kind} for name,kind in (
        ('subAccountID','uint64'),('isMarket','bool'),('timeInForce','uint8'),('postOnly','bool'),
        ('reduceOnly','bool'),('legs','OrderLeg[]'),('nonce','uint32'),('expiration','int64'))],
        'OrderLeg':[{'name':name,'type':kind} for name,kind in (
        ('assetID','uint256'),('contractSize','uint64'),('limitPrice','uint64'),('isBuyingContract','bool'))]}
    message={'subAccountID':123456,'isMarket':False,'timeInForce':4,'postOnly':False,'reduceOnly':False,
        'legs':[{'assetID':123456789,'contractSize':500,'limitPrice':100_000_000_000,'isBuyingContract':True}],
        'nonce':11,'expiration':NOW+20_000_000_000}
    signature=orders[0]['signature']; vrs=(signature['v'],int(signature['r'],16),int(signature['s'],16))
    mainnet=encode_typed_data({'name':'GRVT Exchange','version':'0','chainId':325},types,message)
    testnet=encode_typed_data({'name':'GRVT Exchange','version':'0','chainId':326},types,message)
    assert Account.recover_message(mainnet,vrs=vrs)==SIGNER
    assert Account.recover_message(testnet,vrs=vrs)!=SIGNER
    assert KEY.hex() not in str(orders)


def test_a_hundred_usdt_provider_minimum_can_be_met_on_all_three_legs_with_an_explicit_larger_cap():
    from app.live_execution.grvt_mainnet_bracket import MainnetInstrument,MainnetBracketDraft,sign_mainnet_bracket
    instrument=MainnetInstrument('BTC_USDT_Perp',123456789,3,Decimal('0.01'),Decimal('0.001'),Decimal('100'))
    draft=MainnetBracketDraft('123456','BTC_USDT_Perp','LONG',Decimal('1.5'),Decimal('100'),
        Decimal('95'),Decimal('110'),Decimal('150'),Decimal('200'))
    orders=sign_mainnet_bracket(draft,instrument,parent_client_order_id=str(2**63+101),nonces=(21,22,23),
        private_key=KEY,expected_signer=SIGNER,now_ns=NOW)
    assert all(Decimal(row['legs'][0]['size'])*Decimal(row['legs'][0]['limit_price'])>=100 for row in orders)
    assert Decimal(orders[0]['legs'][0]['size'])*Decimal(orders[0]['legs'][0]['limit_price'])==150
