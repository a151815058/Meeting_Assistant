// Knowledge-base chat (REQ-63): the icon in the corner of every page. CSS shows the panel on
// hover / keyboard focus; this script pins it on click, keeps the conversation for the tab
// (sessionStorage) and talks to /knowledge/ask. Everything from the server or the model is put
// on the page with textContent, never as HTML.
(function () {
  const root = document.getElementById("kb-chat");
  if (!root) return;

  const toggle = document.getElementById("kb-toggle");
  const list = document.getElementById("kb-messages");
  const hint = document.getElementById("kb-hint");
  const form = document.getElementById("kb-form");
  const input = document.getElementById("kb-question");
  const sendButton = document.getElementById("kb-send");
  const meetingSelect = document.getElementById("kb-meeting");
  const projectSelect = document.getElementById("kb-project");
  const dateFrom = document.getElementById("kb-date-from");
  const dateTo = document.getElementById("kb-date-to");

  const STORAGE_KEY = "kbChat:" + root.dataset.user;
  const MAX_STORED = 40; // messages kept for the tab
  const HISTORY_SENT = 8; // earlier messages sent along as context (the server caps it too)

  let messages = loadMessages(); // {role: "user" | "assistant", text, segments?, sources?, error?}
  let busy = false;
  let meetingsLoaded = false;
  let allMeetings = []; // from the server; the meeting list shows those of the selected project
  let rendered = 0; // makes source element ids unique per message

  function loadMessages() {
    try {
      const stored = JSON.parse(sessionStorage.getItem(STORAGE_KEY) || "[]");
      return Array.isArray(stored) ? stored.filter((m) => m && typeof m.text === "string") : [];
    } catch (err) {
      return [];
    }
  }

  function saveMessages() {
    try {
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(messages.slice(-MAX_STORED)));
    } catch (err) {
      // storage full or unavailable: the chat still works for this page
    }
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function scrollToEnd() {
    list.scrollTop = list.scrollHeight;
  }

  // --- rendering ---------------------------------------------------------------------------

  function renderSources(sources, prefix) {
    const box = el("div", "kb-sources");
    box.append(el("div", "kb-sources-title", "引用來源"));
    for (const source of sources) {
      const item = el("details", "kb-source");
      item.id = prefix + source.number;
      const summary = el("summary", "", "[" + source.number + "] ");
      if (typeof source.url === "string" && source.url.startsWith("/")) {
        const link = el("a", "", source.title);
        link.href = source.url;
        summary.append(link);
      } else {
        summary.append(source.title || "");
      }
      summary.append((source.project ? "・" + source.project : "") + "・" + (source.date || "日期未知") + (source.section ? "・" + source.section : ""));
      item.append(summary, el("p", "", source.content || ""));
      box.append(item);
    }
    return box;
  }

  function showSource(id) {
    const item = document.getElementById(id);
    if (!item) return;
    item.open = true;
    item.scrollIntoView({ block: "nearest" });
    item.classList.add("kb-flash");
    setTimeout(() => item.classList.remove("kb-flash"), 1500);
  }

  function renderMessage(message) {
    const bubble = el("div", "kb-msg " + (message.role === "user" ? "user" : "assistant") + (message.error ? " error" : ""));
    const hasSources = Array.isArray(message.sources) && message.sources.length > 0;
    const prefix = "kb-src-" + rendered++ + "-";
    if (message.role === "user" || !Array.isArray(message.segments)) {
      bubble.append(el("span", "kb-text", message.text));
    } else {
      const body = el("span", "kb-text");
      for (const segment of message.segments) {
        if (segment.type === "cite") {
          const cite = el("button", "kb-cite", "[" + segment.value + "]");
          cite.type = "button";
          cite.setAttribute("aria-label", "來源 " + segment.value);
          cite.addEventListener("click", () => showSource(prefix + segment.value));
          body.append(cite);
        } else {
          body.append(String(segment.value));
        }
      }
      bubble.append(body);
    }
    if (hasSources) bubble.append(renderSources(message.sources, prefix));
    list.append(bubble);
  }

  function renderAll() {
    list.replaceChildren(hint);
    hint.hidden = messages.length > 0;
    messages.forEach(renderMessage);
    scrollToEnd();
  }

  function addMessage(message) {
    messages.push(message);
    saveMessages();
    hint.hidden = true;
    renderMessage(message);
    scrollToEnd();
  }

  // --- talking to the server ---------------------------------------------------------------

  function renderMeetingOptions() {
    const projectId = projectSelect.value;
    const selected = meetingSelect.value;
    meetingSelect.replaceChildren(el("option", "", projectId ? "此專案的全部會議" : "全部會議"));
    meetingSelect.firstChild.value = "";
    for (const meeting of allMeetings) {
      if (projectId && meeting.project_id !== projectId) continue;
      const option = el("option", "", (meeting.date || "日期未知") + " " + meeting.title);
      option.value = meeting.id;
      meetingSelect.append(option);
    }
    meetingSelect.value = selected; // falls back to "all" when that meeting is not in the project
    if (meetingSelect.selectedIndex < 0) meetingSelect.value = "";
  }

  async function loadMeetings() {
    if (meetingsLoaded) return;
    meetingsLoaded = true;
    try {
      const resp = await fetch(root.dataset.meetingsUrl, { headers: { Accept: "application/json" }, credentials: "same-origin" });
      const data = await resp.json();
      allMeetings = data.meetings;
      for (const project of data.projects || []) {
        const option = el("option", "", project.name);
        option.value = project.id;
        projectSelect.append(option);
      }
      renderMeetingOptions();
      document.getElementById("kb-count").textContent = "目前可查詢 " + data.meetings.length + " 場會議。";
    } catch (err) {
      meetingsLoaded = false; // try again the next time the panel opens
    }
  }

  async function ask(question) {
    busy = true;
    sendButton.disabled = true;
    root.classList.add("open"); // stay open while waiting, even if the pointer leaves
    const history = messages.filter((m) => !m.error).slice(-HISTORY_SENT).map((m) => ({ role: m.role, text: m.text }));
    addMessage({ role: "user", text: question });

    const pending = el("div", "kb-msg assistant");
    pending.append(el("span", "kb-spinner"), "AI 正在查閱會議記錄…");
    list.append(pending);
    scrollToEnd();

    let reply;
    try {
      const resp = await fetch(root.dataset.askUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRFToken": root.dataset.csrf },
        body: JSON.stringify({
          question: question,
          history: history,
          meeting_id: meetingSelect.value,
          project_id: projectSelect.value,
          date_from: dateFrom.value,
          date_to: dateTo.value,
        }),
      });
      const isJson = (resp.headers.get("Content-Type") || "").includes("application/json");
      if (!isJson) {
        // redirected to the login page, or the CSRF token expired
        reply = { role: "assistant", error: true, text: "登入已逾時或頁面已過期，請重新整理頁面後再試。" };
      } else {
        const data = await resp.json();
        reply = resp.ok
          ? { role: "assistant", text: data.text, segments: data.segments, sources: data.sources }
          : { role: "assistant", error: true, text: (data.errors || ["查詢時發生錯誤，請稍後再試"]).join("\n") };
      }
    } catch (err) {
      reply = { role: "assistant", error: true, text: "無法連線到伺服器，請稍後再試。" };
    }
    pending.remove();
    addMessage(reply);
    busy = false;
    sendButton.disabled = false;
    input.focus();
  }

  projectSelect.addEventListener("change", renderMeetingOptions);

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const question = input.value.trim();
    if (!question || busy) return;
    input.value = "";
    ask(question);
  });

  input.addEventListener("keydown", (event) => {
    // Enter sends; Shift+Enter is a new line; Enter that confirms an IME candidate does neither.
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  document.getElementById("kb-clear").addEventListener("click", () => {
    messages = [];
    try {
      sessionStorage.removeItem(STORAGE_KEY);
    } catch (err) {
      // nothing stored
    }
    renderAll();
    input.focus();
  });

  // --- opening and closing -----------------------------------------------------------------
  // Hover / focus is CSS. "open" pins the panel; "kb-dismissed" hides it although the pointer
  // or focus is still inside (after ✕ or Esc), until the pointer or focus leaves.

  function dismiss() {
    root.classList.remove("open");
    root.classList.add("kb-dismissed");
  }

  toggle.addEventListener("click", () => {
    const pinned = root.classList.contains("open");
    root.classList.toggle("open", !pinned);
    root.classList.toggle("kb-dismissed", pinned);
    if (!pinned) input.focus();
  });
  document.getElementById("kb-close").addEventListener("click", dismiss);
  root.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      dismiss();
      toggle.focus();
    }
  });
  root.addEventListener("mouseenter", loadMeetings);
  root.addEventListener("focusin", loadMeetings);
  root.addEventListener("mouseleave", () => root.classList.remove("kb-dismissed"));
  root.addEventListener("focusout", (event) => {
    if (!root.contains(event.relatedTarget)) root.classList.remove("kb-dismissed");
  });
  document.addEventListener("pointerdown", (event) => {
    if (!root.contains(event.target) && !busy) root.classList.remove("open");
  });

  // The conversation may quote meeting content: do not leave it behind after logging out.
  const logout = document.querySelector(".nav-btn-logout");
  if (logout) {
    logout.addEventListener("click", () => {
      try {
        sessionStorage.removeItem(STORAGE_KEY);
      } catch (err) {
        // nothing stored
      }
    });
  }

  renderAll();
})();
