/* Chronological derivative parameter fitting. LAB only. */
(function () {
  "use strict";

  function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, character =>
      ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[character]);
  }

  function shiftDecimal(raw, places) {
    const [integer, fraction = ''] = raw.split('.');
    const digits = integer + fraction;
    const point = integer.length + places;
    const whole = point > 0 ? digits.slice(0, point).padEnd(point, '0') : '0';
    const decimals = (point < 0 ? '0'.repeat(-point) + digits : digits.slice(point)).replace(/0+$/, '');
    return whole.replace(/^0+(?=\d)/, '') + (decimals ? '.' + decimals : '');
  }

  function parseGrid(raw) {
    const entries = raw.trim().split(/\s+/);
    if (entries.length > 4 || entries.some(value => !/^\d{1,4}(?:[.,]\d{1,18})?$/.test(value))) {
      throw new Error('Introduce de uno a cuatro porcentajes separados por espacios.');
    }
    return entries.map(value => shiftDecimal(value.replace(',', '.'), -2));
  }

  function developmentFraction(value) {
    if (!/^\d{2}(?:\.\d{1,38})?$/.test(value)) {
      throw new Error('Introduce el porcentaje de desarrollo en formato decimal.');
    }
    return shiftDecimal(value, -2);
  }

  const percent = rate => shiftDecimal(rate, 2).replace('.', ',');

  function amount(value) {
    return '<span title="' + esc(value) + '">' + esc(window.OrbisUI.fmtDecimal(String(value), 2, 2) ?? '—') + '</span>';
  }

  function period(score) {
    const stamp = value => new Date(value * 1000).toISOString().slice(0, 16).replace('T', ' ');
    return stamp(score.from_t) + ' → ' + stamp(score.to_t) + ' UTC';
  }

  function mount(root, actions) {
    root.innerHTML =
      '<form class="perp-run-config" id="perp-optimization-form">' +
        '<label class="field">Perfil<select name="profile_id" required></select></label>' +
        '<label class="field">Datos<select name="dataset_id" required></select></label>' +
        '<label class="field" data-help="De uno a cuatro porcentajes separados por espacios.">Stops %<input name="stops" value="1 2 3" maxlength="96" required></label>' +
        '<label class="field" data-help="De uno a cuatro porcentajes separados por espacios.">Objetivos %<input name="targets" value="2 4 6" maxlength="96" required></label>' +
        '<label class="field">Desarrollo %<input name="fraction" type="number" min="50" max="80" step="any" value="70" required></label>' +
        '<div class="row"><button type="submit" disabled>Optimizar</button></div></form>' +
      '<p class="state" id="perp-optimization-status" role="status"></p>' +
      '<div class="row"><label class="field">Pruebas guardadas<select id="perp-optimization-selection"></select></label>' +
        '<button type="button" class="ghost" id="perp-optimization-prev">Anterior</button>' +
        '<span id="perp-optimization-page"></span><button type="button" class="ghost" id="perp-optimization-next">Siguiente</button>' +
        '<button type="button" class="ghost" id="perp-optimization-refresh">Actualizar</button></div>' +
      '<section id="perp-optimization-result" hidden><div class="row"><h3>Desarrollo</h3>' +
        '<span class="statusbadge" data-help="Trayectorias y reglas de mercado modeladas. Sólo investigación en LAB.">Experimental</span>' +
        '<button type="button" class="ghost" id="perp-optimization-export">Exportar JSON</button></div>' +
        '<div class="row" id="perp-optimization-variant" hidden>' +
        '<button type="button" id="perp-optimization-save" data-help="Guarda la combinación elegida como versión de la estrategia y conserva sus dos pruebas.">Guardar variante</button>' +
        '<span class="statusbadge" id="perp-optimization-version-badge" hidden></span>' +
        '<button type="button" class="ghost" id="perp-optimization-open-version" hidden>Abrir versión</button>' +
        '<button type="button" class="ghost" id="perp-optimization-open-development" hidden>Ver desarrollo</button>' +
        '<button type="button" class="ghost" id="perp-optimization-open-reserve" hidden>Ver reserva</button>' +
        '<button type="button" class="ghost" id="perp-optimization-paper" hidden data-help="Abre la revisión de capital ficticio con la reserva archivada.">Preparar PAPER</button></div>' +
        '<p id="perp-optimization-development" class="state"></p><div id="perp-optimization-map" class="lab-parameter-map"></div>' +
        '<div id="perp-optimization-grid" class="tablewrap"></div>' +
        '<h3>Reserva</h3><div id="perp-optimization-reserve"></div></section>';
    const el = key => root.querySelector('#perp-optimization-' + key);
    const form = el('form');
    const status = el('status');
    const selection = el('selection');
    const pageSize = 10;
    let offset = 0, total = 0, alive = true, busy = false, currentRecord = null, controller = null;
    let variant = null, paperEligible = false;

    function ready() {
      Array.from(form.elements).forEach(control => { control.disabled = busy; });
      form.querySelector('button').disabled = busy || !form.elements.profile_id.value || !form.elements.dataset_id.value;
      selection.disabled = busy;
      el('prev').disabled = busy || offset === 0;
      el('next').disabled = busy || offset + pageSize >= total;
      el('refresh').disabled = busy;
      el('export').disabled = busy || !currentRecord;
      ['save', 'open-version', 'open-development', 'open-reserve', 'paper'].forEach(key => { el(key).disabled = busy; });
    }

    function clearResult() {
      currentRecord = null;
      variant = null;
      paperEligible = false;
      el('result').hidden = true;
      ['map', 'grid', 'reserve', 'development'].forEach(key => { el(key).textContent = ''; });
    }

    async function operation(message, action, keepResult = false) {
      if (controller) controller.abort();
      controller = new AbortController();
      const signal = controller.signal;
      const current = () => alive && root.isConnected && !signal.aborted;
      const api = async (path, options = {}) => {
        const response = await fetch('/lab' + path, {...options, cache:'no-store', signal,
          headers:{'Content-Type':'application/json'}});
        const value = await response.json();
        if (!response.ok) throw new Error(response.status === 423 ? 'LAB bloqueado · Ajustes'
          : typeof value.detail === 'string' ? value.detail : 'No se pudo verificar la optimización.');
        return value;
      };
      busy = true;
      ready();
      status.textContent = message;
      try {
        await action(api, current);
      } catch (error) {
        if (current()) {
          if (!keepResult) clearResult();
          status.textContent = error.message;
        }
      } finally {
        if (current()) {
          busy = false;
          ready();
        }
      }
    }

    function setOptions(select, entries, placeholder) {
      select.replaceChildren(new Option(placeholder, ''));
      entries.forEach(([id, label]) => select.add(new Option(label, id)));
    }

    function library(records, wanted) {
      total = records.total;
      setOptions(selection, records.items.map(row => [row.id, row.symbol + ' · ' + row.timeframe + ' · '
        + row.created_at.slice(0, 19).replace('T', ' ')]), 'Selecciona prueba');
      selection.value = wanted || '';
      el('page').textContent = total ? (offset + 1) + '–' + Math.min(offset + pageSize, total) + ' / ' + total : '0';
    }

    function pagePath() {
      return '/perp/optimizations?offset=' + offset + '&limit=' + pageSize;
    }

    function load() {
      close();
      offset = 0;
      return operation('Cargando…', async (api, current) => {
        const [profiles, datasets, versions, records] = await Promise.all([
          api('/perp/profiles'), api('/perp/datasets'), api('/strategy-versions'), api(pagePath()),
        ]);
        if (!current()) return;
        const names = new Map(versions.map(version => [version.id, version]));
        setOptions(form.elements.profile_id, profiles.map(row => {
          const p = row.profile, v = names.get(p.strategy_version_id);
          return [row.id, [v ? v.name + ' · v' + v.version : p.strategy_version_id.slice(0, 8),
            p.margin_mode === 'ISOLATED' ? 'Aislado' : 'Cross', p.leverage + '×',
            p.entry_order ? 'LIMIT' : 'MARKET', p.partial_targets ? 'Parciales' : '',
            'TRADE ' + (p.trade_path === 'O_H_L_C' ? '↑↓' : '↓↑') + ' / MARK ' + (p.mark_path === 'O_H_L_C' ? '↑↓' : '↓↑')].filter(Boolean).join(' · ')];
        }), 'Selecciona perfil');
        setOptions(form.elements.dataset_id, datasets.map(row => [row.id,
          row.symbol + ' · ' + row.timeframe + ' · ' + new Date(row.start * 1000).toISOString().slice(0, 10)]), 'Selecciona datos');
        library(records);
        status.textContent = profiles.length && datasets.length ? '' : 'Guarda un perfil y un histórico de derivados.';
      });
    }

    function showVariant(saved, eligible) {
      variant = saved || null;
      el('variant').hidden = !eligible;
      el('save').hidden = Boolean(variant);
      ['version-badge', 'open-version', 'open-development', 'open-reserve'].forEach(key => {
        el(key).hidden = !variant;
      });
      el('paper').hidden = !variant || !paperEligible;
      el('version-badge').textContent = variant ? 'v' + variant.version + ' · LAB' : '';
    }

    function show(bundle) {
      const {optimization: record, result} = bundle;
      const {candidates, selected_index: selected, out_of_sample: reserve} = result;
      if (!candidates.length || result.execution_authority !== 'NONE') throw new Error('Resultado incompatible.');
      [[form.elements.profile_id, record.profile_id, 'Perfil archivado'],
        [form.elements.dataset_id, record.dataset_id, record.symbol + ' · ' + record.timeframe]].forEach(([input, id, name]) => {
        if (!Array.from(input.options).some(option => option.value === id)) input.add(new Option(name, id));
        input.value = id;
      });
      form.elements.stops.value = result.plan.stop_losses.map(percent).join(' ');
      form.elements.targets.value = result.plan.take_profits.map(percent).join(' ');
      form.elements.fraction.value = shiftDecimal(result.plan.train_fraction, 2);
      const reasons = {INCOMPLETE:'Interrumpida', LIQUIDATED:'Liquidación', INSUFFICIENT_TRADES:'Muestra insuficiente'};
      el('development').textContent = period(candidates[0].development) + ' · ' + candidates[0].development.bars + ' velas';
      window.OrbisLabParameterMap.render(el('map'), candidates.map(candidate => ({
        stop:percent(candidate.stop_loss), target:percent(candidate.take_profit),
        net:candidate.development.metrics.net_equity_change,
        eligible:!candidate.exclusions.length, selected:candidate.index === selected,
      })));
      el('grid').innerHTML = '<table><thead><tr><th>Stop %</th><th>Objetivo %</th><th>Operaciones</th>' +
        '<th>Neto</th><th>Retorno %</th><th>Drawdown %</th><th>Selección</th></tr></thead><tbody>' +
        candidates.map(candidate => {
          const m = candidate.development.metrics;
          const state = candidate.index === selected ? '<strong>Elegida</strong>'
            : esc(candidate.exclusions.map(reason => reasons[reason]).join(' · ') || 'Elegible');
          return '<tr><td>' + esc(percent(candidate.stop_loss)) + '</td><td>'
            + esc(percent(candidate.take_profit)) + '</td><td>' + m.trades + '</td><td>'
            + amount(m.net_equity_change) + '</td><td>' + amount(m.return_pct) + '</td><td>'
            + amount(m.max_drawdown_pct) + '</td><td>' + state + '</td></tr>';
        }).join('') + '</tbody></table>';
      if (reserve) {
        const m = reserve.metrics;
        const warnings = [reserve.status !== 'COMPLETE' || m.open_position ? 'Interrumpida' : '',
          m.liquidated ? 'Liquidación' : '', reserve.warnings.includes('SMALL_SAMPLE') ? 'Muestra pequeña' : ''].filter(Boolean);
        el('reserve').innerHTML = '<p class="state">' + esc(period(reserve)) + ' · ' + reserve.bars
          + ' velas' + (warnings.length ? ' · ' + esc(warnings.join(' · ')) : '') + '</p>'
          + '<div class="tablewrap"><table><thead><tr><th>Operaciones</th><th>Neto</th><th>Retorno %</th>'
          + '<th>Drawdown %</th></tr></thead><tbody><tr><td>' + m.trades + '</td><td>' + amount(m.net_equity_change)
          + '</td><td>' + amount(m.return_pct) + '</td><td>' + amount(m.max_drawdown_pct) + '</td></tr></tbody></table></div>';
      } else {
        el('reserve').textContent = 'Sin evaluación · ninguna combinación elegible.';
      }
      currentRecord = record;
      paperEligible = reserve && reserve.status === 'COMPLETE' && !reserve.metrics.open_position && !reserve.metrics.liquidated;
      showVariant(bundle.variant, selected !== null);
      el('result').hidden = false;
      status.textContent = candidates.length + ' combinaciones · guardado en LAB';
    }

    form.addEventListener('change', ready);
    form.addEventListener('submit', event => {
      event.preventDefault();
      if (busy) return;
      clearResult();
      return operation('Optimizando…', async (api, current) => {
        const body = {profile_id: form.elements.profile_id.value, dataset_id: form.elements.dataset_id.value,
          plan: {stop_losses: parseGrid(form.elements.stops.value), take_profits: parseGrid(form.elements.targets.value),
            train_fraction: developmentFraction(form.elements.fraction.value)}};
        const created = await api('/perp/optimizations', {method:'POST', body:JSON.stringify(body)});
        if (!current()) return;
        offset = 0;
        const records = await api(pagePath());
        if (current()) {
          library(records, created.optimization.id);
          show(created);
        }
      });
    });
    selection.addEventListener('change', () => {
      clearResult();
      const id = selection.value;
      if (!id) {
        status.textContent = '';
        ready();
        return;
      }
      operation('Reproduciendo…', async (api, current) => {
        const saved = await api('/perp/optimizations/' + encodeURIComponent(id));
        if (current()) show(saved);
      });
    });
    [['prev', -pageSize], ['next', pageSize]].forEach(([key, delta]) => {
      el(key).addEventListener('click', () => {
        clearResult();
        operation('Cargando…', async (api, current) => {
          offset += delta;
          const records = await api(pagePath());
          if (current()) {
            library(records);
            status.textContent = '';
          }
        });
      });
    });
    el('refresh').addEventListener('click', load);
    el('export').addEventListener('click', () => {
      const id = currentRecord?.id;
      if (!id || !window.confirm('El JSON incluirá la estrategia y el histórico sin cifrar. ¿Descargar?')) return;
      operation('Verificando exportación…', async (api, current) => {
        const bundle = await api('/perp/optimizations/' + encodeURIComponent(id) + '/export');
        if (!current()) return;
        const url = URL.createObjectURL(new Blob([JSON.stringify(bundle, null, 2)], {type:'application/json'}));
        const link = document.createElement('a');
        link.href = url;
        link.download = 'orbis-perp-optimization-' + id + '.json';
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        status.textContent = 'JSON exportado';
      });
    });

    el('save').addEventListener('click', () => {
      if (!currentRecord || busy || variant) return;
      const record = currentRecord;
      operation('Guardando variante…', async (api, current) => {
        const saved = await api('/perp/optimizations/' + encodeURIComponent(record.id) + '/variant', {
          method:'POST', body:JSON.stringify({expected_hash:record.hash}),
        });
        if (!current()) return;
        showVariant(saved, true);
        status.textContent = 'Versión guardada · v' + saved.version;
      }, true);
    });
    [['development', 'development_run_id'], ['reserve', 'reserve_run_id']].forEach(([key, field]) => {
      el('open-' + key).addEventListener('click', () => {
        if (!variant || busy) return;
        const id = variant[field];
        operation('Abriendo prueba…', async (_, current) => {
          const opened = await actions.onInspect(id, current);
          if (!current()) return;
          if (!opened) throw new Error('No se pudo abrir la prueba.');
          status.textContent = key === 'development' ? 'Desarrollo abierto' : 'Reserva abierta';
        }, true);
      });
    });
    el('open-version').addEventListener('click', () => {
      if (!variant || busy) return;
      const saved = variant;
      operation('Abriendo versión…', async (_, current) => {
        const opened = await actions.onVersion(saved, current);
        if (!current()) return;
        if (!opened) throw new Error('No se pudo abrir la versión.');
        status.textContent = 'Versión abierta · v' + saved.version;
      }, true);
    });

    el('paper').addEventListener('click', () => {
      if (!variant || !paperEligible || !currentRecord || busy) return;
      const saved = variant;
      const record = currentRecord;
      operation('Verificando reserva…', async (api, current) => {
        const validation = await api('/perp/optimizations/' + encodeURIComponent(record.id) + '/paper-validation', {
          method:'POST', body:JSON.stringify({expected_hash:record.hash}),
        });
        if (!current()) return;
        if (!validation.optimization_origin || validation.optimization_origin.version_id !== saved.version_id ||
            validation.optimization_origin.reserve_run_id !== saved.reserve_run_id) {
          throw new Error('La validación no corresponde a la variante seleccionada.');
        }
        const prepared = await actions.onPaper(validation.id, {isCurrent:current, origin:saved});
        if (current()) status.textContent = prepared ? 'Revisión PAPER abierta' : 'Validación guardada · revisa PAPER';
      }, true);
    });

    function close() {
      if (controller) controller.abort();
      busy = false;
      clearResult();
      status.textContent = '';
      selection.replaceChildren();
      form.elements.profile_id.replaceChildren();
      form.elements.dataset_id.replaceChildren();
      form.reset();
      ready();
    }

    ready();
    return {load, close, dispose() { alive = false; close(); }};
  }

  window.OrbisPerpOptimization = {mount};
})();
