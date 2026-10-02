// Forms with data-confirm="..." ask before submitting. The message comes from an
// HTML-escaped attribute, never from inline JS, so user-provided names can't inject script.
document.addEventListener("submit", (event) => {
  const message = event.target.dataset && event.target.dataset.confirm;
  if (message && !window.confirm(message)) event.preventDefault();
});

// Header menu (REQ-71): the navigation links sit behind an icon. CSS opens the panel on hover /
// keyboard focus; this script pins it on click (touch screens have no hover). "open" pins the
// panel; "nav-dismissed" hides it although the pointer or focus is still inside (after a second
// click or Esc), until the pointer or focus leaves.
(function () {
  const menu = document.getElementById("nav-menu");
  const toggle = document.getElementById("nav-toggle");
  if (!menu || !toggle) return;

  function setOpen(open) {
    menu.classList.toggle("open", open);
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
  }

  toggle.addEventListener("click", () => {
    const pinned = menu.classList.contains("open");
    setOpen(!pinned);
    menu.classList.toggle("nav-dismissed", pinned);
  });
  menu.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      setOpen(false);
      menu.classList.add("nav-dismissed");
      toggle.focus();
    }
  });
  menu.addEventListener("mouseleave", () => menu.classList.remove("nav-dismissed"));
  menu.addEventListener("focusout", (event) => {
    if (!menu.contains(event.relatedTarget)) menu.classList.remove("nav-dismissed");
  });
  document.addEventListener("pointerdown", (event) => {
    if (!menu.contains(event.target)) setOpen(false);
  });
})();
