/* Wallet EVM local: lectura, vínculo EOA e ingreso BEP-20 confirmado por wallet. */
(function () {
  "use strict";

  var providers = new Map();
  var root = null;
  var selected = "";
  var active = null;
  var accounts = [];
  var account = null;
  var chain = null;
  var networkAction = null;
  var bnbNetworkMissing = false;
  var networkStatus = "";
  var BNB_CHAIN = "0x38";
  var balance = null;
  var balanceRaw = null;
  var balanceBlock = null;
  var bookSnapshot = null;
  var proofRequest = null;
  var realAccountId = null;
  var proofBusy = false;
  var proofGeneration = 0;
  var proofStatus = "Sin verificar control en esta sesión.";
  var proofVerified = false;
  var linkMatch = null;
  var walletLinks = [];
  var linksStatus = "";
  var linksGeneration = 0;
  var linksBusy = false;
  var tokenContract = null;
  var tokenObservation = null;
  var onMexcContext = null;
  var tokenStatus = "Introduce el contrato del token en esta red.";
  var tokenGeneration = 0;
  var tokenBusy = false;
  var dexQuoteGeneration = 0;
  var dexQuoteBusy = false;
  var dexQuoteStatus = "Cotización sin consultar.";
  var dexQuoteResult = null;
  var depositDestination = null;
  var depositStatus = "Consulta un destino MEXC sólo cuando necesites revisarlo.";
  var depositGeneration = 0;
  var depositBusy = false;
  var txGeneration = 0;
  var txBusy = false;
  var txStatus = "Sin comprobar envíos en esta sesión.";
  var hidden = false;
  var discoveryReady = false;
  var status = "Elige una wallet para consultar su saldo nativo.";
  var generation = 0;
  var chains = {
    "0x1": { network: "Ethereum", unit: "ETH" },
    "0xa4b1": { network: "Arbitrum", unit: "ETH" },
    "0x2105": { network: "Base", unit: "ETH" },
    "0x89": { network: "Polygon", unit: "POL" },
    "0x38": { network: "BNB Chain", unit: "BNB" }
  };
  var transferTopic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef";
  // QuoterV2 Ethereum, despliegue oficial Uniswap v3; nunca es destino de envío.
  var uniswapQuoterV2 = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e";
  var uniswapSwapRouter02 = "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45";
  var uniswapExactInputSingle = "0xc6a5026a";

  function safeAddress(value) { return typeof value === "string" && /^0x[0-9a-fA-F]{40}$/.test(value); }
  function safeHex(value) { return typeof value === "string" && value.length <= 66 && /^0x[0-9a-fA-F]+$/.test(value); }
  function safeWord(value) { return typeof value === "string" && /^0x[0-9a-fA-F]{64}$/.test(value); }
  function topicAddress(value) {
    return safeWord(value) && /^0{24}$/i.test(value.slice(2, 26)) ? "0x" + value.slice(26) : null;
  }
  function shortAddress(value) { return value.slice(0, 6) + "…" + value.slice(-4); }
  function formatUnits(value, decimals) {
    var digits = value.toString();
    if (decimals === 0) return digits;
    var whole = digits.padStart(decimals + 1, "0");
    var fraction = whole.slice(-decimals).replace(/0+$/, "");
    return whole.slice(0, -decimals) + "." + (fraction || "0");
  }
  function signedUnits(value, decimals) {
    return (value < 0n ? "−" : "") + formatUnits(value < 0n ? -value : value, decimals);
  }
  function timedWalletRead(provider, request) {
    var timer;
    return Promise.race([
      Promise.resolve().then(function () { return provider.request(request); }),
      new Promise(function (_resolve, reject) {
        timer = window.setTimeout(function () { reject(new Error("Wallet read timeout")); }, 15000);
      })
    ]).finally(function () { window.clearTimeout(timer); });
  }
  function blockIsRecent(blockTimeMs) {
    return Number.isSafeInteger(blockTimeMs) && blockTimeMs <= Date.now() + 15000 &&
      Date.now() - blockTimeMs <= 120000;
  }
  async function recentBlockTime(provider, block) {
    if (!safeHex(block) || BigInt(block) === 0n) throw new Error("INVALID_WALLET_BLOCK");
    var header = await timedWalletRead(provider, {
      method: "eth_getBlockByNumber", params: [block, false]
    });
    if (!header || !safeHex(header.number) || BigInt(header.number) !== BigInt(block) ||
        !safeHex(header.timestamp) || !safeWord(header.hash))
      throw new Error("INVALID_WALLET_BLOCK");
    var blockTimeMs = Number(BigInt(header.timestamp)) * 1000;
    if (!blockIsRecent(blockTimeMs)) throw new Error("STALE_WALLET_BLOCK");
    return blockTimeMs;
  }
  async function finalizedEthereumReceipt(provider, tx, receipt) {
    if (!safeWord(tx.blockHash) || !safeWord(receipt.blockHash) ||
        tx.blockHash.toLowerCase() !== receipt.blockHash.toLowerCase()) return "INCONSISTENT";
    try {
      var finalized = await timedWalletRead(provider, {
        method: "eth_getBlockByNumber", params: ["finalized", false]
      });
      if (!finalized || !safeHex(finalized.number) || !safeWord(finalized.hash))
        return "UNVERIFIABLE";
      var txHeight = BigInt(tx.blockNumber), finalHeight = BigInt(finalized.number);
      if (finalHeight < txHeight) return "PENDING";
      var canonical = await timedWalletRead(provider, {
        method: "eth_getBlockByNumber", params: [tx.blockNumber, false]
      });
      if (!canonical || !safeHex(canonical.number) || !safeWord(canonical.hash))
        return "UNVERIFIABLE";
      if (BigInt(canonical.number) !== txHeight ||
          canonical.hash.toLowerCase() !== tx.blockHash.toLowerCase() ||
          (finalHeight === txHeight && finalized.hash.toLowerCase() !== tx.blockHash.toLowerCase()))
        return "INCONSISTENT";
      return "FINALIZED";
    } catch (_) {
      return "UNVERIFIABLE";
    }
  }
  function receiptFinalityStatus(result) {
    return result === "PENDING" ?
      "Recibo minado; bloque aún no finalizado según el RPC. No se cruzó con MEXC." :
      result === "INCONSISTENT" ?
      "Recibo y cadena canónica no coinciden según el RPC. No se cruzó con MEXC." :
      "Finalización o cadena canónica no verificable por RPC. No se cruzó con MEXC.";
  }
  function decimalUnits(raw, decimals) {
    if (typeof raw !== "string" || raw.length > 150) return null;
    var match = /^([+-]?)(\d+)(?:\.(\d+))?(?:[eE]([+-]?\d{1,3}))?$/.exec(raw);
    if (!match) return null;
    var exponent = Number(match[4] || 0);
    if (!Number.isInteger(exponent) || Math.abs(exponent) > 100) return null;
    var digits = match[2] + (match[3] || "");
    var shift = decimals + exponent - (match[3] || "").length;
    if (shift < -150 || shift > 150) return null;
    if (shift < 0) {
      if (!/^0*$/.test(digits.slice(shift))) return null;
      digits = digits.slice(0, shift) || "0";
    } else digits += "0".repeat(shift);
    return BigInt(digits) * (match[1] === "-" ? -1n : 1n);
  }
  function bookNativeUnits(unit) {
    if (!bookSnapshot || !bookSnapshot.cash || !Array.isArray(bookSnapshot.positions)) return null;
    var values = [bookSnapshot.cash[unit] || "0"];
    bookSnapshot.positions.forEach(function (item) {
      if (item.asset === unit) values.push(item.quantity);
    });
    var total = 0n;
    for (var value of values) {
      var parsed = decimalUnits(value, 18);
      if (parsed === null) return null;
      total += parsed;
    }
    return total;
  }
  function setBookSnapshot(snapshot) {
    bookSnapshot = snapshot && typeof snapshot === "object" ? snapshot : null;
    if (!bookSnapshot) clearProof();
    paint();
  }
  function clearProof() {
    proofGeneration += 1;
    proofBusy = false;
    proofStatus = "Sin verificar control en esta sesión.";
    proofVerified = false;
    linkMatch = null;
  }
  function validAccounts(values) {
    if (!Array.isArray(values) || !values.length || values.length > 50 || !values.every(safeAddress)) return null;
    return Array.from(new Set(values));
  }
  function clearToken(clearContract) {
    clearDestination();
    clearDexQuote();
    tokenGeneration += 1;
    tokenObservation = null;
    tokenBusy = false;
    if (clearContract) tokenContract = null;
    tokenStatus = tokenContract ? "Token pendiente de actualizar." : "Introduce el contrato del token en esta red.";
    var input = root && root.querySelector("#real-wallet-token-contract");
    if (input && clearContract) input.value = "";
  }
  function clearDestination() {
    depositGeneration += 1;
    depositDestination = null;
    depositBusy = false;
    depositStatus = "Consulta un destino MEXC sólo cuando necesites revisarlo.";
  }
  function clearDexQuote() {
    dexQuoteGeneration += 1;
    dexQuoteBusy = false;
    dexQuoteResult = null;
    dexQuoteStatus = "Cotización sin consultar.";
  }
  function clearTx() {
    txGeneration += 1;
    txBusy = false;
    txStatus = "Sin comprobar envíos en esta sesión.";
    var input = root && root.querySelector("#real-wallet-tx-hash");
    if (input) input.value = "";
  }
  function state(text) {
    status = text;
    paint();
  }
  function resetNetworkAction() {
    networkAction = null;
    bnbNetworkMissing = false;
    networkStatus = "";
  }
  function stopProvider(keepDexSource) {
    resetNetworkAction();
    clearProof();
    clearTx();
    if (active && typeof active.removeListener === "function") {
      active.removeListener("accountsChanged", onAccountsChanged);
      active.removeListener("chainChanged", onChainChanged);
      active.removeListener("disconnect", onDisconnect);
    }
    active = null;
    accounts = [];
    account = null;
    chain = null;
    balance = null;
    balanceRaw = null;
    balanceBlock = null;
    clearToken(true);
    generation += 1;
    if (window.OrbisWalletSwap) window.OrbisWalletSwap.clear();
    if (window.OrbisWalletDexBot) window.OrbisWalletDexBot.clear(keepDexSource);
    if (window.OrbisEconomicReview) window.OrbisEconomicReview.clear();
    if (window.OrbisExternalReview) window.OrbisExternalReview.clear();
  }
  function clear() {
    stopProvider();
    bookSnapshot = null;
    walletLinks = [];
    linksGeneration += 1;
    linksStatus = "";
    state("Desconectada.");
  }
  function providerForSelection() {
    if (selected === "legacy") return window.ethereum && typeof window.ethereum.request === "function" ? window.ethereum : null;
    var item = providers.get(selected);
    return item ? item.provider : null;
  }
  function announce(event) {
    var detail = event && event.detail;
    if (!detail || !detail.info || !detail.provider || typeof detail.provider.request !== "function") return;
    var uuid = detail.info.uuid;
    if (typeof uuid !== "string" || !/^[0-9a-z-]{8,64}$/i.test(uuid)) return;
    if (!providers.has(uuid) && providers.size < 20) {
      providers.set(uuid, {
        name: typeof detail.info.name === "string" ? detail.info.name.slice(0, 80) : "Wallet EVM",
        provider: detail.provider
      });
      if (!selected) selected = uuid;
      paint();
    }
  }
  window.addEventListener("eip6963:announceProvider", announce);
  window.addEventListener("hashchange", function () {
    if (window.location.hash !== "#/real") {
      stopProvider();
      status = "Elige una wallet para consultar su saldo nativo.";
      root = null;
    }
  });

  function paint() {
    if (!root || !root.isConnected) return;
    var picker = root.querySelector("#real-wallet-provider");
    var output = root.querySelector("#real-wallet-state");
    if (!picker || !output) return;
    var options = Array.from(providers.entries());
    if (!options.length && discoveryReady && window.ethereum && typeof window.ethereum.request === "function") {
      options = [["legacy", { name: "Wallet EVM del navegador" }]];
      if (!selected) selected = "legacy";
    }
    picker.replaceChildren();
    if (!options.length) {
      var empty = document.createElement("option");
      empty.textContent = "Sin wallet EVM detectada";
      empty.value = "";
      picker.append(empty);
    } else {
      options.forEach(function (entry) {
        var option = document.createElement("option");
        option.value = entry[0];
        option.textContent = entry[1].name;
        picker.append(option);
      });
      if (!options.some(function (entry) { return entry[0] === selected; })) selected = options[0][0];
      picker.value = selected;
    }
    picker.disabled = !!active || !options.length;
    root.querySelector("#real-wallet-connect").hidden = !!active;
    root.querySelector("#real-wallet-connect").disabled = !options.length;
    var refreshButton = root.querySelector("#real-wallet-refresh");
    refreshButton.hidden = !active;
    refreshButton.disabled = !!networkAction;
    root.querySelector("#real-wallet-disconnect").hidden = !active;
    var networkButton = root.querySelector("#real-wallet-bnb-chain");
    networkButton.hidden = !active || !account || (chain === BNB_CHAIN && !networkAction);
    networkButton.disabled = !!networkAction;
    var networkLabel = bnbNetworkMissing ? "Añadir BNB Chain" : "Cambiar a BNB Chain";
    if (networkButton.textContent !== networkLabel) networkButton.textContent = networkLabel;
    var networkOutput = root.querySelector("#real-wallet-network-state");
    var message = networkButton.hidden ? "" : networkStatus;
    if (networkOutput.textContent !== message) networkOutput.textContent = message;
    networkOutput.hidden = !message;
    var accountWrap = root.querySelector("#real-wallet-account-wrap");
    var accountPicker = root.querySelector("#real-wallet-account");
    accountWrap.hidden = !active || accounts.length < 2;
    accountPicker.replaceChildren();
    accounts.forEach(function (address, index) {
      var option = document.createElement("option");
      option.value = String(index);
      option.textContent = "Cuenta " + (index + 1) + " · " + shortAddress(address);
      accountPicker.append(option);
    });
    if (account) accountPicker.value = String(accounts.indexOf(account));
    output.replaceChildren();
    var note = document.createElement("p");
    note.textContent = status;
    output.append(note);
    if (account) {
      var details = document.createElement("p");
      var network = chains[chain];
      var shownBalance = balance === null ? "—" : hidden ? "••••" : balance;
      details.textContent = shortAddress(account) + " · " +
        (network ? network.network + " · " + shownBalance + " " + network.unit :
          (chain ? "Red EVM " + chain + " · " + shownBalance + " wei" : "Red no disponible")) +
        (balanceBlock === null ? "" : " · bloque " + balanceBlock);
      output.append(details);
    }
    var proofButton = root.querySelector("#real-wallet-proof");
    var proofOutput = root.querySelector("#real-wallet-proof-state");
    proofButton.hidden = !active || !chain || !proofRequest || !realAccountId;
    proofButton.disabled = proofBusy || !account || !!networkAction;
    proofOutput.textContent = proofStatus;
    var linkForm = root.querySelector("#real-wallet-link-form");
    linkForm.hidden = !proofVerified || !linkMatch || linkMatch.status !== "UNLINKED" ||
      !account || !chain || !realAccountId;
    root.querySelector("#real-wallet-link-save").disabled = linksBusy;
    root.querySelector("#real-wallet-links-state").textContent = linksStatus;
    var linksList = root.querySelector("#real-wallet-links-list");
    linksList.replaceChildren();
    walletLinks.forEach(function (item) {
      var row = document.createElement("div");
      row.className = "row";
      var label = document.createElement("span");
      label.textContent = item.alias + " · " + item.chain_id + " · control EOA: " +
        new Date(item.verified_at).toLocaleDateString() + " · el vínculo no autoriza gasto";
      var button = document.createElement("button");
      button.type = "button";
      button.className = "ghost";
      button.textContent = "Desvincular";
      button.dataset.linkId = item.id;
      button.disabled = linksBusy;
      row.append(label, button);
      linksList.append(row);
    });
    var comparePanel = root.querySelector("#real-wallet-compare");
    var compareState = root.querySelector("#real-wallet-compare-state");
    var native = chains[chain];
    comparePanel.hidden = !active || !native || balanceRaw === null || !bookSnapshot;
    if (!comparePanel.hidden) {
      var book = bookNativeUnits(native.unit);
      compareState.textContent = book === null ? "Libro REAL no comparable a unidades mínimas de esta red." :
        hidden ? "Importes ocultos · comparación local no conciliada." :
        "Wallet " + signedUnits(balanceRaw, 18) + " " + native.unit +
        " · libro " + signedUnits(book, 18) + " " + native.unit +
        " · diferencia " + signedUnits(balanceRaw - book, 18) + " " + native.unit +
        (balanceRaw === book ? " · misma cantidad, no conciliada." : " · cantidades distintas.");
    }
    var tokenPanel = root.querySelector("#real-wallet-token-panel");
    var tokenOutput = root.querySelector("#real-wallet-token-state");
    tokenPanel.hidden = !active;
    root.querySelector("#real-wallet-token-contract").disabled = tokenBusy;
    root.querySelector("#real-wallet-token-submit").disabled = tokenBusy || !chain || !!networkAction;
    tokenOutput.replaceChildren();
    var tokenNote = document.createElement("p");
    tokenNote.textContent = tokenStatus;
    tokenOutput.append(tokenNote);
    if (tokenObservation) {
      var tokenDetails = document.createElement("p");
      var shownToken = hidden ? "••••" : tokenObservation.amount;
      tokenDetails.textContent = shortAddress(tokenObservation.contract) + " · " + shownToken +
        (tokenObservation.decimals === null ? " unidades mínimas" : " unidades") +
        " · bloque " + tokenObservation.block;
      tokenOutput.append(tokenDetails);
    }
    var dexPanel = root.querySelector("#real-wallet-dex-panel");
    dexPanel.hidden = !active || chain !== "0x1" || !tokenObservation ||
      tokenObservation.decimals === null;
    root.querySelector("#real-wallet-dex-submit").disabled = dexQuoteBusy || dexPanel.hidden;
    var dexOutput = root.querySelector("#real-wallet-dex-state");
    dexOutput.textContent = dexQuoteResult && dexQuoteResult.expiresAt > Date.now() ?
      hidden ? "Uniswap v3 · importe oculto · cotización y permiso de SwapRouter02 temporales, no ejecutable." :
      "Uniswap v3 · pool " + dexQuoteResult.poolFee + " · bloque " +
      dexQuoteResult.block + " · salida " +
      dexQuoteResult.amountOut + " unidades de " + dexQuoteResult.tokenOut +
      " · mínimo orientativo " +
      dexQuoteResult.minimumOut + " (0,5 %). " +
      (dexQuoteResult.auto ? "Mayor salida bruta entre " + dexQuoteResult.responded +
        "/4 respuestas; " : "") +
      (dexQuoteResult.routerAllowance === null ?
        "Permiso ERC-20 de SwapRouter02 no verificable. " :
        "Permiso ERC-20 de SwapRouter02: " + dexQuoteResult.routerAllowance +
        " unidades (" + (dexQuoteResult.routerAllowanceEnough ? "cubre" : "no cubre") +
        " esta cantidad). ") +
      "Solo para SwapRouter02; no comprueba Permit2 ni gas. No ejecutable." :
      dexQuoteStatus;
    var depositPanel = root.querySelector("#real-wallet-deposit-panel");
    var depositOutput = root.querySelector("#real-wallet-deposit-state");
    depositPanel.hidden = !active || chain !== "0x1" || !tokenObservation;
    root.querySelector("#real-wallet-deposit-submit").disabled = depositBusy || depositPanel.hidden || !realAccountId || !realAccountId();
    depositOutput.replaceChildren();
    var depositNote = document.createElement("p");
    depositNote.textContent = depositStatus;
    depositOutput.append(depositNote);
    if (depositDestination && depositDestination.expiresAt > Date.now() &&
        realAccountId && depositDestination.accountId === realAccountId() &&
        tokenObservation && depositDestination.contract === tokenObservation.contract.toLowerCase()) {
      var destination = document.createElement("code");
      destination.className = "real-wallet-deposit-address";
      destination.textContent = depositDestination.address;
      depositOutput.append(destination);
      var warning = document.createElement("p");
      warning.className = "amber";
      warning.textContent = "Confirma dirección, red Ethereum y token en MEXC antes de enviar. Orbis no transfiere fondos ni guarda este destino.";
      depositOutput.append(warning);
    }
    var txPanel = root.querySelector("#real-wallet-tx-panel");
    txPanel.hidden = !active;
    root.querySelector("#real-wallet-tx-direction").disabled = txBusy;
    root.querySelector("#real-wallet-tx-hash").disabled = txBusy;
    root.querySelector("#real-wallet-tx-submit").disabled = txBusy || !account || !chain || !proofRequest || !realAccountId;
    root.querySelector("#real-wallet-tx-state").textContent = txStatus;
    var bnbToken = root.querySelector("#real-wallet-usdt-bnb");
    if (bnbToken) { bnbToken.hidden = chain !== BNB_CHAIN; bnbToken.disabled = tokenBusy || !!networkAction || !proofRequest || !realAccountId || !realAccountId(); }
    if (window.OrbisWalletFunding) window.OrbisWalletFunding.update();
    if (window.OrbisMexcWithdrawal) window.OrbisMexcWithdrawal.update();
    if (window.OrbisWalletSwap) window.OrbisWalletSwap.update();
    if (window.OrbisWalletDexBot) window.OrbisWalletDexBot.update();
    if (window.OrbisEconomicReview) window.OrbisEconomicReview.update();
    if (window.OrbisExternalReview) window.OrbisExternalReview.update();
    if (onMexcContext) onMexcContext();
  }

  function mexcReadiness() {
    return {connected: !!(active && account), onBnb: chain === BNB_CHAIN && !networkAction,
      changingNetwork: !!networkAction, verified: proofVerified,
      usdtObserved: !!(tokenObservation && tokenObservation.mexcBnbAccount &&
        realAccountId && tokenObservation.mexcBnbAccount === realAccountId())};
  }
  async function refresh() {
    if (!active || !account) return;
    clearTx();
    var current = ++generation;
    var provider = active;
    var address = account;
    balance = null;
    balanceRaw = null;
    balanceBlock = null;
    clearToken(false);
    state("Consultando la red de la wallet…");
    try {
      var chainValue = await timedWalletRead(provider, { method: "eth_chainId" });
      if (!safeHex(chainValue)) throw new Error("Invalid chain");
      var canonical = "0x" + BigInt(chainValue).toString(16);
      var block = await timedWalletRead(provider, { method: "eth_blockNumber" });
      if (!safeHex(block)) throw new Error("Invalid block");
      block = "0x" + BigInt(block).toString(16);
      var blockTimeMs = await recentBlockTime(provider, block);
      if (current !== generation || active !== provider || account !== address) return;
      var wei = await timedWalletRead(provider, { method: "eth_getBalance", params: [address, block] });
      var finalChain = await timedWalletRead(provider, { method: "eth_chainId" });
      if (!safeHex(finalChain) || "0x" + BigInt(finalChain).toString(16) !== canonical) {
        throw new Error("Network changed");
      }
      if (!blockIsRecent(blockTimeMs)) throw new Error("STALE_WALLET_BLOCK");
      if (current !== generation || active !== provider || account !== address) return;
      chain = canonical;
      balanceRaw = safeHex(wei) ? BigInt(wei) : null;
      balance = balanceRaw === null ? null :
        (chains[canonical] ? formatUnits(balanceRaw, 18) : balanceRaw.toString());
      if (balance === null) throw new Error("Invalid balance");
      balanceBlock = BigInt(block).toString();
      state("Saldo actualizado.");
    } catch (error) {
      if (current !== generation || active !== provider) return;
      chain = null;
      balance = null;
      balanceRaw = null;
      balanceBlock = null;
      state(error && error.message === "STALE_WALLET_BLOCK" ?
        "RPC atrasado: saldo no actualizado." :
        "No se pudo consultar el saldo. Revisa la red o la wallet.");
    }
  }
  function onAccountsChanged(values) {
    generation += 1;
    clearProof();
    clearTx();
    var next = validAccounts(values);
    if (!next) { clear(); return; }
    accounts = next;
    account = next.find(function (item) { return account && item.toLowerCase() === account.toLowerCase(); }) || next[0];
    chain = null;
    balance = null;
    balanceRaw = null;
    balanceBlock = null;
    clearToken(false);
    refresh();
  }
  function onChainChanged() {
    if (!networkAction) resetNetworkAction();
    generation += 1;
    clearProof();
    clearTx();
    chain = null;
    balance = null;
    balanceRaw = null;
    balanceBlock = null;
    clearToken(true);
    return refresh();
  }
  async function changeBnbNetwork() {
    if (!active || !account || chain === BNB_CHAIN || networkAction) return;
    var provider = active;
    var adding = bnbNetworkMissing;
    var action = {};
    networkAction = action;
    networkStatus = "Confirma en la wallet.";
    // El cambio de red invalida las pruebas y cotizaciones de la red anterior.
    generation += 1;
    clearProof();
    clearToken(true);
    clearTx();
    paint();

    function current() { return active === provider && networkAction === action; }
    try {
      var request = adding ? {method: "wallet_addEthereumChain", params: [{
        chainId: BNB_CHAIN, chainName: "BNB Smart Chain",
        nativeCurrency: {name: "BNB", symbol: "BNB", decimals: 18},
        rpcUrls: ["https://bsc-dataseed.bnbchain.org"],
        blockExplorerUrls: ["https://bscscan.com"]
      }]} : {method: "wallet_switchEthereumChain", params: [{chainId: BNB_CHAIN}]};
      await provider.request(request);
      if (!current()) return;
      bnbNetworkMissing = false;
      if (chain !== BNB_CHAIN) await onChainChanged();
      if (!current()) return;
      networkStatus = chain === BNB_CHAIN ? "" : adding ?
        "Red añadida. Ahora cambia a BNB Chain." : "Red sin confirmar. Comprueba tu wallet.";
    } catch (error) {
      if (!current()) return;
      var code = error && error.code;
      if (!adding && code === 4902) {
        bnbNetworkMissing = true;
        networkStatus = "Añade la red en tu wallet.";
      } else if (code === 4001) {
        networkStatus = adding ? "Alta cancelada." : "Cambio cancelado.";
      } else if (code === -32002) {
        networkStatus = "Hay una solicitud pendiente en la wallet.";
      } else {
        networkStatus = "Selecciona BNB Chain en tu wallet.";
      }
    } finally {
      if (current()) {
        networkAction = null;
        paint();
      }
    }
  }
  function onDisconnect() { clear(); }
  async function connect() {
    var provider = providerForSelection();
    if (!provider) { state("No se ha detectado una wallet EVM."); return; }
    var current = ++generation;
    state("Esperando autorización de la wallet…");
    try {
      var requested = await provider.request({ method: "eth_requestAccounts" });
      if (current !== generation) return;
      var next = validAccounts(requested);
      if (!next) throw new Error("Invalid account");
      stopProvider(true);
      active = provider;
      accounts = next;
      account = next[0];
      if (typeof provider.on === "function") {
        provider.on("accountsChanged", onAccountsChanged);
        provider.on("chainChanged", onChainChanged);
        provider.on("disconnect", onDisconnect);
      }
      refresh();
    } catch (_) {
      if (current === generation) state("Conexión cancelada o wallet no disponible.");
    }
  }
  function selectAccount(event) {
    var index = Number(event.target.value);
    if (!Number.isInteger(index) || index < 0 || index >= accounts.length) return;
    if (account === accounts[index]) return;
    account = accounts[index];
    clearProof();
    clearTx();
    chain = null;
    balance = null;
    balanceRaw = null;
    clearToken(false);
    refresh();
  }
  async function queryToken(event, mexcBnbAccount) {
    event.preventDefault();
    if (!active || !account || !chain || tokenBusy || networkAction) return;
    var input = root && root.querySelector("#real-wallet-token-contract");
    var contract = input && input.value.trim();
    if (!safeAddress(contract)) {
      tokenStatus = "Contrato EVM inválido.";
      paint();
      return;
    }
    tokenContract = contract;
    clearDestination();
    clearDexQuote();
    tokenObservation = null;
    tokenBusy = true;
    tokenStatus = "Consultando token…";
    var current = ++tokenGeneration;
    var nativeGeneration = generation;
    var provider = active;
    var address = account;
    var expectedChain = chain;
    function stillCurrent() {
      return current === tokenGeneration && nativeGeneration === generation &&
        active === provider && account === address && chain === expectedChain;
    }
    paint();
    try {
      var observedChain = await provider.request({ method: "eth_chainId" });
      if (!safeHex(observedChain) || "0x" + BigInt(observedChain).toString(16) !== expectedChain) throw new Error("Network changed");
      if (!stillCurrent()) return;
      var block = await provider.request({ method: "eth_blockNumber" });
      if (!safeHex(block)) throw new Error("Invalid block");
      var blockTimeMs = await recentBlockTime(provider, block);
      if (!stillCurrent()) return;
      var decimals = null;
      try {
        var encodedDecimals = await provider.request({ method: "eth_call", params: [{ to: contract, data: "0x313ce567" }, block] });
        if (safeWord(encodedDecimals) && BigInt(encodedDecimals) <= 36n) decimals = Number(BigInt(encodedDecimals));
      } catch (_) { /* decimals() es opcional en ERC-20; mostrar unidades mínimas. */ }
      if (!stillCurrent()) return;
      var encodedOwner = address.slice(2).toLowerCase().padStart(64, "0");
      var raw = await provider.request({ method: "eth_call", params: [{ to: contract, data: "0x70a08231" + encodedOwner }, block] });
      if (!safeWord(raw)) throw new Error("Invalid token balance");
      if (!stillCurrent()) return;
      var finalChain = await provider.request({ method: "eth_chainId" });
      if (!safeHex(finalChain) || "0x" + BigInt(finalChain).toString(16) !== expectedChain) throw new Error("Network changed");
      if (!blockIsRecent(blockTimeMs)) throw new Error("STALE_WALLET_BLOCK");
      if (!stillCurrent()) return;
      tokenObservation = {
        contract: contract, decimals: decimals,
        amount: decimals === null ? BigInt(raw).toString() : formatUnits(BigInt(raw), decimals),
        block: BigInt(block).toString(), mexcBnbAccount: mexcBnbAccount || null
      };
      tokenStatus = "Saldo observado · no contabilizado.";
    } catch (error) {
      if (current !== tokenGeneration || nativeGeneration !== generation || active !== provider) return;
      tokenObservation = null;
      tokenStatus = error && error.message === "STALE_WALLET_BLOCK" ?
        "RPC atrasado: token no actualizado." : "No se pudo consultar el token en esta red.";
    } finally {
      if (current === tokenGeneration) { tokenBusy = false; paint(); }
    }
  }
  async function queryBnbUsdt() {
    if (!active || !account || chain !== BNB_CHAIN || tokenBusy || networkAction || !proofRequest || !realAccountId || !realAccountId()) return;
    var provider = active, wallet = account, accountId = realAccountId(), issued = generation;
    tokenStatus = "Consultando USDT/BNB Chain en MEXC…"; paint();
    try {
      var result = await proofRequest("/accounts/"+encodeURIComponent(accountId)+"/mexc-erc20-deposit-destination?asset=USDT&chain_id=0x38");
      if (active !== provider || account !== wallet || generation !== issued || chain !== "0x38" || realAccountId() !== accountId) return;
      if (result.asset !== "USDT" || result.network !== "BNB_SMART_CHAIN_MAINNET" || !safeAddress(result.token_contract) ||
          result.stored !== false || result.transfer_sent !== false) throw new Error("Invalid BSC metadata");
      root.querySelector("#real-wallet-token-contract").value = result.token_contract;
      await queryToken({preventDefault:function () {}}, accountId);
    } catch (_) { tokenStatus = "USDT/BNB no verificable. Comprueba MEXC View y el vínculo de cuenta."; paint(); }
  }
  async function quoteUniswap(event) {
    event.preventDefault();
    if (!active || !account || chain !== "0x1" || !tokenObservation ||
        tokenObservation.decimals === null || dexQuoteBusy) return;
    var form = event.currentTarget;
    var tokenIn = tokenObservation.contract;
    var tokenOut = form.elements.token_out.value.trim();
    var feeChoice = form.elements.pool_fee.value;
    var fees = feeChoice === "auto" ? [100, 500, 3000, 10000] : [Number(feeChoice)];
    var amountIn = decimalUnits(form.elements.amount_in.value.trim(), tokenObservation.decimals);
    if (!safeAddress(tokenIn) || !safeAddress(tokenOut) ||
        tokenIn.toLowerCase() === tokenOut.toLowerCase() ||
        fees.some(function (fee) { return ![100, 500, 3000, 10000].includes(fee); }) ||
        amountIn === null || amountIn <= 0n || amountIn >= (1n << 256n)) {
      dexQuoteStatus = "Revisa contratos, cantidad exacta y comisión del pool.";
      paint();
      return;
    }
    clearDexQuote();
    var current = ++dexQuoteGeneration, nativeGeneration = generation;
    var currentToken = tokenGeneration, provider = active, address = account;
    function stillCurrent() {
      return current === dexQuoteGeneration && nativeGeneration === generation &&
        currentToken === tokenGeneration && active === provider && account === address &&
        chain === "0x1" && tokenObservation &&
        tokenObservation.contract.toLowerCase() === tokenIn.toLowerCase();
    }
    dexQuoteBusy = true;
    dexQuoteStatus = "Consultando Uniswap v3…";
    paint();
    var staleQuoteBlock = false;
    try {
      var observedChain = await timedWalletRead(provider, {method: "eth_chainId"});
      if (!safeHex(observedChain) || BigInt(observedChain) !== 1n) throw new Error("Wrong chain");
      var block = await timedWalletRead(provider, {method: "eth_blockNumber"});
      var blockTimeMs = await recentBlockTime(provider, block);
      if (!stillCurrent()) return;
      var decimalsWord = await timedWalletRead(provider, {method: "eth_call", params: [
        {to: tokenOut, data: "0x313ce567"}, block
      ]});
      if (!safeWord(decimalsWord) || BigInt(decimalsWord) > 36n) throw new Error("Unknown decimals");
      var outputDecimals = Number(BigInt(decimalsWord));
      if (!stillCurrent()) return;
      var encodedOwner = address.slice(2).toLowerCase().padStart(64, "0");
      var balanceWord = await timedWalletRead(provider, {method: "eth_call", params: [
        {to: tokenIn, data: "0x70a08231" + encodedOwner}, block
      ]});
      if (!stillCurrent()) return;
      if (!safeWord(balanceWord) || BigInt(balanceWord) < amountIn) {
        dexQuoteStatus = "Saldo insuficiente en el bloque consultado; no hay cotización aplicable.";
        return;
      }
      if (!stillCurrent()) return;
      function word(value) { return value.toString(16).padStart(64, "0"); }
      async function quoteAtFee(fee) {
        var callData = uniswapExactInputSingle +
          word(BigInt(tokenIn)) + word(BigInt(tokenOut)) + word(amountIn) +
          word(BigInt(fee)) + word(0n);
        var raw = await timedWalletRead(provider, {method: "eth_call", params: [
          {to: uniswapQuoterV2, data: callData}, block
        ]});
        if (typeof raw !== "string" || !/^0x[0-9a-fA-F]{256}$/.test(raw))
          throw new Error("Invalid quote");
        var out = BigInt("0x" + raw.slice(2, 66));
        var sqrtAfter = BigInt("0x" + raw.slice(66, 130));
        var gasEstimate = BigInt("0x" + raw.slice(194, 258));
        if (out <= 0n || sqrtAfter <= 0n || gasEstimate <= 0n)
          throw new Error("Invalid quote");
        return {fee: fee, amountOut: out, gasEstimate: gasEstimate};
      }
      var settled = await Promise.allSettled(fees.map(quoteAtFee));
      if (!stillCurrent()) return;
      var valid = settled.filter(function (item) { return item.status === "fulfilled"; })
        .map(function (item) { return item.value; });
      if (!valid.length) throw new Error("No pool quote");
      valid.sort(function (left, right) {
        if (left.amountOut !== right.amountOut) return left.amountOut > right.amountOut ? -1 : 1;
        if (left.gasEstimate !== right.gasEstimate) return left.gasEstimate < right.gasEstimate ? -1 : 1;
        return left.fee - right.fee;
      });
      var chosen = valid[0], out = chosen.amountOut;
      var routerAllowance = null;
      try {
        var allowanceWord = await timedWalletRead(provider, {method: "eth_call", params: [
          {to: tokenIn, data: "0xdd62ed3e" + encodedOwner +
            uniswapSwapRouter02.slice(2).toLowerCase().padStart(64, "0")}, block
        ]});
        if (safeWord(allowanceWord)) routerAllowance = BigInt(allowanceWord);
      } catch (_) { /* La cotización sigue siendo válida si allowance() no responde. */ }
      if (!stillCurrent()) return;
      var finalChain = await timedWalletRead(provider, {method: "eth_chainId"});
      var finalAccounts = await timedWalletRead(provider, {method: "eth_accounts"});
      if (!stillCurrent()) return;
      if (!safeHex(finalChain) || BigInt(finalChain) !== 1n ||
          !Array.isArray(finalAccounts) || !finalAccounts.some(function (item) {
            return safeAddress(item) && item.toLowerCase() === address.toLowerCase();
          })) throw new Error("Wallet changed");
      if (!blockIsRecent(blockTimeMs)) {
        staleQuoteBlock = true;
        throw new Error("STALE_WALLET_BLOCK");
      }
      var expiresAt = Math.min(Date.now() + 30000, blockTimeMs + 120000);
      dexQuoteResult = {amountOut: formatUnits(out, outputDecimals),
        minimumOut: formatUnits(out * 995n / 1000n, outputDecimals),
        tokenOut: shortAddress(tokenOut),
        poolFee: (chosen.fee / 10000).toString().replace(".", ",") + " %",
        auto: feeChoice === "auto", responded: valid.length,
        routerAllowance: routerAllowance === null ? null : formatUnits(routerAllowance, tokenObservation.decimals),
        routerAllowanceEnough: routerAllowance !== null && routerAllowance >= amountIn,
        block: BigInt(block).toString(), expiresAt: expiresAt};
      dexQuoteStatus = "Cotización obtenida.";
      window.setTimeout(function () {
        if (!stillCurrent() || !dexQuoteResult || dexQuoteResult.expiresAt > Date.now()) return;
        clearDexQuote();
        dexQuoteStatus = "Cotización caducada; consulta de nuevo.";
        paint();
      }, Math.max(0, expiresAt - Date.now() + 50));
    } catch (error) {
      if (error && error.message === "STALE_WALLET_BLOCK") staleQuoteBlock = true;
      if (stillCurrent()) dexQuoteStatus = staleQuoteBlock ?
        "RPC desactualizado: no se muestra cotización. Reintenta con una red al día." :
        "No se pudo verificar la cotización en los pools y bloque consultados.";
    } finally {
      if (stillCurrent()) { dexQuoteBusy = false; paint(); }
    }
  }
  async function queryMexcDepositDestination(event) {
    event.preventDefault();
    if (!active || !account || chain !== "0x1" || !tokenObservation ||
        !proofRequest || !realAccountId || depositBusy) return;
    var form = event.currentTarget;
    var asset = form.elements.asset.value.trim().toUpperCase();
    if (!/^[A-Z0-9]{2,24}$/.test(asset) || asset === "ETH") {
      depositStatus = "Indica el símbolo ERC-20 exacto de MEXC.";
      paint();
      return;
    }
    var accountId = realAccountId();
    if (!accountId) return;
    clearDestination();
    var current = ++depositGeneration;
    var nativeGeneration = generation, currentToken = tokenGeneration;
    var provider = active, walletAccount = account, contract = tokenObservation.contract.toLowerCase();
    function stillCurrent() {
      return current === depositGeneration && nativeGeneration === generation &&
        currentToken === tokenGeneration && active === provider &&
        account === walletAccount && chain === "0x1" && tokenObservation &&
        tokenObservation.contract.toLowerCase() === contract && realAccountId() === accountId;
    }
    depositBusy = true;
    depositStatus = "Consultando destino MEXC…";
    paint();
    try {
      var result = await proofRequest("/accounts/" + encodeURIComponent(accountId) +
        "/mexc-erc20-deposit-destination?asset=" + encodeURIComponent(asset));
      if (!stillCurrent()) return;
      if (!result || result.source !== "MEXC_SPOT_PRIVATE_READONLY" ||
          result.network !== "ETHEREUM_MAINNET" || result.asset !== asset ||
          result.stored !== false || result.transfer_sent !== false ||
          result.ready_for_execution !== false || !safeAddress(result.token_contract) ||
          !safeAddress(result.deposit_address)) throw new Error("Invalid destination");
      if (result.token_contract.toLowerCase() !== contract) {
        depositStatus = "El contrato de la wallet no coincide con la ficha MEXC. No se muestra destino.";
        return;
      }
      depositDestination = {accountId: accountId, contract: contract,
        address: result.deposit_address, expiresAt: Date.now() + 60000};
      depositStatus = asset + " · destino observado, disponible un minuto en esta pantalla.";
      window.setTimeout(function () {
        if (current !== depositGeneration || !depositDestination ||
            depositDestination.expiresAt > Date.now()) return;
        clearDestination();
        paint();
      }, 60050);
    } catch (_) {
      if (!stillCurrent()) return;
      depositStatus = "Destino no verificable. Comprueba vínculo MEXC View, activo y red.";
    } finally {
      if (current === depositGeneration) { depositBusy = false; paint(); }
    }
  }
  async function refreshMexcTransfersForReceipt(accountId, stillCurrent) {
    // Sólo después de un recibo wallet exitoso y de un clic explícito.
    // El identificador EVM no se envía al exchange; el archivo queda cifrado.
    try {
      var link = await proofRequest("/accounts/" + encodeURIComponent(accountId) + "/mexc-link");
      if (!stillCurrent()) return null;
      if (!link || link.connected !== true || link.linked !== true ||
          link.identity_status !== "SAME_KEY") return false;
      var result = await proofRequest("/exchanges/mexc/transfers?days=7");
      if (!stillCurrent()) return null;
      var observedMs = result && Date.parse(result.observed_at);
      var nowMs = Date.now();
      if (!result || result.source !== "MEXC_SPOT_PRIVATE_READONLY" ||
          result.accounting !== "OBSERVED_NOT_IMPORTED" ||
          result.identity_verified !== true ||
          result.coverage !== "RECENT_WINDOW_BELOW_PAGE_LIMIT" ||
          !Number.isFinite(observedMs) || observedMs > nowMs || nowMs - observedMs > 120000 ||
          !Number.isSafeInteger(result.start_ms) || !Number.isSafeInteger(result.end_ms) ||
          result.end_ms < result.start_ms || result.end_ms > nowMs ||
          nowMs - result.end_ms > 120000 ||
          result.end_ms - result.start_ms !== 7 * 86400000 ||
          !result.archive || result.archive.identity_verified !== true ||
          result.archive.coverage !== "RECENT_WINDOW_BELOW_PAGE_LIMIT") return false;
      var after = await proofRequest("/accounts/" + encodeURIComponent(accountId) + "/mexc-link");
      if (!stillCurrent()) return null;
      return !!after && after.connected === true && after.linked === true &&
        after.identity_status === "SAME_KEY";
    } catch (_) {
      return stillCurrent() ? false : null;
    }
  }
  async function checkDepositTx(event) {
    event.preventDefault();
    if (!active || !account || !chain || !proofRequest || !realAccountId || txBusy) return;
    var input = root && root.querySelector("#real-wallet-tx-hash");
    var hash = input && input.value.trim();
    if (input) input.value = "";
    if (!safeWord(hash)) { txStatus = "Identificador EVM inválido."; paint(); return; }
    var accountId = realAccountId();
    if (!accountId) { txStatus = "Selecciona una cuenta REAL."; paint(); return; }
    if (chain !== "0x1") {
      txStatus = "Este cruce sólo comprueba finalización en Ethereum mainnet.";
      paint();
      return;
    }
    var current = ++txGeneration;
    var nativeGeneration = generation;
    var provider = active, address = account, expectedChain = chain;
    function stillCurrent() {
      return current === txGeneration && nativeGeneration === generation &&
        active === provider && account === address && chain === expectedChain &&
        realAccountId() === accountId;
    }
    txBusy = true;
    txStatus = "Consultando transacción en la red de la wallet…";
    paint();
    try {
      var observedChain = await provider.request({method: "eth_chainId"});
      if (!safeHex(observedChain) || "0x" + BigInt(observedChain).toString(16) !== expectedChain)
        throw new Error("Network changed");
      if (!stillCurrent()) return;
      var tx = await provider.request({method: "eth_getTransactionByHash", params: [hash]});
      if (!stillCurrent()) return;
      if (!tx || !safeWord(tx.hash) || tx.hash.toLowerCase() !== hash.toLowerCase() ||
          !safeAddress(tx.from) || tx.from.toLowerCase() !== address.toLowerCase() ||
          !safeHex(tx.blockNumber)) {
        txStatus = "La transacción no está minada o su emisor no coincide con la cuenta seleccionada.";
        return;
      }
      var receipt = await provider.request({method: "eth_getTransactionReceipt", params: [hash]});
      if (!stillCurrent()) return;
      if (!receipt || !safeWord(receipt.transactionHash) ||
          receipt.transactionHash.toLowerCase() !== hash.toLowerCase() ||
          !safeAddress(receipt.from) || receipt.from.toLowerCase() !== address.toLowerCase() ||
          !safeHex(receipt.blockNumber) || BigInt(receipt.blockNumber) !== BigInt(tx.blockNumber) ||
          !safeHex(receipt.status) || BigInt(receipt.status) !== 1n) {
        txStatus = "No hay recibo válido y exitoso para esta cuenta; no se cruzó con MEXC.";
        return;
      }
      var finality = await finalizedEthereumReceipt(provider, tx, receipt);
      if (!stillCurrent()) return;
      if (finality !== "FINALIZED") { txStatus = receiptFinalityStatus(finality); return; }
      var candidates = [];
      if (expectedChain === "0x1") {
        if (safeAddress(tx.to) && safeHex(tx.value) && BigInt(tx.value) > 0n && tx.input === "0x") {
          candidates.push({kind: "native", recipient: tx.to, units: BigInt(tx.value)});
        }
        if (Array.isArray(receipt.logs) && receipt.logs.length <= 500) {
          receipt.logs.forEach(function (log) {
            if (!log || log.removed === true || !safeAddress(log.address) ||
                !Array.isArray(log.topics) || log.topics.length !== 3 ||
                typeof log.topics[0] !== "string" || log.topics[0].toLowerCase() !== transferTopic ||
                !safeWord(log.data)) return;
            var sender = topicAddress(log.topics[1]);
            var recipient = topicAddress(log.topics[2]);
            if (!sender || sender.toLowerCase() !== address.toLowerCase() || !recipient) return;
            candidates.push({kind: "erc20", recipient: recipient, contract: log.address,
              units: BigInt(log.data)});
          });
        }
      }
      if (candidates.length > 20) candidates = [];
      var finalChain = await provider.request({method: "eth_chainId"});
      var exposed = await provider.request({method: "eth_accounts"});
      if (!stillCurrent()) return;
      if (!safeHex(finalChain) || "0x" + BigInt(finalChain).toString(16) !== expectedChain ||
          !Array.isArray(exposed) || !exposed.some(function (item) {
            return safeAddress(item) && item.toLowerCase() === address.toLowerCase();
          })) throw new Error("Wallet changed");
      txStatus = "Recibo observado · actualizando MEXC…";
      paint();
      var mexcUpdated = await refreshMexcTransfersForReceipt(accountId, stillCurrent);
      if (!stillCurrent()) return;
      txStatus = "Recibo observado · buscando el mismo identificador en MEXC…";
      paint();
      var match = await proofRequest("/accounts/" + encodeURIComponent(accountId) + "/mexc-deposit-tx-lookup", {
        method: "POST", body: JSON.stringify({tx_hash: hash,
          destinations: candidates.map(function (item) { return item.recipient; })})
      });
      if (!stillCurrent()) return;
      var lastChain = await provider.request({method: "eth_chainId"});
      var lastAccounts = await provider.request({method: "eth_accounts"});
      if (!stillCurrent()) return;
      if (!safeHex(lastChain) || "0x" + BigInt(lastChain).toString(16) !== expectedChain ||
          !Array.isArray(lastAccounts) || !lastAccounts.some(function (item) {
            return safeAddress(item) && item.toLowerCase() === address.toLowerCase();
          })) throw new Error("Wallet changed");
      if (!match || match.ready_for_execution !== false || match.chain_receipt_verified !== false ||
          match.real_book_link_checked !== true ||
          !["NO_MATCHING_DEPOSIT", "NO_EXPLICIT_LINK", "VERIFIED_EXPLICIT_LINK",
            "LEGACY_LINK_UNVERIFIED_IDENTITY", "INCONSISTENT_LINK"].includes(match.book_link_status) ||
          !["FINAL_MEXC_DEPOSIT", "NOT_FINAL_MEXC_DEPOSIT", "CONTRADICTORY_MEXC_DEPOSIT",
            "NOT_FOUND_IN_VERIFIED_ARCHIVE"].includes(match.status)) throw new Error("Archive response invalid");
      var chainName = chains[expectedChain] ? chains[expectedChain].network : "red " + expectedChain;
      var prefix = "Wallet/RPC: envío con recibo exitoso en bloque finalizado y canónico según RPC en " + chainName + ". ";
      if (match.status === "FINAL_MEXC_DEPOSIT") {
        var detail = "Activo, importe y destino no contrastados en cadena";
        if (expectedChain === "0x1" && ["ERC20", "ETH"].includes(match.network) &&
            match.destination_evidence_available === true && Array.isArray(match.matched_destination_indices)) {
          var matched = match.matched_destination_indices.filter(function (index) {
            return Number.isInteger(index) && index >= 0 && index < candidates.length;
          });
          if (!candidates.length) detail = "No se identificó una transferencia nativa o ERC-20 simple desde esta cuenta";
          else if (matched.length === 0) detail = "El destino declarado por MEXC no coincide con los receptores observados";
          else if (matched.length !== 1) detail = "Varios movimientos al destino MEXC; importe ambiguo";
          else {
            var movement = candidates[matched[0]];
            if (movement.kind === "native" && (match.asset !== "ETH" || match.network !== "ETH")) {
              detail = "Movimiento nativo incompatible con el activo o la red declarados por MEXC";
            } else {
              var decimals = movement.kind === "native" ? 18 : null;
              if (movement.kind === "erc20") {
                try {
                  var encoded = await provider.request({method: "eth_call", params: [
                    {to: movement.contract, data: "0x313ce567"}, receipt.blockNumber
                  ]});
                  if (safeWord(encoded) && BigInt(encoded) <= 36n) decimals = Number(BigInt(encoded));
                } catch (_) { /* decimals() puede faltar; no inferir importe. */ }
              }
              if (!stillCurrent()) return;
              var lastObservedChain = await provider.request({method: "eth_chainId"});
              var lastExposed = await provider.request({method: "eth_accounts"});
              if (!stillCurrent() || !safeHex(lastObservedChain) ||
                  "0x" + BigInt(lastObservedChain).toString(16) !== expectedChain ||
                  !Array.isArray(lastExposed) || !lastExposed.some(function (item) {
                    return safeAddress(item) && item.toLowerCase() === address.toLowerCase();
                  })) throw new Error("Wallet changed");
              if (decimals === null) detail = "Destino observado, pero importe no verificable sin decimales del token";
              else {
                var expectedUnits = decimalUnits(match.amount, decimals);
                var publishedContract = movement.kind === "erc20" && safeAddress(match.token_contract) ?
                  match.token_contract : null;
                if (publishedContract && publishedContract.toLowerCase() !== movement.contract.toLowerCase()) {
                  detail = "Contrato ERC-20 distinto del publicado por MEXC para este activo y red";
                } else if (expectedUnits === null) detail = "Importe MEXC no comparable";
                else if (expectedUnits !== movement.units) {
                  detail = "Destino coincide, pero el importe on-chain difiere del ingreso MEXC";
                } else if (movement.kind === "native") detail = "Destino e importe nativo coinciden";
                else if (publishedContract) {
                  detail = "Destino, importe y contrato ERC-20 coinciden con la ficha MEXC observada";
                } else {
                  detail = "Destino e importe de evento ERC-20 coinciden; contrato sin ficha MEXC verificable";
                }
              }
            }
          }
        } else if (expectedChain === "0x1" && !["ERC20", "ETH"].includes(match.network)) {
          detail = "Red MEXC no comprobada como Ethereum; destino e importe sin contrastar";
        } else if (expectedChain === "0x1" && match.destination_evidence_available !== true) {
          detail = "MEXC no aportó una dirección EVM verificable en esta captura";
        }
        var bookDetail = match.book_link_status === "VERIFIED_EXPLICIT_LINK" ?
          " Libro REAL: asiento vinculado y verificado; cuenta aún no conciliada." :
          match.book_link_status === "LEGACY_LINK_UNVERIFIED_IDENTITY" ?
          " Libro REAL: vínculo antiguo sin identidad View verificada; revisar." :
          match.book_link_status === "INCONSISTENT_LINK" ?
          " Libro REAL: vínculo inconsistente; revisar antes de operar." :
          " Libro REAL: sin asiento vinculado.";
        txStatus = prefix + "MEXC: ingreso final con el mismo txId en capturas verificadas. " +
          detail + "." + bookDetail + " no conciliado.";
      } else if (match.status === "NOT_FINAL_MEXC_DEPOSIT") {
        txStatus = prefix + "MEXC: mismo txId observado, ingreso aún no final · no conciliado.";
      } else if (match.status === "CONTRADICTORY_MEXC_DEPOSIT") {
        txStatus = prefix + "MEXC: capturas contradictorias para el mismo txId · no conciliado.";
      } else {
        txStatus = prefix + "Sin coincidencia en las capturas MEXC verificadas; no demuestra ausencia de ingreso." +
          (mexcUpdated ? " MEXC consultado en los últimos 7 días." : " Actualización MEXC no verificable; búsqueda en capturas archivadas.");
      }
    } catch (_) {
      if (stillCurrent()) txStatus = "No se pudo completar la comprobación wallet ↔ MEXC. No se ha conciliado ningún movimiento.";
    } finally {
      if (stillCurrent()) { txBusy = false; paint(); }
    }
  }
  async function checkWithdrawalTx(event) {
    event.preventDefault();
    if (!active || !account || !chain || !proofRequest || !realAccountId || txBusy) return;
    var input = root && root.querySelector("#real-wallet-tx-hash");
    var hash = input && input.value.trim();
    if (input) input.value = "";
    if (!safeWord(hash)) { txStatus = "Identificador EVM inválido."; paint(); return; }
    var accountId = realAccountId();
    if (!accountId) { txStatus = "Selecciona una cuenta REAL."; paint(); return; }
    if (chain !== "0x1") {
      txStatus = "Este cruce de retiros sólo comprueba Ethereum mainnet.";
      paint();
      return;
    }
    var current = ++txGeneration;
    var nativeGeneration = generation;
    var provider = active, address = account, expectedChain = chain;
    function stillCurrent() {
      return current === txGeneration && nativeGeneration === generation &&
        active === provider && account === address && chain === expectedChain &&
        realAccountId() === accountId;
    }
    async function assertWalletCurrent() {
      var observedChain = await provider.request({method: "eth_chainId"});
      var exposed = await provider.request({method: "eth_accounts"});
      if (!stillCurrent() || !safeHex(observedChain) ||
          "0x" + BigInt(observedChain).toString(16) !== expectedChain ||
          !Array.isArray(exposed) || !exposed.some(function (item) {
            return safeAddress(item) && item.toLowerCase() === address.toLowerCase();
          })) throw new Error("Wallet changed");
    }
    txBusy = true;
    txStatus = "Consultando recepción en la red de la wallet…";
    paint();
    try {
      await assertWalletCurrent();
      var tx = await provider.request({method: "eth_getTransactionByHash", params: [hash]});
      if (!stillCurrent()) return;
      if (!tx || !safeWord(tx.hash) || tx.hash.toLowerCase() !== hash.toLowerCase() ||
          !safeHex(tx.blockNumber)) {
        txStatus = "La transacción no está minada o su identificador no coincide.";
        return;
      }
      var receipt = await provider.request({method: "eth_getTransactionReceipt", params: [hash]});
      if (!stillCurrent()) return;
      if (!receipt || !safeWord(receipt.transactionHash) ||
          receipt.transactionHash.toLowerCase() !== hash.toLowerCase() ||
          !safeAddress(tx.from) || !safeAddress(receipt.from) ||
          tx.from.toLowerCase() !== receipt.from.toLowerCase() ||
          !safeHex(receipt.blockNumber) || BigInt(receipt.blockNumber) !== BigInt(tx.blockNumber) ||
          !safeHex(receipt.status) || BigInt(receipt.status) !== 1n) {
        txStatus = "No hay recibo válido y exitoso; no se cruzó con MEXC.";
        return;
      }
      var finality = await finalizedEthereumReceipt(provider, tx, receipt);
      if (!stillCurrent()) return;
      if (finality !== "FINALIZED") { txStatus = receiptFinalityStatus(finality); return; }
      var received = [];
      if (safeAddress(tx.to) && tx.to.toLowerCase() === address.toLowerCase() &&
          safeHex(tx.value) && BigInt(tx.value) > 0n && tx.input === "0x") {
        received.push({kind: "native", units: BigInt(tx.value)});
      }
      if (Array.isArray(receipt.logs) && receipt.logs.length <= 500) {
        receipt.logs.forEach(function (log) {
          if (log && log.removed !== true && safeAddress(log.address) &&
              Array.isArray(log.topics) && log.topics.length === 3 &&
              typeof log.topics[0] === "string" && log.topics[0].toLowerCase() === transferTopic &&
              (topicAddress(log.topics[2]) || "").toLowerCase() === address.toLowerCase() &&
              safeWord(log.data) && BigInt(log.data) > 0n) {
            received.push({kind: "erc20", contract: log.address, units: BigInt(log.data)});
          }
        });
      }
      if (!received.length) {
        txStatus = "No se observó una recepción nativa o ERC-20 simple para esta cuenta.";
        return;
      }
      await assertWalletCurrent();
      txStatus = "Recibo observado · actualizando MEXC…";
      paint();
      var mexcUpdated = await refreshMexcTransfersForReceipt(accountId, stillCurrent);
      if (!stillCurrent()) return;
      txStatus = "Recibo observado · buscando retiro MEXC…";
      paint();
      var match = await proofRequest("/accounts/" + encodeURIComponent(accountId) + "/mexc-withdrawal-tx-lookup", {
        method: "POST", body: JSON.stringify({tx_hash: hash, recipient: address})
      });
      await assertWalletCurrent();
      if (!match || match.ready_for_execution !== false || match.chain_receipt_verified !== false ||
          match.amount_semantics !== "MEXC_GROSS_NET_UNVERIFIED" ||
          !["FINAL_MEXC_WITHDRAWAL", "NOT_FINAL_MEXC_WITHDRAWAL",
            "NON_EXTERNAL_MEXC_WITHDRAWAL", "CONTRADICTORY_MEXC_WITHDRAWAL",
            "NOT_FOUND_IN_VERIFIED_ARCHIVE"].includes(match.status)) throw new Error("Archive response invalid");
      if (match.status === "FINAL_MEXC_WITHDRAWAL") {
        if (match.recipient_matches_archive === true) {
          var amountDetail = "Importe y comisión sin conciliar";
          if (received.length > 1) amountDetail = "Varios movimientos recibidos; importe ambiguo";
          else {
            var movement = received[0];
            if (movement.kind === "native" && (match.asset !== "ETH" || match.network !== "ETH")) {
              amountDetail = "Movimiento nativo incompatible con el activo/red MEXC";
            } else if (movement.kind === "erc20" && safeAddress(match.token_contract) &&
                       match.token_contract.toLowerCase() !== movement.contract.toLowerCase()) {
              amountDetail = "Contrato ERC-20 distinto de la ficha MEXC observada";
            } else {
              var decimals = movement.kind === "native" ? 18 : null;
              if (movement.kind === "erc20") {
                try {
                  var encoded = await provider.request({method: "eth_call", params: [
                    {to: movement.contract, data: "0x313ce567"}, receipt.blockNumber
                  ]});
                  if (safeWord(encoded) && BigInt(encoded) <= 36n) decimals = Number(BigInt(encoded));
                } catch (_) { /* decimals() opcional: no inventar el importe. */ }
              }
              await assertWalletCurrent();
              if (decimals === null) amountDetail = "Token sin decimales verificables; importe no comparable";
              else {
                var published = decimalUnits(match.amount, decimals);
                var fee = decimalUnits(match.fee, decimals);
                if (published === null || fee === null) amountDetail = "Importe MEXC no comparable";
                else if (movement.units === published) {
                  amountDetail = "Valor numérico igual al importe MEXC; tratamiento de comisión sin verificar";
                } else if (published >= fee && movement.units === published - fee) {
                  amountDetail = "Valor numérico igual a importe menos comisión (si es el mismo activo); hipótesis sin verificar";
                } else {
                  amountDetail = "Valor numérico distinto de importe e importe menos comisión";
                }
                if (movement.kind === "erc20") amountDetail += safeAddress(match.token_contract) ?
                  "; contrato coincide con ficha MEXC, identidad histórica sin verificar" :
                  "; identidad del token sin verificar";
              }
            }
          }
          txStatus = "Wallet/RPC: recepción en bloque finalizado y canónico según RPC. MEXC: retiro externo final con mismo txId y destino. " +
            amountDetail + " · no conciliado.";
        } else if (match.destination_evidence_available === true) {
          txStatus = "Wallet/RPC: recepción observada. MEXC: mismo txId, pero el destino archivado no coincide · no conciliado.";
        } else {
          txStatus = "Wallet/RPC: recepción observada. MEXC: mismo txId, sin destino verificable · no conciliado.";
        }
      } else if (match.status === "NOT_FINAL_MEXC_WITHDRAWAL") {
        txStatus = "MEXC: mismo txId, retiro aún no final · no conciliado.";
      } else if (match.status === "NON_EXTERNAL_MEXC_WITHDRAWAL") {
        txStatus = "MEXC: retiro interno o ruta desconocida; no acredita envío a wallet.";
      } else if (match.status === "CONTRADICTORY_MEXC_WITHDRAWAL") {
        txStatus = "MEXC: capturas contradictorias del retiro · no conciliado.";
      } else {
        txStatus = "La recepción no consta en capturas MEXC verificadas; no demuestra ausencia de retiro." +
          (mexcUpdated ? " MEXC consultado en los últimos 7 días." : " Actualización MEXC no verificable; búsqueda en capturas archivadas.");
      }
    } catch (_) {
      if (stillCurrent()) txStatus = "No se pudo completar el cruce wallet ↔ MEXC; no conciliado.";
    } finally {
      if (stillCurrent()) { txBusy = false; paint(); }
    }
  }
  async function verifyControl() {
    if (!active || !account || !chain || !proofRequest || !realAccountId || proofBusy || networkAction) return;
    var current = ++proofGeneration;
    var nativeGeneration = generation;
    var provider = active, address = account, expectedChain = chain;
    var accountId = realAccountId();
    if (!accountId) return;
    function stillCurrent() {
      return current === proofGeneration && nativeGeneration === generation &&
        active === provider && account === address && chain === expectedChain &&
        realAccountId() === accountId;
    }
    proofBusy = true;
    proofStatus = "Preparando mensaje local…";
    paint();
    try {
      var path = "/accounts/" + encodeURIComponent(accountId) + "/wallet-proof";
      var start = await proofRequest(path + "/start", {method: "POST", body: JSON.stringify({
        address: address, chain_id: expectedChain
      })});
      if (!stillCurrent()) return;
      if (start.purpose !== "SESSION_EOA_CONTROL_ONLY" ||
          !/^[0-9a-f]{64}$/.test(start.nonce) || typeof start.message !== "string" ||
          start.message.length > 1000) throw new Error("Desafío local inválido");
      if (!window.confirm("La wallet mostrará este mensaje para firmar. No autoriza pagos ni trading.\n\n" + start.message)) {
        proofStatus = "Comprobación cancelada; no se solicitó firma.";
        return;
      }
      if (!stillCurrent()) return;
      proofStatus = "Esperando firma de la wallet…";
      paint();
      var bytes = new TextEncoder().encode(start.message);
      var hexMessage = "0x" + Array.from(bytes, function (byte) { return byte.toString(16).padStart(2, "0"); }).join("");
      var signature = await provider.request({method: "personal_sign", params: [hexMessage, address]});
      if (!stillCurrent()) return;
      if (typeof signature !== "string" || !/^0x[0-9a-fA-F]{130}$/.test(signature))
        throw new Error("Firma de wallet inválida");
      var result = await proofRequest(path + "/confirm", {method: "POST", body: JSON.stringify({
        address: address, chain_id: expectedChain, nonce: start.nonce, signature: signature
      })});
      if (!stillCurrent()) return;
      if (!result.verified || result.spending_authorized !== false) throw new Error("Respuesta de verificación inválida");
      var match = await proofRequest("/accounts/" + encodeURIComponent(accountId) + "/wallet-links/inspect", {
        method: "POST", body: JSON.stringify({address: address, chain_id: expectedChain})
      });
      if (!stillCurrent()) return;
      if (!match.control_verified_this_session || match.spending_authorized !== false ||
          !["UNLINKED", "CURRENT_ACCOUNT", "OTHER_ACCOUNT"].includes(match.status))
        throw new Error("Vínculo local no verificable");
      proofVerified = true;
      linkMatch = match;
      proofStatus = match.status === "CURRENT_ACCOUNT" ?
        "Control EOA comprobado · «" + match.alias + "» corresponde a esta cuenta REAL · la prueba no autoriza gasto." :
        match.status === "OTHER_ACCOUNT" ?
          "Control EOA comprobado · vinculada a «" + match.linked_account_name + "», no a esta cuenta REAL." :
          "Control EOA comprobado · aún sin vínculo con esta cuenta REAL · la prueba no autoriza gasto.";
    } catch (_) {
      if (stillCurrent()) proofStatus = "No se pudo comprobar el control. Revisa o cancela la firma en la wallet.";
    } finally {
      if (stillCurrent()) { proofBusy = false; paint(); }
    }
  }
  async function loadLinks() {
    if (!proofRequest || !realAccountId || !realAccountId()) return;
    var current = ++linksGeneration;
    var accountId = realAccountId();
    linksStatus = "Cargando vínculos…";
    paint();
    try {
      var found = await proofRequest("/accounts/" + encodeURIComponent(accountId) + "/wallet-links");
      if (current !== linksGeneration || realAccountId() !== accountId) return;
      if (!Array.isArray(found)) throw new Error("Respuesta inválida");
      walletLinks = found;
      linksStatus = found.length ? "Asociaciones guardadas en la bóveda cifrada." : "Sin wallets vinculadas a esta cuenta REAL.";
    } catch (_) {
      if (current === linksGeneration) linksStatus = "No se pudieron consultar los vínculos.";
    }
    paint();
  }
  async function saveLink(event) {
    event.preventDefault();
    if (!proofVerified || !linkMatch || linkMatch.status !== "UNLINKED" ||
        !account || !chain || linksBusy || !proofRequest || !realAccountId) return;
    var accountId = realAccountId(), address = account, expectedChain = chain;
    var alias = root.querySelector("#real-wallet-link-alias").value.trim();
    if (!alias || alias.length > 80) { linksStatus = "Introduce un alias de hasta 80 caracteres."; paint(); return; }
    if (!window.confirm("Guardar el vínculo «" + alias + "» con esta cuenta REAL y red?\n\nSe guarda una huella en la bóveda, no la dirección completa. No permite operar ni concilia saldos.")) return;
    linksBusy = true;
    linksStatus = "Guardando vínculo…";
    paint();
    try {
      await proofRequest("/accounts/" + encodeURIComponent(accountId) + "/wallet-links", {
        method: "POST", body: JSON.stringify({address: address, chain_id: expectedChain,
          alias: alias, confirmation: "VINCULAR"})
      });
      if (account !== address || chain !== expectedChain || realAccountId() !== accountId) return;
      var savedMatch = await proofRequest("/accounts/" + encodeURIComponent(accountId) + "/wallet-links/inspect", {
        method: "POST", body: JSON.stringify({address: address, chain_id: expectedChain})
      });
      if (account !== address || chain !== expectedChain || realAccountId() !== accountId) return;
      if (savedMatch.status !== "CURRENT_ACCOUNT" || savedMatch.spending_authorized !== false)
        throw new Error("El vínculo guardado no coincide");
      linkMatch = savedMatch;
      proofStatus = "Control EOA comprobado · «" + linkMatch.alias + "» corresponde a esta cuenta REAL · la prueba no autoriza gasto.";
      await loadLinks();
    } catch (_) {
      if (account === address && chain === expectedChain) linksStatus = "No se pudo guardar. Verifica de nuevo la wallet si cambió la sesión.";
    } finally { linksBusy = false; paint(); }
  }
  async function unlinkLink(event) {
    var button = event.target.closest("button[data-link-id]");
    if (!button || !event.currentTarget.contains(button) || linksBusy || !realAccountId) return;
    var id = button.dataset.linkId, accountId = realAccountId();
    var item = walletLinks.find(function (row) { return row.id === id; });
    if (!item || !window.confirm("Desvincular «" + item.alias + "» de esta cuenta REAL? No modifica la wallet ni mueve fondos.")) return;
    linksBusy = true;
    paint();
    try {
      await proofRequest("/accounts/" + encodeURIComponent(accountId) + "/wallet-links/" + encodeURIComponent(id) + "/unlink", {
        method: "POST", body: JSON.stringify({confirmation: "DESVINCULAR"})
      });
      if (realAccountId() === accountId) {
        clearProof();
        await loadLinks();
      }
    } catch (_) { linksStatus = "No se pudo desvincular."; }
    finally { linksBusy = false; paint(); }
  }
  function mount(element, hideAmounts, requestProof, getRealAccountId, onLedgerChange, onContextChange, openMexcJourneyStep) {
    onMexcContext = onContextChange;
    root = element;
    hidden = !!hideAmounts;
    bookSnapshot = null;
    proofRequest = typeof requestProof === "function" ? requestProof : null;
    realAccountId = typeof getRealAccountId === "function" ? getRealAccountId : null;
    clearProof();
    clearTx();
    clearDestination();
    walletLinks = [];
    linksGeneration += 1;
    linksStatus = "";
    root.innerHTML = '<section class="real-panel real-wallet"><div class="real-mexc-head"><h3 data-help="Conectar sólo consulta. Un ingreso BEP-20 requiere un plan separado y tu confirmación en la wallet.">Wallet EVM</h3><span class="statusbadge off">NO CONTABILIZADO</span></div>' +
      '<div class="row"><label class="field">Proveedor<select id="real-wallet-provider" aria-label="Wallet EVM"></select></label>' +
      '<label id="real-wallet-account-wrap" class="field" hidden>Cuenta<select id="real-wallet-account" aria-label="Cuenta de wallet"></select></label>' +
      '<button id="real-wallet-connect" type="button" data-help="Solicita acceso a las cuentas de la extensión. No concede permiso de gasto.">Conectar wallet</button>' +
      '<button id="real-wallet-refresh" class="ghost" type="button" hidden data-help="Actualiza el saldo nativo. El token se debe consultar de nuevo.">Actualizar</button>' +
      '<button id="real-wallet-bnb-chain" class="ghost" type="button" hidden aria-describedby="real-wallet-network-state" data-help="Solicita a la wallet el cambio a BNB Chain. Si falta la red, puedes añadirla con su RPC público oficial en un paso separado.">Cambiar a BNB Chain</button>' +
      '<button id="real-wallet-disconnect" class="ghost" type="button" hidden data-help="Borra la conexión de esta pestaña; el permiso de la extensión se revoca allí.">Desconectar</button></div>' +
      '<p id="real-wallet-network-state" class="muted" role="status" hidden></p>' +
      '<div id="real-wallet-state" class="state muted" role="status"></div>' +
      '<div class="row"><button id="real-wallet-proof" class="ghost" type="button" hidden data-help="Firma un mensaje EIP-191 con nonce para demostrar control EOA en esta sesión. No permite gastar ni operar.">Verificar control</button>' +
      '<span id="real-wallet-proof-state" class="muted" role="status"></span></div>' +
      '<details id="real-wallet-links"><summary data-help="Recuerda una asociación EOA comprobada en esta cuenta y red; no autoriza órdenes, pagos ni conciliación.">Vínculos con la cartera REAL</summary>' +
      '<form id="real-wallet-link-form" class="row" hidden><label class="field">Alias' +
      '<input id="real-wallet-link-alias" type="text" maxlength="80" autocomplete="off" placeholder="Wallet principal" required></label>' +
      '<button id="real-wallet-link-save" type="submit" data-help="Guarda solo una huella seudónima, el alias y la red en la bóveda cifrada.">Guardar vínculo</button></form>' +
      '<p id="real-wallet-links-state" class="muted" role="status"></p><div id="real-wallet-links-list"></div></details>' +
      '<details id="real-wallet-compare" hidden><summary data-help="Compara sólo cantidades en esta pestaña; la cuenta REAL puede incluir otras wallets o redes.">Contrastar con libro REAL</summary>' +
      '<p id="real-wallet-compare-state" class="muted" role="status"></p>' +
      '<p class="muted">Pista local, no conciliada · un vínculo EOA no prueba titularidad del saldo.</p></details>' +
      '<details id="real-wallet-token-panel" hidden><summary data-help="Consulta un ERC-20 por su contrato exacto en la red actual; no importa el saldo a la cartera.">Consultar token ERC-20</summary>' +
      '<form id="real-wallet-token-form" class="row"><label class="field" data-help="El contrato es específico de la red; un nombre o símbolo no basta para identificar el activo.">Contrato' +
      '<input id="real-wallet-token-contract" type="text" placeholder="0x…" autocomplete="off" spellcheck="false" maxlength="42" required></label>' +
      '<button id="real-wallet-token-submit" type="submit" data-help="Consulta decimals y balanceOf en un bloque; no firma ni envía transacciones.">Consultar token</button><button id="real-wallet-usdt-bnb" type="button" class="ghost" hidden>USDT · BNB</button></form>' +
      '<div id="real-wallet-token-state" class="state muted" role="status"></div></details>' +
      '<details id="real-wallet-dex-panel" hidden><summary data-help="Cotiza una ruta directa de un solo pool Uniswap v3 en Ethereum. La wallet aporta RPC; no firma, aprueba ni envía transacciones.">Uniswap v3 · cotizar</summary>' +
      '<form id="real-wallet-dex-form" class="row"><label class="field" data-help="El origen es el token ERC-20 consultado arriba. El contrato de salida debe estar en Ethereum mainnet.">Token de salida' +
      '<input name="token_out" type="text" placeholder="Contrato 0x…" autocomplete="off" spellcheck="false" maxlength="42" required></label>' +
      '<label class="field">Cantidad<input name="amount_in" type="text" inputmode="decimal" placeholder="1.0" autocomplete="off" required></label>' +
      '<label class="field" data-help="Automático compara cuatro comisiones directas en el mismo bloque y elige mayor salida bruta, sin gas ni rutas múltiples.">Pool<select name="pool_fee"><option value="auto" selected>Automático</option><option value="500">0,05 %</option><option value="3000">0,3 %</option><option value="10000">1 %</option><option value="100">0,01 %</option></select></label>' +
      '<button id="real-wallet-dex-submit" type="submit" class="ghost" data-help="Simula quoteExactInputSingle en QuoterV2 al mismo bloque que el saldo. El resultado caduca a los 30 segundos y no autoriza una operación.">Cotizar</button></form>' +
      '<div id="real-wallet-dex-state" class="state muted" role="status"></div></details>' +
      '<details id="real-wallet-deposit-panel" hidden><summary data-help="Consulta un destino ERC-20 de la cuenta MEXC vinculada. La dirección se muestra temporalmente y no se guarda ni se usa para enviar fondos.">Destino MEXC · Ethereum</summary>' +
      '<form id="real-wallet-deposit-form" class="row"><label class="field">Activo MEXC' +
      '<input name="asset" type="text" placeholder="USDT" maxlength="24" autocomplete="off" spellcheck="false" required></label>' +
      '<button id="real-wallet-deposit-submit" type="submit" class="ghost" data-help="Sólo consulta la dirección y coteja el contrato; no firma, aprueba ni envía fondos.">Consultar destino</button></form>' +
      '<div id="real-wallet-deposit-state" class="state muted" role="status"></div></details>' +
      '<details id="real-wallet-tx-panel" hidden><summary data-help="Cruza un recibo EVM con un ingreso o retiro MEXC archivado. No mueve fondos ni concilia importes.">Cruzar txId con MEXC</summary>' +
      '<form id="real-wallet-tx-form" class="row"><label class="field">Movimiento<select id="real-wallet-tx-direction"><option value="deposit">Envío a MEXC</option><option value="withdrawal">Retiro a wallet</option></select></label><label class="field">TxId EVM' +
      '<input id="real-wallet-tx-hash" type="text" placeholder="0x…" autocomplete="off" spellcheck="false" maxlength="66" required></label>' +
      '<button id="real-wallet-tx-submit" type="submit" data-help="La wallet/RPC recibe el txId; Orbis lo cruza temporalmente con capturas MEXC cifradas, sin guardarlo en claro.">Comprobar</button></form>' +
      '<div id="real-wallet-tx-state" class="state muted" role="status"></div></details>' +
      '<div id="real-wallet-funding"></div><div id="real-wallet-withdrawal"></div><div id="real-wallet-swap"></div><div id="real-wallet-dex-bot"></div><div id="real-wallet-economic-review"></div><div id="real-wallet-external-review"></div><p class="muted real-footnote">Saldos observados · no importados automáticamente.</p></section>';
    var walletApi = {
      request:proofRequest, uuid:function () { return window.OrbisUI.newRequestId(); },
      reviewProviders:function () {
        var list=Array.from(providers.values(),function (item) { return {name:item.name,provider:item.provider}; });
        if (!list.length && discoveryReady && window.ethereum && typeof window.ethereum.request==='function') list.push({name:'Wallet EVM del navegador',provider:window.ethereum});
        return list;
      },
      onBooked:typeof onLedgerChange === "function" ? onLedgerChange : null,
      openMexcJourneyStep:openMexcJourneyStep,
      snapshot:function () { return {provider:active,wallet:account,chain:networkAction ? null : chain,generation:generation,
        token:tokenObservation,verified:proofVerified,accountId:realAccountId && realAccountId(),hidden:hidden}; }
    };
    if (window.OrbisWalletFunding) window.OrbisWalletFunding.mount(root.querySelector("#real-wallet-funding"),walletApi);
    if (window.OrbisMexcWithdrawal) window.OrbisMexcWithdrawal.mount(root.querySelector("#real-wallet-withdrawal"),walletApi);
    if (window.OrbisWalletSwap) window.OrbisWalletSwap.mount(root.querySelector("#real-wallet-swap"),walletApi);
    if (window.OrbisWalletDexBot) window.OrbisWalletDexBot.mount(root.querySelector("#real-wallet-dex-bot"),walletApi);
    if (window.OrbisEconomicReview) window.OrbisEconomicReview.mount(root.querySelector("#real-wallet-economic-review"),walletApi);
    if (window.OrbisExternalReview) window.OrbisExternalReview.mount(root.querySelector("#real-wallet-external-review"),walletApi);
    root.querySelector("#real-wallet-provider").addEventListener("change", function (event) { selected = event.target.value; });
    root.querySelector("#real-wallet-account").addEventListener("change", selectAccount);
    root.querySelector("#real-wallet-connect").addEventListener("click", connect);
    root.querySelector("#real-wallet-refresh").addEventListener("click", refresh);
    root.querySelector("#real-wallet-bnb-chain").addEventListener("click", changeBnbNetwork);
    root.querySelector("#real-wallet-disconnect").addEventListener("click", clear);
    root.querySelector("#real-wallet-proof").addEventListener("click", verifyControl);
    root.querySelector("#real-wallet-link-form").addEventListener("submit", saveLink);
    root.querySelector("#real-wallet-links-list").addEventListener("click", unlinkLink);
    root.querySelector("#real-wallet-token-form").addEventListener("submit", queryToken);
    root.querySelector("#real-wallet-usdt-bnb").addEventListener("click",queryBnbUsdt);
    root.querySelector("#real-wallet-dex-form").addEventListener("submit", quoteUniswap);
    root.querySelector("#real-wallet-dex-form").addEventListener("input", function () { clearDexQuote(); paint(); });
    root.querySelector("#real-wallet-dex-form").addEventListener("change", function () { clearDexQuote(); paint(); });
    root.querySelector("#real-wallet-deposit-form").addEventListener("submit", queryMexcDepositDestination);
    root.querySelector("#real-wallet-tx-form").addEventListener("submit", function (event) {
      if (root.querySelector("#real-wallet-tx-direction").value === "withdrawal") checkWithdrawalTx(event);
      else checkDepositTx(event);
    });
    root.querySelector("#real-wallet-tx-direction").addEventListener("change", function () { clearTx(); paint(); });
    if (tokenContract) root.querySelector("#real-wallet-token-contract").value = tokenContract;
    if (tokenObservation) root.querySelector("#real-wallet-token-panel").open = true;
    paint();
    loadLinks();
    window.dispatchEvent(new Event("eip6963:requestProvider"));
    window.setTimeout(function () { discoveryReady = true; paint(); }, 250);
  }
  window.OrbisWallet = { mount: mount, clear: clear, setBookSnapshot: setBookSnapshot, mexcReadiness: mexcReadiness };
})();
