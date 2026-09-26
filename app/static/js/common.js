// Forms with data-confirm="..." ask before submitting. The message comes from an
// HTML-escaped attribute, never from inline JS, so user-provided names can't inject script.
document.addEventListener("submit", (event) => {
  const message = event.target.dataset && event.target.dataset.confirm;
  if (message && !window.confirm(message)) event.preventDefault();
});
