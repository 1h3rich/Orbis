/* Private accounting review. No orders, transfers, approval or trading signatures. */
(function () {
  'use strict';
  var root, client, dialog, key = '', epoch = 0, supported = false, busy = '';
  var rows = [], choices = new Map(), page = 0, proposal = null, submission = null;
  var unknown = false, notFound = false, expectedId = null, rootAccount = null, saved = null, message = '', lastHidden = false;
  var PAGE_SIZE = 50, KINDS = ['WALLET_SWAP', 'OPERATOR', 'CALL_RESOLUTION'];
  var GAS_KINDS = KINDS.concat(['DEPLOYMENT','CONTROL','GAS_FUNDING','GAS_RETURN','DEPLOYMENT_RESOLUTION','NATIVE_RESOLUTION']);
  var gasSupported = false, includeGas = false;
  var orderSupported = false, orderGroups = null, orderRevision = null, orderChoices = new Map(), orderPage = 0, orderNodePage = 0;
  var walletSupported = false, walletScope = null, extraWallets = new Map(), walletDraft = null, walletWatches = new Map();
  var UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
  function esc(value) { return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) {
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
  }); }
  function snapshot() { return client && client.snapshot(); }
  function identity(s) { return s ? JSON.stringify([s.accountId, s.wallet && s.wallet.toLowerCase(), s.chain, s.generation, s.verified]) : ''; }
  function ready(s) { return !!(s && UUID.test(s.accountId) && /^0x[0-9a-fA-F]{40}$/.test(s.wallet) && s.chain === '0x38' && s.verified); }
  function rowKey(row) { return row.source_kind + ':' + row.source_id; }
  function current(operation) {
    var s = snapshot();
    return root && root.isConnected && dialog && dialog.open && operation.epoch === epoch &&
      identity(s) === operation.key && s.provider === operation.provider;
  }
  function begin(kind, text) {
    busy = kind; message = text; paintActions();
    var s = snapshot();
    return {epoch:++epoch, key:identity(s), provider:s.provider, source:s};
  }
  function path(s, action) { return '/accounts/' + encodeURIComponent(s.accountId) + '/economic-review/' + action; }
  function post(url, body, passive) { return client.request(url, {method:'POST', body:JSON.stringify(body)}, !!passive); }
  function finish(operation, error) {
    if (!current(operation)) return;
    busy = ''; if (error) message = error.message || 'No se pudo completar la revisión.';
    paint();
  }
  function decimalText(raw) {
    if (typeof raw !== 'string') return null;
    if (/^[+-]?\d+(\.\d+)?$/.test(raw)) return raw;
    var match=/^([+-]?)(\d+)(?:\.(\d+))?[eE]([+-]?\d{1,5})$/.exec(raw);
    if (!match) return null;
    // Only the bounded exponent is a Number. Financial digits remain text.
    var exponent=Number(match[4]); if (Math.abs(exponent)>10000) return null;
    var digits=match[2]+(match[3]||''), point=match[2].length+exponent;
    if (point<=0) return match[1]+'0.'+'0'.repeat(-point)+digits;
    if (point>=digits.length) return match[1]+digits+'0'.repeat(point-digits.length);
    return match[1]+digits.slice(0,point)+'.'+digits.slice(point);
  }
  function money(raw) {
    if (snapshot().hidden) return '••••';
    raw = String(raw == null ? '0' : raw);
    var plain=decimalText(raw); if (plain===null) return '—';
    var formatter = window.OrbisUI && window.OrbisUI.fmtDecimal;
    if (!formatter) return '<span data-help="'+esc(raw)+'">'+esc(plain)+'</span>';
    var shown = formatter(plain, 0, 8);
    if (shown === null) return '—';
    if (/\.\d{9,}$/.test(plain)) {
      if (shown === '0' && /[1-9]/.test(plain)) shown = (plain[0] === '-' ? '−' : '') + '<0,00000001';
      else shown = '≈ ' + shown;
    }
    return '<span data-help="' + esc(raw) + '">' + esc(shown) + '</span>';
  }
  function time(raw) { return esc(String(raw).slice(0,16).replace('T',' ')) + ' UTC'; }
  function selected() { return rows.filter(function (r) { return !r.booked && choices.get(rowKey(r)).selected; }); }
  function needsProof(row) { return !row.gas_only || row.booked || choices.get(rowKey(row)).selected; }
  function requiredContexts() {
    var contexts = new Map();
    rows.forEach(function (r) {
      if ((!r.order_checked && needsProof(r) || !r.booked && choices.get(rowKey(r)).selected) && !r.context_verified) {
        var wallet=walletScope && walletScope.wallets.find(function (w) { return w.id===r.wallet_link_id; });
        contexts.set(walletSupported ? r.wallet_link_id+'/'+r.context_account_id : r.context_account_id,
          (wallet?wallet.alias+' · ':'')+r.context_name);
      }
    });
    return contexts;
  }
  function walletFor(row, s) { var item=extraWallets.get(row.wallet_link_id);return item ? item.address : s.wallet; }
  function providerFor(row, s) { var item=extraWallets.get(row.wallet_link_id);return item ? item.provider : s.provider; }
  function missingWallets() { return !!(walletScope && walletScope.missing_receipts.length); }
  function releaseWallets() {
    walletWatches.forEach(function (handler,provider) {
      if (typeof provider.removeListener==='function') ['accountsChanged','chainChanged','disconnect'].forEach(function (event) { provider.removeListener(event,handler); });
    });
    walletWatches.clear();extraWallets.clear();walletDraft=null;walletScope=null;
  }
  function watchWallet(provider) {
    if (walletWatches.has(provider)) return;
    if (typeof provider.on!=='function' || typeof provider.removeListener!=='function') throw new Error('Este proveedor no permite vigilar cambios de cuenta o red.');
    var handler=function () { clear();update(); };
    walletWatches.set(provider,handler);
    ['accountsChanged','chainChanged','disconnect'].forEach(function (event) { provider.on(event,handler); });
  }
  function ambiguous() {
    return selected().some(function (r) {
      var choice = choices.get(rowKey(r));
      return r.trade_candidates.length && !choice.trade ||
        r.gas_candidates.length && !choice.gas.length && !choice.newGas;
    });
  }
  function button(action, label, disabled, primary) {
    return '<button type="button" data-er-action="' + action + '"' + (primary ? '' : ' class="ghost"') +
      (disabled ? ' disabled' : '') + '>' + label + '</button>';
  }
  function paintActions() {
    if (!dialog || !dialog.open) return;
    dialog.querySelector('[data-er-message]').textContent = message;
    dialog.querySelector('[data-er-close]').disabled = busy === 'confirm';
    var actions = '';
    if (unknown) actions = (notFound ? button('fresh','Volver a revisar',!!busy,false) : '') + button('recover','Comprobar estado',!!busy,true);
    else if (saved) actions = button('reload','Nueva revisión',!!busy,false);
    else if (proposal) {
      actions = button('back','Volver',!!busy,false) + button('confirm','Aceptar criterio',
        !!busy || snapshot().hidden || !proposal.operating.can_accept,true);
    } else if (orderGroups !== null) {
      actions = button('receipts','Recibos',!!busy,false) + button('order-reload','Actualizar orden',!!busy,false) + button('prepare','Revisar',!!busy,true);
    } else {
      actions = button('reload','Actualizar',!!busy,false) + button('prepare','Revisar',
        !!busy || !rows.length || ambiguous() || requiredContexts().size > 0 || missingWallets() || !!walletDraft,true);
    }
    dialog.querySelector('[data-er-actions]').innerHTML = actions;
  }
  function sourceMarkup(row) {
    var id = rowKey(row), choice = choices.get(id), fields = '';
    var wallet=walletScope && walletScope.wallets.find(function (w) { return w.id===row.wallet_link_id; });
    if (!row.booked) {
      if (row.trade_candidates.length) {
        fields += '<label class="field">Asiento de ' + (row.action === 'BUY' ? 'compra' : 'venta') +
          '<select data-er-trade data-er-key="' + id + '"><option value="">Elegir asiento</option>' +
          row.trade_candidates.map(function (c) { return '<option value="' + c.id + '"' + (choice.trade === c.id ? ' selected' : '') +
            '>' + esc(c.occurred_at.slice(0,19).replace('T',' ')) + ' UTC · ' + esc(c.id.slice(0,8)) + '</option>'; }).join('') +
          '<option value="new"' + (choice.trade === 'new' ? ' selected' : '') + '>Otra operación · crear asiento</option></select></label>';
      }
      if (row.gas_candidates.length) {
        fields += '<fieldset class="economic-gas"><legend>Asientos de gas</legend>' + row.gas_candidates.map(function (c) {
          return '<label><input type="checkbox" data-er-gas="' + c.id + '" data-er-key="' + id + '"' +
            (choice.gas.includes(c.id) ? ' checked' : '') + '> ' + money(c.amount) + ' BNB · ' + esc(c.id.slice(0,8)) + '</label>';
        }).join('') + '<label><input type="checkbox" data-er-new-gas data-er-key="' + id + '"' +
          (choice.newGas ? ' checked' : '') + '> Otro gasto · crear gas restante</label></fieldset>';
      }
    }
    var reasons={APPROVE:'Permiso',REVOKE:'Revocación',STOP:'Parada',CANCELLED:'Cancelación',REVERTED:'Reversión'};
    var kinds={DEPLOYMENT:'Contrato',CONTROL:'Control',GAS_FUNDING:'Envío',GAS_RETURN:'Devolución',
      DEPLOYMENT_RESOLUTION:'Contrato',NATIVE_RESOLUTION:'Movimiento',CALL_RESOLUTION:'Llamada',OPERATOR:'Operadora',WALLET_SWAP:'Llamada'};
    var label=row.gas_only ? 'Gas · '+(reasons[row.gas_action]||kinds[row.source_kind]) : (row.action==='BUY'?'Compra':'Venta')+' · WBNB';
    var values=row.gas_only ? money(row.gas_amount)+' BNB' : money(row.quantity)+' WBNB · '+money(row.gross_amount)+' USDT <span class="muted"> · Gas '+money(row.gas_amount)+' BNB</span>';
    return '<article class="economic-source" data-er-source="' + id + '"><header><label>' +
      (row.booked ? '<span class="economic-mark">✓</span>' : '<input type="checkbox" data-er-selected data-er-key="' + id + '"' + (choice.selected ? ' checked' : '') + '>') +
      '<strong' + (row.gas_only?' data-help="Coste de red del recibo. El principal transferido se registra por separado."':'') + '>' + esc(label) + '</strong></label><span class="muted">' +
      (row.booked ? 'Registrado · ' : '') + (row.order_checked ? 'Bloque ✓' : 'Bloque pendiente') + '</span></header>' +
      '<p class="muted">' + time(row.occurred_at) + ' · ' + (wallet?esc(wallet.alias)+' · ':'') + esc(row.context_name) + ' · ' + esc(row.source_id.slice(0,8)) + '</p>' +
      '<p>' + values + '</p>' +
      (fields ? '<div data-er-fields' + (choice.selected ? '' : ' hidden') + '>' + fields + '</div>' : '') + '</article>';
  }
  function sourcesMarkup() {
    var contexts = requiredContexts(), html = '';
    if (walletSupported && walletScope) html+=walletMarkup();
    if (gasSupported) html += '<label class="economic-type-toggle" data-help="Incluye los recibos de gas y los gastos ya registrados. Los criterios aceptados se conservan."><input type="checkbox" data-er-include-gas'+(includeGas?' checked':'')+'> Gas</label>';
    if (contexts.size) html += '<div class="economic-contexts">' + Array.from(contexts).map(function (entry) {
      return button('verify:' + entry[0], 'Verificar contexto · ' + esc(entry[1]),!!busy,false);
    }).join('') + '</div>';
    html += '<div class="economic-sources">' + rows.slice(page*PAGE_SIZE,(page+1)*PAGE_SIZE).map(sourceMarkup).join('') + '</div>';
    if (!rows.length) html += '<p class="muted">Sin recibos '+(includeGas?'para esta wallet.':'de compra o venta para esta wallet.')+'</p>';
    if (rows.length > PAGE_SIZE) html += '<nav class="economic-pages" aria-label="Páginas de recibos">' +
      button('previous','Anterior',!!busy || !page,false) + '<span>' + (page+1) + ' / ' + Math.ceil(rows.length/PAGE_SIZE) + '</span>' +
      button('next','Siguiente',!!busy || (page+1)*PAGE_SIZE >= rows.length,false) + '</nav>';
    return html;
  }
  function walletMarkup() {
    var html='<section class="economic-wallets"><h3>Wallets</h3><div class="economic-contexts">'+walletScope.wallets.map(function (item) {
      return '<span>'+esc(item.alias)+' · '+esc(item.account_name)+' '+(item.provided ? '<span class="economic-mark">✓</span>' :
        button('wallet:'+item.id,'Conectar',!!busy,false))+'</span>';
    }).join('')+'</div>';
    if (missingWallets()) html+='<p class="economic-warning" role="status">'+walletScope.missing_receipts.length+' recibos de otras wallets pendientes de verificar.</p>';
    if (walletDraft) {
      html+='<div class="economic-wallet-connect"><label class="field">Proveedor<select data-er-wallet-provider>'+walletDraft.providers.map(function (item,index) {
        return '<option value="'+index+'"'+(walletDraft.providerIndex===index?' selected':'')+'>'+esc(item.name)+'</option>';
      }).join('')+'</select></label>'+button('wallet-connect','Elegir cuenta',!!busy||!walletDraft.providers.length,false);
      if (walletDraft.accounts.length) html+='<label class="field">Cuenta<select data-er-wallet-address>'+walletDraft.accounts.map(function (address,index) {
        return '<option value="'+index+'"'+(walletDraft.accountIndex===index?' selected':'')+'>'+esc(address.slice(0,8)+'…'+address.slice(-6))+'</option>';
      }).join('')+'</select></label>'+button('wallet-verify','Verificar wallet',!!busy,true);
      html+=button('wallet-cancel','Cancelar',!!busy,false)+'</div>';
    }
    return html+'</section>';
  }
  function comparisonMarkup(account) {
    var before = account.current, after = account.network, lines = '';
    function line(label, a, b, unit) { lines += '<tr><th scope="row">' + esc(label) + '</th><td data-label="Actual">' +
      (before ? money(a) : '—') + ' ' + esc(unit) + '</td><td data-label="Propuesto · red">' + money(b) + ' ' + esc(unit) + '</td></tr>'; }
    [['cash','Efectivo'],['contributions','Aportaciones'],['withdrawals','Retiradas'],['realized_pnl','Resultado realizado'],['cash_fees','Comisiones de caja']].forEach(function (item) {
      var a = before && before[item[0]] || {}, b = after[item[0]] || {};
      Array.from(new Set(Object.keys(a).concat(Object.keys(b)))).sort().forEach(function (unit) { line(item[1],a[unit]||'0',b[unit]||'0',unit); });
    });
    var positions = new Map();
    (before && before.positions || []).forEach(function (p) { positions.set(p.asset+':'+p.quote_currency,{before:p}); });
    after.positions.forEach(function (p) { var id=p.asset+':'+p.quote_currency, pair=positions.get(id)||{}; pair.after=p; positions.set(id,pair); });
    positions.forEach(function (pair, id) {
      var a=pair.before||{}, b=pair.after||{}, parts=id.split(':');
      line('Unidades',a.quantity||'0',b.quantity||'0',parts[0]);
      line('Coste · '+parts[0],a.cost_basis||'0',b.cost_basis||'0',parts[1]);
    });
    return '<details class="economic-account"' + (account.account_id === proposal.root_account_id ? ' open' : '') +
      '><summary>' + esc(account.name) + ' <span class="muted">' + (account.changed ? 'Cambio propuesto' : 'Sin cambios') + '</span></summary>' +
      (account.current_error ? '<p class="economic-warning">Libro actual sin cálculo: ' + esc(account.current_error) + '</p>' : '') +
      '<div class="table-scroll"><table><thead><tr><th>Concepto</th><th>Actual</th><th>Propuesto · red</th></tr></thead><tbody>' + lines + '</tbody></table></div></details>';
  }
  function reviewMarkup() {
    var review=proposal.review, blockers=proposal.operating.blockers;
    var labels={BOT_ACTIVE:'Bot activo',ORDER_PENDING:'Orden pendiente',TRANSFER_PENDING:'Traslado pendiente',WALLET_PENDING:'Solicitud de wallet pendiente'};
    return '<p class="economic-counts">' + review.checked_receipts + ' recibos · ' + review.events_to_create + ' asientos nuevos</p>' +
      (blockers.length ? '<div class="economic-warning" role="status">' + blockers.map(function (b) {
        return esc(labels[b.code]||'Operativa pendiente')+' · '+esc(b.kind)+' · '+esc(b.id.slice(0,8));
      }).join('<br>') + '</div>' : '') + review.accounts.map(comparisonMarkup).join('');
  }
  function orderKey(group) { return group.account_id+'/'+group.at; }
  function canMove(choice, index, delta) {
    var next=index+delta;
    return choice.fixed && next>=0 && next<choice.nodes.length &&
      !(choice.nodes[index].type==='CHAIN' && choice.nodes[next].type==='CHAIN');
  }
  function orderMarkup() {
    if (!orderGroups.length) return '<p class="muted">Sin movimientos simultáneos.</p>';
    var group=orderGroups[orderPage], choice=orderChoices.get(orderKey(group));
    var labels={BUY:'Compra',SELL:'Venta',FEE:'Gas / comisión',CONTRIBUTION:'Aportación',WITHDRAWAL:'Retirada',
      TRANSFER_OUT:'Salida de activo',TRANSFER_IN:'Entrada de activo',CASH_TRANSFER_OUT:'Salida interna',CASH_TRANSFER_IN:'Entrada interna',
      ADJUSTMENT_IN:'Ajuste de entrada',ADJUSTMENT_OUT:'Ajuste de salida',REWARD:'Recompensa'};
    var offset=orderNodePage*PAGE_SIZE;
    return '<section class="economic-order"><header><div><h3>'+esc(group.name)+'</h3><p class="muted">'+
      esc(group.at.replace('T',' ').replace(/\+00:00$|Z$/,''))+' UTC</p></div><span class="economic-mark">'+
      (choice.fixed?(group.accepted&&!choice.changed?'Orden vigente':'Orden explícito'):'Propuesta sin fijar')+'</span></header>'+
      '<label class="economic-type-toggle" data-help="Fija la posición de los movimientos manuales. Los recibos conservan su orden de bloque. La revisión comprobará los saldos y traslados."><input type="checkbox" data-er-fix-order'+(choice.fixed?' checked':'')+'> Fijar orden</label>'+
      '<ol class="economic-order-nodes" start="'+(offset+1)+'">'+choice.nodes.slice(offset,offset+PAGE_SIZE).map(function (node,index) {
        index+=offset;var label=(labels[node.kind]||'Movimiento')+' · '+node.asset;
        var values=(node.quantity!==null?money(node.quantity)+' '+esc(node.asset):'')+
          (node.amount!==null?' · '+money(node.amount)+' '+esc(node.quote_currency):'');
        return '<li data-er-order-node="'+node.id+'"><div><strong>'+esc(label)+'</strong><p class="muted">'+
          (node.type==='CHAIN'?'Red · Bloque '+esc(node.block_number)+' · #'+esc(node.transaction_index):'Manual · '+esc(node.id.slice(6,14)))+
          '</p><p>'+values+(node.gas_amount!==null?' · Gas '+money(node.gas_amount)+' BNB':'')+'</p></div><div class="economic-order-moves">'+
          ['up','down'].map(function (direction) { var delta=direction==='up'?-1:1;
            return '<button type="button" class="ghost" data-er-action="order-'+direction+'" data-er-node="'+node.id+'" aria-label="'+
              (direction==='up'?'Subir':'Bajar')+' · '+esc(label)+' · '+node.id.slice(-8)+'"'+(!canMove(choice,index,delta)||busy?' disabled':'')+'>'+
              (direction==='up'?'↑':'↓')+'</button>'; }).join('')+'</div></li>';
      }).join('')+'</ol>'+(choice.nodes.length>PAGE_SIZE?'<nav class="economic-pages" aria-label="Movimientos del grupo">'+
        button('order-node-prev','Anterior',!!busy||!orderNodePage,false)+'<span>'+(orderNodePage+1)+' / '+Math.ceil(choice.nodes.length/PAGE_SIZE)+'</span>'+
        button('order-node-next','Siguiente',!!busy||(orderNodePage+1)*PAGE_SIZE>=choice.nodes.length,false)+'</nav>':'')+
      (orderGroups.length>1?'<nav class="economic-pages" aria-label="Grupos simultáneos">'+button('order-prev','Grupo anterior',!!busy||!orderPage,false)+
        '<span>'+(orderPage+1)+' / '+orderGroups.length+'</span>'+button('order-next','Grupo siguiente',!!busy||orderPage+1>=orderGroups.length,false)+'</nav>':'')+'</section>';
  }
  function paint() {
    if (!dialog || !dialog.open) return;
    var body=dialog.querySelector('[data-er-body]');
    if (unknown) body.innerHTML='<p>Confirmación sin respuesta.</p><p class="muted">Consulta el registro antes de iniciar otra revisión.</p>';
    else if (saved) body.innerHTML='<p class="economic-saved">Criterio guardado ✓</p><p class="muted">'+
      (saved.is_current ? 'Vigente en esta cartera.' : 'Guardado · otra revisión es la vigente.')+'</p>';
    else body.innerHTML=proposal ? reviewMarkup() : orderGroups!==null ? orderMarkup() : sourcesMarkup();
    body.querySelectorAll('input,select').forEach(function (el) { el.disabled=!!busy; });
    paintActions();
  }
  function discard() {
    ++epoch; busy=''; rows=[]; choices.clear(); proposal=null; message=''; page=0; rootAccount=null;
    releaseWallets();
    resetOrder();
    if (dialog) { if (dialog.open) dialog.close(); dialog.querySelector('[data-er-body]').replaceChildren();
      dialog.querySelector('[data-er-message]').textContent=''; dialog.querySelector('[data-er-account]').textContent=''; }
  }
  function clear() { discard(); includeGas=false; submission=null; expectedId=null; unknown=false; notFound=false; saved=null; key=''; }
  function update() {
    if (!root || !root.isConnected) { clear(); return; }
    var s=snapshot(), next=identity(s);
    if (key !== next) { clear(); key=next; }
    var entry=root.querySelector('[data-er-open]'); entry.hidden=!supported;
    entry.disabled=!ready(s);
    if (lastHidden !== !!s.hidden) { lastHidden=!!s.hidden; paint(); }
    paintActions();
  }
  function close() { if (busy !== 'confirm') discard(); }
  function resetOrder() { orderGroups=null; orderRevision=null; orderChoices.clear(); orderPage=0; orderNodePage=0; }
  function validSources(data) {
    var ids=new Set();
    return data && UUID.test(data.root_account_id) && (!walletSupported || validWalletScope(data.wallet_scope)) && Array.isArray(data.sources) && data.sources.length<=100000 && data.sources.every(function (r) {
      if (!r || ids.has(rowKey(r))) return false; ids.add(rowKey(r));
      var profile=r.gas_only===true ? gasSupported && includeGas && GAS_KINDS.includes(r.source_kind) && r.action==='GAS' &&
        typeof r.gas_action==='string' && r.quantity===null && r.gross_amount===null && Array.isArray(r.trade_candidates) && !r.trade_candidates.length :
        (r.gas_only===undefined||r.gas_only===false) && KINDS.includes(r.source_kind) && ['BUY','SELL'].includes(r.action);
      return profile && UUID.test(r.source_id) && UUID.test(r.context_account_id) && (!walletSupported ||
        UUID.test(r.wallet_account_id) && data.wallet_scope.wallets.some(function (w) { return w.id===r.wallet_link_id && w.account_id===r.wallet_account_id && w.provided; })) &&
        decimalText(r.gas_amount)!==null && typeof r.booked==='boolean' && typeof r.order_checked==='boolean' &&
        typeof r.context_verified==='boolean' && typeof r.occurred_at==='string' && Array.isArray(r.trade_candidates) && Array.isArray(r.gas_candidates) &&
        r.trade_candidates.every(function (c) { return UUID.test(c.id) && typeof c.occurred_at==='string'; }) &&
        r.gas_candidates.every(function (c) { return UUID.test(c.id) && typeof c.occurred_at==='string'; });
    });
  }
  function validWalletScope(value) {
    var ids=new Set(), receipts=new Set();
    return value && value.schema==='orbis.portfolio.economic.wallet.scope.v1' && value.moves_funds===false && value.trading_authority===false &&
      Array.isArray(value.wallets) && value.wallets.length>0 && value.wallets.length<=10000 && value.wallets.every(function (w) {
        if (!w || !UUID.test(w.id) || ids.has(w.id) || !UUID.test(w.account_id) || typeof w.alias!=='string' || typeof w.account_name!=='string' || typeof w.provided!=='boolean') return false;
        ids.add(w.id);return true;
      }) && Array.isArray(value.missing_receipts) && value.missing_receipts.length<=100000 && value.missing_receipts.every(function (r) {
        if (!r || !GAS_KINDS.includes(r.source_kind) || !UUID.test(r.source_id) || receipts.has(rowKey(r))) return false;
        receipts.add(rowKey(r));return true;
      });
  }
  function validSnapshot(value, account) {
    return value && value.account_id===account && ['cash','contributions','withdrawals','realized_pnl','cash_fees'].every(function (name) {
      var values=value[name]; return values && typeof values==='object' && !Array.isArray(values) &&
        Object.keys(values).every(function (unit) { return decimalText(values[unit])!==null; });
    }) && Array.isArray(value.positions) && value.positions.every(function (p) {
      return p && typeof p.asset==='string' && typeof p.quote_currency==='string' && decimalText(p.quantity)!==null && decimalText(p.cost_basis)!==null;
    });
  }
  function validPreview(data) {
    return data && data.saved===false && UUID.test(data.id) && /^[0-9a-f]{64}$/.test(data.preview_digest) &&
      data.root_account_id===rootAccount && ['EXISTING','BATCH'].includes(data.mode) &&
      (data.mode==='BATCH' ? UUID.test(data.batch_id) : data.batch_id===null) && typeof data.recorded_at==='string' &&
      data.review && ['checked_receipts','events_to_create'].every(function (name) { return Number.isSafeInteger(data.review[name]) && data.review[name]>=0; }) &&
      Array.isArray(data.review.accounts) && data.review.accounts.length>0 && data.review.accounts.every(function (a) {
        return a && UUID.test(a.account_id) && typeof a.name==='string' && typeof a.changed==='boolean' &&
          validSnapshot(a.network,a.account_id) && (a.current===null ? typeof a.current_error==='string' : validSnapshot(a.current,a.account_id));
      }) && data.operating && typeof data.operating.can_accept==='boolean' && Array.isArray(data.operating.blockers) &&
      data.operating.blockers.every(function (b) { return b && typeof b.code==='string' && typeof b.kind==='string' && UUID.test(b.id); });
  }
  function validOptions(data) {
    var groups=new Set(), allNodes=new Set(), nodePattern=/^(event|chain):[0-9a-f-]{36}$/;
    return data && data.schema==='orbis.portfolio.economic.order.options.v1' && /^[0-9a-f]{64}$/.test(data.revision) &&
      data.root_account_id===rootAccount && data.moves_funds===false && data.trading_authority===false && data.saved===false &&
      Array.isArray(data.groups) && data.groups.length<=10000 && data.groups.every(function (g) {
        if (!g || !UUID.test(g.account_id) || typeof g.at!=='string' || g.at.length>40 || typeof g.name!=='string' || typeof g.accepted!=='boolean' || groups.has(orderKey(g))) return false;
        groups.add(orderKey(g));var ids=new Set(), chain=new Set();
        if (!Array.isArray(g.nodes) || g.nodes.length<2 || g.nodes.length>10000 || !g.nodes.every(function (n) {
          if (!n || !nodePattern.test(n.id) || !UUID.test(n.id.split(':')[1]) || ids.has(n.id) || allNodes.has(n.id) || !['MANUAL','CHAIN'].includes(n.type) ||
              (n.type==='CHAIN')!==n.id.startsWith('chain:') || typeof n.kind!=='string' || typeof n.asset!=='string' || typeof n.quote_currency!=='string') return false;
          ids.add(n.id);allNodes.add(n.id);if (n.type==='CHAIN') chain.add(n.id);
          return ['quantity','amount','gas_amount'].every(function (k) { return n[k]===null || decimalText(n[k])!==null; }) &&
            (n.type!=='CHAIN' || typeof n.block_number==='string' && /^\d{1,78}$/.test(n.block_number) && typeof n.transaction_index==='string' && /^\d{1,6}$/.test(n.transaction_index));
        })) return false;
        return chain.size<g.nodes.length && Array.isArray(g.chain_order) && new Set(g.chain_order).size===chain.size &&
          g.chain_order.length===chain.size && g.chain_order.every(function (id) { return chain.has(id); }) &&
          JSON.stringify(g.nodes.filter(function (n) { return n.type==='CHAIN'; }).map(function (n) { return n.id; }))===JSON.stringify(g.chain_order);
      });
  }
  async function load(focusGas) {
    if (busy || unknown) return;
    proposal=null; saved=null; submission=null; expectedId=null; notFound=false; rows=[]; choices.clear(); page=0;
    resetOrder();
    var op=begin('load','Cargando recibos…'); paint();
    try {
      var query={address:op.source.wallet}; if (gasSupported && includeGas) query.include_gas=true;
      if (walletSupported) { query.include_wallets=true;query.wallets=walletContexts(); }
      var data=await post(path(op.source,'sources'),query,true);
      if (!current(op)) return;
      if (!validSources(data)) throw new Error('Catálogo de recibos inválido.');
      rows=data.sources; rootAccount=data.root_account_id;walletScope=walletSupported ? data.wallet_scope : null;
      rows.forEach(function (r) { choices.set(rowKey(r),{selected:!r.booked,trade:'',gas:[],newGas:false}); });
      message=rows.length ? 'Selecciona los recibos que quieras revisar.' : '';
      dialog.querySelector('[data-er-account]').textContent=data.root_account_name;
      finish(op);
      if (focusGas) { var toggle=dialog.querySelector('[data-er-include-gas]'); if (toggle) toggle.focus(); }
    } catch (error) { finish(op,error); }
  }
  function selectionBody(s) {
    var result={address:s.wallet,receipts:selected().map(function (r) {
      var c=choices.get(rowKey(r));
      return {source_kind:r.source_kind,source_id:r.source_id,context_account_id:r.context_account_id,
        trade_event_id:!r.gas_only && c.trade && c.trade!=='new' ? c.trade : null,gas_event_ids:c.gas.slice(),
        confirm_new_trade:!r.gas_only && c.trade==='new',confirm_new_gas:c.newGas,
        ...(r.gas_only?{gas_only:true}:{}),...(walletSupported?{wallet_address:walletFor(r,s)}:{})};
    })};
    if (walletSupported) result.wallets=walletContexts();
    if (gasSupported && includeGas) result.include_gas=true;
    if (orderSupported && orderRevision) {
      result.order_options_digest=orderRevision;
      result.tie_orders=orderGroups.filter(function (g) { return orderChoices.get(orderKey(g)).fixed; }).map(function (g) {
        return {account_id:g.account_id,at:g.at,nodes:orderChoices.get(orderKey(g)).nodes.map(function (n) { return n.id; })};
      });
    }
    return result;
  }
  function walletContexts() { return Array.from(extraWallets.values(),function (item) { return {context_account_id:item.accountId,address:item.address}; }); }
  async function fetchOrder(op) {
    var body=selectionBody(op.source);delete body.order_options_digest;delete body.tie_orders;
    var data=await post(path(op.source,'order-options'),body,false);
    if (!current(op)) return false;
    if (!validOptions(data)) throw new Error('Opciones de orden inválidas.');
    orderGroups=data.groups;orderRevision=data.revision;orderChoices.clear();orderPage=0;orderNodePage=0;
    orderGroups.forEach(function (g) { orderChoices.set(orderKey(g),{fixed:g.accepted,changed:false,nodes:g.nodes.slice()}); });
    return true;
  }
  async function reloadOrder() {
    if (busy || !orderSupported || orderGroups===null) return;
    var op=begin('order','Actualizando orden…');paint();
    try { if (await fetchOrder(op)) { message=orderGroups.length?'Revisa los movimientos simultáneos.':'Sin movimientos simultáneos.';finish(op); } }
    catch (error) { resetOrder();finish(op,error); }
  }
  async function prepare() {
    if (busy || ambiguous() || requiredContexts().size || !rows.length || missingWallets() || walletDraft) return;
    var op=begin('prepare','Comprobando recibos…'); paint();
    try {
      // Financial chronology needs all known executions; fee-only rows need
      // their proof when already booked or selected for this review.
      var pending=rows.filter(function (r) { return !r.order_checked && needsProof(r); });
      for (var i=0;i<pending.length;i++) {
        var r=pending[i];
        if (!window.OrbisDexReceiptOrder) throw new Error('Comprobación de bloques no disponible.');
        await window.OrbisDexReceiptOrder.check(client,{accountId:r.context_account_id,wallet:walletFor(r,op.source)},r.source_kind,r.source_id,i>0,r.gas_only===true);
        if (!current(op)) return;
        r.order_checked=true; message='Bloques comprobados · '+(i+1)+' / '+pending.length; paintActions();
      }
      if (orderSupported && !orderRevision) {
        if (!await fetchOrder(op)) return;
        if (orderGroups.length) { message='Revisa los movimientos simultáneos.';finish(op);return; }
      }
      var body=selectionBody(op.source), data=await post(path(op.source,'preview'),body,false);
      if (!current(op)) return;
      if (!validPreview(data)) throw new Error('Revisión sin confirmar.');
      proposal=data; expectedId=data.id;
      submission=Object.assign({},body,{mode:data.mode,batch_id:data.batch_id,recorded_at:data.recorded_at,
        preview_digest:data.preview_digest,confirmation:'ACEPTAR_CRITERIO_CONTABLE'});
      message=data.operating.can_accept ? 'Revisa los cambios antes de aceptar.' : 'Resuelve la operativa pendiente y actualiza la revisión.';
      finish(op);
    } catch (error) { finish(op,error); }
  }
  async function verifyContext(id) {
    if (busy || !requiredContexts().has(id)) return;
    var op=begin('verify','Preparando prueba de contexto…'); paint();
    try {
      var row=walletSupported ? rows.find(function (r) { return r.wallet_link_id+'/'+r.context_account_id===id; }) : null;
      var address=row?walletFor(row,op.source):op.source.wallet,provider=row?providerFor(row,op.source):op.provider;
      var context=row?row.context_account_id:id;
      await proveControl(op,context,address,provider);
      if (!current(op)) return;
      rows.forEach(function (r) { if (r.context_account_id===context && walletFor(r,op.source).toLowerCase()===address.toLowerCase()) r.context_verified=true; });
      message='Contexto verificado.'; finish(op);
    } catch (error) { finish(op,error); }
  }
  async function proveControl(op, context, address, provider) {
      if (!current(op)) return;
      var base='/accounts/'+encodeURIComponent(context)+'/wallet-proof';
      var challenge=await post(base+'/start',{address:address,chain_id:'0x38'},false);
      if (!current(op)) return;
      if (challenge.purpose!=='SESSION_EOA_CONTROL_ONLY' || !/^[0-9a-f]{64}$/.test(challenge.nonce) ||
          typeof challenge.message!=='string' || challenge.message.length>1000) throw new Error('Desafío local inválido.');
      if (!window.confirm('Verificar contexto · sólo prueba de control, sin permiso de gasto.\n\n'+challenge.message)) {
        throw new Error('Verificación cancelada.');
      }
      if (!current(op)) return;
      var encoded='0x'+Array.from(new TextEncoder().encode(challenge.message),function (byte) { return byte.toString(16).padStart(2,'0'); }).join('');
      if (walletSupported) { await checkWallet(provider,address);if (!current(op)) return; }
      var signature=await provider.request({method:'personal_sign',params:[encoded,address]});
      if (!current(op)) return;
      if (typeof signature!=='string' || !/^0x[0-9a-fA-F]{130}$/.test(signature)) throw new Error('Firma inválida.');
      if (walletSupported) { await checkWallet(provider,address);if (!current(op)) return; }
      var result=await post(base+'/confirm',{address:address,chain_id:'0x38',nonce:challenge.nonce,signature:signature},false);
      if (!current(op)) return;
      if (result.verified!==true || result.spending_authorized!==false) throw new Error('Contexto sin verificar.');
  }
  async function checkWallet(provider,address) {
    var permitted=await provider.request({method:'eth_accounts'});
    if (!Array.isArray(permitted) || !permitted.some(function (a) { return typeof a==='string' && a.toLowerCase()===address.toLowerCase(); }) ||
        await provider.request({method:'eth_chainId'})!=='0x38') throw new Error('La cuenta o la red de la wallet cambió.');
  }
  function chooseWallet(id) {
    if (!walletSupported || !walletScope || !walletScope.wallets.some(function (w) { return w.id===id && !w.provided; })) return;
    var providers=typeof client.reviewProviders==='function' ? client.reviewProviders() : [{name:'Wallet conectada',provider:snapshot().provider}];
    walletDraft={id:id,providers:providers.filter(function (p) { return p && typeof p.name==='string' && p.provider && typeof p.provider.request==='function'; }).slice(0,20),
      providerIndex:0,accounts:[],accountIndex:0};
    resetOrder();message='Elige la cuenta vinculada a esta wallet.';paint();
  }
  async function connectWallet() {
    if (!walletDraft || !walletDraft.providers.length) return;
    var draft=walletDraft,provider=draft.providers[draft.providerIndex].provider,op=begin('wallet','Consultando cuentas autorizadas…');paint();
    try {
      var accounts=await provider.request({method:'eth_requestAccounts'});
      if (!current(op)) return;
      if (!Array.isArray(accounts) || !accounts.length || accounts.length>100 || !accounts.every(function (a) { return typeof a==='string' && /^0x[0-9a-fA-F]{40}$/.test(a); }) ||
          new Set(accounts.map(function (a) { return a.toLowerCase(); })).size!==accounts.length) throw new Error('Cuentas de wallet inválidas.');
      if (await provider.request({method:'eth_chainId'})!=='0x38') throw new Error('Selecciona BNB Chain en esa wallet.');
      if (!current(op)) return;
      watchWallet(provider);draft.accounts=accounts;draft.accountIndex=0;message='Selecciona la cuenta y verifica su control.';finish(op);
    } catch (error) { finish(op,error); }
  }
  async function verifyWallet() {
    if (!walletDraft || !walletDraft.accounts.length) return;
    var draft=walletDraft,item=walletScope.wallets.find(function (w) { return w.id===draft.id; });
    var address=draft.accounts[draft.accountIndex],provider=draft.providers[draft.providerIndex].provider,op=begin('wallet','Verificando wallet…');paint();
    try {
      var permitted=await provider.request({method:'eth_accounts'});
      if (!current(op)) return;
      if (!Array.isArray(permitted) || !permitted.some(function (a) { return typeof a==='string' && a.toLowerCase()===address.toLowerCase(); }) ||
          await provider.request({method:'eth_chainId'})!=='0x38') throw new Error('La cuenta o la red de la wallet cambió.');
      await proveControl(op,item.account_id,address,provider);
      if (!current(op)) return;
      var query={address:op.source.wallet,include_wallets:true,wallets:walletContexts().concat([{context_account_id:item.account_id,address:address}])};
      if (gasSupported && includeGas) query.include_gas=true;
      var data=await post(path(op.source,'sources'),query,true);
      if (!current(op)) return;
      if (!validSources(data) || data.root_account_id!==rootAccount) throw new Error('Catálogo de wallets inválido.');
      if (!data.wallet_scope.wallets.some(function (w) { return w.id===item.id && w.provided; })) throw new Error('La cuenta elegida no corresponde a esta wallet.');
      extraWallets.set(item.id,{accountId:item.account_id,address:address,provider:provider});walletDraft=null;walletScope=data.wallet_scope;
      rows=data.sources;choices.clear();page=0;resetOrder();
      rows.forEach(function (r) { choices.set(rowKey(r),{selected:!r.booked,trade:'',gas:[],newGas:false}); });
      message='Wallet verificada.';finish(op);
    } catch (error) { finish(op,error); }
  }
  function acceptResult(result) {
    if (!result || result.saved!==true || result.id!==expectedId || typeof result.is_current!=='boolean')
      throw new Error('Resultado de aceptación sin confirmar.');
    saved=result; unknown=false; notFound=false; proposal=null; submission=null; expectedId=null; message='';
    rows=[]; choices.clear();
    if (typeof client.onBooked==='function') { try { client.onBooked(); } catch (_) { message='Guardado · actualiza la cartera.'; } }
  }
  async function confirm() {
    if (busy || unknown || !proposal || !proposal.operating.can_accept || snapshot().hidden) return;
    var op=begin('confirm','Guardando criterio…'); paint();
    try {
      var result=await post(path(op.source,'confirm'),submission,false);
      if (!current(op)) return;
      acceptResult(result); finish(op);
    } catch (error) {
      if (!current(op)) return;
      // A transport error or incomplete successful response can hide a commit.
      // Keep the exact reviewed request in memory and recover by digest only.
      if (!error.status || error.status>=500) { unknown=true; notFound=false; message='Resultado pendiente de comprobar.'; finish(op); }
      else { proposal=null; submission=null; expectedId=null; message=error.message; finish(op); }
    }
  }
  async function recover() {
    if (busy || !unknown || !submission) return;
    var op=begin('recover','Consultando registro…'); paint();
    try {
      var result=await post(path(op.source,'result'),{address:op.source.wallet,preview_digest:submission.preview_digest},true);
      if (!current(op)) return;
      if (!result || result.id!==expectedId || typeof result.saved!=='boolean') throw new Error('Resultado de otra revisión.');
      if (result.saved===true) acceptResult(result);
      else { notFound=true; message='Todavía no consta guardado · vuelve a comprobar o prepara otra revisión.'; }
      finish(op);
    } catch (error) { finish(op,error); }
  }
  function change(event) {
    if (busy || proposal || unknown) return;
    var input=event.target, id=input.dataset.erKey, choice=choices.get(id);
    if (walletDraft && input.hasAttribute('data-er-wallet-provider')) {
      var providerIndex=Number(input.value);if (!Number.isInteger(providerIndex) || providerIndex<0 || providerIndex>=walletDraft.providers.length) return;
      walletDraft.providerIndex=providerIndex;walletDraft.accounts=[];paint();dialog.querySelector('[data-er-wallet-provider]').focus();return;
    }
    if (walletDraft && input.hasAttribute('data-er-wallet-address')) {
      var accountIndex=Number(input.value);if (Number.isInteger(accountIndex) && accountIndex>=0 && accountIndex<walletDraft.accounts.length) walletDraft.accountIndex=accountIndex;return;
    }
    if (input.hasAttribute('data-er-fix-order') && orderGroups && orderGroups.length) {
      orderChoices.get(orderKey(orderGroups[orderPage])).fixed=input.checked;paint();
      dialog.querySelector('[data-er-fix-order]').focus();return;
    }
    if (input.hasAttribute('data-er-include-gas') && gasSupported) { includeGas=input.checked; load(true); return; }
    if (!choice) return;
    resetOrder();
    if (input.hasAttribute('data-er-selected')) {
      choice.selected=input.checked;
      var fields=input.closest('[data-er-source]').querySelector('[data-er-fields]');
      if (fields) fields.hidden=!input.checked;
    } else if (input.hasAttribute('data-er-trade')) choice.trade=input.value;
    else if (input.hasAttribute('data-er-gas')) {
      choice.gas=Array.from(input.closest('.economic-gas').querySelectorAll('[data-er-gas]:checked'),function (el) { return el.dataset.erGas; });
    } else if (input.hasAttribute('data-er-new-gas')) choice.newGas=input.checked;
    paintActions();
  }
  function action(event) {
    var target=event.target.closest('[data-er-action]'); if (!target || target.disabled || busy) return;
    var name=target.dataset.erAction;
    if (name==='reload') load();
    else if (name.startsWith('wallet:')) chooseWallet(name.slice(7));
    else if (name==='wallet-connect') connectWallet();
    else if (name==='wallet-verify') verifyWallet();
    else if (name==='wallet-cancel') { walletDraft=null;message='';paint(); }
    else if (name==='prepare') prepare();
    else if (name==='confirm') confirm();
    else if (name==='recover') recover();
    else if (name==='back') { proposal=null; submission=null; expectedId=null; message=''; paint(); }
    else if (name==='receipts') { resetOrder();message='';paint(); }
    else if (name==='order-reload') reloadOrder();
    else if (name==='order-prev' || name==='order-next') { orderPage+=name==='order-next'?1:-1;orderNodePage=0;paint(); }
    else if (name==='order-node-prev' || name==='order-node-next') { orderNodePage+=name==='order-node-next'?1:-1;paint(); }
    else if ((name==='order-up'||name==='order-down') && orderGroups && orderGroups.length) {
      var choice=orderChoices.get(orderKey(orderGroups[orderPage])),index=choice.nodes.findIndex(function (n) { return n.id===target.dataset.erNode; }),delta=name==='order-up'?-1:1;
      if (index<0 || !canMove(choice,index,delta)) return;
      var node=choice.nodes[index];choice.nodes[index]=choice.nodes[index+delta];choice.nodes[index+delta]=node;
      choice.changed=true;
      orderNodePage=Math.floor((index+delta)/PAGE_SIZE);paint();
      var card=dialog.querySelector('[data-er-order-node="'+node.id+'"]');
      if (card) { var focus=card.querySelector('button:not(:disabled)')||dialog.querySelector('[data-er-fix-order]');if (focus) focus.focus(); }
    }
    else if (name==='fresh' && notFound) { unknown=false; notFound=false; load(); }
    else if (name==='previous' || name==='next') { page+=name==='next' ? 1 : -1; paint(); }
    else if (name.startsWith('verify:')) verifyContext(name.slice(7));
  }
  function mount(element, api) {
    clear(); root=element; client=api; supported=false; gasSupported=false;orderSupported=false;walletSupported=false;
    if (!root) return;
    root.innerHTML='<button type="button" class="ghost economic-review-open" data-er-open hidden data-help="Revisa recibos DEX y su efecto contable antes de registrarlos. No envía operaciones.">Revisar cuenta</button>'+
      '<dialog class="economic-review-dialog" aria-labelledby="economic-review-title"><header class="economic-review-header"><div><p class="eyebrow">CONTABILIDAD · DEX <span data-er-account></span></p>'+
      '<h2 id="economic-review-title">Revisar cuenta</h2></div><button type="button" class="ghost" data-er-close aria-label="Cerrar revisión">Cerrar</button></header>'+
      '<div class="economic-review-body" data-er-body></div><footer><p class="muted" role="status" data-er-message></p><div class="economic-review-actions" data-er-actions></div></footer></dialog>';
    dialog=root.querySelector('dialog');
    root.querySelector('[data-er-open]').addEventListener('click',function () {
      if (!ready(snapshot()) || !supported) return;
      dialog.showModal();
      if (unknown) paint(); else load();
    });
    dialog.querySelector('[data-er-close]').addEventListener('click',close);
    dialog.addEventListener('cancel',function (event) { event.preventDefault(); close(); });
    dialog.addEventListener('click',action); dialog.addEventListener('change',change);
    var mounted=root;
    client.request('/vault/status',null,true).then(function (status) {
      if (root!==mounted || !mounted.isConnected) return;
      supported=!!(status && status.capabilities && status.capabilities.economic_review===true);
      gasSupported=!!(supported && status.capabilities.economic_review_gas===true && status.capabilities.dex_gas_order_proof===true);
      orderSupported=!!(supported && status.capabilities.economic_review_order===true);
      walletSupported=!!(supported && status.capabilities.economic_review_wallets===true);update();
    }).catch(function () {});
    update();
  }
  window.OrbisEconomicReview={mount:mount,update:update,clear:clear};
})();
