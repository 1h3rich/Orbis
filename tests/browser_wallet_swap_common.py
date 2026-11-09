"""Synthetic browser provider only; API/SQLCipher are real in owned fixtures.

The EVM receipt integration flows use the separate real-Anvil fixture. This
provider controls browser timing, rejection and UI scopes without user funds.
"""
import json
from eth_account import Account
from eth_account.messages import encode_defunct

PROVIDER = r"""
(() => {
  const listeners = {}, router = '0xb971ef87ede563556b2ed4b1c0b0019111dd85d2';
  const usdt = '0x55d398326f99059ff775485246999027b3197955', wbnb = '0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c';
  const factory = '0xdb1d10011ad0ff90774d0c6bb92e5c5c8b4461f7', pool = '0x'+'34'.repeat(20);
  const word = value => '0x'+BigInt(value).toString(16).padStart(64,'0');
  const wallet = {address:WALLET_ADDRESS,chain:'0x38',nonce:3n,height:0x1234n,nativeWei:10n**18n,sends:[],receipts:{},blocks:{},
    balances:{[usdt]:1000n*10n**18n,[wbnb]:0n},allowance:{[usdt]:0n,[wbnb]:0n},mode:'ok'};
  const observedHeaders = {};
  const header = number => wallet.blocks[number] || (observedHeaders[number] ||= {
    number,hash:word(number),timestamp:'0x'+Math.floor(Date.now()/1000).toString(16)});
  wallet.changeNextBlockRead = false;
  const quote = token => token.toLowerCase() === usdt ? 20000000000000000n : 13210000000000000000n;
  wallet.emit = (name,value) => (listeners[name] || []).forEach(fn => fn(value));
  wallet.provider = {
    request: async ({method,params}) => {
      if (method === 'eth_requestAccounts' || method === 'eth_accounts') return [wallet.address];
      if (method === 'eth_chainId') return wallet.chain;
      if (method === 'eth_blockNumber') return '0x'+wallet.height.toString(16);
      if (method === 'eth_getBlockByNumber') {
        const tag = params[0], numbered = tag !== 'latest' && tag !== 'finalized';
        const block = header(numbered ? tag : '0x'+wallet.height.toString(16));
        if (numbered && wallet.changeNextBlockRead) {
          wallet.changeNextBlockRead = false;
          return {...block, hash:word(0x7777)};
        }
        return block;
      }
      if (method === 'eth_getBalance') return '0x'+wallet.nativeWei.toString(16);
      if (method === 'eth_getCode') return params[0].toLowerCase() === wallet.address.toLowerCase() ||
        params[0].toLowerCase() === pool && wallet.emptyPoolCode ? '0x' : '0x60006000';
      if (method === 'personal_sign') return window.fixtureSign(params[0]);
      if (method === 'eth_getTransactionCount') return '0x'+wallet.nonce.toString(16);
      if (method === 'eth_gasPrice') return '0x3b9aca00';
      if (method === 'eth_estimateGas') return '0x186a0';
      if (method === 'eth_call') {
        const {to,data} = params[0], target = to.toLowerCase();
        if (data === '0x313ce567') return word(18);
        if (data.startsWith('0x70a08231')) return word(wallet.balances[target] || 0n);
        if (data.startsWith('0xdd62ed3e')) return word(wallet.allowance[target] || 0n);
        if (data.startsWith('0x1698ee82') && target === factory) {
          if (wallet.emptyPool) return word(0);
          if (params[1] === 'pending' && wallet.replacePendingPool) return word('0x'+'56'.repeat(20));
          return word(pool);
        }
        if (data.startsWith('0xc6a5026a')) {
          const token = '0x'+data.slice(34,74);
          return word(quote(token))+word(1n<<96n).slice(2)+word(1).slice(2)+word(100000).slice(2);
        }
        if (data.startsWith('0x095ea7b3')) return word(1);
        if (data.startsWith('0x5ae401dc')) {
          const inner = data.slice(330), token = '0x'+inner.slice(32,72);
          return word(32)+word(1).slice(2)+word(32).slice(2)+word(32).slice(2)+word(quote(token)).slice(2);
        }
      }
      if (method === 'eth_sendTransaction') {
        const requested = JSON.parse(JSON.stringify(params[0])); wallet.sends.push(requested);
        if (wallet.mode === 'hold_reject') return await new Promise((_resolve,reject) => {wallet.rejectSend = () => reject({code:4001});});
        if (wallet.mode === 'reject') throw {code:4001};
        wallet.nonce++; wallet.height++; wallet.nativeWei -= 100000n*BigInt(requested.gasPrice);
        const signature = await window.fixtureSignTransaction(requested), {hash} = signature;
        const block = {...header('0x'+wallet.height.toString(16)),transactions:[hash]}; wallet.blocks[block.number] = block;
        const tx = {...requested,...signature,input:requested.data,type:'0x0',blockNumber:block.number,blockHash:block.hash,transactionIndex:'0x0'};
        const log = (token,topic,source,target,value,index) => ({address:token,topics:[topic,word(source),word(target)],data:word(value),
          removed:false,transactionHash:hash,blockNumber:block.number,blockHash:block.hash,transactionIndex:'0x0',logIndex:'0x'+index.toString(16)});
        let logs;
        if (requested.data.startsWith('0x095ea7b3')) {
          const approved = BigInt('0x'+requested.data.slice(74)); wallet.allowance[requested.to.toLowerCase()] = approved;
          logs = [log(requested.to,'0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925',wallet.address,router,approved,1)];
        } else {
          const inner = requested.data.slice(330), input = '0x'+inner.slice(32,72), output = input === usdt ? wbnb : usdt;
          const amount = BigInt('0x'+inner.slice(8+4*64,8+5*64)); wallet.allowance[input] -= amount;
          wallet.balances[input] -= amount; wallet.balances[output] += quote(input);
          const topic = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef';
          logs = [log(input,topic,wallet.address,pool,amount,1),log(output,topic,pool,wallet.address,quote(input),2)];
        }
        const receipt = {transactionHash:hash,from:requested.from,to:requested.to,type:'0x0',blockNumber:block.number,
          blockHash:block.hash,transactionIndex:'0x0',status:'0x1',gasUsed:'0x186a0',effectiveGasPrice:requested.gasPrice,logs,contractAddress:null};
        wallet.receipts[hash] = receipt;
        const published = await fetch('/__test/mined',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({tx,receipt,block,pool:word(pool),nonce:'0x'+wallet.nonce.toString(16),
            allowance:word(wallet.allowance[requested.to.toLowerCase()] || 0n)})});
        if (!published.ok) throw new Error('Fixture could not publish the receipt');
        if (wallet.mode === 'unknown') throw {code:-32603};
        if (wallet.mode === 'hold') return await new Promise(resolve => {wallet.releaseSend = () => resolve(hash);});
        return hash;
      }
      throw new Error('Unexpected fixture RPC '+method);
    },on:(name,fn) => {(listeners[name] ||= []).push(fn);},removeListener:(name,fn) => {listeners[name] = (listeners[name] || []).filter(item => item !== fn);}
  };
  window.testWallet = wallet;
  window.addEventListener('eip6963:requestProvider',() => window.dispatchEvent(new CustomEvent('eip6963:announceProvider',{detail:{
    info:{uuid:'11111111-1111-4111-8111-111111111111',name:'Wallet DEX de prueba'},provider:wallet.provider}})));
})();
"""


def install_provider(page, wallet):
    page.expose_function("fixtureSign", lambda message: "0x"+Account.sign_message(
        encode_defunct(hexstr=message),wallet.key).signature.hex())

    def sign(request):
        transaction = {key: int(request[key], 16) for key in ("chainId", "nonce", "gas", "gasPrice", "value")}
        transaction.update(to=bytes.fromhex(request["to"][2:]), data=request["data"])
        signed = wallet.sign_transaction(transaction)
        return {"hash": "0x"+signed.hash.hex(), **{key: hex(getattr(signed, key)) for key in ("v", "r", "s")}}

    page.expose_function("fixtureSignTransaction", sign)
    page.add_init_script(PROVIDER.replace("WALLET_ADDRESS",json.dumps(wallet.address)))
