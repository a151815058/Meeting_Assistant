// Poll minutes-generation progress and reload the page when the job finishes (REQ-12).
(function () {
  const panel = document.getElementById("minutes-job");
  if (!panel || panel.dataset.running !== "true") return;

  const progressEl = document.getElementById("minutes-progress");
  const url = panel.dataset.statusUrl;

  async function poll() {
    try {
      const resp = await fetch(url, { headers: { Accept: "application/json" }, credentials: "same-origin" });
      if (resp.ok) {
        const data = await resp.json();
        if (data.state !== "running") {
          window.location.reload();
          return;
        }
        if (progressEl && data.progress) progressEl.textContent = data.progress;
      }
    } catch (_err) {
      // transient network error: keep polling
    }
    setTimeout(poll, 3000);
  }
  setTimeout(poll, 3000);
})();
