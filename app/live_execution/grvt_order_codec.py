"""Codec EIP-712 común. Elegir una cadena no concede permiso de ejecución."""

from eth_account.messages import encode_typed_data


ORDER_TYPES={
    'OrderWithBuilderFee':[
        {'name':name,'type':kind} for name,kind in (
            ('subAccountID','uint64'),('isMarket','bool'),('timeInForce','uint8'),
            ('postOnly','bool'),('reduceOnly','bool'),('legs','OrderLeg[]'),
            ('builder','address'),('builderFee','uint32'),('nonce','uint32'),('expiration','int64'))],
    'OrderLeg':[{'name':name,'type':kind} for name,kind in (
        ('assetID','uint256'),('contractSize','uint64'),('limitPrice','uint64'),('isBuyingContract','bool'))]}
ORDER_WITHOUT_BUILDER_TYPES={
    'Order':[field for field in ORDER_TYPES['OrderWithBuilderFee'] if field['name'] not in ('builder','builderFee')],
    'OrderLeg':ORDER_TYPES['OrderLeg']}


def order_signable(message: dict,*,chain_id: int):
    if type(chain_id) is not int or chain_id not in (325,326):
        raise ValueError('Cadena GRVT no admitida')
    if message['builder'].lower()=='0x'+'0'*40:
        types=ORDER_WITHOUT_BUILDER_TYPES
        message={key:value for key,value in message.items() if key not in ('builder','builderFee')}
    else: types=ORDER_TYPES
    return encode_typed_data({'name':'GRVT Exchange','version':'0','chainId':chain_id},types,message)
