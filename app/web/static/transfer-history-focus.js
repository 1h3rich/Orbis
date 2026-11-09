/* Focus the referenced local transfer without invoking its controls. */
(function () {
  "use strict";

  function create(panel, list) {
    const summary = panel.querySelector("summary");
    let pending = null;

    function reset() {
      pending = null;
      for (const card of list.querySelectorAll("[aria-current]")) card.removeAttribute("aria-current");
    }

    function open(id, load) {
      if (typeof id !== "string" || !/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(id)) return false;
      const wasOpen = panel.open;
      pending = {id, query: "?focus_id=" + encodeURIComponent(id)};
      panel.open = true;
      summary.focus({preventScroll:true});
      panel.scrollIntoView({block:"start"});
      if (wasOpen) load();
      return true;
    }

    function finish(request) {
      if (!request || request !== pending) return;
      pending = null;
      if (!panel.open) return;
      const card = Array.from(list.children).find(item => item.dataset.transferId === request.id);
      if (!card) return;
      card.setAttribute("aria-current", "true");
      if (document.activeElement === summary) {
        card.focus({preventScroll:true});
        card.scrollIntoView({block:"nearest"});
      }
    }

    panel.addEventListener("toggle", () => { if (!panel.open) reset(); });
    return {open, finish, reset, request: () => pending};
  }

  window.OrbisTransferHistoryFocus = {create};
})();
