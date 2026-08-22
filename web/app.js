const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const state = {
  authenticated: false,
  mode: null,
  chatHistory: [],
  currentConversationId: null,
  currentConversationCreatedAt: 0,
  restoredConversation: false,
  pendingConversationDeleteId: null,
  selectedMovieId: null,
  selectedMovie: null,
  historyState: "watched",
  historyCursor: 0,
  historyNextCursor: null,
  accountHint: "guest",
  weeklyEnabled: true,
  monthlyRecap: null,
  shareSource: null,
  shareCardId: null,
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
  reflectionBundle: null,
  onboardingStatus: "not_started",
  onboardingSelected: [],
  tasteProfile: null,
  autoGenerateReflectionDrafts: true,
  recorder: null,
  recordingStream: null,
  recordingChunks: [],
  recordingStartedAt: 0,
  recordingTimer: null,
  cancelRecording: false,
};

const localConversationStore = {
  prefix: "yingban.conversation.v2.",
  legacyPrefix: "yingban.chatDraft.v1.",
  retentionMs: 7 * 24 * 60 * 60 * 1000,
  accountPrefix() {
    return `${this.prefix}${state.accountHint}.`;
  },
  key(id) {
    return `${this.accountPrefix()}${id}`;
  },
  newId() {
    if (window.crypto?.randomUUID) return `conv_${window.crypto.randomUUID()}`;
    return `conv_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
  },
  isValid(value) {
    return Boolean(
      value
      && value.schema_version === 2
      && value.storage === "local-browser"
      && value.account_hint === state.accountHint
      && value.mode === "discussion"
      && Number(value.expires_at) > Date.now()
    );
  },
  read(id) {
    try {
      const value = JSON.parse(window.localStorage.getItem(this.key(id)) || "null");
      if (!this.isValid(value)) {
        window.localStorage.removeItem(this.key(id));
        return null;
      }
      return value;
    } catch {
      try { window.localStorage.removeItem(this.key(id)); } catch { /* storage unavailable */ }
      return null;
    }
  },
  list() {
    const items = [];
    try {
      Object.keys(window.localStorage)
        .filter((key) => key.startsWith(this.accountPrefix()))
        .forEach((key) => {
          const id = key.slice(this.accountPrefix().length);
          const value = this.read(id);
          if (value?.movie?.title_zh && (value.history?.length || value.input?.trim())) items.push(value);
        });
    } catch { return []; }
    return items.sort((left, right) => Number(right.updated_at) - Number(left.updated_at));
  },
  writeCurrent(input = "") {
    if (state.mode !== "discussion" || !state.currentConversationId) return;
    if (!state.chatHistory.length && !input.trim()) return;
    const now = Date.now();
    const previous = this.read(state.currentConversationId);
    const movie = state.selectedMovie || previous?.movie || null;
    try {
      window.localStorage.setItem(this.key(state.currentConversationId), JSON.stringify({
        schema_version: 2,
        storage: "local-browser",
        id: state.currentConversationId,
        account_hint: state.accountHint,
        mode: "discussion",
        title: movie?.title_zh ? `《${movie.title_zh}》` : "尚未指定电影",
        movie,
        history: state.chatHistory.slice(-40),
        input: input.slice(0, 6000),
        spoilers_allowed: $("#spoilers-allowed").checked,
        created_at: Number(previous?.created_at || state.currentConversationCreatedAt || now),
        updated_at: now,
        expires_at: now + this.retentionMs,
      }));
    } catch { /* storage unavailable */ }
  },
  remove(id) {
    try { window.localStorage.removeItem(this.key(id)); } catch { /* storage unavailable */ }
  },
  clearAll() {
    try {
      Object.keys(window.localStorage)
        .filter((key) => key.startsWith(this.accountPrefix()) || key.startsWith(`${this.legacyPrefix}${state.accountHint}.`))
        .forEach((key) => window.localStorage.removeItem(key));
    } catch { /* storage unavailable */ }
  },
  migrateLegacyDrafts() {
    try {
      const legacyAccountPrefix = `${this.legacyPrefix}${state.accountHint}.discussion.`;
      Object.keys(window.localStorage)
        .filter((key) => key.startsWith(legacyAccountPrefix))
        .forEach((key) => {
          let legacy = null;
          try {
            legacy = JSON.parse(window.localStorage.getItem(key) || "null");
          } catch {
            window.localStorage.removeItem(key);
            return;
          }
          if (!legacy || Number(legacy.expires_at) <= Date.now() || (!legacy.history?.length && !legacy.input?.trim())) {
            window.localStorage.removeItem(key);
            return;
          }
          const id = `conv_migrated_${Number(legacy.updated_at) || Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
          const movie = legacy.movie || null;
          window.localStorage.setItem(this.key(id), JSON.stringify({
            schema_version: 2,
            storage: "local-browser",
            id,
            account_hint: state.accountHint,
            mode: "discussion",
            title: movie?.title_zh ? `《${movie.title_zh}》` : "尚未指定电影",
            movie,
            history: (legacy.history || []).slice(-40),
            input: String(legacy.input || "").slice(0, 6000),
            spoilers_allowed: legacy.spoilers_allowed !== false,
            created_at: Number(legacy.updated_at) || Date.now(),
            updated_at: Number(legacy.updated_at) || Date.now(),
            expires_at: Math.min(Number(legacy.expires_at) || 0, Date.now() + this.retentionMs),
          }));
          window.localStorage.removeItem(key);
        });
    } catch { /* malformed legacy drafts are ignored */ }
  },
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
  state.accountHint = me.account?.id_hint || "account";
  localConversationStore.migrateLegacyDrafts();
  $("#mode-badge").hidden = !me.demo_mode;
  state.voiceConfigured = Boolean(me.voice_available);
  state.openings = { ...state.openings, ...(me.openings || {}) };
  state.onboardingStatus = me.onboarding?.status || "not_started";
  state.autoGenerateReflectionDrafts = me.reflection_preferences?.auto_generate_drafts !== false;
  state.weeklyEnabled = me.weekly_recommendations?.enabled !== false;
  $("#weekly-enabled").checked = state.weeklyEnabled;
  $("#reflection-auto-generate").checked = state.autoGenerateReflectionDrafts;
  state.voiceSupported = Boolean(window.MediaRecorder && navigator.mediaDevices?.getUserMedia);
  const voiceButton = $("#voice-mode-button");
  voiceButton.disabled = !state.voiceSupported;
  voiceButton.title = state.voiceSupported ? "语音输入" : "当前浏览器不支持录音";
  updateVoiceAutoPlayToggle();
  setVoiceMode(false);
  if (state.onboardingStatus === "completed") {
    navigate("home");
    loadWeeklyRecommendation();
  }
  else openOnboarding();
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
  if (state.onboardingStatus !== "completed" && view !== "onboarding") view = "onboarding";
  $$(".view").forEach((node) => { node.hidden = node.id !== `${view}-view`; });
  $$("[data-nav]").forEach((node) => node.classList.toggle("is-active", node.dataset.nav === view));
  if (view === "history") {
    state.historyCursor = 0;
    loadHistory();
  }
  if (view === "home") loadWeeklyRecommendation();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function openOnboarding() {
  navigate("onboarding");
  await Promise.all([loadOnboarding(), loadOnboardingCandidates()]);
}

async function loadOnboarding() {
  const data = await api("/api/onboarding");
  state.onboardingStatus = data.status;
  state.onboardingSelected = data.selected || [];
  renderOnboardingSelected();
  return data;
}

async function loadOnboardingCandidates(query = "") {
  const wrap = $("#onboarding-candidates");
  wrap.setAttribute("aria-busy", "true");
  try {
    const data = await api(`/api/onboarding/candidates${query ? `?query=${encodeURIComponent(query)}` : ""}`);
    wrap.replaceChildren(...data.items.map(onboardingCandidate));
    if (!data.items.length) {
      const empty = document.createElement("p");
      empty.className = "empty-state";
      empty.textContent = "没有找到这部电影，试试原名或别名。";
      wrap.append(empty);
    }
  } finally {
    wrap.removeAttribute("aria-busy");
  }
}

function onboardingCandidate(movie) {
  const card = document.createElement("article");
  card.className = "onboarding-movie";
  card.append(posterNode(movie));
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = "选这部";
  button.disabled = state.onboardingSelected.length >= 5;
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await api("/api/onboarding/movies", {
        method: "POST",
        body: JSON.stringify({ movie_id: movie.id, sentiment: "neutral" }),
      });
      await loadOnboarding();
      card.remove();
    } catch (error) {
      showToast(error.message);
      button.disabled = false;
    }
  });
  card.append(button);
  return card;
}

function renderOnboardingSelected() {
  const count = state.onboardingSelected.length;
  $("#onboarding-count").textContent = `${count}/5`;
  $("#onboarding-remaining").textContent = count === 5 ? "可以生成初始画像" : `还需要 ${5 - count} 部`;
  const complete = $("#onboarding-complete");
  complete.disabled = count !== 5;
  complete.textContent = count === 5 ? "生成我的初始口味" : `还需要选择 ${5 - count} 部电影`;
  $("#onboarding-selected").replaceChildren(...state.onboardingSelected.map((movie, index) => {
    const item = document.createElement("li");
    item.className = "selected-movie";
    const head = document.createElement("div");
    head.className = "selected-movie-head";
    const title = document.createElement("strong");
    title.textContent = `${index + 1}. ${movie.title_zh}`;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.setAttribute("aria-label", `从冷启动选择中移除《${movie.title_zh}》`);
    remove.textContent = "移除";
    remove.addEventListener("click", async () => {
      await api(`/api/onboarding/movies/${encodeURIComponent(movie.id)}`, { method: "DELETE" });
      await Promise.all([loadOnboarding(), loadOnboardingCandidates()]);
    });
    head.append(title, remove);
    const sentiments = document.createElement("div");
    sentiments.className = "sentiment-options";
    for (const [value, label] of [["positive", "喜欢"], ["neutral", "一般"], ["negative", "不喜欢"]]) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = label;
      button.setAttribute("aria-pressed", String(movie.sentiment === value));
      button.addEventListener("click", async () => {
        await api("/api/onboarding/movies", {
          method: "POST",
          body: JSON.stringify({ movie_id: movie.id, sentiment: value, updating: true }),
        });
        await loadOnboarding();
      });
      sentiments.append(button);
    }
    item.append(head, sentiments);
    return item;
  }));
  $$(".onboarding-movie > button").forEach((button) => { button.disabled = count >= 5; });
}

async function completeOnboarding() {
  const button = $("#onboarding-complete");
  button.disabled = true;
  $("#onboarding-status").textContent = "正在根据这 5 部电影整理可见口味…";
  try {
    const data = await api("/api/onboarding/complete", { method: "POST", body: "{}" });
    state.onboardingStatus = "completed";
    state.tasteProfile = data.profile;
    $("#onboarding-status").textContent = "初始口味已生成。";
    navigate("home");
    await openTasteProfile();
  } catch (error) {
    $("#onboarding-status").textContent = error.message;
    button.disabled = false;
  }
}

async function openTasteProfile() {
  const data = await api("/api/taste-profile");
  state.tasteProfile = data.profile;
  $("#taste-summary").textContent = data.profile.taste_summary || "还没有形成口味画像。";
  const visible = (data.profile.taste_dimensions || []).filter((item) => !item.hidden);
  $("#taste-dimensions").replaceChildren(...visible.map((dimension) => {
    const card = document.createElement("article");
    card.className = "taste-dimension";
    const head = document.createElement("div");
    head.className = "taste-dimension-head";
    const title = document.createElement("strong");
    title.textContent = dimension.label;
    const direction = document.createElement("span");
    direction.className = "direction";
    direction.textContent = dimension.direction === "prefer" ? "目前偏好" : "目前避开";
    head.append(title, direction);
    const evidence = document.createElement("p");
    evidence.className = "taste-evidence";
    evidence.textContent = `电影证据：${(dimension.evidence || []).map((item) => `《${item.title}》`).join("、")}`;
    const correct = document.createElement("button");
    correct.type = "button";
    correct.className = "text-button";
    correct.textContent = "这条不准，移除结论";
    correct.addEventListener("click", async () => {
      await api("/api/taste-profile/corrections", {
        method: "POST",
        body: JSON.stringify({ dimension_id: dimension.id }),
      });
      await openTasteProfile();
    });
    card.append(head, evidence, correct);
    return card;
  }));
  const dialog = $("#taste-dialog");
  if (!dialog.open) dialog.showModal();
}

async function openChat(mode, movie = null, options = {}) {
  if (state.currentConversationId) {
    localConversationStore.writeCurrent($("#chat-input")?.value || "");
  }
  try {
    const me = await api("/api/me");
    state.openings = { ...state.openings, ...(me.openings || {}) };
  } catch { /* keep the last known openings */ }
  const restored = options.conversation || null;
  state.mode = mode;
  state.chatHistory = restored?.history ? [...restored.history] : [];
  state.currentConversationId = mode === "discussion"
    ? (restored?.id || localConversationStore.newId())
    : null;
  state.currentConversationCreatedAt = Number(restored?.created_at || Date.now());
  state.restoredConversation = Boolean(restored);
  state.selectedMovie = restored?.movie || movie;
  state.selectedMovieId = state.selectedMovie?.id || state.selectedMovie?.movie_id || null;
  $("#reflection-generate-button").hidden = !(mode === "discussion" && state.selectedMovie);
  $("#conversation-summary-button").hidden = !(mode === "discussion" && state.selectedMovie);
  $("#chat-history-button").hidden = mode !== "discussion";
  $("#chat-messages").replaceChildren();
  $("#spoiler-control").hidden = mode !== "discussion";

  const discussion = mode === "discussion";
  $("#chat-kicker").textContent = discussion ? "散场之后" : "下一部电影";
  $("#chat-title").textContent = discussion ? (state.selectedMovie ? `聊聊《${state.selectedMovie.title_zh}》` : "刚看完哪一部？") : "此刻想看点什么？";
  $("#chat-input").placeholder = discussion ? "片名，或者看完后的第一句话……" : "说说此刻的心情、口味或最近的烦恼……";
  renderChips(discussion
    ? ["我很喜欢，但说不上为什么", "有个地方我一直没看懂", "结局让我有点难受"]
    : ["最近压力很大，想放松", "想看一部能给我力量的", "不要爱情片，想看点烧脑的"]
  );
  navigate("chat");
  const opening = discussion
    ? (state.selectedMovie ? state.openings.discussion_movie.replaceAll("{movie}", state.selectedMovie.title_zh) : state.openings.discussion)
    : state.openings.recommendation;
  if (restored) {
    if (state.chatHistory.length) state.chatHistory.forEach((item) => renderMessage(item.role, item.content));
    else renderMessage("assistant", opening);
    $("#chat-input").value = restored.input || "";
    $("#spoilers-allowed").checked = restored.spoilers_allowed !== false;
    $("#chat-draft-message").textContent = `正在查看 ${restored.title || "这段"} 历史对话；新的内容仍只保存在此设备 7 天。`;
    $("#chat-draft-clear").textContent = "开始新对话";
    $("#chat-draft-notice").hidden = false;
  } else {
    renderMessage("assistant", opening);
    $("#chat-input").value = "";
    $("#spoilers-allowed").checked = true;
    $("#chat-draft-notice").hidden = true;
  }
  autoResize($("#chat-input"));
  $("#chat-input").focus();
}

function conversationHistoryItem(conversation) {
  const item = document.createElement("article");
  item.className = "conversation-history-item";
  const copy = document.createElement("div");
  copy.className = "conversation-history-copy";
  const title = document.createElement("h3");
  title.textContent = conversation.title || "尚未指定电影";
  const meta = document.createElement("p");
  const userTurns = (conversation.history || []).filter((entry) => entry.role === "user").length;
  const time = new Intl.DateTimeFormat("zh-CN", {
    month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit",
  }).format(new Date(Number(conversation.updated_at)));
  meta.textContent = `${time} · ${userTurns} 次发言 · 7 天后自动过期`;
  const firstUserMessage = (conversation.history || []).find((entry) => entry.role === "user")?.content;
  if (firstUserMessage) {
    const preview = document.createElement("p");
    preview.className = "conversation-history-preview";
    preview.textContent = firstUserMessage;
    copy.append(title, meta, preview);
  } else {
    copy.append(title, meta);
  }
  const actions = document.createElement("div");
  actions.className = "conversation-history-actions";
  const open = document.createElement("button");
  open.type = "button";
  open.className = "button button-secondary";
  open.textContent = "打开对话";
  open.addEventListener("click", () => {
    $("#conversation-history-dialog").close();
    openChat("discussion", conversation.movie || null, { conversation });
  });
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "text-button danger-text";
  remove.textContent = "删除";
  remove.setAttribute("aria-label", `删除历史对话${conversation.title || ""}`);
  remove.addEventListener("click", () => {
    state.pendingConversationDeleteId = conversation.id;
    $("#conversation-history-delete-name").textContent = conversation.title || "这段对话";
    $("#conversation-history-dialog").close();
    $("#conversation-history-delete-dialog").showModal();
  });
  actions.append(open, remove);
  item.append(copy, actions);
  return item;
}

function renderConversationHistory() {
  const list = $("#conversation-history-list");
  const items = localConversationStore.list();
  list.replaceChildren();
  $("#conversation-history-count").textContent = items.length ? `当前设备保存了 ${items.length} 段对话` : "当前设备还没有历史对话";
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "conversation-history-empty";
    const text = document.createElement("p");
    text.textContent = "从一部刚看完的电影开始，聊过的内容会在这里保留 7 天。";
    const start = document.createElement("button");
    start.type = "button";
    start.className = "button button-primary";
    start.textContent = "开始新的聊电影";
    start.addEventListener("click", () => {
      $("#conversation-history-dialog").close();
      openChat("discussion");
    });
    empty.append(text, start);
    list.append(empty);
    return;
  }
  list.append(...items.map(conversationHistoryItem));
}

function openConversationHistory() {
  localConversationStore.writeCurrent($("#chat-input")?.value || "");
  renderConversationHistory();
  $("#conversation-history-dialog").showModal();
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
      miniButton("想看", () => sendRecommendationFeedback(movie, "watchlist")),
      miniButton("看过了，换一部", async () => {
        await sendRecommendationFeedback(movie, "watched");
        await sendMessage(`《${movie.title_zh}》我看过了，换一个方向相近但没看过的。`);
      }),
      miniButton("聊聊这部", async () => {
        await sendRecommendationFeedback(movie, "discuss");
        openChat("discussion", movie);
      }),
      miniButton("不适合现在", () => { reasons.hidden = !reasons.hidden; })
    );
    const reasons = document.createElement("div");
    reasons.className = "feedback-reasons";
    reasons.hidden = true;
    for (const [action, label] of [
      ["not_now", "暂时不想看"], ["wrong_tone", "氛围不对"],
      ["wrong_genre", "类型不对"], ["too_heavy", "太沉重"],
      ["not_interested", "以后也别推荐"],
    ]) {
      reasons.append(miniButton(label, async () => {
        await sendRecommendationFeedback(movie, action);
        reasons.replaceChildren(Object.assign(document.createElement("span"), {
          className: "feedback-note",
          textContent: "已记录。它只影响电影推荐，不会被用来推断你的现实生活。",
        }));
      }));
    }
    body.append(meta, reason, notes, actions, reasons);
    card.append(poster, body);
    row.append(card);
  });
  $("#chat-messages").append(row);
  row.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function sendRecommendationFeedback(movie, action) {
  if (!movie.impression_id) throw new Error("这张推荐卡缺少可追踪曝光，请重新请求推荐");
  await api(`/api/recommendations/${encodeURIComponent(movie.impression_id)}/feedback`, {
    method: "POST",
    body: JSON.stringify({ action }),
  });
  const labels = {
    watchlist: "已加入想看", watched: "已加入看过", discuss: "已进入讨论",
    not_now: "已记为暂时不看", wrong_tone: "已记下氛围不符",
    wrong_genre: "已记下类型不符", too_heavy: "已记下太沉重",
    not_interested: "已加入持续排除",
  };
  showToast(`${labels[action] || "反馈已记录"} · 只影响电影推荐`);
  refreshMe();
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
  localConversationStore.writeCurrent("");
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
      state.selectedMovie = data.selected_movie;
      $("#chat-title").textContent = `聊聊《${data.selected_movie.title_zh}》`;
      $("#reflection-generate-button").hidden = state.mode !== "discussion";
      $("#conversation-summary-button").hidden = state.mode !== "discussion";
    }
    const assistantMessage = renderMessage("assistant", data.reply);
    state.chatHistory.push({ role: "assistant", content: data.reply });
    localConversationStore.writeCurrent("");
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
    localConversationStore.writeCurrent(input.value);
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

async function loadWeeklyRecommendation() {
  if (!state.authenticated || state.onboardingStatus !== "completed") return;
  const card = $("#weekly-card");
  try {
    const data = await api("/api/weekly-recommendation");
    if (["disabled", "dismissed"].includes(data.status)) {
      card.hidden = true;
      return;
    }
    card.hidden = false;
    if (data.status === "needs_input") {
      $("#weekly-reason").textContent = "还缺一个当下方向。告诉我这周想看得轻一点、热闹一点，或避开什么。";
      $("#weekly-movies").replaceChildren();
      return;
    }
    renderWeeklyRecommendation(data.recommendation);
  } catch {
    card.hidden = true;
  }
}

function renderWeeklyRecommendation(recommendation) {
  $("#weekly-reason").textContent = recommendation?.reason_summary || "一份克制的本周建议。";
  $("#weekly-movies").replaceChildren(...(recommendation?.movies || []).map((movie) => {
    const item = document.createElement("article");
    item.className = "weekly-movie";
    item.append(posterNode(movie));
    const body = document.createElement("div");
    const title = document.createElement("h3");
    title.textContent = movie.title_zh;
    const meta = document.createElement("p");
    meta.textContent = `${movie.year} · ${(movie.genres || []).slice(0, 2).join(" / ")}`;
    const actions = document.createElement("div");
    actions.className = "history-item-actions";
    actions.append(
      miniButton("留在想看", () => markMovie(movie, "watchlist", "weekly_recommendation")),
      miniButton("聊聊这部", () => openChat("discussion", movie)),
    );
    body.append(title, meta, actions);
    item.append(body);
    return item;
  }));
}

async function openConversationSummary() {
  const movie = state.selectedMovie;
  if (!movie) return;
  const movieId = movie.id || movie.movie_id;
  $("#conversation-summary-title").textContent = `下次继续聊《${movie.title_zh}》`;
  const existing = await api(`/api/conversations/${encodeURIComponent(movieId)}/summary`);
  const userThoughts = state.chatHistory
    .filter((item) => item.role === "user")
    .map((item) => item.content)
    .filter(Boolean)
    .slice(-5);
  $("#conversation-summary-text").value = existing.summary?.summary || userThoughts.join("\n").slice(0, 2000);
  $("#conversation-open-question").value = existing.summary?.open_questions?.[0] || "";
  $("#conversation-summary-delete").hidden = !existing.summary;
  $("#conversation-summary-state").textContent = existing.summary
    ? "已保存过一份摘要。再次保存会更新它，不会创建原始聊天副本。"
    : "只有点击“同意并保存”后，服务端才会创建电影会话摘要。";
  $("#conversation-summary-dialog").showModal();
}

async function saveConversationSummary() {
  const movieId = state.selectedMovie?.id || state.selectedMovie?.movie_id;
  if (!movieId) return;
  const question = $("#conversation-open-question").value.trim();
  await api(`/api/conversations/${encodeURIComponent(movieId)}/summary`, {
    method: "POST",
    body: JSON.stringify({
      summary: $("#conversation-summary-text").value,
      topics: (state.selectedMovie?.themes || []).slice(0, 8),
      open_questions: question ? [question] : [],
      spoilers_allowed: $("#spoilers-allowed").checked,
    }),
  });
  $("#conversation-summary-state").textContent = "已保存。你可以随时删除，且不会影响观后感。";
  $("#conversation-summary-delete").hidden = false;
  showToast("电影会话摘要已保存，下次可以从这里继续");
}

async function deleteConversationSummary() {
  const movieId = state.selectedMovie?.id || state.selectedMovie?.movie_id;
  if (!movieId) return;
  await api(`/api/conversations/${encodeURIComponent(movieId)}/summary`, { method: "DELETE" });
  $("#conversation-summary-text").value = "";
  $("#conversation-open-question").value = "";
  $("#conversation-summary-delete").hidden = true;
  $("#conversation-summary-state").textContent = "摘要已删除；观后感没有变化。";
}

function currentMonthKey() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
}

async function openMonthlyRecap() {
  $("#monthly-recap-dialog").showModal();
  const data = await api(`/api/recaps/monthly/${currentMonthKey()}`);
  state.monthlyRecap = data.recap;
  renderMonthlyRecap();
}

function renderMonthlyRecap() {
  const wrap = $("#monthly-recap-content");
  const recap = state.monthlyRecap;
  if (!recap) {
    wrap.innerHTML = "<p>还没有生成本月回顾。它只会使用这个月真实新增的电影状态和已确认观后感。</p>";
    $("#monthly-recap-confirm").disabled = true;
    $("#monthly-recap-share").disabled = true;
    return;
  }
  const content = recap.content || {};
  const watched = content.watched || [];
  const reflections = content.confirmed_reflections || [];
  const themes = content.themes || [];
  wrap.replaceChildren();
  const summary = document.createElement("p");
  summary.textContent = watched.length
    ? `本月新增看过 ${watched.length} 部：${watched.map((item) => `《${item.title}》`).join("、")}。`
    : "本月还没有新增看过电影，这是一个真实空状态。";
  const detail = document.createElement("p");
  detail.textContent = `已确认观后感 ${reflections.length} 份${themes.length ? `；可回链的电影主题：${themes.map((item) => item.label).join("、")}` : "；暂无足够的已确认主题"}。`;
  const status = document.createElement("p");
  status.className = "reflection-state";
  status.textContent = recap.status === "confirmed" ? "已确认，可分享" : "草稿，确认后才可分享";
  wrap.append(summary, detail, status);
  $("#monthly-next-direction").value = content.next_direction || "";
  $("#monthly-recap-confirm").disabled = false;
  $("#monthly-recap-share").disabled = recap.status !== "confirmed";
}

async function generateMonthlyRecap() {
  const data = await api(`/api/recaps/monthly/${currentMonthKey()}/generate`, { method: "POST", body: "{}" });
  state.monthlyRecap = data.recap;
  renderMonthlyRecap();
}

async function confirmMonthlyRecap() {
  const data = await api(`/api/recaps/monthly/${currentMonthKey()}/confirm`, {
    method: "POST",
    body: JSON.stringify({ next_direction: $("#monthly-next-direction").value }),
  });
  state.monthlyRecap = data.recap;
  renderMonthlyRecap();
  showToast("月度电影回顾已确认");
}

function openSharePreview(sourceType, sourceId, text) {
  state.shareSource = { sourceType, sourceId };
  $("#share-preview-text").value = text;
  $("#share-result").hidden = true;
  $("#share-revoke").hidden = true;
  state.shareCardId = null;
  $("#share-dialog").showModal();
}

async function createShareCard() {
  if (!state.shareSource) return;
  const data = await api("/api/share-cards", {
    method: "POST",
    body: JSON.stringify({
      source_type: state.shareSource.sourceType,
      source_id: state.shareSource.sourceId,
      text: $("#share-preview-text").value,
      expires_days: 7,
    }),
  });
  const url = new URL(data.share_path, window.location.origin).href;
  state.shareCardId = data.card.id;
  const result = $("#share-result");
  result.hidden = false;
  result.textContent = `分享已创建，7 天后过期：${url}`;
  $("#share-revoke").hidden = false;
  try { await navigator.clipboard.writeText(url); showToast("分享链接已复制"); } catch { /* copy is optional */ }
}

async function createMonthlyRecapShare() {
  const recap = state.monthlyRecap;
  const button = $("#monthly-recap-share");
  if (!recap || recap.status !== "confirmed" || button.disabled) return;
  const originalLabel = button.textContent;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  button.textContent = "正在生成分享卡…";
  try {
    const data = await api("/api/share-cards", {
      method: "POST",
      body: JSON.stringify({
        source_type: "monthly_recap",
        source_id: recap.month_key,
        expires_days: 7,
      }),
    });
    $("#monthly-recap-dialog").close();
    window.location.assign(data.share_path);
  } catch (error) {
    button.disabled = false;
    button.removeAttribute("aria-busy");
    button.textContent = originalLabel;
    showToast(error.message);
  }
}

async function pollJob(jobId, onSuccess, onFailure) {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 500));
    const data = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
    if (data.job.status === "succeeded") return onSuccess(data.job);
    if (["failed", "expired"].includes(data.job.status)) {
      throw new Error(onFailure || "后台任务没有完成，请稍后重试");
    }
  }
  throw new Error("后台任务等待超时，请稍后回来查看");
}

async function loadHistory(append = false) {
  const grid = $("#history-grid");
  const empty = $("#history-empty");
  const cursor = append ? state.historyNextCursor : 0;
  if (append && cursor === null) return;
  grid.setAttribute("aria-busy", "true");
  try {
    const data = await api(`/api/history?state=${encodeURIComponent(state.historyState)}&cursor=${cursor}&limit=12`);
    if (append) grid.append(...data.items.map(historyItem));
    else grid.replaceChildren(...data.items.map(historyItem));
    state.historyCursor = data.cursor;
    state.historyNextCursor = data.next_cursor;
    $("#history-load-more").hidden = data.next_cursor === null;
    empty.hidden = data.total > 0;
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
  if (state.historyState === "watchlist") {
    actions.append(
      miniButton("还想看", () => followUpWatchlist(movie, "keep")),
      miniButton("已经看了", async () => {
        await followUpWatchlist(movie, "watched");
        await openChat("discussion", movie);
      }),
      miniButton("暂时不看", () => followUpWatchlist(movie, "not_now")),
      miniButton("不再感兴趣", () => followUpWatchlist(movie, "not_interested")),
    );
  } else {
    actions.append(miniButton("聊聊", () => openChat("discussion", movie)));
  }
  actions.append(
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

async function followUpWatchlist(movie, action) {
  await api(`/api/watchlist/${encodeURIComponent(movie.movie_id)}/follow-up`, {
    method: "POST",
    body: JSON.stringify({ action }),
  });
  const labels = {
    keep: "继续留在想看",
    watched: "已转为看过",
    not_now: "已暂时移出想看，不会记成不喜欢",
    not_interested: "已转为持续排除",
  };
  showToast(labels[action]);
  await Promise.all([loadHistory(), refreshMe()]);
}

async function openReflection(movie, trigger) {
  state.reflectionMovie = movie;
  state.reflectionTrigger = trigger;
  $("#reflection-title").textContent = `《${movie.title_zh}》观后感`;
  $("#reflection-meta").textContent = [movie.year, ...(movie.genres || []).slice(0, 3)].filter(Boolean).join(" · ");
  $("#reflection-poster").replaceChildren(posterNode(movie));
  const dialog = $("#reflection-dialog");
  dialog.showModal();
  dialog.querySelector(".icon-button").focus();
  try {
    const data = await api(`/api/reflections/${encodeURIComponent(movie.id || movie.movie_id)}`);
    state.reflectionBundle = data;
    renderReflectionBundle();
  } catch (error) {
    $("#reflection-empty").hidden = false;
    $("#reflection-empty").textContent = error.message;
  }
}

function renderReflectionBundle() {
  const current = state.reflectionBundle?.current;
  const note = String(current?.content || "");
  $("#reflection-note").value = note;
  $("#reflection-note").hidden = !current;
  $("#reflection-empty").hidden = Boolean(current);
  const labels = { draft: "AI 草稿", confirmed: "已确认", locked: "已锁定" };
  $("#reflection-state").textContent = current
    ? `${labels[current.status] || current.status} · 第 ${current.version} 版 · ${current.source === "ai" ? "AI 整理" : current.source === "user" ? "用户撰写" : "AI 草稿后编辑"} · ${new Date(current.updated_at).toLocaleString("zh-CN")}`
    : "尚无观后感";
  $("#reflection-confirm").hidden = current?.status !== "draft";
  $("#reflection-lock").hidden = !current || current.status === "draft";
  $("#reflection-lock").textContent = current?.status === "locked" ? "解锁" : "锁定";
  $("#reflection-save").hidden = !current;
  $("#reflection-regenerate").hidden = !current;
  $("#reflection-share").hidden = !current || !["confirmed", "locked"].includes(current.status);
  $("#reflection-delete").hidden = !current;
}

async function generateReflection(movie = state.reflectionMovie) {
  if (!movie) return;
  const movieId = movie.id || movie.movie_id;
  const data = await api(`/api/reflections/${encodeURIComponent(movieId)}/generate`, {
    method: "POST",
    body: JSON.stringify({
      history: state.chatHistory,
      regenerate: Boolean(state.reflectionBundle?.current),
      async: true,
    }),
  });
  showToast("正在后台整理观后感；聊天仍可继续");
  await pollJob(data.job.id, async () => {
    const bundle = await api(`/api/reflections/${encodeURIComponent(movieId)}`);
    state.reflectionBundle = bundle;
    showToast("新的 AI 观后感草稿已生成，确认前不会成为正式版本");
    if (!$("#reflection-dialog").open) await openReflection(movie, null);
    else renderReflectionBundle();
  }, "观后感整理失败，请保留当前文字后重试");
}

async function saveReflectionEdit() {
  const movieId = state.reflectionMovie?.id || state.reflectionMovie?.movie_id;
  const current = state.reflectionBundle?.current;
  if (!movieId || !current) return;
  const data = await api(`/api/reflections/${encodeURIComponent(movieId)}`, {
    method: "POST",
    body: JSON.stringify({ content: $("#reflection-note").value, based_on_version: current.version }),
  });
  state.reflectionBundle = data;
  renderReflectionBundle();
  showToast("你的编辑已保存为新版本");
}

async function reflectionStatusAction(action) {
  const movieId = state.reflectionMovie?.id || state.reflectionMovie?.movie_id;
  const current = state.reflectionBundle?.current;
  if (!movieId || !current) return;
  const data = await api(`/api/reflections/${encodeURIComponent(movieId)}/${action}`, {
    method: "POST",
    body: JSON.stringify({ version: current.version }),
  });
  state.reflectionBundle = data;
  renderReflectionBundle();
  showToast({ confirm: "草稿已确认", lock: "当前版本已锁定", unlock: "当前版本已解锁" }[action]);
}

async function deleteReflection() {
  const movieId = state.reflectionMovie?.id || state.reflectionMovie?.movie_id;
  if (!movieId || !window.confirm("只删除这部电影的观后感，保留“看过”状态。继续吗？")) return;
  await api(`/api/reflections/${encodeURIComponent(movieId)}`, { method: "DELETE" });
  state.reflectionBundle = { current: null, history: [] };
  renderReflectionBundle();
  showToast("观后感已删除，“看过”状态仍保留");
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
    const queued = await api("/api/jobs/voice", {
      method: "POST",
      body: JSON.stringify({ text, full: false }),
    });
    let data = null;
    await pollJob(queued.job.id, (job) => { data = job.result; }, "语音摘要生成失败");
    if (!data?.audio_base64) throw new Error("语音缓存已过期");
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
    const fullButton = document.createElement("button");
    fullButton.type = "button";
    fullButton.className = "voice-full-button";
    fullButton.textContent = "完整朗读";
    fullButton.addEventListener("click", async () => {
      fullButton.disabled = true;
      fullButton.textContent = "准备中…";
      try {
        const queuedFull = await api("/api/jobs/voice", {
          method: "POST",
          body: JSON.stringify({ text, full: true }),
        });
        await pollJob(queuedFull.job.id, (job) => {
          if (!job.result.audio_base64) throw new Error("完整语音缓存已过期");
          audio.src = `data:${job.result.content_type};base64,${job.result.audio_base64}`;
          audio.load();
        }, "完整朗读生成失败");
        fullButton.textContent = "已切换完整朗读";
      } catch (error) {
        fullButton.textContent = "完整朗读";
        showToast(error.message);
      } finally {
        fullButton.disabled = false;
      }
    });
    wrap.replaceChildren(button, fullButton, audio);
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

async function logout(clearDrafts) {
  if (clearDrafts) localConversationStore.clearAll();
  await api("/api/auth/logout", { method: "POST", body: "{}" });
  state.chatHistory = [];
  state.currentConversationId = null;
  state.restoredConversation = false;
  $("#logout-dialog").close();
  showLogin();
}

$("#logout-button").addEventListener("click", () => $("#logout-dialog").showModal());
$("#logout-keep-drafts").addEventListener("click", () => logout(false).catch((error) => showToast(error.message)));
$("#logout-clear-drafts").addEventListener("click", () => logout(true).catch((error) => showToast(error.message)));

$("#brand-home").addEventListener("click", () => navigate("home"));
$$("[data-nav]").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.nav)));
$("#discussion-card").addEventListener("click", () => openChat("discussion"));
$("#recommendation-card").addEventListener("click", () => openChat("recommendation"));
$("#chat-back").addEventListener("click", () => {
  const movie = state.selectedMovie;
  const history = [...state.chatHistory];
  localConversationStore.writeCurrent($("#chat-input").value);
  navigate("home");
  if (state.mode === "discussion" && movie && state.autoGenerateReflectionDrafts
      && history.some((item) => item.role === "user" && item.content.trim().length >= 12)) {
    api(`/api/reflections/${encodeURIComponent(movie.id)}/generate`, {
      method: "POST", body: JSON.stringify({ history, async: true }),
    }).then((data) => pollJob(data.job.id, () => showToast("已根据这次有效电影讨论生成一份 AI 草稿")))
      .catch(() => { /* 没有足够新内容时安静降级，不影响返回首页。 */ });
  }
});
$("#onboarding-search-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const query = $("#onboarding-search-input").value.trim();
  $("#onboarding-reset-search").hidden = !query;
  await loadOnboardingCandidates(query);
});
$("#onboarding-reset-search").addEventListener("click", async () => {
  $("#onboarding-search-input").value = "";
  $("#onboarding-reset-search").hidden = true;
  await loadOnboardingCandidates();
});
$("#onboarding-complete").addEventListener("click", completeOnboarding);
$("#taste-profile-button").addEventListener("click", openTasteProfile);
$("#continuity-settings-button").addEventListener("click", () => $("#continuity-settings-dialog").showModal());
$("#taste-recommend").addEventListener("click", () => {
  $("#taste-dialog").close();
  navigate("home");
  openChat("recommendation");
});
$("#taste-discuss").addEventListener("click", async () => {
  if (!state.onboardingSelected.length) await loadOnboarding();
  $("#taste-dialog").close();
  const movie = state.onboardingSelected[0];
  if (movie) openChat("discussion", movie);
});
$("#reflection-generate-button").addEventListener("click", async () => {
  try { await generateReflection(state.selectedMovie); } catch (error) { showToast(error.message); }
});
$("#reflection-save").addEventListener("click", async () => {
  try { await saveReflectionEdit(); } catch (error) { showToast(error.message); }
});
$("#reflection-confirm").addEventListener("click", async () => {
  try { await reflectionStatusAction("confirm"); } catch (error) { showToast(error.message); }
});
$("#reflection-lock").addEventListener("click", async () => {
  try {
    await reflectionStatusAction(state.reflectionBundle?.current?.status === "locked" ? "unlock" : "lock");
  } catch (error) { showToast(error.message); }
});
$("#reflection-regenerate").addEventListener("click", async () => {
  try { await generateReflection(); } catch (error) { showToast(error.message); }
});
$("#reflection-share").addEventListener("click", () => {
  const movieId = state.reflectionMovie?.id || state.reflectionMovie?.movie_id;
  const confirmed = state.reflectionBundle?.confirmed || state.reflectionBundle?.current;
  if (movieId && confirmed && ["confirmed", "locked"].includes(confirmed.status)) {
    $("#reflection-dialog").close();
    openSharePreview("reflection", movieId, confirmed.content);
  }
});
$("#reflection-delete").addEventListener("click", async () => {
  try { await deleteReflection(); } catch (error) { showToast(error.message); }
});
$("#reflection-auto-generate").addEventListener("change", async (event) => {
  try {
    const data = await api("/api/reflection-preferences", {
      method: "POST",
      body: JSON.stringify({ auto_generate_drafts: event.target.checked }),
    });
    state.autoGenerateReflectionDrafts = data.auto_generate_drafts;
    showToast(data.auto_generate_drafts ? "已开启自动生成草稿" : "已关闭自动生成草稿");
  } catch (error) {
    event.target.checked = state.autoGenerateReflectionDrafts;
    showToast(error.message);
  }
});
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
$("#chat-input").addEventListener("input", (event) => {
  autoResize(event.target);
  localConversationStore.writeCurrent(event.target.value);
});
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
$("#history-load-more").addEventListener("click", () => loadHistory(true));
$("#monthly-recap-button").addEventListener("click", () => openMonthlyRecap().catch((error) => showToast(error.message)));
$("#monthly-recap-generate").addEventListener("click", () => generateMonthlyRecap().catch((error) => showToast(error.message)));
$("#monthly-recap-confirm").addEventListener("click", () => confirmMonthlyRecap().catch((error) => showToast(error.message)));
$("#monthly-recap-share").addEventListener("click", () => createMonthlyRecapShare());

$("#conversation-summary-button").addEventListener("click", () => openConversationSummary().catch((error) => showToast(error.message)));
$("#conversation-summary-save").addEventListener("click", () => saveConversationSummary().catch((error) => showToast(error.message)));
$("#conversation-summary-delete").addEventListener("click", () => deleteConversationSummary().catch((error) => showToast(error.message)));
$("#chat-history-button").addEventListener("click", openConversationHistory);
$("#chat-draft-clear").addEventListener("click", () => {
  const movie = state.selectedMovie;
  openChat("discussion", movie).then(() => showToast("已开始一段新的电影对话"));
});
$("#clear-all-local-drafts").addEventListener("click", () => {
  localConversationStore.clearAll();
  state.currentConversationId = null;
  state.currentConversationCreatedAt = 0;
  state.restoredConversation = false;
  showToast("此设备上的历史对话已全部清空");
});
$("#conversation-history-delete-cancel").addEventListener("click", () => {
  state.pendingConversationDeleteId = null;
  $("#conversation-history-delete-dialog").close();
  renderConversationHistory();
  $("#conversation-history-dialog").showModal();
});
$("#conversation-history-delete-confirm").addEventListener("click", () => {
  const id = state.pendingConversationDeleteId;
  if (!id) return;
  const deletingCurrent = id === state.currentConversationId;
  localConversationStore.remove(id);
  state.pendingConversationDeleteId = null;
  $("#conversation-history-delete-dialog").close();
  if (deletingCurrent) {
    openChat("discussion", state.selectedMovie).then(() => showToast("历史对话已删除，已开始新对话"));
    return;
  }
  renderConversationHistory();
  $("#conversation-history-dialog").showModal();
  showToast("历史对话已删除");
});
$("#weekly-enabled").addEventListener("change", async (event) => {
  try {
    const data = await api("/api/weekly-recommendation/preferences", {
      method: "POST",
      body: JSON.stringify({ enabled: event.target.checked }),
    });
    state.weeklyEnabled = data.enabled;
    if (data.enabled) await loadWeeklyRecommendation();
    else $("#weekly-card").hidden = true;
  } catch (error) {
    event.target.checked = state.weeklyEnabled;
    showToast(error.message);
  }
});
$("#weekly-dismiss").addEventListener("click", async () => {
  await api("/api/weekly-recommendation/dismiss", { method: "POST", body: "{}" });
  $("#weekly-card").hidden = true;
  showToast("本周建议已关闭，下周才会再次出现");
});
$("#weekly-refresh-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const data = await api("/api/weekly-recommendation/refresh", {
      method: "POST",
      body: JSON.stringify({ direction: $("#weekly-direction").value }),
    });
    renderWeeklyRecommendation(data.recommendation);
    $("#weekly-direction").value = "";
  } catch (error) { showToast(error.message); }
});
$("#share-create").addEventListener("click", () => createShareCard().catch((error) => showToast(error.message)));
$("#share-revoke").addEventListener("click", async () => {
  if (!state.shareCardId) return;
  try {
    await api(`/api/share-cards/${encodeURIComponent(state.shareCardId)}`, { method: "DELETE" });
    $("#share-result").textContent = "分享已撤回，原链接现在不可访问。";
    $("#share-revoke").hidden = true;
    state.shareCardId = null;
  } catch (error) { showToast(error.message); }
});

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
