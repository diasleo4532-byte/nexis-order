(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const chat = $("chat");
  const input = $("input");
  const micBtn = $("micBtn");
  const sendBtn = $("sendBtn");
  const stopBtn = $("stopBtn");
  const welcomeHTML = $("welcome") ? $("welcome").outerHTML : "";

  const state = {
    turnActive: false,
    current: null,
    buffer: "",
    pendingTools: [],
    tts: localStorage.getItem("nexis.tts") === "1",
    handsfree: false,
    voiceMode: null,
    confirmId: null,
    lastMeta: null,
  };

  let audio = null;
  let toastTimer = null;
  let warnedDisconnect = false;

  // ---------- helpers ----------

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function esc(s) {
    return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function md(text) {
    const parts = esc(text).split(/```/);
    let out = "";
    for (let i = 0; i < parts.length; i++) {
      let chunk = parts[i];
      if (i % 2 === 1) {
        const nl = chunk.indexOf("\n");
        if (nl > -1 && /^[a-zA-Z0-9_+#.-]*\s*$/.test(chunk.slice(0, nl))) chunk = chunk.slice(nl + 1);
        out += `<pre><code>${chunk.replace(/\n$/, "")}</code></pre>`;
      } else {
        let h = chunk.replace(/`([^`\n]+)`/g, "<code>$1</code>");
        h = h.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
        h = h.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
        h = h
          .split(/\n{2,}/)
          .map((block) => `<p>${block.replace(/\n/g, "<br>")}</p>`)
          .join("");
        out += h;
      }
    }
    return out;
  }

  function toast(message) {
    const node = $("toast");
    node.textContent = message;
    node.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (node.hidden = true), 2800);
  }

  function scrollDown(force) {
    const nearBottom = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 160;
    if (force || nearBottom) chat.scrollTop = chat.scrollHeight;
  }

  function hideWelcome() {
    const w = $("welcome");
    if (w) w.remove();
  }

  function setBusy(busy) {
    stopBtn.hidden = !busy;
    sendBtn.disabled = busy;
    stopBtn.disabled = false;
    stopBtn.textContent = "■ Stop";
  }

  // ---------- transcript ----------

  function addUser(text) {
    hideWelcome();
    const root = el("div", "msg user");
    root.appendChild(el("div", "avatar", "You"));
    const body = el("div", "body");
    const content = el("div", "content");
    content.textContent = text;
    body.appendChild(content);
    root.appendChild(body);
    chat.appendChild(root);
    scrollDown(true);
  }

  function addAssistantStatic(text) {
    hideWelcome();
    const root = el("div", "msg assistant");
    root.appendChild(el("div", "avatar", "N"));
    const body = el("div", "body");
    const content = el("div", "content");
    content.innerHTML = md(text);
    body.appendChild(content);
    root.appendChild(body);
    chat.appendChild(root);
  }

  function ensureAssistant() {
    if (state.current) return state.current;
    hideWelcome();
    const root = el("div", "msg assistant");
    root.appendChild(el("div", "avatar", "N"));
    const body = el("div", "body");
    const status = el("div", "statusLine");
    const spinner = el("div", "spinner");
    const statusText = el("span", "", "Thinking…");
    status.append(spinner, statusText);
    const tools = el("div", "tools");
    tools.hidden = true;
    const content = el("div", "content");
    const meta = el("div", "meta");
    meta.hidden = true;
    body.append(status, tools, content, meta);
    root.appendChild(body);
    chat.appendChild(root);
    state.current = { root, status, statusText, tools, content, meta, spinner };
    scrollDown(true);
    return state.current;
  }

  function setStatus(text) {
    const cur = ensureAssistant();
    cur.status.hidden = false;
    cur.statusText.textContent = text;
    scrollDown();
  }

  function hideStatus() {
    if (state.current) state.current.status.hidden = true;
  }

  function addToolChip(name, summary) {
    const cur = ensureAssistant();
    cur.tools.hidden = false;
    const chip = el("div", "tool-chip");
    chip.appendChild(el("span", "t-dot"));
    const label = el("span", "", summary ? `${summary}` : `Using ${name}`);
    label.title = name;
    chip.appendChild(label);
    cur.tools.appendChild(chip);
    state.pendingTools.push({ name, el: chip, label });
    scrollDown();
  }

  function resolveToolChip(name, detail) {
    let entry = null;
    for (let i = state.pendingTools.length - 1; i >= 0; i--) {
      if (state.pendingTools[i].name === name) {
        entry = state.pendingTools.splice(i, 1)[0];
        break;
      }
    }
    if (!entry) return;
    if (detail && detail.ok) {
      entry.el.classList.add("ok");
    } else if (detail && detail.declined) {
      entry.el.classList.add("declined");
      entry.label.textContent += " · declined";
    } else {
      entry.el.classList.add("fail");
      entry.label.textContent += " · failed";
    }
  }

  function renderBuffer() {
    const cur = ensureAssistant();
    cur.content.innerHTML = md(state.buffer);
  }

  function finishTurn(data) {
    const text = (data && data.text) || "";
    const keepPartial = !!(data && data.interrupted && state.buffer);
    if (!keepPartial && text && text !== state.buffer) {
      state.buffer = text;
      renderBuffer();
    }
    if (state.confirmId) hideConfirm();
    if (state.current) {
      const cur = state.current;
      cur.status.hidden = true;
      for (const pending of state.pendingTools) {
        pending.el.classList.add("fail");
      }
      state.pendingTools = [];
      const bits = [];
      if (typeof data.seconds === "number") bits.push(`${data.seconds}s`);
      if (data.steps) bits.push(`${data.steps} step${data.steps === 1 ? "" : "s"}`);
      if (data.tools) bits.push(`${data.tools} tool${data.tools === 1 ? "" : "s"}`);
      if (data.interrupted) bits.push("stopped");
      if (bits.length && (state.buffer || text)) {
        cur.meta.textContent = bits.join(" · ");
        cur.meta.hidden = false;
      }
    }
    state.turnActive = false;
    state.voiceMode = state.voiceMode === "recording" ? null : state.voiceMode;
    micBtn.classList.remove("rec");
    setBusy(false);
    if (state.buffer) speak(stripForSpeech(state.buffer));
    scrollDown(true);
    state.current = null;
    state.buffer = "";
  }

  function showError(message) {
    hideStatus();
    const cur = ensureAssistant();
    cur.content.appendChild(el("div", "error-note", message));
    scrollDown();
  }

  function showNotice(message) {
    const cur = ensureAssistant();
    cur.content.appendChild(el("div", "notice", message));
    scrollDown();
  }

  // ---------- speech ----------

  function stripForSpeech(text) {
    return text
      .replace(/```[\s\S]*?```/g, ". Code block. ")
      .replace(/`([^`]*)`/g, "$1")
      .replace(/[*_#>|]/g, " ")
      .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, 1500);
  }

  function speak(text) {
    if (!state.tts || !text) return;
    try {
      if (!audio) {
        audio = new Audio();
        audio.preload = "none";
      }
      audio.pause();
      audio.src = "/api/tts?text=" + encodeURIComponent(text);
      audio.play().catch(() => {});
    } catch (_) {
      /* audio unavailable */
    }
  }

  // ---------- sending ----------

  async function sendMessage(text) {
    text = (text || "").trim();
    if (!text) return;
    if (state.turnActive) {
      toast("Nexis is still answering — press Stop or wait.");
      return;
    }
    addUser(text);
    input.value = "";
    autosize();
    state.current = null;
    state.buffer = "";
    state.pendingTools = [];
    state.turnActive = true;
    setBusy(true);
    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        state.turnActive = false;
        setBusy(false);
        toast(data.error || "Could not send that message.");
      }
    } catch (_) {
      state.turnActive = false;
      setBusy(false);
      toast("Cannot reach Nexis. Is the app still running?");
    }
  }

  async function stopTurn() {
    stopBtn.disabled = true;
    stopBtn.textContent = "Stopping…";
    try {
      await fetch("/api/stop", { method: "POST" });
    } catch (_) {
      stopBtn.disabled = false;
      stopBtn.textContent = "■ Stop";
    }
  }

  // ---------- voice ----------

  async function startMic() {
    if (state.voiceMode) return;
    if (state.turnActive) {
      toast("Wait for Nexis to finish first.");
      return;
    }
    state.voiceMode = "starting";
    try {
      const res = await fetch("/api/voice", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "start" }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || "Microphone is unavailable.");
      }
      state.voiceMode = "recording";
      micBtn.classList.add("rec");
      setWakeLabel("recording");
    } catch (err) {
      state.voiceMode = null;
      toast(err.message || "Microphone is unavailable.");
    }
  }

  async function stopMic() {
    if (state.voiceMode !== "recording") return;
    state.voiceMode = "transcribing";
    micBtn.classList.remove("rec");
    setWakeLabel("transcribing");
    try {
      const res = await fetch("/api/voice", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "stop" }),
      });
      const data = await res.json().catch(() => ({}));
      const text = (data.text || "").trim();
      if (text) {
        sendMessage(text);
      } else {
        toast("I did not catch that — try again a little closer to the mic.");
      }
    } catch (_) {
      toast("Transcription failed.");
    } finally {
      state.voiceMode = null;
      micBtn.classList.remove("rec");
      setWakeLabel("idle");
    }
  }

  function toggleMic() {
    if (state.voiceMode === "recording") stopMic();
    else if (!state.voiceMode) startMic();
  }

  function setWakeLabel(mode) {
    const dot = $("wakeDot");
    const label = $("wakeStatus");
    dot.classList.remove("on", "hearing");
    if (mode === "recording") {
      dot.classList.add("on");
      label.textContent = "Recording…";
    } else if (mode === "transcribing") {
      dot.classList.add("hearing");
      label.textContent = "Transcribing…";
    } else if (state.handsfree && mode === "hearing") {
      dot.classList.add("hearing");
      label.textContent = "Heard you…";
    } else if (state.handsfree) {
      dot.classList.add("on");
      label.textContent = "Listening for wake word";
    } else {
      label.textContent = "Voice idle";
    }
  }

  // ---------- hands-free ----------

  function applyHandsfree(on) {
    state.handsfree = !!on;
    $("handsfreeToggle").checked = state.handsfree;
    $("handsfreeToggle2").checked = state.handsfree;
    $("wakeBar").hidden = !state.handsfree;
    $("wakeBarText").textContent = "Listening for “Nexis”…";
    setWakeLabel("idle");
  }

  async function setHandsfree(on) {
    const toggles = [$("handsfreeToggle"), $("handsfreeToggle2")];
    toggles.forEach((t) => (t.checked = on));
    try {
      const res = await fetch("/api/handsfree", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ on }),
      });
      if (!res.ok) throw new Error();
      const data = await res.json();
      applyHandsfree(data.on);
      toast(data.on ? "Hands-free on — say “Nexis, …”" : "Hands-free off");
    } catch (_) {
      toggles.forEach((t) => (t.checked = !on));
      toast("Could not change the wake-word setting.");
    }
  }

  // ---------- confirmations ----------

  function showConfirm(data) {
    state.confirmId = data.id;
    $("confirmTitle").textContent = data.summary || data.tool || "An action needs approval";
    const args = data.details && data.details.arguments;
    $("confirmDetails").textContent = args && Object.keys(args).length
      ? JSON.stringify(args, null, 2)
      : "(no arguments)";
    $("confirmModal").hidden = false;
    $("denyBtn").focus();
  }

  function hideConfirm() {
    state.confirmId = null;
    $("confirmModal").hidden = true;
  }

  async function answerConfirm(approved) {
    const id = state.confirmId;
    if (!id) return;
    hideConfirm();
    try {
      await fetch("/api/confirm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id, approved }),
      });
    } catch (_) {
      toast("Could not deliver your answer.");
    }
  }

  // ---------- state / settings ----------

  function applyState(s) {
    if (!s) return;
    $("modelBadge").textContent = s.model || "…";
    $("modelSmall").textContent = s.model || "—";
    $("engineSmall").textContent = "Ollama";
    $("ollamaBadge").hidden = !!s.ollama;
    if (s.ollama_detail) $("ollamaBadge").title = s.ollama_detail;
    $("workspacePath").textContent = s.workspace || "—";
    $("versionLabel").textContent = "Nexis " + (s.version ? "v" + s.version : "");
    applyHandsfree(s.handsfree);
    if (s.turn_active) setBusy(true);
    fillModels(s.models, s.model);
  }

  function fillModels(models, current) {
    const select = $("modelSelect");
    const list = Array.isArray(models) && models.length ? models.slice() : [];
    if (current && !list.includes(current)) list.unshift(current);
    const before = select.value;
    select.innerHTML = "";
    for (const name of list) {
      const opt = el("option", "", name);
      opt.value = name;
      select.appendChild(opt);
    }
    if (before && list.includes(before)) select.value = before;
    else if (current) select.value = current;
  }

  async function refreshModels() {
    try {
      const res = await fetch("/api/models");
      if (!res.ok) return;
      const data = await res.json();
      fillModels(data.models, data.model);
    } catch (_) {
      /* keep the existing list */
    }
  }

  async function loadHistory() {
    try {
      const res = await fetch("/api/history");
      if (!res.ok) return;
      const data = await res.json();
      for (const msg of data.messages || []) {
        if (msg.role === "user") addUser(msg.content);
        else if (msg.role === "assistant" && msg.content) addAssistantStatic(msg.content);
      }
    } catch (_) {
      /* empty transcript is fine */
    }
  }

  function autosize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 180) + "px";
  }

  // ---------- events ----------

  function handleEvent(ev) {
    switch (ev.type) {
      case "status":
        setStatus(ev.text || "Working…");
        break;
      case "delta":
        hideStatus();
        state.buffer += ev.text || "";
        renderBuffer();
        scrollDown();
        break;
      case "tool_call":
        if (ev.tool) addToolChip(ev.tool, ev.detail && ev.detail.summary);
        break;
      case "tool_result":
        if (ev.tool) resolveToolChip(ev.tool, ev.detail);
        break;
      case "notice":
        if (ev.text) showNotice(ev.text);
        break;
      case "confirm":
        showConfirm(ev);
        setStatus("Waiting for your approval…");
        break;
      case "done":
        finishTurn(ev);
        break;
      case "error":
        showError(ev.text || "Something went wrong.");
        break;
      case "wake":
        if (ev.command) sendMessage(ev.command);
        else toast("I'm listening — say what you need.");
        break;
      case "wake_state":
        if (ev.state === "hearing") {
          setWakeLabel("hearing");
          $("wakeBarText").textContent = "I'm listening…";
        } else {
          setWakeLabel("idle");
          $("wakeBarText").textContent = "Listening for “Nexis”…";
        }
        break;
      case "voice_state":
        if (ev.state === "recording") {
          micBtn.classList.add("rec");
          setWakeLabel("recording");
        } else if (ev.state === "transcribing") {
          micBtn.classList.remove("rec");
          setWakeLabel("transcribing");
        } else {
          micBtn.classList.remove("rec");
          setWakeLabel("idle");
        }
        break;
      case "handsfree":
        applyHandsfree(ev.on);
        break;
      case "model":
        $("modelBadge").textContent = ev.model;
        $("modelSmall").textContent = ev.model;
        if ($("modelSelect").value) $("modelSelect").value = ev.model;
        break;
      case "session":
        break;
      default:
        break;
    }
  }

  function connect() {
    const source = new EventSource("/api/events");
    source.onopen = () => {
      warnedDisconnect = false;
    };
    source.onmessage = (event) => {
      try {
        handleEvent(JSON.parse(event.data));
      } catch (_) {
        /* ignore malformed frames */
      }
    };
    source.onerror = () => {
      if (!warnedDisconnect) {
        warnedDisconnect = true;
        toast("Connection to Nexis dropped — reconnecting…");
      }
    };
  }

  function newConversation() {
    if (state.turnActive) {
      toast("Stop the current answer first.");
      return;
    }
    fetch("/api/session/new", { method: "POST" })
      .then(() => {
        chat.innerHTML = welcomeHTML;
        state.current = null;
        state.buffer = "";
        state.pendingTools = [];
        state.lastMeta = null;
      })
      .catch(() => toast("Could not start a new conversation."));
  }

  function openSettings() {
    refreshModels();
    $("ttsToggle").checked = state.tts;
    $("settingsModal").hidden = false;
  }

  // ---------- wiring ----------

  function bind() {
    sendBtn.addEventListener("click", () => sendMessage(input.value));
    input.addEventListener("input", autosize);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMessage(input.value);
      }
    });

    micBtn.addEventListener("click", toggleMic);

    stopBtn.addEventListener("click", stopTurn);

    $("newChatBtn").addEventListener("click", newConversation);

    $("handsfreeToggle").addEventListener("change", (e) => setHandsfree(e.target.checked));
    $("handsfreeToggle2").addEventListener("change", (e) => setHandsfree(e.target.checked));

    $("settingsBtn").addEventListener("click", openSettings);
    $("closeSettingsBtn").addEventListener("click", () => ($("settingsModal").hidden = true));

    $("modelSelect").addEventListener("change", async (e) => {
      const name = e.target.value;
      try {
        const res = await fetch("/api/model", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ model: name }),
        });
        if (!res.ok) throw new Error();
        $("modelBadge").textContent = name;
        $("modelSmall").textContent = name;
        toast("Model switched to " + name);
      } catch (_) {
        toast("Could not switch models.");
      }
    });

    $("ttsToggle").addEventListener("change", (e) => {
      state.tts = e.target.checked;
      localStorage.setItem("nexis.tts", state.tts ? "1" : "0");
      if (!state.tts && audio) audio.pause();
    });

    $("openWorkspaceBtn").addEventListener("click", () => {
      fetch("/api/workspace/open", { method: "POST" }).catch(() => toast("Could not open the folder."));
    });

    $("approveBtn").addEventListener("click", () => answerConfirm(true));
    $("denyBtn").addEventListener("click", () => answerConfirm(false));

    chat.addEventListener("click", (e) => {
      const chip = e.target.closest("[data-say]");
      if (chip) sendMessage(chip.getAttribute("data-say"));
    });

    document.addEventListener("keydown", (e) => {
      if (e.key === "F2") {
        e.preventDefault();
        toggleMic();
      } else if (e.key === "Escape") {
        $("settingsModal").hidden = true;
        if (state.confirmId) answerConfirm(false);
      }
    });
  }

  async function init() {
    bind();
    autosize();
    $("ttsToggle").checked = state.tts;
    setBusy(false);
    try {
      const res = await fetch("/api/state");
      if (res.ok) applyState(await res.json());
    } catch (_) {
      $("ollamaBadge").hidden = false;
    }
    await loadHistory();
    connect();
    scrollDown(true);
  }

  init();
})();
