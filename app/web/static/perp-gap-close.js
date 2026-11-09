/* Estimated PAPER closure after missing coverage; no observed-book mutation. */
(function () {
  'use strict';
  var pendingKey = 'orbis.paper.perp.gapClose.pending';
  function esc(value) { return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) { return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
  function utc(ns) { return new Date(Number(BigInt(ns) / 1000000000n) * 1000).toISOString().slice(0,19).replace('T',' '); }
  function mount(root, options) {
    var alive = true, busy = false, generation = 0, controller = new AbortController(), review = null, pending = null;
    root.hidden = true;
    root.innerHTML = '<p id="gap-close-status" class="state" role="status"></p>' +
      '<div id="gap-close-pending" class="row" hidden><button type="button" class="ghost" data-gap-action="check">Consultar cierre</button><button type="button" class="ghost" data-gap-action="forget" data-help="Descarta sólo esta consulta local. Un cierre ya confirmado puede terminar y seguirá en la biblioteca; no se envía otro.">Dejar consulta</button></div>' +
      '<section id="gap-close-review" hidden><h4>Cierre estimado · PAPER</h4><div id="gap-close-values" class="perp-run-metrics"></div>' +
      '<label data-help="No conocemos la trayectoria, los stops ni la supervivencia durante el hueco. Se estima conservar la posición hasta los puntos de financiación conocidos y el precio actual. Una liquidación en esos puntos también es estimada."><input type="checkbox" id="gap-close-ack"> Aceptar intervalo desconocido</label>' +
      '<div class="row"><button type="button" data-gap-action="confirm" disabled>Guardar cierre estimado</button><button type="button" class="ghost" data-gap-action="discard">Descartar</button></div></section>' +
      '<section id="gap-close-result" data-quality="ESTIMATED_GAP_CLOSE" hidden><div class="row"><h4>Cierre estimado · PAPER</h4><span id="gap-close-proof" class="statusbadge"></span><a id="gap-close-export" class="ghost" download>Exportar cierre</a></div><div id="gap-close-metrics" class="perp-run-metrics"></div><p id="gap-close-times" class="muted"></p><details><summary>Diario estimado</summary><pre id="gap-close-journal"></pre></details></section>';
    function el(id) { return root.querySelector('#' + id); }
    function current(ticket) { return alive && root.isConnected && ticket === generation; }
    function setBusy(value) { if (busy !== value) { busy = value; options.onBusy(value); } ready(); }
    function ready() {
      root.querySelectorAll('button').forEach(function (button) { button.disabled = busy; });
      root.querySelector('[data-gap-action="confirm"]').disabled = busy || !review || !!pending || !el('gap-close-ack').checked;
    }
    function remember() {
      try { if (pending) sessionStorage.setItem(pendingKey, JSON.stringify(pending)); else sessionStorage.removeItem(pendingKey); } catch (error) {}
      el('gap-close-pending').hidden = !pending;
      ready();
    }
    try {
      var saved = JSON.parse(sessionStorage.getItem(pendingKey));
      if (saved && /^[0-9a-f-]{36}$/.test(saved.session_id) && /^[0-9a-f-]{36}$/.test(saved.request_id) && /^[0-9a-f]{64}$/.test(saved.expected_head)) pending = saved;
    } catch (error) {}
    function status(text) { root.hidden = false; el('gap-close-status').textContent = text; }
    function fail(error) { if (error.status === 423) options.onLocked(error); else status(error.message); }
    function api(path, init) { return options.request(path, Object.assign({signal: controller.signal}, init || {})); }
    function metrics(node, values) { node.innerHTML = values.map(function (item) { return '<div><span class="muted">' + esc(item[0]) + '</span><strong>' + options.amount(item[1]) + '</strong></div>'; }).join(''); }
    function clear() {
      generation++; controller.abort(); controller = new AbortController(); review = null;
      el('gap-close-review').hidden = el('gap-close-result').hidden = true;
      el('gap-close-values').innerHTML = el('gap-close-metrics').innerHTML = '';
      el('gap-close-journal').textContent = el('gap-close-times').textContent = el('gap-close-status').textContent = '';
      el('gap-close-export').removeAttribute('href'); el('gap-close-ack').checked = false;
      if (options.onClear) options.onClear();
      root.hidden = !pending; setBusy(false); remember();
    }
    async function showRecord(value, ticket) {
      if (value.verification !== 'GAP_CLOSE_REPLAY_VERIFIED' || value.quality !== 'ESTIMATED_GAP_CLOSE' || value.execution_authority !== 'NONE') throw new Error('El cierre no está reproducido.');
      await options.onShow(value);
      if (!current(ticket)) return;
      var result = value.result, m = result.metrics, id = value.receipt.source.session_id;
      review = null; el('gap-close-review').hidden = true; el('gap-close-result').hidden = false;
      metrics(el('gap-close-metrics'), [['Patrimonio estimado',m.equity],['Neto estimado',m.net_profit],['Neto de la posición',result.position_net_profit],['Comisiones',m.fees_paid],['Financiación pagada',m.funding_paid]]);
      el('gap-close-proof').textContent = result.status === 'ESTIMATED_LIQUIDATED' ? 'Liquidación estimada' : 'Reproducido';
      el('gap-close-times').textContent = 'Hueco: ' + utc(result.unknown_interval.from_ns) + ' → ' + utc(result.unknown_interval.through_ns) + ' UTC · calculado ' + utc(result.known_at_ns) + ' UTC';
      el('gap-close-journal').textContent = JSON.stringify({assumptions:result.assumptions,events:result.events,journal:result.journal}, null, 2);
      el('gap-close-export').href = '/paper/perp/observed/sessions/' + encodeURIComponent(id) + '/gap-close/export';
      el('gap-close-export').download = 'orbis-paper-gap-close-' + id + '.jsonl';
      if (pending && pending.session_id === id) { pending = null; remember(); }
      status('Cierre guardado');
    }
    async function showSaved(id) {
      if (busy) return;
      clear();
      var ticket = ++generation; setBusy(true); status('Reproduciendo cierre…');
      try { var value = await api('/sessions/' + encodeURIComponent(id) + '/gap-close'); if (current(ticket)) await showRecord(value, ticket); }
      catch (error) { if (current(ticket)) fail(error); }
      finally { if (current(ticket)) setBusy(false); }
    }
    async function open(id) {
      if (busy) return;
      if (pending) { status('Consulta el cierre pendiente antes de confirmar otro.'); return; }
      clear(); var ticket = generation; setBusy(true); status('Verificando posición…');
      try {
        var value = await api('/sessions/' + encodeURIComponent(id));
        if (!current(ticket)) return;
        var row = value.session, cp = row.checkpoint;
        if (row.active_in_this_process || !cp.opened || row.verification !== 'OBSERVED_REPLAY_VERIFIED') throw new Error('La sesión debe estar interrumpida y conservar una posición.');
        if (options.onReview) await options.onReview(value);
        if (!current(ticket)) return;
        review = {session_id:id,expected_head:row.head_hash};
        var size=cp.account.positions[0].size.replace(/^-/,'');
        metrics(el('gap-close-values'), [['Cantidad abierta',size],['Entrada',cp.opened.entry_price]]);
        el('gap-close-values').insertAdjacentHTML('afterbegin','<div><span class="muted">Mercado</span><strong>'+esc(cp.account.markets[0].instrument)+'</strong></div><div><span class="muted">Dirección</span><strong>'+esc(cp.account.positions[0].size.startsWith('-')?'SHORT':'LONG')+'</strong></div>');
        el('gap-close-review').hidden = false; status('Cotización actual al confirmar · resultado estimado.');
      } catch (error) { if (current(ticket)) fail(error); }
      finally { if (current(ticket)) setBusy(false); }
    }
    async function confirm() {
      if (busy || pending || !review || !el('gap-close-ack').checked) return;
      var ticket = ++generation;
      pending = {session_id:review.session_id,expected_head:review.expected_head,request_id:crypto.randomUUID()}; remember(); setBusy(true);
      status('Recogiendo precio y financiación…');
      try {
        var value = await api('/sessions/' + encodeURIComponent(pending.session_id) + '/gap-close', {method:'POST',body:JSON.stringify({request_id:pending.request_id,expected_head:pending.expected_head,acknowledge_gap:true})});
        if (current(ticket)) await showRecord(value, ticket);
      } catch (error) { if (current(ticket)) { fail(error); if (error.status !== 423) status('Respuesta pendiente · ' + error.message); } }
      finally { if (current(ticket)) setBusy(false); }
    }
    function click(event) {
      var button = event.target.closest('[data-gap-action]');
      if (!button || button.disabled || busy) return;
      var action = button.dataset.gapAction;
      if (action === 'confirm') confirm();
      else if (action === 'check' && pending) showSaved(pending.session_id);
      else if (action === 'forget') { pending = null; remember(); clear(); }
      else if (action === 'discard') clear();
    }
    root.addEventListener('click', click); el('gap-close-ack').addEventListener('change', ready);
    remember(); if (pending) status('Cierre pendiente · consultar no envía otra operación.');
    return {open:open,showSaved:showSaved,clear:clear,dispose:function () { if (!alive) return; alive=false; generation++; controller.abort(); root.removeEventListener('click',click); }};
  }
  window.OrbisPerpGapClose = {mount:mount};
})();
