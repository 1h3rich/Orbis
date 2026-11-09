(function () {
  "use strict";

  var KEY = "orbis.home.workspaces.v1";
  var TITLES = {
    paper: "Simulación PAPER", market: "Gráfico BTC", real: "Patrimonio registrado",
    watch: "Mercados seguidos", strategies: "Planes PAPER", activity: "Movimientos PAPER",
    dca: "Planes de aportación", alerts: "Alertas"
  };
  var PRESETS = {
    general: {order:["paper","market","real","watch","strategies","activity","dca","alerts"], hidden:[], wide:["paper","market"]},
    trading: {order:["market","paper","watch","activity","strategies","real","dca","alerts"], hidden:["real","dca"], wide:["market","paper"]},
    research: {order:["market","watch","strategies","paper","real","activity","dca","alerts"], hidden:["paper","real","activity","dca"], wide:["market"]},
    portfolio: {order:["real","dca","paper","activity","watch","market","strategies","alerts"], hidden:["watch","market","strategies","alerts"], wide:["real","paper"]}
  };

  function defaults(name) {
    var preset = PRESETS[name];
    return preset.order.map(function (id) {
      return {id:id, visible:!preset.hidden.includes(id), wide:preset.wide.includes(id)};
    });
  }

  function restore(name, saved) {
    var fallback = defaults(name);
    if (!Array.isArray(saved)) return fallback;
    var remaining = new Map(fallback.map(function (row) { return [row.id, row]; }));
    var layout = [];
    saved.forEach(function (row) {
      if (!row || !remaining.has(row.id)) return;
      var original = remaining.get(row.id);
      layout.push({id:row.id,
        visible:typeof row.visible === "boolean" ? row.visible : original.visible,
        wide:typeof row.wide === "boolean" ? row.wide : original.wide});
      remaining.delete(row.id);
    });
    layout.push.apply(layout, Array.from(remaining.values()));
    return layout.some(function (row) { return row.visible; }) ? layout : fallback;
  }

  function mount(grid, select, editor, onChange) {
    var saved;
    try { saved = JSON.parse(localStorage.getItem(KEY)); } catch (error) { saved = null; }
    var active = saved && typeof saved.active === "string" && Object.hasOwn(PRESETS, saved.active) ? saved.active : "general";
    var layouts = {};
    Object.keys(PRESETS).forEach(function (name) {
      layouts[name] = restore(name, saved && saved.layouts && saved.layouts[name]);
    });
    var panels = new Map(Array.from(grid.children, function (panel) { return [panel.dataset.homePanel, panel]; }));
    var rows = editor.querySelector("[data-workspace-rows]");
    var status = editor.querySelector("[data-workspace-status]");

    function showLayout() {
      layouts[active].forEach(function (row, index) {
        var panel = panels.get(row.id);
        if (grid.children[index] !== panel) grid.insertBefore(panel, grid.children[index] || null);
        panel.hidden = !row.visible;
        panel.dataset.homeWidth = row.wide ? "full" : "half";
      });
      onChange();
    }

    function showControls(focus) {
      var layout = layouts[active];
      var visibleCount = layout.filter(function (row) { return row.visible; }).length;
      rows.innerHTML = layout.map(function (row, index) {
        var title = TITLES[row.id];
        return '<li data-workspace-row="' + row.id + '"><strong>' + title + '</strong>' +
          '<label class="home-workspace-visible"><input type="checkbox" data-workspace-action="visible" aria-label="Mostrar ' + title + '"' +
          (row.visible ? ' checked' : '') + (row.visible && visibleCount === 1 ? ' disabled' : '') + '> Visible</label>' +
          '<button type="button" class="ghost" data-workspace-action="wide" aria-label="Ancho completo para ' + title + '" aria-pressed="' + row.wide + '">Ancho</button>' +
          '<button type="button" class="ghost" data-workspace-action="up" aria-label="Subir ' + title + '"' + (index === 0 ? ' disabled' : '') + '>↑</button>' +
          '<button type="button" class="ghost" data-workspace-action="down" aria-label="Bajar ' + title + '"' + (index === layout.length - 1 ? ' disabled' : '') + '>↓</button></li>';
      }).join("");
      if (focus) {
        var row = rows.querySelector('[data-workspace-row="' + focus.id + '"]');
        var button = row.querySelector('[data-workspace-action="' + focus.action + '"]');
        if (button.disabled) button = row.querySelector('[data-workspace-action]:not([disabled])');
        if (button) button.focus({preventScroll:true});
      }
    }

    function save(focus) {
      try {
        localStorage.setItem(KEY, JSON.stringify({active:active, layouts:layouts}));
        status.textContent = "Guardado en este navegador";
      } catch (error) {
        status.textContent = "El navegador no permite guardar esta disposición";
      }
      showLayout();
      showControls(focus);
    }

    function edit(control) {
      var id = control.closest("[data-workspace-row]").dataset.workspaceRow;
      var action = control.dataset.workspaceAction;
      var layout = layouts[active];
      var index = layout.findIndex(function (row) { return row.id === id; });
      if (action === "visible") {
        layout[index].visible = control.checked;
      } else if (action === "wide") {
        layout[index].wide = !layout[index].wide;
      } else {
        var next = index + (action === "up" ? -1 : 1);
        if (next < 0 || next >= layout.length) return;
        layout.splice(next, 0, layout.splice(index, 1)[0]);
      }
      save({id:id, action:action});
    }

    select.value = active;
    select.addEventListener("change", function () {
      if (!Object.hasOwn(PRESETS, select.value)) return;
      active = select.value;
      save();
    });
    rows.addEventListener("click", function (event) {
      var button = event.target.closest("button[data-workspace-action]");
      if (button) edit(button);
    });
    rows.addEventListener("change", function (event) {
      if (event.target.matches('input[data-workspace-action="visible"]')) edit(event.target);
    });
    editor.querySelector("[data-workspace-reset]").addEventListener("click", function () {
      layouts[active] = defaults(active);
      save();
    });
    showControls();
    showLayout();
  }

  window.OrbisHomeWorkspaces = {mount:mount};
})();
