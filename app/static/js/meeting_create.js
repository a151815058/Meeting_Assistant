// New-meeting form: picking a calendar event fills in its title, unless the user typed their own.
(function () {
  const select = document.getElementById("calendar-event");
  const title = document.getElementById("meeting-title");
  if (!select || !title) return;

  let autofilled = "";
  select.addEventListener("change", () => {
    const option = select.options[select.selectedIndex];
    const eventTitle = (option && option.dataset.title) || "";
    if (title.value === "" || title.value === autofilled) {
      title.value = eventTitle;
      autofilled = eventTitle;
    }
  });
})();
