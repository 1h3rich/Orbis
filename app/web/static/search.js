/* Buscador local de destinos reales. No consulta saldos, credenciales ni red. */
(function () {
  "use strict";
  var dialog = document.getElementById("global-search-dialog");
  var trigger = document.getElementById("global-search-open");
  var input = document.getElementById("global-search-input");
  var results = document.getElementById("global-search-results");
  if (!dialog || !trigger || !input || !results || !dialog.showModal) return;

  var destinations = [
    { name: "Estudio", kind: "Sección", terms: "mercado grafico velas", href: "#/estudio" },
    { name: "Laboratorio", kind: "Sección", terms: "estrategias backtest historico", href: "#/laboratorio" },
    { name: "Nueva estrategia", kind: "Acción", terms: "crear estrategia reglas", href: "#/laboratorio", focus: "#lab-strategy-form [name='name']" },
    { name: "Simulación PAPER", kind: "Operar", terms: "operar ficticio paper comprar vender", href: "#/operativa" },
    { name: "Saldo y posiciones", kind: "PAPER", terms: "cartera saldo ficticio simulacion", href: "#/portfolio" },
    { name: "DCA", kind: "PAPER", terms: "compras periodicas automatizacion", href: "#/dca" },
    { name: "Patrimonio registrado", kind: "Registro real", terms: "cartera activos", href: "#/real" },
    { name: "Registrar compra", kind: "Patrimonio · requiere desbloqueo", terms: "movimiento activo compra", href: "#/real", focus: "#real-event-form [name='asset']" },
    { name: "Movimientos", kind: "PAPER", terms: "historial ledger", href: "#/ledger" },
    { name: "Alertas", kind: "Sección", terms: "aviso vigilar precio umbral", href: "#/alertas" },
    { name: "Configuración", kind: "Sección", terms: "ajustes privacidad seguridad", href: "#/config" }
  ];
  ["BTC", "ETH", "SOL"].forEach(function (asset) {
    ["EUR", "USD", "USDT"].forEach(function (quote) {
      var pair = asset + "/" + quote;
      destinations.push({ name: pair, kind: "Mercado · Estudio", terms: asset + " " + quote + " grafico", href: "#/estudio?market=" + encodeURIComponent(pair) });
    });
  });
  var visible = [], selected = 0;
  function plain(value) {
    return String(value || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase().trim();
  }
  function select(index) {
    if (!visible.length) return;
    selected = (index + visible.length) % visible.length;
    Array.prototype.forEach.call(results.querySelectorAll(".global-search-result"), function (button, i) {
      button.setAttribute("aria-selected", i === selected ? "true" : "false");
      if (i === selected) button.scrollIntoView({ block: "nearest" });
    });
    input.setAttribute("aria-activedescendant", "global-search-result-" + selected);
  }
  function focusDestination(selector) {
    if (!selector) return;
    var node = document.querySelector(selector);
    if (node && node.getClientRects().length) { node.focus(); return; }
    var observer = new MutationObserver(function () {
      var target = document.querySelector(selector);
      if (target && target.getClientRects().length) { observer.disconnect(); target.focus(); }
    });
    observer.observe(document.getElementById("view"), { childList: true, subtree: true });
    setTimeout(function () { observer.disconnect(); }, 5000);
  }
  function openDestination(item) {
    dialog.close();
    if (location.hash !== item.href) location.hash = item.href;
    focusDestination(item.focus);
  }
  function paint() {
    var query = plain(input.value).slice(0, 80);
    var terms = query.split(/\s+/).filter(Boolean);
    visible = destinations.filter(function (item) {
      if (query.indexOf("/") >= 0 && item.kind === "Mercado · Estudio") {
        return plain(item.name) === query;
      }
      var haystack = plain(item.name + " " + item.kind + " " + item.terms);
      return terms.every(function (term) { return haystack.indexOf(term) >= 0; });
    }).slice(0, 12);
    results.replaceChildren();
    input.removeAttribute("aria-activedescendant");
    if (!visible.length) {
      var empty = document.createElement("p");
      empty.className = "global-search-empty";
      empty.textContent = "Sin resultados. Busca un mercado o una sección disponible.";
      results.appendChild(empty);
      return;
    }
    visible.forEach(function (item, index) {
      var button = document.createElement("button");
      button.type = "button";
      button.id = "global-search-result-" + index;
      button.className = "global-search-result";
      button.setAttribute("role", "option");
      button.setAttribute("aria-selected", "false");
      var name = document.createElement("span"), kind = document.createElement("small");
      name.textContent = item.name; kind.textContent = item.kind;
      button.append(name, kind);
      button.addEventListener("click", function () { openDestination(item); });
      results.appendChild(button);
    });
    select(0);
  }
  function open() {
    if (dialog.open) { input.focus(); return; }
    input.value = "";
    paint();
    dialog.showModal();
    input.focus();
  }
  trigger.addEventListener("click", open);
  input.addEventListener("input", paint);
  input.addEventListener("keydown", function (event) {
    if (event.key === "ArrowDown") { event.preventDefault(); select(selected + 1); }
    else if (event.key === "ArrowUp") { event.preventDefault(); select(selected - 1); }
    else if (event.key === "Enter" && visible.length) { event.preventDefault(); openDestination(visible[selected]); }
    else if (event.key === "Escape") { event.preventDefault(); dialog.close(); }
  });
  document.addEventListener("keydown", function (event) {
    if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === "k") {
      event.preventDefault(); open();
    }
  });
  dialog.addEventListener("click", function (event) { if (event.target === dialog) dialog.close(); });
})();
