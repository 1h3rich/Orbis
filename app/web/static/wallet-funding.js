/* USDT/BSC → MEXC. Una concesión durable, una petición a la wallet, cero reenvíos. */
(function () {
  "use strict";
  var root = null, client = null, epoch = 0, identity = "", plan = null, payload = null;
  var busy = false, walletPending = false, message = "Verifica control y consulta USDT en BNB Chain.", pendingHash = null;
  var suggestedGas = false, historyEpoch = 0, historyFocus;
  var maxUint = (1n << 256n) - 1n;
  function word(value) { return typeof value === "string" && /^0x[0-9a-fA-F]{64}$/.test(value); }
  function quantity(value) { return typeof value === "string" && /^0x[0-9a-fA-F]{1,64}$/.test(value); }
  function address(value) { return typeof value === "string" && /^0x[0-9a-fA-F]{40}$/.test(value) && BigInt(value) > 0n; }
  function units(raw, decimals, allowZero) {
    if (!Number.isInteger(decimals) || decimals < 0 || decimals > 36 || typeof raw !== "string" || raw.length > 100) throw new Error("Importe no válido");
    var match = /^(\d+)(?:\.(\d+))?$/.exec(raw);
    if (!match || (match[2] || "").length > decimals) throw new Error("Usa un importe decimal con punto, sin redondear unidades.");
    var value = BigInt(match[1] + (match[2] || "").padEnd(decimals, "0"));
    if ((!allowZero && value === 0n) || value > maxUint) throw new Error("Importe fuera de rango.");
    return value;
  }
  function format(value, decimals) {
    var raw = value.toString().padStart(decimals + 1, "0");
    return decimals ? raw.slice(0, -decimals) + "." + (raw.slice(-decimals).replace(/0+$/, "") || "0") : raw;
  }
  function mexcDepositLabel(observation) {
    if (!observation) return "";
    var labels = {
      1:"Importe insuficiente",2:"Abono demorado",3:"Demora prolongada",4:"Pendiente",
      6:"En revisión",7:"Rechazado",8:"Devolución indicada",9:"Preabono",
      10:"Inválido",11:"Restringido",12:"Completado · revisar"
    };
    return "MEXC · " + (labels[observation.code] || "Estado " + observation.code + " · revisar");
  }
  function pendingCreditMessage(result, rpc) {
    var label = mexcDepositLabel(result.mexc_deposit);
    return label ? label + " · recibo observado en " + rpc + " · sin abono confirmado." :
      "Recibo observado en " + rpc + " · pendiente de acreditación MEXC.";
  }
  function snapshot() { return client && client.snapshot(); }
  function key(source) {
    return source ? JSON.stringify([source.accountId, source.wallet, source.chain, source.generation,
      source.token && source.token.contract, source.token && source.token.decimals, source.verified]) : "";
  }
  function valid(source) {
    return source && source.provider && source.accountId && address(source.wallet) && source.chain === "0x38" &&
      source.verified && source.token && address(source.token.contract) && Number.isInteger(source.token.decimals);
  }
  function canDiscover(source) {
    return source && source.provider && address(source.wallet) && source.chain === "0x38" && source.verified;
  }
  function current(host, issued, sourceKey) {
    return root === host && root.isConnected && epoch === issued && key(snapshot()) === sourceKey;
  }
  function paint() {
    if (!root || !root.isConnected) return;
    var source = snapshot();
    // El historial durable se puede revisar aunque la wallet esté desconectada.
    root.hidden = !source || !source.accountId;
    root.querySelector("[data-funding-prepare]").disabled = busy || walletPending || !valid(source);
    root.querySelector("[data-funding-estimate]").disabled = busy || walletPending || !valid(source) ||
      source.hidden || !!(plan && plan.status === "WALLET_REQUESTED");
    root.querySelectorAll("[data-funding-form] input").forEach(function (input) { input.disabled = busy || walletPending; });
    root.querySelector("[data-funding-reset]").disabled = busy || walletPending || !!(plan && plan.status === "WALLET_REQUESTED");
    var confirm = root.querySelector("[data-funding-send]");
    confirm.hidden = !plan;
    confirm.disabled = busy || walletPending || !valid(source) || !plan || plan.status !== "PREPARED" || Date.now() >= Date.parse(plan.expires_at);
    root.querySelector("[data-funding-state]").textContent = message;
    root.querySelectorAll("[data-funding-discover], [data-funding-check-bnb]").forEach(function (button) {
      button.disabled = busy || walletPending || !canDiscover(source);
    });
    root.querySelectorAll("[data-funding-roundtrip]").forEach(function (button) {
      button.disabled = busy || walletPending || !source.accountId || Boolean(button.dataset.reading);
    });
    root.querySelectorAll("[data-funding-private]").forEach(function (item) { item.textContent = source.hidden ? item.orbisMasked : item.orbisVisible; });
    var preview = root.querySelector("[data-funding-preview]"); preview.replaceChildren();
    if (plan && source && plan.status === "PREPARED") {
      var amounts = document.createElement("p");
      amounts.textContent = source.hidden ? "Importes ocultos · revisa el plan en la wallet." :
        format(BigInt(plan.amount_units),plan.decimals) + " USDT · mínimo indicado " +
        format(BigInt(plan.minimum_deposit_units),plan.decimals) + " USDT · gas máximo del plan " +
        format(BigInt(plan.gas_wei),18) + " BNB";
      preview.append(amounts);
      var destination = document.createElement("code"); destination.className = "real-wallet-deposit-address";
      destination.textContent = plan.destination; preview.append(destination);
    }
    if (pendingHash && pendingHash.sourceKey === key(source)) {
      var hash = document.createElement("code"); hash.className = "real-wallet-deposit-address";
      hash.textContent = pendingHash.hash; preview.append(hash);
    }
  }
  async function read(provider, method, params) {
    var timer;
    try {
      return await Promise.race([Promise.resolve().then(function () { return provider.request({method:method,params:params}); }),
        new Promise(function (_resolve,reject) { timer = setTimeout(function () { reject(new Error("Lectura RPC sin respuesta.")); },15000); })]);
    } finally { clearTimeout(timer); }
  }
  async function checkWallet(source) {
    var chain = await read(source.provider,"eth_chainId",[]);
    var accounts = await read(source.provider,"eth_accounts",[]);
    if (!quantity(chain) || BigInt(chain) !== 56n || !Array.isArray(accounts) ||
        !accounts.some(function (item) { return address(item) && item.toLowerCase() === source.wallet.toLowerCase(); }))
      throw new Error("La cuenta o red de la wallet cambió.");
  }
  function transferData(target, amount) { return "0xa9059cbb" + target.slice(2).toLowerCase().padStart(64,"0") + amount.toString(16).padStart(64,"0"); }
  async function checkFunds(source, transaction, amount, decimals) {
    await checkWallet(source);
    var owner = source.wallet.slice(2).toLowerCase().padStart(64,"0"), provider = source.provider;
    var values = await Promise.all([
      read(provider,"eth_getCode",[source.wallet,"pending"]),read(provider,"eth_getCode",[transaction.to,"pending"]),
      read(provider,"eth_call",[{to:transaction.to,data:"0x313ce567"},"pending"]),
      read(provider,"eth_call",[{to:transaction.to,data:"0x70a08231"+owner},"pending"]),
      read(provider,"eth_getBalance",[source.wallet,"pending"]),
      read(provider,"eth_getTransactionCount",[source.wallet,"pending"]),
      read(provider,"eth_call",[{from:source.wallet,to:transaction.to,data:transaction.data,value:"0x0"},"pending"])
    ]);
    if (!/^0x0*$/.test(values[0]) || typeof values[1] !== "string" || !/^0x[0-9a-fA-F]+$/.test(values[1]) || /^0x0*$/.test(values[1]) ||
        !word(values[2]) || BigInt(values[2]) !== BigInt(decimals) || !word(values[3]) || BigInt(values[3]) < amount ||
        !quantity(values[4]) || !quantity(values[5]) || BigInt(values[5]) !== BigInt(transaction.nonce) ||
        !word(values[6]) || BigInt(values[6]) !== 1n) throw new Error("Cuenta EOA, token, saldo, nonce o simulación no verificables.");
    if (BigInt(values[4]) < BigInt(transaction.gas) * BigInt(transaction.gasPrice)) throw new Error("BNB insuficiente para el gas máximo del plan.");
  }
  function fundingAmount(form, decimals) {
    var amountText = form.elements.amount.value.trim().replace(",", ".");
    var minimumText = form.elements.deposit_minimum.value.trim().replace(",", ".");
    var amount = units(amountText,decimals);
    var minimum = units(minimumText,decimals,true);
    if (amount < minimum) throw new Error("El importe es inferior al mínimo de depósito indicado para MEXC.");
    return {amountText:amountText,minimumText:minimumText,amount:amount,minimum:minimum};
  }
  async function reviewFunding(source, amount, localClient, isCurrent) {
    var path = "/accounts/"+encodeURIComponent(source.accountId)+"/mexc-erc20-deposit-destination?asset=USDT&chain_id=0x38";
    var destination = await localClient.request(path);
    if (!isCurrent()) return null;
    if (!destination || destination.network !== "BNB_SMART_CHAIN_MAINNET" || destination.asset !== "USDT" ||
        destination.stored !== false || destination.transfer_sent !== false || !address(destination.deposit_address) ||
        !address(destination.token_contract) || destination.token_contract.toLowerCase() !== source.token.contract.toLowerCase())
      throw new Error("USDT, contrato o destino MEXC no coinciden con la wallet en BNB Chain.");
    await checkWallet(source);
    var block = await read(source.provider,"eth_getBlockByNumber",["latest",false]);
    if (!block || !quantity(block.number) || !word(block.hash) || !quantity(block.timestamp) ||
        BigInt(block.timestamp)*1000n > BigInt(Date.now()+15000) || BigInt(Date.now())-BigInt(block.timestamp)*1000n > 120000n)
      throw new Error("El RPC de la wallet está desactualizado.");
    var nonce = await read(source.provider,"eth_getTransactionCount",[source.wallet,"pending"]);
    var price = await read(source.provider,"eth_gasPrice",[]);
    if (!quantity(nonce) || !quantity(price) || BigInt(price) <= 0n || BigInt(price) > 10n**18n)
      throw new Error("Nonce o gas no verificable.");
    var transaction = {from:source.wallet,to:source.token.contract,value:"0x0",chainId:"0x38",nonce:nonce,
      data:transferData(destination.deposit_address,amount),gasPrice:price};
    var estimate = await read(source.provider,"eth_estimateGas",[transaction]);
    if (!quantity(estimate)) throw new Error("No se pudo estimar la comisión.");
    var gas = (BigInt(estimate)*120n+99n)/100n;
    if (gas <= 0n || gas > 2000000n) throw new Error("Gas estimado fuera del rango del plan.");
    transaction.gas = "0x"+gas.toString(16);
    await checkFunds(source,transaction,amount,source.token.decimals);
    return isCurrent() ? {destination:destination,transaction:transaction,fee:gas*BigInt(price)} : null;
  }
  function clearSuggestedGas(form) {
    if (suggestedGas) form.elements.max_gas.value = "";
    suggestedGas = false;
  }
  async function estimateGas() {
    var source = snapshot(), host = root, localClient = client;
    if (busy || walletPending || !valid(source) || source.hidden || plan && plan.status === "WALLET_REQUESTED") return;
    var issued = epoch, sourceKey = key(source), form = host.querySelector("[data-funding-form]");
    function isCurrent() { return current(host,issued,sourceKey) && !snapshot().hidden; }
    busy = true; message = "Estimando gas en BNB Chain…"; paint();
    try {
      var values = fundingAmount(form,source.token.decimals);
      var review = await reviewFunding(source,values.amount,localClient,isCurrent);
      if (!review || !isCurrent()) return;
      form.elements.max_gas.value = format(review.fee,18);
      suggestedGas = true;
      plan = null; payload = null;
      message = "Gas estimado · revisa el límite";
    } catch (error) { if (isCurrent()) message = error.message || "No se pudo estimar el gas."; }
    finally {
      if (current(host,issued,sourceKey) && snapshot().hidden && message === "Estimando gas en BNB Chain…")
        message = "Importes ocultos";
      busy = false; paint();
    }
  }
  async function prepare(event) {
    event.preventDefault();
    var source = snapshot(); if (busy || walletPending || !valid(source)) return;
    var host = root, issued = epoch, sourceKey = key(source), localClient = client, form = event.currentTarget;
    busy = true; message = "Revisando destino, saldo, simulación y comisión…"; paint();
    try {
      var gasText = form.elements.max_gas.value.trim().replace(",", ".");
      var values = fundingAmount(form,source.token.decimals), maxGas = units(gasText,18);
      var amount = values.amount, minimum = values.minimum;
      var base = "/accounts/"+encodeURIComponent(source.accountId);
      var review = await reviewFunding(source,amount,localClient,function () {
        return current(host,issued,sourceKey);
      });
      if (!review) return;
      if (review.fee > maxGas) throw new Error("El gas estimado supera tu límite. Estima el gas de nuevo o ajusta el límite.");
      var transaction = review.transaction, destination = review.destination;
      if (!payload) payload = {request_id:localClient.uuid(),address:source.wallet,chain_id:"0x38",asset:"USDT",amount:values.amountText,
        minimum_deposit_usdt:values.minimumText,decimals:source.token.decimals,nonce:BigInt(transaction.nonce).toString(),
        gas:BigInt(transaction.gas).toString(),gas_price:BigInt(transaction.gasPrice).toString(),max_gas_bnb:gasText};
      var prepared = await localClient.request(base+"/wallet-funding",{method:"POST",body:JSON.stringify(payload)});
      if (!current(host,issued,sourceKey)) return;
      var tx = prepared.transaction;
      if (!tx || prepared.chain_id !== "0x38" || prepared.asset !== "USDT" || !word("0x"+prepared.plan_digest) ||
          prepared.amount_units !== amount.toString() || prepared.minimum_deposit_units !== minimum.toString() ||
          prepared.decimals !== source.token.decimals ||
          tx.from.toLowerCase() !== source.wallet.toLowerCase() || tx.to.toLowerCase() !== source.token.contract.toLowerCase() ||
          tx.data.toLowerCase() !== transaction.data.toLowerCase() || tx.value !== "0x0" || tx.chainId !== "0x38" ||
          BigInt(tx.gas)*BigInt(tx.gasPrice) > maxGas || !address(prepared.destination) ||
          prepared.destination.toLowerCase() !== destination.deposit_address.toLowerCase()) throw new Error("El plan devuelto no coincide con el revisado.");
      plan = prepared;
      message = prepared.status === "PREPARED" && !prepared.expired ? "Plan listo · confirma el envío en la wallet." : "Solicitud ya utilizada o caducada · no se repetirá el envío.";
      setTimeout(function () {
        if (plan === prepared && current(host,issued,sourceKey) && Date.now() >= Date.parse(prepared.expires_at)) {
          message = "Plan caducado · pulsa Nuevo plan antes de preparar otro ingreso."; paint();
        }
      },Math.max(0,Date.parse(prepared.expires_at)-Date.now()+50));
    } catch (error) { if (current(host,issued,sourceKey)) message = error.message || "No se pudo preparar el ingreso."; }
    finally { busy = false; paint(); }
  }
  async function send() {
    var source = snapshot(), prepared = plan;
    if (busy || walletPending || !valid(source) || !prepared || prepared.status !== "PREPARED" || Date.now() >= Date.parse(prepared.expires_at)) return;
    if (!confirm("¿Enviar "+format(BigInt(prepared.amount_units),prepared.decimals)+" USDT a MEXC por BNB Chain?\n\nDestino: "+prepared.destination+
        "\nContrato: "+prepared.transaction.to+"\nGas máximo del plan: "+format(BigInt(prepared.gas_wei),18)+" BNB.\n\nDinero real: revisa red y destino; no se puede deshacer. La wallet pedirá autorización. Si cambias el importe, nonce o comisión allí, queda fuera del plan. Enviado no significa acreditado en MEXC.")) return;
    var host = root, issued = epoch, sourceKey = key(source), localClient = client;
    var base = "/accounts/"+encodeURIComponent(source.accountId)+"/wallet-funding/"+encodeURIComponent(prepared.id), claimed = false, claimAttempted = false;
    busy = true; message = "Verificando el plan antes de pedir autorización…"; paint();
    try {
      await checkFunds(source,prepared.transaction,BigInt(prepared.amount_units),prepared.decimals);
      if (!current(host,issued,sourceKey) || Date.now() >= Date.parse(prepared.expires_at)) throw new Error("El plan o la sesión cambió.");
      claimAttempted = true;
      var grant = await localClient.request(base+"/claim",{method:"POST",body:JSON.stringify({address:source.wallet,
        plan_digest:prepared.plan_digest,confirmation:"SOLICITAR_ENVIO_WALLET"})});
      if (!grant || grant.claim_granted !== true || grant.id !== prepared.id || grant.plan_digest !== prepared.plan_digest || grant.status !== "WALLET_REQUESTED") {
        if (grant && ["WALLET_REQUESTED","HASH_REPORTED","WALLET_REJECTED"].includes(grant.status)) prepared.status = grant.status;
        throw new Error("La solicitud ya se usó o no se pudo validar. Revisa la wallet; no se enviará otra vez.");
      }
      claimed = true; prepared.status = "WALLET_REQUESTED";
      if (!current(host,issued,sourceKey) || Date.now() >= Date.parse(prepared.expires_at)) throw new Error("Sesión cambiada; revisión necesaria, sin reenvío.");
      await checkWallet(source);
      var finalNonce = await read(source.provider,"eth_getTransactionCount",[source.wallet,"pending"]);
      if (!quantity(finalNonce) || BigInt(finalNonce) !== BigInt(prepared.transaction.nonce) ||
          !current(host,issued,sourceKey) || Date.now() >= Date.parse(prepared.expires_at))
        throw new Error("La wallet, nonce, plan o sesión cambió. Revisa la solicitud, sin reenviar.");
      walletPending = true; message = "Confirma en la wallet · no repitas ni recargues mientras responde."; paint();
      var hash;
      try {
        // Sin timeout/reintento: perder esta respuesta no prueba que no se emitió.
        hash = await source.provider.request({method:"eth_sendTransaction",params:[prepared.transaction]});
      } catch (error) {
        if (error && error.code === 4001) {
          await localClient.request(base+"/rejected",{method:"POST",body:JSON.stringify({confirmation:"WALLET_RECHAZO_4001"})});
          prepared.status = "WALLET_REJECTED";
          throw new Error("La wallet comunicó rechazo. No se reintentará este envío.");
        }
        throw new Error("Resultado de wallet desconocido. Revisa su actividad; no vuelvas a enviar automáticamente.");
      }
      if (!word(hash)) throw new Error("La wallet no devolvió un hash verificable. Revisa su actividad, sin repetir.");
      if (current(host,issued,sourceKey)) pendingHash = {sourceKey:sourceKey,fundingId:prepared.id,hash:hash};
      await localClient.request(base+"/hash",{method:"POST",body:JSON.stringify({tx_hash:hash})});
      prepared.status = "HASH_REPORTED"; plan = null; payload = null;
      if (current(host,issued,sourceKey)) message = "Hash registrado · pendiente de confirmar en cadena y de acreditación en MEXC. No contabilizado.";
    } catch (error) {
      if (current(host,issued,sourceKey)) {
        if (claimAttempted && !claimed) {
          prepared.status = "WALLET_REQUESTED";
          message = "Permiso no confirmado en esta pestaña · consulta el historial y la wallet. No se solicitará otro envío.";
        } else message = error.message || (claimed ? "Resultado pendiente de revisión; no se reenviará." : "No se solicitó envío a la wallet.");
      }
    } finally { walletPending = false; busy = false; paint(); history(); }
  }
  async function history() {
    var source = snapshot(); if (!source || !source.accountId || !root || !root.isConnected) return;
    var host = root, issued = epoch, sourceKey = key(source), localClient = client, reading = ++historyEpoch;
    var focus = historyFocus.request();
    try {
      var rows = await localClient.request("/accounts/"+encodeURIComponent(source.accountId)+"/wallet-funding"+
        (focus ? focus.query : ""),null,true);
      if (!current(host,issued,sourceKey) || reading !== historyEpoch) return;
      var list = root.querySelector("[data-funding-history]"); list.replaceChildren();
      var labels = {PREPARED:"Preparado",WALLET_REQUESTED:"Resultado desconocido · revisa wallet",HASH_REPORTED:"Hash recibido · pendiente de MEXC",WALLET_REJECTED:"Rechazo comunicado por wallet"};
      rows.forEach(function (row) {
        var group = document.createElement("div");
        group.dataset.fundingItem = group.dataset.transferId = row.id;
        group.tabIndex = -1; list.append(group);
        var item = document.createElement("p"); item.className = "muted";
        var closed = row.chain_close === "CANCELLED" ? "Cancelado en BNB" : row.chain_close === "REVERTED" ? "Fallido en BNB" : null;
        var deposit = row.mexc_deposit;
        var state = row.booked ? "Acreditado · contabilizado" : row.exchange_credited ? "Acreditado · sin contabilizar" :
          closed || mexcDepositLabel(deposit) || labels[row.status] || "Revisar";
        var prefix = row.id.slice(0,8)+" · "+row.created_at.slice(0,16).replace("T"," ")+" UTC · ";
        var rpcLabel = (row.chain_source || (deposit && deposit.chain_source)) === "ORBIS_RPC_OBSERVED" ? " · RPC de ORBIS" : "";
        var suffix = " · "+state+rpcLabel+(row.expired && row.status === "PREPARED" ? " · caducado" : "");
        if (deposit) suffix += " · consulta " + deposit.observed_at.slice(0,16).replace("T"," ") + " UTC";
        item.dataset.fundingPrivate = ""; item.orbisMasked = prefix+"Importe oculto"+suffix;
        item.orbisVisible = prefix+format(BigInt(row.amount_units),row.decimals)+" USDT"+suffix+
          (row.gas_fee_wei ? " · gas "+format(BigInt(row.gas_fee_wei),18)+" BNB" : "");
        item.textContent = source.hidden ? item.orbisMasked : item.orbisVisible; group.append(item);
        if (row.status === "WALLET_REQUESTED" || row.status === "HASH_REPORTED" && !row.exchange_credited) {
          var button = document.createElement("button"); button.type = "button"; button.className = "ghost";
          button.textContent = "Registrar txId"; button.dataset.fundingHash = row.id; button.disabled = busy || walletPending;
          button.dataset.help = "Referencia de la actividad de tu wallet. No prueba finalización ni acreditación en MEXC. Sólo se guarda una huella del hash.";
          var discover = document.createElement("button"); discover.type = "button"; discover.className = "ghost";
          discover.textContent = "Buscar en BNB"; discover.dataset.fundingDiscover = row.id;
          discover.dataset.help = "Busca el nonce original en bloques finalizados. Requiere verificar el control de la wallet. El abono en MEXC se comprueba aparte.";
          group.append(discover,button);
        }
        if (row.status === "HASH_REPORTED" && !row.booked) {
          var check = document.createElement("button"); check.type = "button"; check.className = "ghost";
          check.textContent = "Comprobar ingreso"; check.dataset.fundingCheck = row.id;
          check.disabled = busy || walletPending || !source.provider || source.chain !== "0x38";
          check.dataset.help = "Comprueba el recibo con el RPC de tu wallet y el abono con View de la cuenta original. Puedes usar una clave renovada.";
          group.append(check);
          if (!row.exchange_credited) {
            var bnb = document.createElement("button"); bnb.type = "button"; bnb.className = "ghost";
            bnb.textContent = "Comprobar en BNB"; bnb.dataset.fundingCheckBnb = row.id;
            bnb.dataset.help = "ORBIS consulta el txId que conserva en esta vista. Si falta, consulta el nonce de tu wallet en BNB. Una clave View de la cuenta original confirma el abono, aunque se haya renovado.";
            group.append(bnb);
          }
        }
        if (row.exchange_credited && !row.booked) {
          var book = document.createElement("button"); book.type = "button"; book.className = "ghost";
          book.textContent = "Contabilizar"; book.dataset.fundingBook = row.id; book.disabled = busy || walletPending;
          group.append(book);
        }
        if (row.status === "HASH_REPORTED") {
          var audit = document.createElement("button"); audit.type = "button"; audit.className = "ghost";
          audit.textContent = "Ver recorrido"; audit.dataset.fundingRoundtrip = row.id;
          audit.dataset.help = "Consulta el archivo local. Si falta el vínculo con una retirada antigua, verifica la wallet de origen.";
          var report = document.createElement("div"); report.dataset.fundingRoundtripReport = "";
          report.setAttribute("role","status");
          group.append(audit,report);
        }
      });
      paint();
      historyFocus.finish(focus);
    } catch (_) {
      if (current(host,issued,sourceKey) && reading === historyEpoch) {
        root.querySelector("[data-funding-history]").textContent = "Historial no verificable. Desbloquea la bóveda o revisa la evidencia del ingreso.";
        historyFocus.finish(focus);
      }
    }
  }
  function focusHistory(id) {
    return root && root.isConnected && !root.hidden && historyFocus.open(id, history);
  }
  async function recoverHash(event) {
    var button = event.target.closest("button[data-funding-hash]"); if (!button || busy || walletPending) return;
    var source = snapshot(), localClient = client, host = root, issued = epoch, sourceKey = key(source);
    var hash = prompt("Pega el txId de la actividad de tu wallet. Se registra sólo como referencia, no como depósito confirmado.");
    if (hash === null) return;
    hash = hash.trim(); if (!word(hash)) { message = "TxId EVM no válido."; paint(); return; }
    busy = true; paint();
    try {
      await localClient.request("/accounts/"+encodeURIComponent(source.accountId)+"/wallet-funding/"+encodeURIComponent(button.dataset.fundingHash)+"/hash",
        {method:"POST",body:JSON.stringify({tx_hash:hash})});
      if (!current(host,issued,sourceKey)) return;
      pendingHash = {sourceKey:sourceKey,fundingId:button.dataset.fundingHash,hash:hash}; plan = null; payload = null;
      message = "Referencia registrada · confirma la cadena y la acreditación en MEXC antes de contabilizar.";
    } catch (_) { if (current(host,issued,sourceKey)) message = "No se pudo registrar: comprueba el txId original y la cuenta REAL."; }
    finally { busy = false; paint(); history(); }
  }
  async function discoverHash(event) {
    var button = event.target.closest("button[data-funding-discover]");
    var source = snapshot();
    if (!button || busy || walletPending || !canDiscover(source)) return;
    var host = root, issued = epoch, sourceKey = key(source), localClient = client, id = button.dataset.fundingDiscover;
    busy = true; message = "Buscando la transacción finalizada en BNB…"; paint();
    try {
      await checkWallet(source);
      if (!current(host,issued,sourceKey)) return;
      var result = await localClient.request("/accounts/"+encodeURIComponent(source.accountId)+"/wallet-funding/"+encodeURIComponent(id)+"/discover",
        {method:"POST",body:JSON.stringify({address:source.wallet})});
      if (!current(host,issued,sourceKey)) return;
      if (!result || result.id !== id) throw new Error("Resultado de búsqueda no verificable.");
      if (result.status === "CHAIN_CLOSED" && ["CANCELLED","REVERTED"].includes(result.chain_close)) {
        if (pendingHash && pendingHash.fundingId === id) pendingHash = null;
        message = result.chain_close === "CANCELLED" ? "Cancelación finalizada en BNB · sin abono." : "Envío fallido en BNB · sin abono.";
      } else if (result.status === "HASH_REPORTED" && word(result.discovered_tx_hash)) {
        pendingHash = {sourceKey:sourceKey,fundingId:id,hash:result.discovered_tx_hash};
        message = "TxId recuperado · comprueba la acreditación en MEXC.";
      } else throw new Error("Resultado de búsqueda no verificable.");
      plan = null; payload = null;
    } catch (error) {
      if (current(host,issued,sourceKey)) message = error.message || "Búsqueda BNB no verificable.";
    } finally { busy = false; paint(); history(); }
  }
  async function checkCredit(event) {
    var button = event.target.closest("button[data-funding-check]"); if (!button || busy || walletPending) return;
    var source = snapshot(), localClient = client, host = root, issued = epoch, sourceKey = key(source);
    if (!source.provider || source.chain !== "0x38") return;
    var hash = pendingHash && pendingHash.sourceKey === sourceKey && pendingHash.fundingId === button.dataset.fundingCheck ?
      pendingHash.hash : prompt("TxId original del ingreso en BNB Chain. Se consulta, no se vuelve a enviar.");
    if (hash === null) return;
    hash = hash.trim(); if (!word(hash)) { message = "TxId EVM no válido."; paint(); return; }
    busy = true; message = "Consultando recibo, bloque finalizado y depósito MEXC…"; paint();
    try {
      var chain = await read(source.provider,"eth_chainId",[]);
      if (!quantity(chain) || BigInt(chain) !== 56n) throw new Error("Selecciona BNB Chain en la wallet.");
      var pair = await Promise.all([read(source.provider,"eth_getTransactionByHash",[hash]),read(source.provider,"eth_getTransactionReceipt",[hash])]);
      var tx = pair[0], receipt = pair[1];
      if (!tx || !receipt || !quantity(tx.blockNumber)) throw new Error("Pendiente de minado o recibo no disponible; no se reenviará.");
      var blocks = await Promise.all([read(source.provider,"eth_getBlockByNumber",[tx.blockNumber,false]),
        read(source.provider,"eth_getBlockByNumber",["finalized",false])]);
      var afterChain = await read(source.provider,"eth_chainId",[]);
      if (!quantity(afterChain) || BigInt(afterChain) !== 56n || !current(host,issued,sourceKey)) throw new Error("La sesión o red cambió. Vuelve a consultar.");
      var result = await localClient.request("/accounts/"+encodeURIComponent(source.accountId)+"/wallet-funding/"+encodeURIComponent(button.dataset.fundingCheck)+"/check",
        {method:"POST",body:JSON.stringify({evidence:{chain_id:"0x38",tx_hash:hash,transaction:tx,receipt:receipt,block:blocks[0],finalized:blocks[1]}})});
      if (!current(host,issued,sourceKey)) return;
      if (result.exchange_credited && pendingHash && pendingHash.fundingId === button.dataset.fundingCheck) pendingHash = null;
      message = result.exchange_credited ? "Acreditado por MEXC · recibo observado en RPC wallet · "+(result.booked ? "ya contabilizado." : "pendiente de contabilizar.") :
        pendingCreditMessage(result,"RPC wallet");
    } catch (error) { if (current(host,issued,sourceKey)) message = error.message || "Ingreso no verificable; no se reenviará."; }
    finally { busy = false; paint(); history(); }
  }
  async function checkCreditBnb(event) {
    var button = event.target.closest("button[data-funding-check-bnb]"), source = snapshot();
    if (!button || busy || walletPending || !canDiscover(source)) return;
    var localClient = client, host = root, issued = epoch, sourceKey = key(source), id = button.dataset.fundingCheckBnb;
    var body = {address:source.wallet};
    if (pendingHash && pendingHash.sourceKey === sourceKey && pendingHash.fundingId === id) body.tx_hash = pendingHash.hash;
    busy = true; message = "Consultando BNB y MEXC…"; paint();
    try {
      var result = await localClient.request("/accounts/"+encodeURIComponent(source.accountId)+"/wallet-funding/"+encodeURIComponent(id)+"/check-bnb",
        {method:"POST",body:JSON.stringify(body)});
      if (!current(host,issued,sourceKey)) return;
      if (result.exchange_credited || result.status === "CHAIN_CLOSED") {
        if (pendingHash && pendingHash.fundingId === id) pendingHash = null;
        plan = null; payload = null;
      }
      if (result.status === "CHAIN_CLOSED") {
        message = result.chain_close === "CANCELLED" ? "Cancelación finalizada en BNB · sin abono." : "Envío fallido en BNB · sin abono.";
      } else if (result.exchange_credited) {
        message = "Acreditado por MEXC · "+(result.booked ? "ya contabilizado." : "pendiente de contabilizar.");
      } else if (result.status === "PENDING_MEXC") {
        message = pendingCreditMessage(result,"RPC de ORBIS");
      } else if (result.status === "BNB_PENDING") {
        message = "Recibo pendiente en BNB · vuelve a consultar.";
      } else throw new Error("Ingreso sin confirmar.");
    } catch (error) { if (current(host,issued,sourceKey)) message = error.message || "Ingreso no verificable; no se reenviará."; }
    finally { busy = false; paint(); history(); }
  }
  async function showRoundtrip(event) {
    var button = event.target.closest("button[data-funding-roundtrip]"), source = snapshot();
    if (!button || busy || walletPending || !source || !source.accountId || button.dataset.reading) return;
    var host = root, issued = epoch, sourceKey = key(source), localClient = client;
    var report = button.parentElement.querySelector("[data-funding-roundtrip-report]");
    button.dataset.reading = "1";
    button.disabled = true;
    report.textContent = "Consultando el recorrido…";
    try {
      var path = "/accounts/"+encodeURIComponent(source.accountId)+"/wallet-funding/"+
        encodeURIComponent(button.dataset.fundingRoundtrip)+"/roundtrip";
      var result = await localClient.request(path,null,true);
      if (!report.isConnected || !current(host,issued,sourceKey)) return;
      if (result.withdrawal === "DESTINATION_UNVERIFIED" && canDiscover(source)) {
        try {
          result = await localClient.request(path,{method:"POST",body:JSON.stringify({address:source.wallet})},true);
        } catch (error) {
          if (error.status !== 409) throw error;
        }
        if (!report.isConnected || !current(host,issued,sourceKey)) return;
      }
      window.OrbisMexcJourneyReport.render(report, result, snapshot().hidden, function (step) {
        if (report.isConnected && current(host,issued,sourceKey)) localClient.openMexcJourneyStep(step);
      });
      paint();
    } catch (error) {
      if (report.isConnected && current(host,issued,sourceKey)) report.textContent = error.message || "Recorrido no verificable.";
    } finally {
      delete button.dataset.reading;
      if (button.isConnected) paint();
    }
  }
  async function showBooking(event) {
    var button = event.target.closest("button[data-funding-book]"); if (!button || busy || walletPending) return;
    var source = snapshot(), host = root, issued = epoch, sourceKey = key(source), localClient = client;
    busy = true; paint();
    try {
      var options = await localClient.request("/accounts/"+encodeURIComponent(source.accountId)+"/wallet-funding/"+encodeURIComponent(button.dataset.fundingBook)+"/booking-options",
        {method:"POST",body:JSON.stringify({address:source.verified && source.chain === "0x38" && address(source.wallet) ? source.wallet : null})},true);
      if (!current(host,issued,sourceKey)) return;
      if (options.booked) { history(); return; }
      var form = document.createElement("form"); form.className = "row"; form.dataset.fundingBooking = button.dataset.fundingBook;
      var label = document.createElement("label"); label.className = "field"; label.textContent = "Origen contable";
      var select = document.createElement("select"); select.name = "origin"; select.required = true;
      var option = document.createElement("option"); option.value = ""; option.textContent = "Seleccionar…"; select.append(option);
      options.events.forEach(function (row) {
        var existing = document.createElement("option"); existing.value = row.id;
        existing.textContent = (row.kind === "CASH_TRANSFER_IN" ? "Traslado" : "Aportación")+" · "+row.occurred_at.slice(0,16).replace("T"," ")+" · "+row.id.slice(0,8);
        select.append(existing);
      });
      if (options.internal) {
        option = document.createElement("option"); option.value = "NEW_INTERNAL"; option.textContent = "Desde "+options.internal.name+" · traslado USDT"; select.append(option);
        form.orbisSource = options.internal;
      }
      if (options.external_available) {
        option = document.createElement("option"); option.value = "NEW_CONTRIBUTION"; option.textContent = "Fondos nuevos · fuera de ORBIS"; select.append(option);
      }
      label.append(select); form.append(label);
      if (options.reason) { var reason = document.createElement("small"); reason.className = "muted"; reason.textContent = options.reason; form.append(reason); }
      if (options.internal) {
        var preview = document.createElement("small"); preview.className = "muted"; preview.dataset.fundingPrivate = "";
        preview.orbisMasked = "Traslado USDT · importes ocultos";
        preview.orbisVisible = options.internal.available_at_time+" − "+options.internal.amount+" = "+options.internal.remaining_at_time+" USDT";
        preview.dataset.help = "Saldo del libro wallet en la fecha del abono MEXC. Se registra un traslado entre cuentas, no una aportación ni beneficio. El gas BNB se revisa aparte.";
        form.append(preview);
      }
      var submit = document.createElement("button"); submit.type = "submit"; submit.textContent = "Registrar ingreso";
      form.append(submit); button.parentElement.querySelectorAll("form").forEach(function (old) { old.remove(); }); button.parentElement.append(form);
    } catch (error) { if (current(host,issued,sourceKey)) message = error.message || "No se pueden revisar los asientos."; }
    finally { busy = false; paint(); }
  }
  async function bookCredit(event) {
    var form = event.target.closest("form[data-funding-booking]"); if (!form) return;
    event.preventDefault(); if (busy || walletPending) return;
    var selected = form.elements.origin.value; if (!selected) return;
    var source = snapshot(), host = root, issued = epoch, sourceKey = key(source), localClient = client;
    var isNew = selected === "NEW_CONTRIBUTION", internal = selected === "NEW_INTERNAL", origin = form.orbisSource;
    if ((isNew || internal) && !address(source.wallet)) { message = "Conecta la wallet de origen para registrar el ingreso."; paint(); return; }
    if (internal && !origin) return;
    if (!confirm(internal ? "Registrar el traslado de "+origin.amount+" USDT desde "+origin.name+" a MEXC. Descuenta de la cuenta wallet y abona MEXC en un solo registro, sin crear una aportación ni beneficio. No vuelve a enviar fondos. El gas BNB se revisa aparte. ¿Continuar?" :
      isNew ? "Registrar como aportación nueva USDT: confirma que estos fondos NO estaban registrados en ninguna cuenta de ORBIS. Si ya estaban registrados, enlaza el traslado existente. El ingreso no es beneficio. El gas BNB queda documentado aparte, sin conversión ni ajuste automático de tu saldo BNB." :
      "Enlazar el depósito con el asiento seleccionado, sin añadir saldo ni repetir el movimiento. El gas BNB queda documentado aparte; revisa su asiento si llevas ese saldo en ORBIS. ¿Continuar?")) return;
    busy = true; paint();
    try {
      var result = await localClient.request("/accounts/"+encodeURIComponent(source.accountId)+"/wallet-funding/"+encodeURIComponent(form.dataset.fundingBooking)+"/book",
        {method:"POST",body:JSON.stringify({mode:internal ? "NEW_INTERNAL" : isNew ? "NEW_CONTRIBUTION" : "EXISTING_EVENT",address:isNew || internal ? source.wallet : null,
          event_id:isNew || internal ? null : selected,source_account_id:internal ? origin.source_account_id : null,
          source_revision:internal ? origin.source_revision : null,confirmation:"REGISTRAR_INGRESO_ACREDITADO"})});
      if (!current(host,issued,sourceKey)) return;
      message = result.booked ? "Ingreso contabilizado · sin beneficio ficticio ni duplicar saldo." : "No contabilizado.";
    } catch (error) {
      if (current(host,issued,sourceKey)) message = error.message && !/failed to fetch|networkerror|load failed/i.test(error.message) ? error.message :
        "Respuesta no confirmada · consulta el historial. No repitas el ingreso: el asiento puede estar guardado.";
    }
    finally {
      busy = false; paint(); await history();
      if (current(host,issued,sourceKey) && localClient.onBooked) localClient.onBooked();
    }
  }
  function update() {
    if (!root || !root.isConnected) return;
    var next = key(snapshot());
    if (next !== identity) {
      identity = next; epoch += 1; plan = null; payload = null; historyFocus.reset();
      var form = root.querySelector("[data-funding-form]");
      form.elements.deposit_minimum.value = "";
      clearSuggestedGas(form);
      root.querySelectorAll("[data-funding-roundtrip-report]").forEach(function (report) { report.replaceChildren(); });
      if (pendingHash && pendingHash.sourceKey !== next) pendingHash = null;
      message = "Verifica control y consulta USDT en BNB Chain.";
    }
    paint();
  }
  function mount(element, api) {
    root = element; client = api; identity = ""; epoch += 1; plan = null; payload = null; suggestedGas = false;
    root.innerHTML = '<details><summary data-help="Transferencia USDT/BEP-20 desde tu wallet a un destino MEXC verificado. Tú autorizas en la extensión; ORBIS no guarda claves ni acredita el depósito por recibir un hash.">Enviar a MEXC · BNB</summary>'+
      '<form class="row" data-funding-form><label class="field">Importe · USDT<input name="amount" inputmode="decimal" autocomplete="off" required></label>'+
      '<label class="field" data-help="Introduce el mínimo que muestra tu pantalla de depósito para USDT en BNB Chain.">Mínimo en MEXC · USDT<input name="deposit_minimum" inputmode="decimal" autocomplete="off" required></label>'+
      '<label class="field" data-help="Límite máximo de comisión del plan en BNB. La wallet conserva la decisión final.">Gas máximo · BNB<input name="max_gas" inputmode="decimal" autocomplete="off" required></label>'+
      '<button type="button" class="ghost" data-funding-estimate data-help="Calcula un límite con la simulación y el precio actual del gas en BNB Chain.">Estimar gas</button>'+
      '<button type="submit" data-funding-prepare>Preparar ingreso</button><button type="button" class="ghost" data-funding-reset>Nuevo plan</button>'+
      '<a href="https://www.mexc.com/assets/deposit/USDT" target="_blank" rel="noopener noreferrer">Consultar en MEXC ↗</a></form><div data-funding-state class="state muted" role="status"></div>'+
      '<div data-funding-preview></div><button type="button" data-funding-send hidden>Confirmar en wallet</button>'+
      '<button type="button" class="ghost" data-funding-refresh>Consultar envíos</button><div data-funding-history></div></details>';
    historyFocus = window.OrbisTransferHistoryFocus.create(root.querySelector("details"),
      root.querySelector("[data-funding-history]"));
    root.querySelector("[data-funding-form]").addEventListener("submit",prepare);
    root.querySelector("[data-funding-form]").addEventListener("input",function (event) {
      if (event.target === this.elements.max_gas) suggestedGas = false;
      else clearSuggestedGas(this);
      epoch += 1; plan = null; payload = null; message = "Prepara un plan con estos importes."; paint();
    });
    root.querySelector("[data-funding-estimate]").addEventListener("click",estimateGas);
    root.querySelector("[data-funding-send]").addEventListener("click",send);
    root.querySelector("[data-funding-reset]").addEventListener("click",function () {
      if (busy || walletPending || plan && plan.status === "WALLET_REQUESTED") return;
      epoch += 1; plan = null; payload = null; message = "Prepara otro plan; no se solicitará gasto hasta confirmar."; paint();
    });
    root.querySelector("[data-funding-history]").addEventListener("click",recoverHash);
    root.querySelector("[data-funding-history]").addEventListener("click",discoverHash);
    root.querySelector("[data-funding-history]").addEventListener("click",checkCredit);
    root.querySelector("[data-funding-history]").addEventListener("click",checkCreditBnb);
    root.querySelector("[data-funding-history]").addEventListener("click",showRoundtrip);
    root.querySelector("[data-funding-history]").addEventListener("click",showBooking);
    root.querySelector("[data-funding-history]").addEventListener("submit",bookCredit);
    root.querySelector("[data-funding-refresh]").addEventListener("click",history);
    root.querySelector("details").addEventListener("toggle",function () { if (this.open) history(); });
    update();
  }
  window.addEventListener("beforeunload",function (event) { if (walletPending) { event.preventDefault(); event.returnValue = ""; } });
  window.OrbisWalletFunding = {mount:mount,update:update,focusHistory:focusHistory};
})();
