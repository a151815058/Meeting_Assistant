// Browser recorder + live transcript (REQ-01, REQ-02, REQ-05, REQ-09) and uploaded recordings (REQ-47).
(function () {
  const root = document.getElementById("recorder");
  if (!root) return;

  const meetingId = root.dataset.meetingId;
  const workletUrl = root.dataset.workletUrl;
  const startBtn = document.getElementById("record-start");
  const stopBtn = document.getElementById("record-stop");
  const statusEl = document.getElementById("record-status");
  const meetingStatusEl = document.getElementById("meeting-status");
  const tbody = document.getElementById("transcript-body");
  const uploadForm = document.getElementById("upload-form");
  const uploadFile = document.getElementById("upload-file");
  const uploadBtn = document.getElementById("upload-submit");
  const uploadProgress = document.getElementById("upload-progress");
  const uploadStatus = document.getElementById("upload-status");
  const UPLOAD_TYPES = ["mp3", "wav", "m4a", "aac", "flac", "ogg", "oga", "opus", "webm", "wma", "mp4"];

  const SEND_SAMPLES = 4000; // send every 250 ms of 16 kHz audio

  const ERRORS = {
    not_found: "找不到會議或無權限錄音",
    already_recording: "此會議正在其他視窗錄音或轉錄錄音檔中",
    invalid_payload: "錄音請求格式錯誤",
    rate_limited: "音訊傳送過快，部分音訊已略過",
    max_duration_reached: "已達單次錄音時間上限，錄音已自動停止",
    invalid_audio_chunk: "音訊資料格式錯誤",
    not_recording: "目前沒有進行中的錄音",
    transcription_failed: "轉錄發生錯誤，請查看伺服器紀錄",
    diarization_failed: "發言人分離失敗，逐字稿仍已保存",
    transcription_lagging: "轉錄速度跟不上錄音，逐字稿會延遲出現（可改用較小的 Whisper 模型）",
  };

  // Connect on page load so the server starts loading the speech models before "Record" is pressed.
  const socket = io("/transcription");
  let audioCtx = null;
  let stream = null;
  let pending = [];
  let pendingSamples = 0;
  let recording = false;
  let uploadPhase = "idle"; // idle | uploading | transcribing (this page's upload, or one found on connect)

  function setStatus(text) { statusEl.textContent = text; }
  // Display labels come from the server-side meeting_status filter (data-label-<status>).
  function setMeetingStatus(status) {
    if (meetingStatusEl) meetingStatusEl.textContent = meetingStatusEl.dataset["label" + status[0].toUpperCase() + status.slice(1)] || status;
  }
  // Live recording and file upload share the meeting: only one audio source at a time.
  function setButtons(isRecording) {
    startBtn.disabled = isRecording || uploadPhase !== "idle";
    stopBtn.disabled = !isRecording;
    if (uploadForm) uploadBtn.disabled = uploadFile.disabled = isRecording || uploadPhase !== "idle";
  }
  function setUploadPhase(phase) {
    uploadPhase = phase;
    setButtons(recording);
  }

  function formatSeconds(ms) { return (ms / 1000).toFixed(1) + "s"; }

  function addSegment(seg) {
    const empty = document.getElementById("transcript-empty");
    if (empty) empty.remove();
    if (tbody.querySelector(`tr[data-segment-id="${CSS.escape(seg.id)}"]`)) return;

    const tr = document.createElement("tr");
    tr.dataset.segmentId = seg.id;
    tr.dataset.startMs = seg.start_ms;
    for (const value of [formatSeconds(seg.start_ms), seg.speaker_label || "-", seg.text]) {
      const td = document.createElement("td");
      td.textContent = value; // never innerHTML: transcript text is untrusted
      tr.appendChild(td);
    }
    tr.children[1].classList.add("speaker");

    // Keep rows ordered by start time.
    const next = [...tbody.querySelectorAll("tr[data-start-ms]")]
      .find((row) => Number(row.dataset.startMs) > seg.start_ms);
    tbody.insertBefore(tr, next || null);
    tr.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  function sendPending() {
    if (!pendingSamples) return;
    const merged = new Int16Array(pendingSamples);
    let offset = 0;
    for (const part of pending) { merged.set(part, offset); offset += part.length; }
    pending = [];
    pendingSamples = 0;
    socket.emit("audio_chunk", merged.buffer);
  }

  async function teardownAudio() {
    if (stream) stream.getTracks().forEach((t) => t.stop());
    if (audioCtx) await audioCtx.close();
    stream = null;
    audioCtx = null;
  }

  async function start() {
    setButtons(true);
    setStatus("正在取得麥克風權限…");
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
      });
    } catch (err) {
      setButtons(false);
      setStatus("無法使用麥克風：" + err.message);
      return;
    }

    audioCtx = new AudioContext();
    await audioCtx.audioWorklet.addModule(workletUrl);
    const source = audioCtx.createMediaStreamSource(stream);
    const node = new AudioWorkletNode(audioCtx, "pcm-downsampler");
    const mute = audioCtx.createGain();
    mute.gain.value = 0; // keep the graph running without playing the mic back
    source.connect(node).connect(mute).connect(audioCtx.destination);

    node.port.onmessage = (e) => {
      if (!recording) return;
      const samples = new Int16Array(e.data);
      pending.push(samples);
      pendingSamples += samples.length;
      if (pendingSamples >= SEND_SAMPLES) sendPending();
    };

    if (!socket.connected) socket.connect();
    socket.emit("start_recording", { meeting_id: meetingId }, async (ack) => {
      if (!ack || !ack.ok) {
        await teardownAudio();
        setButtons(false);
        setStatus("無法開始錄音：" + (ERRORS[ack && ack.error] || (ack && ack.error) || "未知錯誤"));
        return;
      }
      recording = true;
      setMeetingStatus("recording");
      setStatus("● 錄音中，逐字稿會在每段話結束後出現");
    });
  }

  async function stop() {
    if (!recording) return;
    recording = false;
    sendPending();
    await teardownAudio();
    stopBtn.disabled = true;
    setStatus("正在處理最後一段語音…");
    socket.emit("stop_recording");
  }

  socket.on("transcript_segment", addSegment);

  socket.on("speaker_labels", (data) => {
    for (const item of data.labels) {
      const row = tbody.querySelector(`tr[data-segment-id="${CSS.escape(item.id)}"]`);
      if (row) row.querySelector(".speaker").textContent = item.speaker_label;
    }
    setStatus("發言人標記已更新");
  });

  socket.on("recording_stopped", (data) => {
    recording = false;
    uploadPhase = "idle"; // one audio source at a time: a finished recording frees the meeting
    setButtons(false);
    setMeetingStatus("transcribed");
    setStatus(`錄音已結束，本次共 ${data.segments} 段逐字稿`);
  });

  socket.on("recording_error", async (data) => {
    const msg = ERRORS[data.error] || data.error;
    if (data.error === "max_duration_reached") {
      recording = false;
      await teardownAudio();
    }
    setStatus("⚠ " + msg);
  });

  socket.on("connect_error", () => setStatus("無法連線到轉錄服務，請重新整理頁面後再試"));
  socket.on("disconnect", async () => {
    if (recording) {
      recording = false;
      await teardownAudio();
      setButtons(false);
      setStatus("連線中斷，錄音已停止（已轉錄的內容已保存）");
    }
  });

  // --- uploaded recording (REQ-47) ------------------------------------------------
  function formatClock(ms) {
    const total = Math.floor(ms / 1000);
    const h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), sec = total % 60;
    const mm = String(m).padStart(h ? 2 : 1, "0"), ss = String(sec).padStart(2, "0");
    return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
  }
  function setUploadStatus(text) { if (uploadStatus) uploadStatus.textContent = text; }
  function showProgress(percent) {
    if (!uploadProgress) return;
    uploadProgress.hidden = percent === null;
    if (percent === undefined) uploadProgress.removeAttribute("value"); // indeterminate
    else if (percent !== null) uploadProgress.value = percent;
  }

  const UPLOAD_ERRORS = {
    invalid_audio: "無法讀取音訊，檔案可能已損毀或不是錄音檔",
    too_long: "錄音長度超過上限",
    transcription_failed: "轉錄發生錯誤，請查看伺服器紀錄",
  };

  function uploadAudio(event) {
    event.preventDefault();
    const file = uploadFile.files[0];
    if (!file) { setUploadStatus("請選擇要上傳的錄音檔"); return; }
    const ext = file.name.includes(".") ? file.name.split(".").pop().toLowerCase() : "";
    if (!UPLOAD_TYPES.includes(ext)) { setUploadStatus("⚠ 不支援的檔案格式"); return; }
    const maxBytes = Number(uploadForm.dataset.maxBytes);
    if (file.size > maxBytes) {
      setUploadStatus(`⚠ 檔案超過大小上限（${Math.floor(maxBytes / 1048576)} MB）`);
      return;
    }

    const xhr = new XMLHttpRequest();
    xhr.open("POST", uploadForm.action);
    xhr.setRequestHeader("Accept", "application/json");
    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      const percent = Math.round((e.loaded / e.total) * 100);
      showProgress(percent);
      setUploadStatus(`上傳中… ${percent}%`);
    };
    xhr.onload = () => {
      let body = {};
      try { body = JSON.parse(xhr.responseText); } catch (_) { /* e.g. 413 HTML page */ }
      if (xhr.status === 202) {
        if (uploadPhase === "uploading") { // finished events may already have arrived for a short file
          setUploadPhase("transcribing");
          setMeetingStatus("transcribing");
          showProgress(undefined);
          setUploadStatus("上傳完成，正在轉錄…（可切換頁籤，完成後逐字稿會出現在下方）");
        }
        uploadForm.reset();
        return;
      }
      setUploadPhase("idle");
      showProgress(null);
      const msg = xhr.status === 413 ? "檔案超過大小上限" : (body.message || `上傳失敗（HTTP ${xhr.status}）`);
      setUploadStatus("⚠ " + msg);
    };
    xhr.onerror = () => {
      setUploadPhase("idle");
      showProgress(null);
      setUploadStatus("⚠ 上傳失敗，請檢查網路連線後再試");
    };

    setUploadPhase("uploading");
    showProgress(0);
    setUploadStatus("上傳中… 0%");
    xhr.send(new FormData(uploadForm));
  }

  socket.on("upload_progress", (data) => {
    if (uploadPhase === "idle") setUploadPhase("transcribing");
    if (data.total_ms) {
      showProgress(Math.min(100, Math.round((data.processed_ms / data.total_ms) * 100)));
      setUploadStatus(`轉錄中… ${formatClock(data.processed_ms)} / ${formatClock(data.total_ms)}`);
    } else {
      showProgress(undefined);
      setUploadStatus(`轉錄中… 已處理 ${formatClock(data.processed_ms)}`);
    }
  });

  socket.on("upload_finished", (data) => {
    setUploadPhase("idle");
    showProgress(null);
    setMeetingStatus("transcribed");
    setUploadStatus(`✓ 錄音檔轉錄完成，新增 ${data.segments} 段逐字稿（錄音檔已刪除）`);
  });

  socket.on("upload_error", (data) => {
    setUploadPhase("idle");
    showProgress(null);
    if (data.segments) setMeetingStatus("transcribed");
    const kept = data.segments ? `，已轉出的 ${data.segments} 段逐字稿已保存` : "";
    setUploadStatus(`⚠ ${UPLOAD_ERRORS[data.error] || data.error}${kept}（錄音檔已刪除）`);
  });

  // Join the meeting's room on every (re)connect, so an upload still running on the server
  // (e.g. after a page reload) keeps reporting progress here.
  socket.on("connect", () => {
    socket.emit("watch_meeting", { meeting_id: meetingId }, (ack) => {
      if (ack && ack.ok && ack.busy && !recording && uploadPhase === "idle") {
        setUploadPhase("transcribing");
        setUploadStatus("此會議正在錄音或轉錄錄音檔中…");
      }
    });
  });

  if (uploadForm) uploadForm.addEventListener("submit", uploadAudio);

  startBtn.addEventListener("click", start);
  stopBtn.addEventListener("click", stop);
  window.addEventListener("beforeunload", (e) => {
    if (recording || uploadPhase === "uploading") { e.preventDefault(); e.returnValue = ""; }
  });
})();
