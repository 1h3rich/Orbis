/* BSC manual: cotización → permiso exacto → swap. Nunca delega claves ni reintenta envíos. */
(function () {
  "use strict";
  var root = null, client = null, epoch = 0, identity = "", busy = false, walletPending = false;
  var quote = null, plan = null, booking = null, hashes = new Map(), closedResults = new Set(), message = "Conecta y verifica tu wallet en BNB Chain.", resolver=null;
  var balance = null, amountFromBalance = false;
  var orderProofSupported=false,capabilityEpoch=0,orderProofs=new Map();
  function resolutionBusy(){return !!(resolver&&resolver.isBusy());}
  var router = "0xb971ef87ede563556b2ed4b1c0b0019111dd85d2", quoter = "0x78d78e420da98ad378d7799be8f4af69033eb077";
  var factory = "0xdb1d10011ad0ff90774d0c6bb92e5c5c8b4461f7";
  var tokens = {USDT:"0x55d398326f99059ff775485246999027b3197955",WBNB:"0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"};
  var maxUint = (1n << 256n)-1n;
  function address(value) { return typeof value === "string" && /^0x[0-9a-fA-F]{40}$/.test(value) && BigInt(value) > 0n; }
  function word(value) { return typeof value === "string" && /^0x[0-9a-fA-F]{64}$/.test(value); }
  function quantity(value) { return typeof value === "string" && /^0x(?:0|[1-9a-fA-F][0-9a-fA-F]{0,63})$/.test(value); }
  function contractCode(value) { return typeof value === "string" && /^0x(?:[0-9a-fA-F]{2})+$/.test(value) && !/^0x0*$/.test(value); }
  function pad(value) { return BigInt(value).toString(16).padStart(64,"0"); }
  function units(raw, decimals) {
    if (typeof raw !== "string" || raw.length > 100 || !/^\d+(?:\.\d+)?$/.test(raw)) throw new Error("Usa decimales con punto.");
    var parts = raw.split(".");
    if ((parts[1] || "").length > decimals) throw new Error("El importe no cabe sin redondear.");
    var value = BigInt(parts[0]+(parts[1] || "").padEnd(decimals,"0"));
    if (value <= 0n || value >= maxUint) throw new Error("Importe fuera de rango.");
    return value;
  }
  function format(value) { var raw = BigInt(value).toString().padStart(19,"0"); return raw.slice(0,-18)+"."+(raw.slice(-18).replace(/0+$/,"") || "0"); }
  function snapshot() { return client && client.snapshot(); }
  function key(source) { return source ? JSON.stringify([source.accountId,source.wallet,source.chain,source.generation,source.verified]) : ""; }
  function valid(source) { return source && source.provider && source.accountId && address(source.wallet) && source.chain === "0x38" && source.verified; }
  function current(host, issued, sourceKey) { return root === host && root.isConnected && epoch === issued && key(snapshot()) === sourceKey; }
  function path(source) { return "/accounts/"+encodeURIComponent(source.accountId)+"/wallet-swaps"; }
  function bookingPath(source,row){return row.booking_source_kind==='CALL_RESOLUTION'?'/accounts/'+encodeURIComponent(source.accountId)+'/dex-receipts/CALL_RESOLUTION/'+encodeURIComponent(row.booking_source_id):path(source)+'/'+encodeURIComponent(row.id);}
  function output(input) { return input === "USDT" ? "WBNB" : "USDT"; }
  function post(url, body) { return client.request(url,{method:"POST",body:JSON.stringify(body)}); }
  async function read(source, method, params) {
    var timer;
    try {
      return await Promise.race([Promise.resolve().then(function () { return source.provider.request({method:method,params:params}); }),
        new Promise(function (_resolve,reject) { timer = setTimeout(function () { reject(new Error("RPC sin respuesta.")); },15000); })]);
    } finally { clearTimeout(timer); }
  }
  async function checkWallet(source) {
    var chain = await read(source,"eth_chainId",[]), accounts = await read(source,"eth_accounts",[]);
    if (!quantity(chain) || BigInt(chain) !== 56n || !Array.isArray(accounts) || !accounts.some(function (item) { return address(item) && item.toLowerCase() === source.wallet.toLowerCase(); }))
      throw new Error("La cuenta o red cambió.");
  }
  function blockExpiry(block) {
    if (!block || !quantity(block.number) || !quantity(block.timestamp) || !word(block.hash))
      throw new Error("Bloque no verificable.");
    var at = BigInt(block.timestamp)*1000n, now = BigInt(Date.now());
    if (at > now+15000n || now-at >= 120000n) throw new Error("RPC atrasado o bloque no verificable.");
    return Math.min(Number(at)+120000,Number(now)+30000);
  }
  async function confirmBlock(source, block) {
    var again = await read(source,"eth_getBlockByNumber",[block.number,false]);
    var expires = blockExpiry(again);
    if (BigInt(again.number) !== BigInt(block.number) || BigInt(again.timestamp) !== BigInt(block.timestamp) ||
        again.hash.toLowerCase() !== block.hash.toLowerCase()) throw new Error("El bloque cambió · vuelve a consultar.");
    return expires;
  }
  async function checkContracts(source, block, contracts) {
    var checks = await Promise.all([source.wallet].concat(contracts).map(function (contract) {
      return read(source,"eth_getCode",[contract,block.number]);
    }));
    if (typeof checks[0] !== "string" || !/^0x0*$/.test(checks[0]) || checks.slice(1).some(function (value) {
      return !contractCode(value);
    })) throw new Error("Wallet EOA o contratos no verificables.");
  }
  function allowanceData(wallet) { return "0xdd62ed3e"+pad(wallet)+pad(router); }
  async function poolAt(source, input, fee, blockTag) {
    var data = "0x1698ee82"+pad(tokens[input])+pad(tokens[output(input)])+pad(fee);
    var result = await read(source,"eth_call",[{to:factory,data:data},blockTag]);
    if (typeof result !== "string" || !/^0x0{24}[0-9a-fA-F]{40}$/.test(result)) throw new Error("Pool no verificable.");
    var pool = "0x"+result.slice(26).toLowerCase();
    if (!address(pool) || !contractCode(await read(source,"eth_getCode",[pool,blockTag]))) {
      throw new Error("Pool no verificable.");
    }
    return pool;
  }
  function compiled(input, action, amount, minimum, fee, wallet, deadline) {
    if (action !== "SWAP") return "0x095ea7b3"+pad(router)+pad(action === "REVOKE" ? 0n : amount);
    var inner = "04e45aaf"+pad(tokens[input])+pad(tokens[output(input)])+pad(fee)+pad(wallet)+pad(amount)+pad(minimum)+pad(0n);
    return "0x5ae401dc"+pad(deadline)+pad(64n)+pad(1n)+pad(32n)+pad(inner.length/2)+inner.padEnd(Math.ceil(inner.length/64)*64,"0");
  }
  function actionFor(q) { return q.allowance >= q.amount ? "SWAP" : q.allowance === 0n ? "APPROVE" : "REVOKE"; }
  function fresh(q) { return q && Date.now() < q.expires; }
  function show(value, hidden) { return hidden ? "••••" : value; }
  function paint() {
    if (!root || !root.isConnected) return;
    var source = snapshot(), enabled = valid(source);
    if (source && source.hidden && amountFromBalance) {
      root.querySelector('[name="amount"]').value = "";
      amountFromBalance = false; quote = null; plan = null;
    }
    root.hidden = !source || !source.accountId;
    root.querySelectorAll("[data-swap-form] input,[data-swap-form] select").forEach(function (item) { item.disabled = busy || walletPending || resolutionBusy(); });
    root.querySelector("[data-swap-quote]").disabled = busy || walletPending || resolutionBusy() || !enabled;
    root.querySelector("[data-swap-max]").disabled = busy || walletPending || resolutionBusy() || !enabled || source.hidden;
    var observed = root.querySelector("[data-swap-balance]");
    observed.hidden = !fresh(balance);
    observed.textContent = observed.hidden ? "" : "Saldo "+balance.input+" "+show(format(balance.amount),source.hidden)+
      " · Gas disponible "+show(format(balance.gas),source.hidden)+" BNB · Bloque "+balance.block;
    var prepare = root.querySelector("[data-swap-prepare]");
    prepare.hidden = !quote || !!plan;
    prepare.disabled = busy || walletPending || resolutionBusy() || !enabled || !fresh(quote);
    prepare.textContent = quote ? {SWAP:"Preparar swap",APPROVE:"Preparar permiso",REVOKE:"Revocar permiso previo"}[actionFor(quote)] : "Preparar";
    var revoke = root.querySelector("[data-swap-revoke]");
    revoke.hidden = !quote || quote.allowance === 0n || !!plan || actionFor(quote) === "REVOKE";
    revoke.disabled = busy || walletPending || resolutionBusy() || !enabled || !fresh(quote);
    var send = root.querySelector("[data-swap-send]");
    send.hidden = !plan;
    send.disabled = busy || walletPending || resolutionBusy() || !enabled || !plan || plan.status !== "PREPARED" || Date.now() >= Date.parse(plan.expires_at) || !fresh(quote);
    root.querySelector("[data-swap-state]").textContent = message;
    var preview = root.querySelector("[data-swap-preview]"); preview.replaceChildren();
    if (quote) {
      var amount = document.createElement("p");
      amount.textContent = show(format(quote.amount),source.hidden)+" "+quote.input+" → "+show(format(quote.out),source.hidden)+" "+output(quote.input)+
        " · mín. "+show(format(quote.minimum),source.hidden)+" · pool "+(quote.fee/10000).toString().replace(".",",")+" %";
      preview.append(amount);
      var extra = document.createElement("small"); extra.className = "muted";
      extra.textContent = "Bloque "+quote.block+" · "+quote.responses+"/4 pools · "+(fresh(quote) ? "cotización temporal" : "cotización caducada")+
        (plan ? " · gas máximo "+show(format(plan.gas_wei),source.hidden)+" BNB" : "");
      extra.title = "Pool verificado · "+quote.pool;
      preview.append(extra);
      if (plan && plan.simulatedOut !== null) {
        var simulation = document.createElement("p");
        simulation.textContent = "Salida simulada "+show(format(plan.simulatedOut),source.hidden)+" "+output(quote.input);
        preview.append(simulation);
      }
    }
    root.querySelectorAll("[data-swap-private]").forEach(function (item) { item.textContent = source.hidden ? item.orbisMasked : item.orbisVisible; });
    root.querySelectorAll("[data-swap-check]").forEach(function (item) { item.disabled = busy || resolutionBusy() || !enabled; });
    root.querySelectorAll("[data-swap-book-trigger],[data-swap-book-refresh],[data-swap-book-selection]").forEach(function (item) { item.disabled = busy || resolutionBusy() || !enabled; });
    root.querySelectorAll("[data-swap-book-confirm]").forEach(function (item) { item.disabled = busy || resolutionBusy() || !enabled || !booking || !booking.preview.available; });
    root.querySelectorAll("[data-swap-book-private]").forEach(function (item) { item.textContent = source.hidden ? item.orbisMasked : item.orbisVisible; });
    root.querySelectorAll('[data-swap-resolve],[data-swap-hash]').forEach(function(item){item.disabled=busy||resolutionBusy()||!enabled;});
    root.querySelectorAll('[data-swap-order]').forEach(function(item){item.disabled=busy||walletPending||resolutionBusy()||!enabled;});
    root.querySelectorAll('[data-swap-order-saved]').forEach(function(item){var proof=orderProofs.get(item.dataset.swapOrderSaved);if(proof)item.title=window.OrbisDexReceiptOrder.title(proof,source.hidden);});
    if(resolver)resolver.update();
  }
  async function useBalance(event) {
    var source = snapshot();
    if (!valid(source) || source.hidden || busy || walletPending || resolutionBusy()) return;
    var host = root, issued = epoch, sourceKey = key(source), form = event.currentTarget.form;
    var input = form.elements.token_in.value, token = tokens[input];
    if (!token) return;
    if (amountFromBalance) form.elements.amount.value = "";
    amountFromBalance = false; quote = null; plan = null; balance = null;
    busy = true; message = "Consultando saldo…"; paint();
    try {
      await checkWallet(source);
      var block = await read(source,"eth_getBlockByNumber",["latest",false]);
      blockExpiry(block);
      await checkContracts(source,block,[token]);
      var values = await Promise.all([
        read(source,"eth_call",[{to:token,data:"0x313ce567"},block.number]),
        read(source,"eth_call",[{to:token,data:"0x70a08231"+pad(source.wallet)},block.number]),
        read(source,"eth_getBalance",[source.wallet,block.number])
      ]);
      if (!word(values[0]) || BigInt(values[0]) !== 18n || !word(values[1]) || !quantity(values[2]))
        throw new Error("Saldo o decimales no verificables.");
      var expires = await confirmBlock(source,block);
      await checkWallet(source);
      if (!current(host,issued,sourceKey) || snapshot().hidden || form.elements.token_in.value !== input) return;
      if (Date.now() >= expires) throw new Error("El saldo caducó durante la consulta.");
      var amount = BigInt(values[1]), gas = BigInt(values[2]);
      if (amount >= maxUint) throw new Error("Saldo fuera de rango para cotizar.");
      balance = {input:input,amount:amount,gas:gas,expires:expires,block:BigInt(block.number).toString()};
      form.elements.amount.value = amount ? format(amount) : "";
      amountFromBalance = amount > 0n;
      message = !amount ? "Sin saldo de "+input+"." : !gas ? "Sin BNB para gas." : "Cantidad cargada · cotiza para revisar.";
      var selected = balance;
      setTimeout(function () { if (balance === selected && current(host,issued,sourceKey)) paint(); },
        Math.max(0,expires-Date.now()+20));
    } catch (error) { if (current(host,issued,sourceKey)) message = error.message || "No se pudo consultar el saldo."; }
    finally { if (current(host,issued,sourceKey)) { busy = false; paint(); } }
  }
  async function cotizar(event) {
    event.preventDefault();
    var source = snapshot(); if (!valid(source) || busy || walletPending || resolutionBusy()) return;
    var host = root, issued = epoch, sourceKey = key(source), form = event.currentTarget;
    quote = null; plan = null; busy = true; message = "Consultando pools…"; paint();
    try {
      var input = form.elements.token_in.value, amount = units(form.elements.amount.value.trim(),18);
      var bps = units(form.elements.slippage.value.trim(),2), maxGas = units(form.elements.max_gas.value.trim(),18);
      if (bps > 500n || !tokens[input]) throw new Error("Deslizamiento máximo: 5 %.");
      await checkWallet(source);
      var block = await read(source,"eth_getBlockByNumber",["latest",false]);
      blockExpiry(block);
      await checkContracts(source,block,[tokens.USDT,tokens.WBNB,router,quoter,factory]);
      var values = await Promise.all([
        read(source,"eth_call",[{to:tokens[input],data:"0x313ce567"},block.number]),
        read(source,"eth_call",[{to:tokens[output(input)],data:"0x313ce567"},block.number]),
        read(source,"eth_call",[{to:tokens[input],data:"0x70a08231"+pad(source.wallet)},block.number]),
        read(source,"eth_call",[{to:tokens[input],data:allowanceData(source.wallet)},block.number])]);
      if (values.some(function (value) { return !word(value); }) || BigInt(values[0]) !== 18n || BigInt(values[1]) !== 18n || BigInt(values[2]) < amount)
        throw new Error("Decimales o saldo no verificables, o cantidad insuficiente.");
      var results = await Promise.allSettled([100,500,3000,10000].map(async function (fee) {
        var pool = await poolAt(source,input,fee,block.number);
        var data = "0xc6a5026a"+pad(tokens[input])+pad(tokens[output(input)])+pad(amount)+pad(fee)+pad(0n);
        var raw = await read(source,"eth_call",[{to:quoter,data:data},block.number]);
        if (typeof raw !== "string" || !/^0x[0-9a-fA-F]{256}$/.test(raw)) throw new Error("Pool no disponible");
        var out = BigInt("0x"+raw.slice(2,66)), sqrt = BigInt("0x"+raw.slice(66,130)), gas = BigInt("0x"+raw.slice(194));
        if (out <= 0n || sqrt <= 0n || sqrt >= (1n<<160n) || gas <= 0n || gas > 2000000n) throw new Error("Cotización no válida");
        return {fee:fee,out:out,pool:pool};
      }));
      var routes = results.filter(function (item) { return item.status === "fulfilled"; }).map(function (item) { return item.value; });
      if (!routes.length) throw new Error("Sin pool directo disponible para este importe.");
      routes.sort(function (a,b) { return a.out === b.out ? a.fee-b.fee : a.out > b.out ? -1 : 1; });
      var expires = await confirmBlock(source,block);
      await checkWallet(source);
      if (!current(host,issued,sourceKey)) return;
      if (expires <= Date.now()) throw new Error("La cotización caducó durante la consulta.");
      var minimum = routes[0].out*(10000n-bps)/10000n;
      if (minimum <= 0n) throw new Error("Mínimo de salida demasiado pequeño.");
      quote = {input:input,amount:amount,out:routes[0].out,minimum:minimum,fee:routes[0].fee,pool:routes[0].pool,bps:Number(bps),allowance:BigInt(values[3]),
        expires:expires,block:BigInt(block.number).toString(),responses:routes.length,maxGas:maxGas,amountText:form.elements.amount.value.trim(),gasText:form.elements.max_gas.value.trim()};
      message = "Cotización lista · "+{SWAP:"permiso suficiente",APPROVE:"requiere permiso exacto",REVOKE:"revoca primero el permiso insuficiente"}[actionFor(quote)]+".";
      var selected = quote;
      setTimeout(function () { if (quote === selected && current(host,issued,sourceKey)) { message = "Cotización caducada · vuelve a cotizar."; paint(); } },Math.max(0,expires-Date.now()+20));
    } catch (error) { if (current(host,issued,sourceKey)) message = error.message || "No se pudo cotizar."; }
    finally { if (current(host,issued,sourceKey)) { busy = false; paint(); } }
  }
  async function inspect(source, q, action, tx) {
    await checkWallet(source);
    var token = tokens[q.input];
    var values = await Promise.all([
      read(source,"eth_getTransactionCount",[source.wallet,"pending"]),read(source,"eth_getBalance",[source.wallet,"pending"]),
      read(source,"eth_call",[{to:token,data:allowanceData(source.wallet)},"pending"]),
      read(source,"eth_call",[{to:token,data:"0x70a08231"+pad(source.wallet)},"pending"]),
      read(source,"eth_call",[{from:tx.from,to:tx.to,data:tx.data,value:"0x0"},"pending"])
    ]);
    if (!quantity(values[0]) || BigInt(values[0]) !== BigInt(tx.nonce) || !quantity(values[1]) ||
        BigInt(values[1]) < BigInt(tx.gas)*BigInt(tx.gasPrice) || !word(values[2]) || !word(values[3]))
      throw new Error("Nonce, gas o saldo no verificables.");
    var allowance = BigInt(values[2]);
    if (action === "SWAP" && (allowance < q.amount || BigInt(values[3]) < q.amount) || action === "APPROVE" && allowance !== 0n || action === "REVOKE" && allowance === 0n)
      throw new Error("Saldo o permiso cambió · vuelve a cotizar.");
    var simulatedOut = null;
    if (action === "SWAP") {
      if (await poolAt(source,q.input,q.fee,"pending") !== q.pool) throw new Error("El pool cambió · vuelve a cotizar.");
      // multicall devuelve bytes[] con un uint256 de salida.
      var raw = values[4];
      if (typeof raw !== "string" || !/^0x[0-9a-fA-F]{320}$/.test(raw) || BigInt("0x"+raw.slice(2,66)) !== 32n ||
          BigInt("0x"+raw.slice(66,130)) !== 1n || BigInt("0x"+raw.slice(130,194)) !== 32n ||
          BigInt("0x"+raw.slice(194,258)) !== 32n || BigInt("0x"+raw.slice(258)) < q.minimum)
        throw new Error("La simulación no cumple el mínimo de salida.");
      simulatedOut = BigInt("0x"+raw.slice(258));
    } else if (!word(values[4]) || BigInt(values[4]) !== 1n) throw new Error("La simulación del permiso falló.");
    await checkWallet(source);
    return simulatedOut;
  }
  async function prepare(_event, override) {
    var source = snapshot(), q = quote;
    if (!valid(source) || busy || walletPending || resolutionBusy() || !fresh(q) || plan) return;
    var host = root, issued = epoch, sourceKey = key(source), action = override === "REVOKE" ? "REVOKE" : actionFor(q);
    busy = true; message = "Simulando y limitando gas…"; paint();
    try {
      await checkWallet(source);
      var nonce = await read(source,"eth_getTransactionCount",[source.wallet,"pending"]), price = await read(source,"eth_gasPrice",[]);
      if (!quantity(nonce) || !quantity(price) || BigInt(price) <= 0n) throw new Error("Nonce o gas no verificables.");
      var tx = {from:source.wallet,to:action === "SWAP" ? router : tokens[q.input],chainId:"0x38",value:"0x0",type:"0x0",nonce:nonce,gasPrice:price,
        data:compiled(q.input,action,q.amount,q.minimum,q.fee,source.wallet,Math.floor(Date.now()/1000)+120)};
      var estimate = await read(source,"eth_estimateGas",[tx]);
      if (!quantity(estimate)) throw new Error("No se pudo estimar el gas.");
      var gas = (BigInt(estimate)*120n+99n)/100n;
      if (gas <= 0n || gas > 2000000n || gas*BigInt(price) > q.maxGas) throw new Error("Gas por encima de tu límite.");
      tx.gas = "0x"+gas.toString(16);
      var simulatedOut = await inspect(source,q,action,tx);
      if (!current(host,issued,sourceKey) || quote !== q || !fresh(q)) return;
      var prepared = await post(path(source),{request_id:client.uuid(),address:source.wallet,chain_id:"0x38",action:action,token_in:q.input,
        amount:q.amountText,quote_units:q.out.toString(),fee:q.fee,slippage_bps:q.bps,nonce:BigInt(nonce).toString(),gas:gas.toString(),gas_price:BigInt(price).toString(),max_gas_bnb:q.gasText});
      if (!current(host,issued,sourceKey)) return;
      var returned = prepared.transaction, deadline = Date.parse(prepared.expires_at)/1000;
      if (!returned || !word("0x"+prepared.plan_digest) || prepared.action !== action || prepared.token_in !== q.input ||
          prepared.amount_units !== q.amount.toString() || prepared.minimum_units !== q.minimum.toString() || prepared.chain_id !== "0x38" ||
          returned.from.toLowerCase() !== source.wallet.toLowerCase() || returned.to !== tx.to || returned.chainId !== "0x38" || returned.value !== "0x0" || returned.type !== "0x0" ||
          returned.nonce !== tx.nonce || returned.gas !== tx.gas || returned.gasPrice !== tx.gasPrice ||
          !Number.isInteger(deadline) || deadline > Math.floor(Date.now()/1000)+125 || deadline <= Math.floor(Date.now()/1000) ||
          returned.data !== compiled(q.input,action,q.amount,q.minimum,q.fee,source.wallet,deadline)) throw new Error("El plan devuelto no coincide con lo revisado.");
      plan = Object.assign({}, prepared, {simulatedOut:simulatedOut}); message = "Plan listo · confirma en la wallet.";
    } catch (error) { if (current(host,issued,sourceKey)) message = error.message || "No se pudo preparar."; }
    finally { if (current(host,issued,sourceKey)) { busy = false; paint(); history(); } }
  }
  async function send() {
    var source = snapshot(), q = quote, selected = plan;
    if (!valid(source) || busy || walletPending || resolutionBusy() || !selected || selected.status !== "PREPARED" || !fresh(q) || Date.now() >= Date.parse(selected.expires_at)) return;
    var description = selected.action === "SWAP" ? format(q.amount)+" "+q.input+" → mínimo "+format(q.minimum)+" "+output(q.input)+" en tu misma wallet" :
      selected.action === "REVOKE" ? "Revocar el permiso de "+q.input+" a SwapRouter02" : "Autorizar exactamente "+format(q.amount)+" "+q.input+" a SwapRouter02";
    if (!confirm(description+".\n\nBNB Chain · dinero real. Gas máximo: "+format(selected.gas_wei)+" BNB.\nRouter: "+router+
      "\nContrato "+q.input+": "+tokens[q.input]+(selected.action === "SWAP" ? "\nContrato "+output(q.input)+": "+tokens[output(q.input)] : "")+
      "\nRevisa el contrato y los importes en la wallet. No cambies nonce, gas ni datos. Un error de red no autoriza repetir. No activa un bot.")) return;
    var host = root, issued = epoch, sourceKey = key(source), api = client, base = path(source)+"/"+encodeURIComponent(selected.id), attempted = false;
    balance = null; busy = true; message = "Revisando antes de solicitar firma…"; paint();
    try {
      await inspect(source,q,selected.action,selected.transaction);
      if (!current(host,issued,sourceKey) || !fresh(q) || Date.now() >= Date.parse(selected.expires_at)) throw new Error("Plan o sesión caducada.");
      attempted = true;
      var grant = await api.request(base+"/claim",{method:"POST",body:JSON.stringify({address:source.wallet,plan_digest:selected.plan_digest,confirmation:"SOLICITAR_SWAP_WALLET"})});
      if (!grant || grant.id !== selected.id || grant.plan_digest !== selected.plan_digest || grant.claim_granted !== true || grant.status !== "WALLET_REQUESTED")
        throw new Error("Solicitud ya utilizada o permiso sin confirmar; revisa el historial.");
      selected.status = "WALLET_REQUESTED";
      await checkWallet(source);
      var finalNonce = await read(source,"eth_getTransactionCount",[source.wallet,"pending"]);
      if (!current(host,issued,sourceKey) || !fresh(q) || Date.now() >= Date.parse(selected.expires_at) || !quantity(finalNonce) || BigInt(finalNonce) !== BigInt(selected.transaction.nonce))
        throw new Error("Sesión, cotización o nonce cambió · sin reenvío.");
      walletPending = true; message = "Confirma en la wallet · no repitas."; paint();
      var timeout;
      // The live request survives a visual timeout and component remounts.
      // Only its actual settlement releases another financial wallet prompt.
      var emitted = Promise.resolve().then(function () {
        if (!current(host,issued,sourceKey)) throw new Error("La sesión cambió · sin envío.");
        return source.provider.request({method:"eth_sendTransaction",params:[selected.transaction]});
      });
      emitted.finally(function () { walletPending = false; paint(); }).catch(function () {});
      var reported = emitted.then(async function (hash) {
        if (!current(host,issued,sourceKey)) return null;
        if (closedResults.has(selected.id)) return {message:"Resultado ya comprobado · revisa el historial."};
        if (!word(hash)) throw new Error("Hash no verificable · revisa la wallet, sin repetir.");
        await api.request(base+"/hash",{method:"POST",body:JSON.stringify({tx_hash:hash})});
        if (!current(host,issued,sourceKey)) return null;
        selected.status = "HASH_REPORTED";
        if (!hashes.has(selected.id)) hashes.set(selected.id,hash);
        if (plan === selected) { quote = null; plan = null; }
        return {message:"Hash registrado · comprueba el recibo. No contabilizado."};
      },async function (error) {
        if (!current(host,issued,sourceKey)) return null;
        if (closedResults.has(selected.id)) return {message:"Resultado ya comprobado · revisa el historial."};
        if (error && error.code === 4001) {
          await api.request(base+"/rejected",{method:"POST",body:JSON.stringify({confirmation:"WALLET_RECHAZO_4001"})});
          if (!current(host,issued,sourceKey)) return null;
          selected.status = "WALLET_REJECTED";
          if (plan === selected) { quote = null; plan = null; }
          return {message:"Rechazo comunicado por wallet · sin reintento."};
        }
        throw new Error("Resultado desconocido · revisa la actividad de la wallet, sin reenviar.");
      }).catch(function (error) { return {error:error}; });
      // A late reply can only report to its original still-verified scope.
      // It never changes another operation's busy state or repeats a send.
      reported.then(function (result) {
        if (!result || !current(host,issued,sourceKey) || busy) return;
        message = result.error ? result.error.message || "Resultado sin confirmar · revisa el historial." : result.message;
        paint(); history();
      });
      try {
        var result = await Promise.race([reported,new Promise(function (_resolve,reject) {
          timeout = setTimeout(function () { reject(new Error("Wallet sin respuesta · revisa el historial; no repitas.")); },30000);
        })]);
        if (!current(host,issued,sourceKey) || !result) return;
        if (result.error) throw result.error;
        message = result.message;
      } finally { clearTimeout(timeout); }
    } catch (error) {
      if (attempted && selected.status === "PREPARED") selected.status = "WALLET_REQUESTED";
      if (current(host,issued,sourceKey)) message = error.message || "Revisión necesaria · no se reenviará.";
    } finally { if (current(host,issued,sourceKey)) { busy = false; paint(); history(); } }
  }
  async function check(row) {
    var source = snapshot(); if (!valid(source) || busy || resolutionBusy()) return;
    var hash = hashes.get(row.id) || prompt("TxId original de esta acción (0x…). Se consulta sin guardarlo en claro.","");
    if (!word(hash)) return;
    var host = root, issued = epoch, sourceKey = key(source), api = client, base = path(source)+"/"+encodeURIComponent(row.id);
    busy = true; message = "Comprobando recibo en BNB Chain…"; paint();
    try {
      await checkWallet(source);
      if (!current(host,issued,sourceKey)) return;
      if (row.status === "WALLET_REQUESTED") await api.request(base+"/hash",{method:"POST",body:JSON.stringify({tx_hash:hash})});
      if (!current(host,issued,sourceKey)) return;
      var result = await api.request("/accounts/"+encodeURIComponent(source.accountId)+"/dex-call-resolutions/WALLET_SWAP/"+encodeURIComponent(row.id)+"/check-original",
        {method:"POST",body:JSON.stringify({address:source.wallet,tx_hash:hash})});
      if (current(host,issued,sourceKey)) {
        closedResults.add(row.id); quote = null; plan = null;
        message = result.outcome === "EXECUTED" ? "Recibo verificado · vuelve a cotizar." : "Recibo verificado · revisa el resultado.";
      }
    } catch (error) { if (current(host,issued,sourceKey)) message = error.message || "No se pudo comprobar; no se reenvía."; }
    finally { if (current(host,issued,sourceKey)) { busy = false; paint(); history(); } }
  }
  async function history() {
    var source = snapshot(); if (!source || !source.accountId || !root || !root.isConnected) return;
    var host = root, issued = epoch, sourceKey = key(source);
    try {
      var rows = await client.request(path(source),null,true);
      if (!current(host,issued,sourceKey)) return;
      if(resolver)resolver.reconcile(rows);
      if(booking&&rows.some(function(item){return item.id===booking.row.id&&item.booked;})){
        closeBooking();if(resolver)resolver.clear();message='Contabilizado';if(typeof client.onBooked==='function')client.onBooked();}
      var list = root.querySelector("[data-swap-history]"); list.replaceChildren();
      var states = {PREPARED:"Preparado",WALLET_REQUESTED:"Resultado desconocido · revisar wallet",HASH_REPORTED:"Recibo pendiente",WALLET_REJECTED:"Rechazo comunicado",FINALIZED:"Finalizado según RPC",EXECUTED:'Confirmada',CANCELLED:'Cancelada',REVERTED:"Fallido · gas consumido",EXTERNAL_REVIEWED:'Revisada · externa'};
      rows.forEach(function (row) {
        if (row.resolution || row.effective_outcome==='EXTERNAL_REVIEWED' || ["FINALIZED","REVERTED","WALLET_REJECTED"].includes(row.status)) closedResults.add(row.id);
        var group = document.createElement("div"), line = document.createElement("p"); line.className = "muted";
        var prefix = row.id.slice(0,8)+" · "+{SWAP:"Swap",APPROVE:"Permiso",REVOKE:"Revocación"}[row.action]+" · ";
        var stateLabel=states[row.effective_outcome||row.status],observed=row.resolution?row.resolution.receipt:row.receipt;
        var suffix = " "+row.token_in+" · "+stateLabel+(row.expired && row.status === "PREPARED" ? " · caducado" : "")+
          (observed ? " · gas "+format(observed.gas_fee_wei)+" BNB" : "");
        var recorded = row.booked ? " · contabilizado en "+row.target_account_name : "";
        line.dataset.swapPrivate = ""; line.orbisMasked = prefix+"•••• "+row.token_in+" · "+stateLabel+recorded;
        line.orbisVisible = prefix+format(row.amount_units)+suffix+recorded; group.append(line);
        var effect = row.resolution ? row.resolution.effect : row.receipt;
        if (row.action === "SWAP" && ["EXECUTED", "FINALIZED"].includes(row.effective_outcome || row.status) &&
            effect?.amount_out_units) {
          var received = document.createElement("p"), asset = output(row.token_in);
          received.className = "muted";
          received.dataset.swapPrivate = "";
          received.orbisVisible = "Recibido "+format(effect.amount_out_units)+" "+asset+" · Mínimo "+format(row.minimum_units)+" "+asset;
          received.orbisMasked = "Recibido •••• "+asset+" · Mínimo •••• "+asset;
          group.append(received);
        }
        group.dataset.swapAction = row.id;
        if (["WALLET_REQUESTED","HASH_REPORTED"].includes(row.status)&&!row.resolution&&row.effective_outcome!=='EXTERNAL_REVIEWED') {
          var hash=document.createElement('input');hash.type='text';hash.maxLength=66;hash.autocomplete='off';hash.spellcheck=false;hash.placeholder='Hash opcional';
          hash.dataset.swapHash=row.id;hash.setAttribute('aria-label','Hash de transacción '+row.id.slice(0,8));hash.value=hashes.get(row.id)||'';
          hash.addEventListener('input',function(){hashes.set(row.id,hash.value.trim());});group.append(hash);
          var button = document.createElement("button"); button.type = "button"; button.className = "ghost";
          button.dataset.swapCheck = ""; button.textContent = "Comprobar recibo"; button.addEventListener("click",function () { check(row); }); group.append(button);
          var resolve=document.createElement('button');resolve.type='button';resolve.className='ghost';resolve.dataset.swapResolve=row.id;resolve.textContent='Revisar resultado';
          resolve.setAttribute('data-help','Comprueba el envío original con cambios de tarifa o su reemplazo al mismo nonce. Sólo consulta y registra el resultado.');
          resolve.addEventListener('click',function(){resolver.review(row.id,hashes.get(row.id)||'',false,
            {label:row.action==='SWAP'?(row.token_in==='USDT'?'Compra':'Venta'):row.action==='REVOKE'?'Revocación':'Permiso',asset:row.action==='SWAP'?output(row.token_in):null});});group.append(resolve);
          if(window.OrbisExternalReview&&window.OrbisExternalReview.available()){
            var external=document.createElement('button');external.type='button';external.className='ghost';external.textContent='Recibo externo';external.dataset.swapExternal=row.id;
            external.disabled=busy||walletPending||resolutionBusy()||!valid(source);external.addEventListener('click',function(){window.OrbisExternalReview.openSource('WALLET_SWAP',row.id,hashes.get(row.id)||'');});group.append(external);
          }
        }
        if(row.effective_outcome==='EXTERNAL_REVIEWED'&&window.OrbisExternalReview&&window.OrbisExternalReview.available()){
          var savedExternal=document.createElement('button');savedExternal.type='button';savedExternal.className='ghost';savedExternal.textContent='Ver registro';savedExternal.dataset.swapExternalSaved=row.id;
          savedExternal.addEventListener('click',function(){window.OrbisExternalReview.showRecord('WALLET_SWAP',row.id);});group.append(savedExternal);
        }
        if (observed && !row.booked && row.booking_supported === true) {
          var record = document.createElement("button"); record.type = "button"; record.className = "ghost";
          record.dataset.swapBookTrigger = ""; record.textContent = "Revisar registro";
          record.dataset.help = "Revisa operación y gas para registrarlos en la cuenta vinculada a esta wallet. No mueve fondos ni inventa aportaciones.";
          record.addEventListener("click",function () { openBooking(row); }); group.append(record);
        }
        if(orderProofSupported&&row.action==='SWAP'&&observed&&
            (row.effective_outcome==='EXECUTED'||(!row.resolution&&row.status==='FINALIZED'))){
          var proof=orderProofs.get(row.id),control=document.createElement(proof?'span':'button');
          if(proof){control.className='badge';control.textContent='Bloque ✓';control.dataset.swapOrderSaved=row.id;
            control.title=window.OrbisDexReceiptOrder.title(proof,source.hidden);
          }else{control.type='button';control.className='ghost';control.textContent='Comprobar bloque';
            control.dataset.swapOrder=row.id;control.title='Verifica el orden de ejecución en el bloque';
            control.addEventListener('click',function(){checkOrder(row);});}
          group.append(control);
        }
        list.append(group);
      }); paint();
    } catch (_) { if (current(host,issued,sourceKey)) { message = "Historial no disponible · no repitas operaciones pendientes."; paint(); } }
  }
  async function checkOrder(row){
    var source=snapshot();if(busy||walletPending||resolutionBusy()||!valid(source)||!orderProofSupported)return;
    var host=root,issued=epoch,sourceKey=key(source),kind=row.booking_source_kind==='CALL_RESOLUTION'?'CALL_RESOLUTION':'WALLET_SWAP';
    busy=true;closeBooking();message='Comprobando bloque…';paint();
    try{var proof=await window.OrbisDexReceiptOrder.check(client,source,kind,kind==='CALL_RESOLUTION'?row.booking_source_id:row.id);
      if(!current(host,issued,sourceKey))return;orderProofs.set(row.id,proof);message='Bloque verificado';
    }catch(error){if(current(host,issued,sourceKey)){orderProofs.delete(row.id);message=error.status&&error.message?error.message:'Bloque sin confirmar · Reintenta.';}}
    finally{if(current(host,issued,sourceKey)){busy=false;paint();history();}}
  }
  function privateText(parent, visible, masked) {
    var node = document.createElement("span"); node.dataset.swapBookPrivate = "";
    node.orbisVisible = visible; node.orbisMasked = masked || "Importes ocultos";
    node.textContent = snapshot().hidden ? node.orbisMasked : visible; parent.append(node); return node;
  }
  function closeBooking() {
    booking = null;
    if (root && root.isConnected) { var panel = root.querySelector("[data-swap-booking]"); panel.replaceChildren(); panel.hidden = true; }
  }
  function renderBooking() {
    var panel = root.querySelector("[data-swap-booking]"); panel.replaceChildren(); panel.hidden = !booking;
    if (!booking) return;
    var proposed = booking.preview, row = booking.row;
    var title = document.createElement("h4"); title.textContent = "Registro · "+proposed.target_account_name; panel.append(title);
    var summary = document.createElement("p"); summary.style.overflowWrap = "anywhere";
    var trade = proposed.trade;
    privateText(summary, (trade ? (trade.kind === "BUY" ? "Compra " : "Venta ")+trade.quantity+" WBNB · "+trade.gross_amount+" USDT · " : "Sólo comisión · ")+
      "gas "+proposed.gas_amount+" BNB", trade ? "Operación y gas ocultos" : "Gas oculto"); panel.append(summary);
    var form = document.createElement("div"); form.className = "row"; panel.append(form);
    var select = null, gasChecks = [];
    if (proposed.trade_candidates.length) {
      var label = document.createElement("label"); label.className = "field"; label.append("Operación existente");
      label.style.minWidth = "0"; label.style.flex = "1 1 260px";
      select = document.createElement("select"); select.dataset.swapBookSelection = ""; select.setAttribute("aria-label","Operación existente");
      select.style.maxWidth = "100%";
      var blank = document.createElement("option"); blank.value = ""; blank.textContent = "Selecciona el asiento"; select.append(blank);
      proposed.trade_candidates.forEach(function (item) {
        var option = document.createElement("option"); option.value = item.id;
        option.dataset.swapBookPrivate = ""; option.orbisMasked = item.id.slice(0,8)+" · importe oculto";
        option.orbisVisible = item.id.slice(0,8)+" · "+item.quantity+" WBNB · "+item.gross_amount+" USDT";
        option.textContent = snapshot().hidden ? option.orbisMasked : option.orbisVisible;
        option.selected = proposed.trade_event_id === item.id; select.append(option);
      }); label.append(select); form.append(label);
    }
    if (proposed.gas_candidates.length) {
      var fieldset = document.createElement("fieldset"), legend = document.createElement("legend"); legend.textContent = "Gas ya anotado";
      fieldset.style.border = "0"; fieldset.style.padding = "0"; fieldset.style.margin = "8px 0"; legend.className = "muted";
      fieldset.append(legend); panel.append(fieldset);
      proposed.gas_candidates.forEach(function (item) {
        var label = document.createElement("label"), input = document.createElement("input"); input.type = "checkbox"; input.value = item.id;
        label.style.display = "flex"; label.style.alignItems = "center"; label.style.gap = "8px"; label.style.minHeight = "32px";
        input.style.width = "16px"; input.style.height = "16px"; input.style.minHeight = "0"; input.style.margin = "0";
        input.style.flex = "0 0 auto"; input.style.accentColor = "var(--menta-fuerte)";
        input.dataset.swapBookSelection = ""; input.checked = proposed.gas_event_ids.includes(item.id); gasChecks.push(input); label.append(input);
        privateText(label," "+item.native_amount+" BNB · "+(item.asset ? "inventario "+item.quote_currency : "caja BNB")+" · "+item.id.slice(0,8)," Gas oculto · "+item.id.slice(0,8));
        fieldset.append(label);
      });
    }
    proposed.gas_items.forEach(function (item) {
      var line = document.createElement("p"); line.className = "muted";
      privateText(line,"Nuevo gasto: "+(item.quantity || item.amount)+" BNB · "+(item.asset ? "inventario BNB/"+item.quote_currency : "caja BNB"),"Nuevo gasto · importe oculto"); panel.append(line);
    });
    var state = document.createElement("p"); state.className = "muted"; state.textContent = proposed.reason || "Revisión local · sin mover fondos ni añadir capital."; panel.append(state);
    var buttons = document.createElement("div"); buttons.className = "row"; panel.append(buttons);
    if (proposed.trade_candidates.length || proposed.gas_candidates.length) {
      var review = document.createElement("button"); review.type = "button"; review.className = "ghost";
      review.dataset.swapBookRefresh = ""; review.textContent = "Revisar selección";
      review.addEventListener("click",function () { openBooking(row,{trade_event_id:select && select.value || null,
        gas_event_ids:gasChecks.filter(function (item) { return item.checked; }).map(function (item) { return item.value; })}); }); buttons.append(review);
      form.addEventListener("change",function () { if (booking) { booking.preview.available = false; paint(); } });
      panel.querySelectorAll('input[type="checkbox"]').forEach(function (item) { item.addEventListener("change",function () { if (booking) { booking.preview.available = false; paint(); } }); });
    }
    var confirm = document.createElement("button"); confirm.type = "button"; confirm.dataset.swapBookConfirm = ""; confirm.textContent = "Registrar";
    confirm.addEventListener("click",commitBooking); buttons.append(confirm);
    var close = document.createElement("button"); close.type = "button"; close.className = "ghost"; close.textContent = "Cerrar";
    close.addEventListener("click",function () { closeBooking(); paint(); }); buttons.append(close);
    paint();
  }
  async function openBooking(row, selected) {
    var source = snapshot(); if (!valid(source) || busy || resolutionBusy()) return;
    var host = root, issued = epoch, sourceKey = key(source), api = client;
    busy = true; message = "Revisando cartera, operación y gas…"; paint();
    try {
      await checkWallet(source);
      if (!current(host,issued,sourceKey)) return;
      var proposed = await api.request(bookingPath(source,row)+"/booking-preview",{method:"POST",body:JSON.stringify({address:source.wallet,
        trade_event_id:selected && selected.trade_event_id || null,gas_event_ids:selected && selected.gas_event_ids || []})});
      if (!current(host,issued,sourceKey)) return;
      if (proposed.booked) { closeBooking(); message = "Esta acción ya está contabilizada."; return; }
      var expected=row.booking_source_kind==='CALL_RESOLUTION'?'DEX_RECEIPT_BOOKING_V4':'WALLET_SWAP_BOOKING_V1',actionId=row.booking_source_kind==='CALL_RESOLUTION'?row.booking_source_id:row.id;
      if (proposed.contract !== expected || proposed.action_id !== actionId || proposed.context_account_id !== source.accountId ||
          !word("0x"+proposed.revision) || !word("0x"+proposed.preview_digest) || !Array.isArray(proposed.gas_items) ||
          !Array.isArray(proposed.trade_candidates) || !Array.isArray(proposed.gas_candidates)) throw new Error("Vista previa contable no válida.");
      booking = {row:row,preview:proposed}; renderBooking(); message = "Revisa el registro antes de confirmar.";
    } catch (error) { if (current(host,issued,sourceKey)) { closeBooking(); message = error.message || "No se pudo revisar el registro."; } }
    finally { if (current(host,issued,sourceKey)) { busy = false; paint(); } }
  }
  async function commitBooking() {
    var source = snapshot(), selected = booking;
    if (!valid(source) || busy || resolutionBusy() || !selected || !selected.preview.available) return;
    var proposed = selected.preview;
    var host = root, issued = epoch, sourceKey = key(source), api = client;
    busy = true; message = "Registrando operación y gas…"; paint();
    try {
      await checkWallet(source);
      if (!current(host,issued,sourceKey) || booking !== selected) return;
      var result = await api.request(bookingPath(source,selected.row)+"/book",{method:"POST",body:JSON.stringify({address:source.wallet,
        trade_event_id:proposed.trade_event_id,gas_event_ids:proposed.gas_event_ids,expected_revision:proposed.revision,
        preview_digest:proposed.preview_digest,confirmation:selected.row.booking_source_kind==='CALL_RESOLUTION'?'CONTABILIZAR_RECIBO_DEX':'REGISTRAR_ACCION_DEX'})});
      if (!current(host,issued,sourceKey)) return;
      if (!result || result.booked !== true || result.target_account_id !== proposed.target_account_id) throw new Error("Registro no confirmado · actualiza el historial.");
      closeBooking(); message = "Contabilizado · operación y gas, sin duplicar capital.";
      if(resolver)resolver.clear();
      if (typeof api.onBooked === "function") api.onBooked();
    } catch (error) { if (current(host,issued,sourceKey)) message = error.message || "Registro sin confirmar · revisa el historial antes de repetir."; }
    finally { if (current(host,issued,sourceKey)) { busy = false; paint(); history(); } }
  }
  function update() {
    var next = key(snapshot());
    if (next !== identity) { epoch++; identity = next; busy = false; quote = null; plan = null; balance = null;
      root.querySelector('[name="amount"]').value = ""; amountFromBalance = false; closeBooking(); hashes.clear(); closedResults.clear();orderProofs.clear();
      if(resolver)resolver.clear();
      message = valid(snapshot()) ? "Cotiza USDT ↔ WBNB en BNB Chain · ejecución manual." : "Conecta y verifica tu wallet en BNB Chain."; history(); }
    paint();
  }
  function clear() {
    if(resolver)resolver.clear();
    epoch++; identity = ""; busy = false; quote = null; plan = null; balance = null; amountFromBalance = false; closeBooking(); hashes.clear(); closedResults.clear();orderProofs.clear(); message = "Conecta y verifica tu wallet en BNB Chain.";
    if (root && root.isConnected) {
      root.querySelector("[data-swap-preview]").replaceChildren();
      root.querySelector("[data-swap-balance]").textContent = "";
      root.querySelector("[data-swap-balance]").hidden = true;
      root.querySelector("[data-swap-history]").replaceChildren();
      root.querySelector('[name="amount"]').value = "";
    }
  }
  function mount(element, api) {
    root = element; client = api; identity = ""; epoch++; busy = false; quote = null; plan = null; booking = null; balance = null; amountFromBalance = false; hashes.clear(); closedResults.clear();
    orderProofs.clear();orderProofSupported=false;var mounted=++capabilityEpoch;
    root.innerHTML = '<details><summary data-help="Swap manual Uniswap v3 en BNB Chain, USDT Binance-Peg ↔ WBNB. Firmas cada permiso y operación; no activa bots ni importa saldos al libro REAL.">Intercambiar · BNB</summary>'+
      '<form class="row" data-swap-form><label class="field">Vender<select name="token_in"><option value="USDT">USDT → WBNB</option><option value="WBNB">WBNB → USDT</option></select></label>'+
      '<label class="field">Cantidad<input name="amount" type="text" inputmode="decimal" autocomplete="off" placeholder="0.0" required></label>'+
      '<label class="field" data-help="Pérdida máxima frente a esta cotización, sin gas. Entre 0,01 % y 5 %. No garantiza el mejor precio del mercado.">Deslizamiento %<input name="slippage" type="text" inputmode="decimal" value="0.5" required></label>'+
      '<label class="field">Gas máx. BNB<input name="max_gas" type="text" inputmode="decimal" value="0.001" required></label>'+
      '<button type="button" data-swap-max class="ghost" data-help="Carga el saldo del token elegido. El BNB para gas se consulta por separado.">Máximo</button>'+
      '<button type="submit" data-swap-quote class="ghost">Cotizar</button></form>'+
      '<small data-swap-balance class="muted" hidden></small><div data-swap-preview></div><div class="row"><button type="button" data-swap-prepare hidden>Preparar</button>'+
      '<button type="button" class="ghost" data-swap-revoke hidden data-help="Prepara approve(0) para este token y SwapRouter02. Requiere otra firma y gas; cerrar ORBIS no revoca permisos en cadena.">Revocar</button>'+
      '<button type="button" data-swap-send hidden>Confirmar en wallet</button></div><p data-swap-state class="state muted" role="status"></p>'+
      '<details><summary>Historial DEX</summary><button type="button" class="ghost" data-swap-history-refresh>Actualizar</button><div data-swap-history></div></details>'+
      '<section data-swap-booking hidden style="overflow-wrap:anywhere"></section><div data-swap-resolution class="real-panel" hidden></div></details>';
    resolver=window.OrbisReceiptResolution.create({callKind:'WALLET_SWAP'});
    resolver.mount(root.querySelector('[data-swap-resolution]'),{snapshot:client.snapshot,request:client.request,
      onBusyChange:paint,blocked:function(){return busy;},onResolved:history});
    root.querySelector("[data-swap-form]").addEventListener("submit",cotizar);
    root.querySelector("[data-swap-max]").addEventListener("click",useBalance);
    root.querySelector("[data-swap-form]").addEventListener("input",function (event) {
      if (event.target.name === "amount") amountFromBalance = false;
      if (event.target.name === "token_in") {
        balance = null;
        if (amountFromBalance) event.currentTarget.elements.amount.value = "";
        amountFromBalance = false;
      }
      quote = null; plan = null; message = "Parámetros cambiados · vuelve a cotizar."; paint();
    });
    root.querySelector("[data-swap-prepare]").addEventListener("click",prepare);
    root.querySelector("[data-swap-revoke]").addEventListener("click",function (event) { prepare(event,"REVOKE"); });
    root.querySelector("[data-swap-send]").addEventListener("click",send);
    root.querySelector("[data-swap-history-refresh]").addEventListener("click",history);
    update();
    if(window.OrbisDexReceiptOrder)window.OrbisDexReceiptOrder.supported(api).then(function(supported){
      if(mounted!==capabilityEpoch||root!==element||!element.isConnected)return;
      orderProofSupported=supported;if(supported)history();
    });
  }
  window.OrbisWalletSwap = {mount:mount,update:update,clear:clear};
})();
