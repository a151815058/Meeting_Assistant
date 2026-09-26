// Tabs on the meeting page (WAI-ARIA tabs pattern). Panels stay in the DOM, so a recording
// keeps running and the transcript keeps updating while another tab is shown.
// The URL hash (#live / #minutes / #participants) selects the tab, so redirects after
// adding or removing a participant come back to the same tab.
(() => {
  const tabs = [...document.querySelectorAll('[role="tab"]')];
  if (!tabs.length) return;
  const panelOf = (tab) => document.getElementById(tab.getAttribute("aria-controls"));

  function select(tab, { focus = false, updateHash = true } = {}) {
    for (const t of tabs) {
      const selected = t === tab;
      t.setAttribute("aria-selected", String(selected));
      t.tabIndex = selected ? 0 : -1;
      panelOf(t).hidden = !selected;
    }
    if (focus) tab.focus();
    if (updateHash) history.replaceState(null, "", "#" + panelOf(tab).dataset.hash);
  }

  function fromHash() {
    const hash = location.hash.slice(1);
    return tabs.find((t) => panelOf(t).dataset.hash === hash) || tabs[0];
  }

  tabs.forEach((tab, i) => {
    tab.addEventListener("click", () => select(tab));
    tab.addEventListener("keydown", (event) => {
      const moves = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 };
      if (!(event.key in moves)) return;
      event.preventDefault();
      select(tabs[(moves[event.key] + tabs.length) % tabs.length], { focus: true });
    });
  });
  window.addEventListener("hashchange", () => select(fromHash(), { updateHash: false }));
  select(fromHash(), { updateHash: false });
})();
