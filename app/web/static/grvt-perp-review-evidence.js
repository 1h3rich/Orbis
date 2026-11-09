/* Read-only lineage displayed inside the sealed GRVT review. */
(function () {
  "use strict";
  const uuid = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/;
  const hash = /^[0-9a-f]{64}$/;

  function node(tag, text, className) {
    const element = document.createElement(tag);
    element.textContent = text;
    if (className) element.className = className;
    return element;
  }

  function utc(milliseconds) {
    return new Date(milliseconds).toISOString().slice(0, 19).replace('T', ' ');
  }

  function milliseconds(ns) {
    return Number(BigInt(ns) / 1000000n);
  }

  function create(lab, research, paper) {
    if (!lab || !research?.profile || !paper || !uuid.test(lab.version_id) ||
        lab.version_id !== research.profile.strategy_version_id || !Number.isSafeInteger(lab.version) ||
        lab.version < 1 || !uuid.test(paper.session_id) || !hash.test(paper.head_hash) ||
        !uuid.test(research.artifact_id) || !hash.test(research.artifact_hash) ||
        !Number.isSafeInteger(paper.sequence) || paper.sequence < 0 ||
        !Number.isSafeInteger(paper.closed_trades) || paper.closed_trades < 1 ||
        !Number.isSafeInteger(paper.observed_bars) || paper.observed_bars < 1 ||
        !Array.isArray(research.windows) || !research.windows.length) {
      throw new Error('Origen LAB y PAPER no verificable.');
    }
    const optimization = lab.optimization;
    if (optimization && (optimization.version_id !== lab.version_id ||
        !uuid.test(optimization.optimization_id) || !hash.test(optimization.optimization_hash) ||
        !uuid.test(optimization.development_run_id) || !uuid.test(optimization.reserve_run_id))) {
      throw new Error('La optimización no corresponde a la versión revisada.');
    }
    const details = node('details', '');
    details.dataset.grvtPerpReviewEvidence = '';
    details.append(node('summary', 'Origen LAB y PAPER · v' + lab.version));
    const fields = node('div', '', 'grvt-mainnet-metrics');
    function field(label, value, identity) {
      const item = node('div', '');
      const text = node('strong', value);
      if (identity) text.title = identity;
      item.append(node('small', label, 'muted'), text);
      fields.append(item);
    }
    field('Versión LAB', 'v' + lab.version + ' · ' + lab.version_id.slice(0, 8), lab.version_id);
    field('Validación LAB', research.artifact_id.slice(0, 8), research.artifact_hash);
    field('Sesión PAPER', paper.session_id.slice(0, 8), paper.session_id);
    field('Registro PAPER', '#' + paper.sequence + ' · ' + paper.head_hash.slice(0, 8), paper.head_hash);
    field('Velas observadas', String(paper.observed_bars));
    field('Operaciones cerradas', String(paper.closed_trades));
    field('Inicio PAPER · UTC', utc(milliseconds(paper.started_at_ns)));
    field('Último dato PAPER · UTC', utc(milliseconds(paper.last_received_at_ns)));
    research.windows.forEach(function (window, index) {
      field('Reserva ' + (index + 1) + ' · UTC', utc(window.from_t * 1000) + ' → ' +
        utc(window.to_t * 1000) + ' · ' + window.bars + ' velas', window.result_hash);
    });
    if (optimization) {
      field('Optimización', optimization.optimization_id.slice(0, 8), optimization.optimization_hash);
      field('Prueba de reserva', optimization.reserve_run_id.slice(0, 8), optimization.reserve_run_id);
    }
    details.append(fields);
    return details;
  }

  window.OrbisGrvtPerpReviewEvidence = {create};
})();
