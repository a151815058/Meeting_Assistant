// Project form (REQ-64): add and remove stakeholder rows. Without this script the form still
// works: the server always renders one blank row and ignores rows left empty.
(function () {
  const rows = document.getElementById("stakeholder-rows");
  const template = document.getElementById("stakeholder-template");
  const addButton = document.getElementById("stakeholder-add");
  if (!rows || !template || !addButton) return;

  const max = Number(rows.dataset.max) || 50;

  function refresh() {
    addButton.disabled = rows.querySelectorAll(".stakeholder-row").length >= max;
  }

  addButton.addEventListener("click", () => {
    rows.append(template.content.cloneNode(true));
    const added = rows.querySelector(".stakeholder-row:last-child input");
    if (added) added.focus();
    refresh();
  });

  rows.addEventListener("click", (event) => {
    const button = event.target.closest(".stakeholder-remove");
    if (!button) return;
    button.closest(".stakeholder-row").remove();
    refresh();
  });

  refresh();
})();
