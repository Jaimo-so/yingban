const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const state = {
  authenticated: false,
  mode: null,
  chatHistory: [],
  selectedMovieId: null,
  historyState: "watched",
  sending: false,
  voiceConfigured: false,
  voiceSupported: false,
  voiceMode: false,
  voiceAutoPlay: readVoiceAutoPlayPreference(),
  openings: {
    discussion: "我在。片名告诉我就好；有同名版本的话，我们再一起确认。",
    discussion_movie: "嗯，《{movie}》。先不急着分析，你看完后脑子里冒出来的第一句话是什么？",
    recommendation: "今晚想让电影替你做什么？放松一下、陪你待会儿，还是换个角度看看最近的一件事？",
  },
  activeVoiceAudio: null,
  reflectionMovie: null,
  reflectionTrigger: null,
  recorder: null,
  recordingStream: null,
  recordingChunks: [],
  recordingStartedAt: 0,
  recordingTimer: null,
  cancelRecording: false,
};

function readVoiceAutoPlayPreference() {
  try {
    const saved = window.localStorage.getItem("yingban.voiceAutoPlay");
    return saved === null ? true : saved === "true";
  } catch {
    return true;
  }
}

function saveVoiceAutoPlayPreference(value) {
  try { window.localStorage.setItem("yingban.voiceAutoPlay", String(value)); } catch { /* no-op */ }
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.error || `请求失败（${response.status}）`);
    error.status = response.status;
    throw error;
  }
  return data;
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("is-visible");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove("is-visible"), 2800);
}

function showAuthenticated(me) {
  state.authenticated = true;
  $("#login-view").hidden = true;
  $("#app-shell").hidden = false;
  $("#watched-count").textContent = me.account?.watched_count ?? 0;
  $("#mode-badge").hidden = !me.demo_mode;
  state.voiceConfigured = Boolean(me.voice_available);
  state.openings = { ...state.openings, ...(me.openings || {}) };
  state.voiceSupported = Boolean(window.MediaRecorder && navigator.mediaDevices?.getUserMedia);
  const voiceButton = $("#voice-mode-button");
  voiceButton.disabled = !state.voiceSupported;
  voiceButton.title = state.voiceSupported ? "语音输入" : "当前浏览器不支持录音";
  updateVoiceAutoPlayToggle();
  setVoiceMode(false);
  navigate("home");
}

function showLogin() {
  state.authenticated = false;
  $("#login-view").hidden = false;
  $("#app-shell").hidden = true;
  $("#invite-code").focus();
}

async function boot() {
  try {
    const me = await api("/api/me");
    me.authenticated ? showAuthenticated(me) : showLogin();
  } catch {
    showLogin();
  }
}

function navigate(view) {
  $$(".view").forEach((node) => { node.hidden = node.id !== `${view}-view`; });
  $$("[data-nav]").forEach((node) => node.classList.toggle("is-active", node.dataset.nav === view));
  if (view === "history") loadHistory();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function openChat(mode, movie = null) {
  try {
    const me = await api("/api/me");
    state.openings = { ...state.openings, ...(me.openings || {}) };
  } catch { /* keep the last known openings */ }
  state.mode = mode;
  state.chatHistory = [];
  state.selectedMovieId = movie?.id || null;
  $("#chat-messages").replaceChildren();
  $("#spoiler-control").hidden = mode !== "discussion";

  const discussion = mode === "discussion";
  $("#chat-kicker").textContent = discussion ? "散场之后" : "下一部电影";
  $("#chat-title").textContent = discussion ? (movie ? `聊聊《${movie.title_zh}》` : "刚看完哪一部？") : "此刻想看点什么？";
  $("#chat-input").placeholder = discussion ? "片名，或者看完后的第一句话……" : "说说此刻的心情、口味或最近的烦恼……";
  renderChips(discussion
    ? ["我很喜欢，但说不上为什么", "有个地方我一直没看懂", "结局让我有点难受"]
    : ["最近压力很大，想放松", "想看一部能给我力量的", "不要爱情片，想看点烧脑的"]
  );
  navigate("chat");
  const opening = discussion
    ? (movie ? state.openings.discussion_movie.replaceAll("{movie}", movie.title_zh) : state.openings.discussion)
    : state.openings.recommendation;
  renderMessage("assistant", opening);
  $("#chat-input").focus();
}

function renderChips(items) {
  const wrap = $("#prompt-chips");
  wrap.replaceChildren(...items.map((text) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "prompt-chip";
    button.textContent = text;
    button.addEventListener("click", () => {
      $("#chat-input").value = text;
      autoResize($("#chat-input"));
      $("#chat-input").focus();
    });
    return button;
  }));
}

function renderMessage(role, text, loading = false, options = {}) {
  const message = document.createElement("div");
  message.className = `message ${role}`;
  if (role === "assistant") {
    const avatar = document.createElement("span");
    avatar.className = "avatar";
    avatar.textContent = "映";
    message.append(avatar);
  }
  const bubble = document.createElement("div");
  bubble.className = "message-bubble";
  if (loading) {
    bubble.innerHTML = '<span class="typing" aria-label="阿映正在回复"><span></span><span></span><span></span></span>';
    message.dataset.loading = "true";
  } else {
    if (role === "assistant") {
      const name = document.createElement("span");
      name.className = "message-name";
      name.textContent = "阿映";
      bubble.append(name);
    }
    bubble.append(document.createTextNode(text));
    if (options.voiceDuration) {
      const voiceMeta = document.createElement("span");
      voiceMeta.className = "voice-message-meta";
      voiceMeta.textContent = `◖ ${Math.max(1, Math.round(options.voiceDuration))}″ 语音已转写`;
      bubble.append(voiceMeta);
    }
  }
  message.append(bubble);
  $("#chat-messages").append(message);
  message.scrollIntoView({ behavior: "smooth", block: "end" });
  return message;
}

function posterNode(movie) {
  const poster = document.createElement("div");
  poster.className = "poster";
  const year = document.createElement("span");
  year.className = "poster-year";
  year.textContent = `${movie.year || "—"}${movie.regions?.length ? ` · ${movie.regions.join(" / ")}` : ""}`;
  const title = document.createElement("strong");
  title.className = "poster-title";
  title.textContent = movie.title_zh;
  if (movie.poster_url) {
    const image = document.createElement("img");
    image.className = "poster-image";
    image.src = movie.poster_url;
    image.alt = `《${movie.title_zh}》电影海报`;
    image.loading = "lazy";
    image.decoding = "async";
    image.referrerPolicy = "no-referrer";
    poster.classList.add("is-loading");
    image.addEventListener("load", () => {
      poster.classList.remove("is-loading");
      poster.classList.add("has-image");
    }, { once: true });
    image.addEventListener("error", () => {
      image.remove();
      poster.classList.remove("has-image", "is-loading");
      poster.classList.add("image-failed");
    }, { once: true });
    poster.append(image);
  }
  poster.append(year, title);
  return poster;
}

function renderRecommendations(movies) {
  if (!movies?.length) return;
  const row = document.createElement("div");
  row.className = "recommendation-row";
  movies.forEach((movie) => {
    const card = document.createElement("article");
    card.className = "movie-card";

    const poster = posterNode(movie);

    const body = document.createElement("div");
    body.className = "movie-card-body";
    const meta = document.createElement("span");
    meta.className = "movie-meta";
    meta.textContent = (movie.genres || []).join(" · ");
    const reason = document.createElement("p");
    reason.className = "movie-reason";
    reason.textContent = movie.match_reason || movie.summary;
    const notes = document.createElement("p");
    notes.className = "content-note";
    notes.textContent = movie.content_notes?.length ? `留意：${movie.content_notes.slice(0, 2).join("、")}` : "";
    const actions = document.createElement("div");
    actions.className = "movie-actions";
    actions.append(
      miniButton("想看", () => markMovie(movie, "watchlist", "recommendation")),
      miniButton("看过了，换一部", async () => {
        await markMovie(movie, "watched", "recommendation_feedback");
        await sendMessage(`《${movie.title_zh}》我看过了，换一个方向相近但没看过的。`);
      }),
      miniButton("聊聊这部", async () => {
        await markMovie(movie, "watched", "discussion");
        openChat("discussion", movie);
      })
    );
    body.append(meta, reason, notes, actions);
    card.append(poster, body);
    row.append(card);
  });
  $("#chat-messages").append(row);
  row.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function miniButton(label, action) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "mini-button";
  button.textContent = label;
  button.addEventListener("click", async () => {
    button.disabled = true;
    try { await action(); } catch (error) { showToast(error.message); }
    finally { button.disabled = false; }
  });
  return button;
}

async function sendMessage(forcedText = null, options = {}) {
  if (state.sending) return;
  const input = $("#chat-input");
  const text = (forcedText ?? input.value).trim();
  if (!text) return;
  const priorHistory = [...state.chatHistory];
  state.chatHistory.push({ role: "user", content: text });
  renderMessage("user", text, false, options);
  input.value = "";
  autoResize(input);
  $("#prompt-chips").replaceChildren();
  const loading = renderMessage("assistant", "", true);
  state.sending = true;
  $("#send-button").disabled = true;
  try {
    const data = await api("/api/chat", {
      method: "POST",
      body: JSON.stringify({
        mode: state.mode,
        message: text,
        history: priorHistory,
        selected_movie_id: state.selectedMovieId,
        spoilers_allowed: $("#spoilers-allowed").checked,
      }),
    });
    loading.remove();
    if (data.selected_movie) {
      state.selectedMovieId = data.selected_movie.id;
      $("#chat-title").textContent = `聊聊《${data.selected_movie.title_zh}》`;
    }
    const assistantMessage = renderMessage("assistant", data.reply);
    state.chatHistory.push({ role: "assistant", content: data.reply });
    if (state.voiceConfigured) attachVoiceReply(assistantMessage, data.reply);
    renderRecommendations(data.recommendations);
    if (data.reflection_updated && data.selected_movie) {
      showToast(`《${data.selected_movie.title_zh}》的观后感笔记已更新`);
    }
    if (data.memory_event) {
      showToast(`${data.memory_event.message} · 可在“我的电影”里撤销`);
      refreshMe();
    }
  } catch (error) {
    loading.remove();
    renderMessage("assistant", error.message || "我刚才没接上，稍后再试一次吧。");
  } finally {
    state.sending = false;
    $("#send-button").disabled = false;
    if (!state.voiceMode) input.focus();
  }
}

async function markMovie(movie, movieState, source) {
  await api("/api/history", {
    method: "POST",
    body: JSON.stringify({ movie_id: movie.id, state: movieState, source }),
  });
  const labels = { watched: "看过", watchlist: "想看", disliked: "不感兴趣" };
  showToast(`《${movie.title_zh}》已标记为${labels[movieState]}`);
  refreshMe();
}

async function refreshMe() {
  const me = await api("/api/me");
  if (me.authenticated) $("#watched-count").textContent = me.account.watched_count;
}

async function loadHistory() {
  const grid = $("#history-grid");
  const empty = $("#history-empty");
  grid.setAttribute("aria-busy", "true");
  try {
    const data = await api(`/api/history?state=${encodeURIComponent(state.historyState)}`);
    grid.replaceChildren(...data.items.map(historyItem));
    empty.hidden = data.items.length > 0;
  } catch (error) {
    grid.replaceChildren();
    empty.hidden = false;
    $("#history-empty h2").textContent = "暂时没能读取片单";
    $("#history-empty p").textContent = error.message;
  } finally {
    grid.removeAttribute("aria-busy");
  }
}

function historyItem(movie) {
  const card = document.createElement("article");
  card.className = "history-item";
  const poster = posterNode(movie);
  let posterView = poster;
  if (state.historyState === "watched") {
    const posterButton = document.createElement("button");
    posterButton.type = "button";
    posterButton.className = "history-poster-button";
    posterButton.setAttribute("aria-label", `查看《${movie.title_zh}》的观后感笔记`);
    posterButton.append(poster);
    if (movie.note) {
      const noteBadge = document.createElement("span");
      noteBadge.className = "reflection-badge";
      noteBadge.textContent = "有笔记";
      posterButton.append(noteBadge);
    }
    posterButton.addEventListener("click", () => openReflection(movie, posterButton));
    posterView = posterButton;
  }
  const body = document.createElement("div");
  body.className = "history-item-body";
  const heading = document.createElement("h3");
  heading.textContent = movie.title_zh;
  const meta = document.createElement("p");
  meta.textContent = `${movie.title_original} · ${(movie.genres || []).join(" / ")}`;
  const actions = document.createElement("div");
  actions.className = "history-item-actions";
  actions.append(
    miniButton("聊聊", () => openChat("discussion", movie)),
    miniButton("移除", async () => {
      await api(`/api/history/${encodeURIComponent(movie.movie_id)}`, { method: "DELETE" });
      showToast(`已从片单移除《${movie.title_zh}》`);
      await Promise.all([loadHistory(), refreshMe()]);
    })
  );
  body.append(heading, meta, actions);
  card.append(posterView, body);
  return card;
}

function openReflection(movie, trigger) {
  state.reflectionMovie = movie;
  state.reflectionTrigger = trigger;
  $("#reflection-title").textContent = `《${movie.title_zh}》观后感`;
  $("#reflection-meta").textContent = [movie.year, ...(movie.genres || []).slice(0, 3)].filter(Boolean).join(" · ");
  $("#reflection-poster").replaceChildren(posterNode(movie));
  const note = String(movie.note || "").trim();
  $("#reflection-note").textContent = note;
  $("#reflection-note").hidden = !note;
  $("#reflection-empty").hidden = Boolean(note);
  const dialog = $("#reflection-dialog");
  dialog.showModal();
  dialog.querySelector(".icon-button").focus();
}

function autoResize(textarea) {
  textarea.style.height = "auto";
  textarea.style.height = `${Math.min(textarea.scrollHeight, 160)}px`;
}

function setVoiceMode(enabled) {
  if (enabled && !state.voiceSupported) {
    $("#voice-availability-status").textContent = "当前浏览器不支持录音，请使用最新版浏览器";
    showToast("当前浏览器不支持录音");
    return;
  }
  state.voiceMode = Boolean(enabled && state.voiceSupported);
  const toggle = $("#voice-mode-button");
  toggle.setAttribute("aria-pressed", String(state.voiceMode));
  toggle.setAttribute("aria-label", state.voiceMode ? "切换到文字输入" : "切换到语音输入");
  $("#text-composer").hidden = state.voiceMode;
  $("#voice-composer").hidden = !state.voiceMode;
  $("#chat-input").disabled = state.voiceMode;
  $("#send-button").hidden = state.voiceMode;
  const holdButton = $("#hold-to-talk");
  holdButton.disabled = state.voiceMode && !state.voiceConfigured;
  if (state.voiceMode && !state.voiceConfigured) {
    $("#voice-recording-status").textContent = "豆包语音服务待管理员填写 App Key 后启用";
    $("#voice-availability-status").textContent = "语音输入入口已显示，但豆包语音服务尚未配置";
  } else if (state.voiceMode) {
    $("#voice-recording-status").textContent = "松开发送，Esc 取消";
    $("#voice-availability-status").textContent = "已切换到语音输入";
  } else {
    $("#voice-availability-status").textContent = "已切换到文字输入";
  }
  if (state.voiceMode) holdButton.focus();
}

function bestRecordingMimeType() {
  return ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"]
    .find((type) => MediaRecorder.isTypeSupported(type)) || "";
}

async function startRecording() {
  if (state.recorder || state.sending) return;
  if (!state.voiceConfigured) {
    $("#voice-recording-status").textContent = "豆包语音服务待管理员填写 App Key 后启用";
    showToast("请先在管理后台完成豆包语音配置");
    return;
  }
  state.stopRequested = false;
  state.cancelRecording = false;
  const button = $("#hold-to-talk");
  const status = $("#voice-recording-status");
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const mimeType = bestRecordingMimeType();
    const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    state.recordingStream = stream;
    state.recorder = recorder;
    state.recordingChunks = [];
    state.recordingStartedAt = performance.now();
    button.classList.add("is-recording");
    button.textContent = "正在听…松开发送";
    recorder.addEventListener("dataavailable", (event) => {
      if (event.data.size) state.recordingChunks.push(event.data);
    });
    recorder.addEventListener("stop", finishRecording, { once: true });
    recorder.start(250);
    const updateTimer = () => {
      const elapsed = (performance.now() - state.recordingStartedAt) / 1000;
      status.textContent = `正在录音 ${elapsed.toFixed(1)} 秒；松开发送，Esc 取消`;
      if (elapsed >= 60) stopRecording();
    };
    updateTimer();
    state.recordingTimer = setInterval(updateTimer, 200);
    if (state.stopRequested) stopRecording();
  } catch (error) {
    button.classList.remove("is-recording");
    button.textContent = "按住说话";
    status.textContent = "需要麦克风权限才能发送语音";
    showToast(error.name === "NotAllowedError" ? "请允许浏览器使用麦克风" : "暂时无法开始录音");
  }
}

function stopRecording(cancel = false) {
  state.stopRequested = true;
  state.cancelRecording = state.cancelRecording || cancel;
  if (state.recorder?.state === "recording") state.recorder.stop();
}

async function finishRecording() {
  clearInterval(state.recordingTimer);
  const duration = (performance.now() - state.recordingStartedAt) / 1000;
  const recorder = state.recorder;
  const chunks = [...state.recordingChunks];
  state.recordingStream?.getTracks().forEach((track) => track.stop());
  state.recorder = null;
  state.recordingStream = null;
  state.recordingChunks = [];
  const button = $("#hold-to-talk");
  const status = $("#voice-recording-status");
  button.classList.remove("is-recording");
  button.textContent = "按住说话";
  if (state.cancelRecording) {
    status.textContent = "已取消；按住说话";
    return;
  }
  if (duration < 0.5 || !chunks.length) {
    status.textContent = "录音太短，请按住后再说";
    return;
  }
  button.disabled = true;
  status.textContent = "正在把语音转成文字…";
  try {
    const blob = new Blob(chunks, { type: recorder?.mimeType || "audio/webm" });
    const audioBase64 = await blobToBase64(blob);
    const data = await api("/api/voice/transcribe", {
      method: "POST",
      body: JSON.stringify({ audio_base64: audioBase64, mime_type: blob.type || "audio/webm" }),
    });
    status.textContent = `已识别：${data.text}`;
    await sendMessage(data.text, { voiceDuration: duration });
    status.textContent = "松开发送，Esc 取消";
  } catch (error) {
    status.textContent = `语音发送失败：${error.message}`;
  } finally {
    button.disabled = !state.voiceConfigured;
    button.focus();
  }
}

function blobToBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () => resolve(String(reader.result).split(",", 2)[1] || ""), { once: true });
    reader.addEventListener("error", () => reject(reader.error), { once: true });
    reader.readAsDataURL(blob);
  });
}

async function attachVoiceReply(message, text) {
  const bubble = message.querySelector(".message-bubble");
  const wrap = document.createElement("div");
  wrap.className = "voice-reply";
  wrap.setAttribute("role", "status");
  wrap.textContent = "正在准备语音回复…";
  bubble.classList.add("has-voice-reply");
  const name = bubble.querySelector(".message-name");
  if (name) name.after(wrap);
  else bubble.prepend(wrap);
  try {
    const data = await api("/api/voice/synthesize", {
      method: "POST",
      body: JSON.stringify({ text }),
    });
    const button = document.createElement("button");
    button.type = "button";
    button.className = "voice-reply-button";
    button.setAttribute("aria-label", "播放阿映的语音回复");
    const wave = document.createElement("span");
    wave.className = "voice-wave";
    wave.setAttribute("aria-hidden", "true");
    wave.append(...Array.from({ length: 4 }, () => document.createElement("span")));
    const duration = document.createElement("span");
    duration.className = "voice-reply-duration";
    duration.textContent = "…";
    button.append(wave, duration);
    const audio = document.createElement("audio");
    audio.preload = "metadata";
    audio.setAttribute("aria-label", "播放阿映的语音回复");
    audio.src = `data:${data.content_type};base64,${data.audio_base64}`;
    audio.addEventListener("loadedmetadata", () => {
      const seconds = Math.max(1, Math.round(audio.duration || 1));
      duration.textContent = `${seconds}″`;
      button.setAttribute("aria-label", `播放阿映的语音回复，${seconds} 秒`);
    }, { once: true });
    audio.addEventListener("play", () => {
      button.classList.add("is-playing");
      button.setAttribute("aria-label", "暂停阿映的语音回复");
    });
    audio.addEventListener("pause", () => {
      button.classList.remove("is-playing");
      if (audio.duration) button.setAttribute("aria-label", `播放阿映的语音回复，${Math.max(1, Math.round(audio.duration))} 秒`);
    });
    audio.addEventListener("ended", () => button.classList.remove("is-playing"));
    button.addEventListener("click", async () => {
      if (audio.paused) await playVoiceAudio(audio, button, false);
      else audio.pause();
    });
    wrap.replaceChildren(button, audio);
    wrap.removeAttribute("role");
    state.activeVoiceAudio = audio;
    if (state.voiceAutoPlay) await playVoiceAudio(audio, button, true);
  } catch {
    wrap.remove();
    bubble.classList.remove("has-voice-reply");
  }
}

async function playVoiceAudio(audio, button, automatic) {
  document.querySelectorAll(".voice-reply audio").forEach((item) => {
    if (item !== audio && !item.paused) item.pause();
  });
  try {
    await audio.play();
    button.classList.remove("autoplay-blocked");
  } catch {
    button.classList.add("autoplay-blocked");
    if (automatic) showToast("浏览器阻止了自动播放，点语音胶囊即可收听");
  }
}

function updateVoiceAutoPlayToggle() {
  const button = $("#voice-autoplay-toggle");
  if (!button) return;
  button.disabled = !state.voiceConfigured;
  button.setAttribute("aria-pressed", String(state.voiceAutoPlay));
  button.setAttribute("aria-label", state.voiceAutoPlay ? "关闭 AI 语音自动播放" : "开启 AI 语音自动播放");
  button.querySelector("span").textContent = state.voiceAutoPlay ? "自动播放" : "手动播放";
}

async function setVoiceAutoPlay(enabled) {
  state.voiceAutoPlay = Boolean(enabled);
  saveVoiceAutoPlayPreference(state.voiceAutoPlay);
  updateVoiceAutoPlayToggle();
  if (!state.voiceAutoPlay) {
    document.querySelectorAll(".voice-reply audio").forEach((audio) => audio.pause());
    showToast("后续语音回复将由你手动播放");
    return;
  }
  showToast("后续语音回复将自动播放");
  if (state.activeVoiceAudio?.paused) {
    const button = state.activeVoiceAudio.closest(".voice-reply")?.querySelector(".voice-reply-button");
    if (button) await playVoiceAudio(state.activeVoiceAudio, button, false);
  }
}

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button");
  const errorNode = $("#login-error");
  button.disabled = true;
  errorNode.textContent = "";
  try {
    await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ invite_code: $("#invite-code").value }),
    });
    const me = await api("/api/me");
    showAuthenticated(me);
  } catch (error) {
    errorNode.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

$("#logout-button").addEventListener("click", async () => {
  await api("/api/auth/logout", { method: "POST", body: "{}" });
  state.chatHistory = [];
  showLogin();
});

$("#brand-home").addEventListener("click", () => navigate("home"));
$$("[data-nav]").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.nav)));
$("#discussion-card").addEventListener("click", () => openChat("discussion"));
$("#recommendation-card").addEventListener("click", () => openChat("recommendation"));
$("#chat-back").addEventListener("click", () => navigate("home"));
$("#chat-form").addEventListener("submit", (event) => { event.preventDefault(); sendMessage(); });
$("#voice-mode-button").addEventListener("click", () => setVoiceMode(!state.voiceMode));
$("#voice-autoplay-toggle").addEventListener("click", () => setVoiceAutoPlay(!state.voiceAutoPlay));
$("#reflection-continue").addEventListener("click", () => {
  const movie = state.reflectionMovie;
  $("#reflection-dialog").close();
  if (movie) openChat("discussion", movie);
});
$("#reflection-dialog").addEventListener("close", () => {
  if (state.reflectionTrigger?.isConnected) state.reflectionTrigger.focus();
  state.reflectionTrigger = null;
});
$("#hold-to-talk").addEventListener("pointerdown", (event) => {
  event.preventDefault();
  event.currentTarget.setPointerCapture?.(event.pointerId);
  startRecording();
});
$("#hold-to-talk").addEventListener("pointerup", () => stopRecording());
$("#hold-to-talk").addEventListener("pointercancel", () => stopRecording(true));
$("#hold-to-talk").addEventListener("keydown", (event) => {
  if ((event.key === " " || event.key === "Enter") && !event.repeat) {
    event.preventDefault();
    startRecording();
  }
});
$("#hold-to-talk").addEventListener("keyup", (event) => {
  if (event.key === " " || event.key === "Enter") {
    event.preventDefault();
    stopRecording();
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && state.recorder) stopRecording(true);
});
$("#chat-input").addEventListener("input", (event) => autoResize(event.target));
$("#chat-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("#chat-form").requestSubmit();
  }
});

$$(".history-tab").forEach((tab) => tab.addEventListener("click", () => {
  state.historyState = tab.dataset.state;
  $$(".history-tab").forEach((item) => item.classList.toggle("is-active", item === tab));
  loadHistory();
}));

$("#add-movie-button").addEventListener("click", () => {
  $("#movie-search-results").replaceChildren();
  $("#movie-search-input").value = "";
  $("#movie-dialog").showModal();
  $("#movie-search-input").focus();
});

$("#movie-search-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const query = $("#movie-search-input").value.trim();
  const wrap = $("#movie-search-results");
  try {
    const data = await api(`/api/movies?query=${encodeURIComponent(query)}`);
    if (!data.items.length) {
      const empty = document.createElement("p");
      empty.className = "empty-state";
      empty.textContent = "热门片库里暂时没有找到。";
      wrap.replaceChildren(empty);
      return;
    }
    wrap.replaceChildren(...data.items.map((movie) => {
      const row = document.createElement("div");
      row.className = "search-item";
      const copy = document.createElement("div");
      const title = document.createElement("strong");
      title.textContent = movie.title_zh;
      const meta = document.createElement("span");
      meta.textContent = `${movie.year} · ${movie.title_original}`;
      copy.append(title, meta);
      const add = document.createElement("button");
      add.className = "mini-button";
      add.type = "button";
      add.textContent = state.historyState === "watched" ? "标记看过" : "加入";
      add.addEventListener("click", async () => {
        await markMovie(movie, state.historyState, "manual");
        $("#movie-dialog").close();
        loadHistory();
      });
      row.append(copy, add);
      return row;
    }));
  } catch (error) {
    showToast(error.message);
  }
});

boot();
