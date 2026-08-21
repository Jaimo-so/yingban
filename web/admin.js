const $ = (selector) => document.querySelector(selector);
let promptDefaults = { discussion: "", recommendation: "" };
let openingDefaults = { discussion: "", discussion_movie: "", recommendation: "" };

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `请求失败（${response.status}）`);
  return data;
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("is-visible");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.classList.remove("is-visible"), 2600);
}

function showDashboard() {
  loadDashboard();
}

function showLogin() {
  $("#admin-login").hidden = false;
  $("#admin-dashboard").hidden = true;
  $("#admin-logout").hidden = true;
  $("#admin-token").focus();
}

async function loadDashboard() {
  try {
    const [invites, config] = await Promise.all([
      api("/api/admin/invites"),
      api("/api/admin/agent-config"),
    ]);
    $("#invite-rows").replaceChildren(...invites.items.map(inviteRow));
    renderAgentConfig(config);
    showDashboardShell();
  } catch (error) {
    if (error.message.includes("未登录")) showLogin();
    else toast(error.message);
  }
}

async function loadInvites() {
  const data = await api("/api/admin/invites");
  $("#invite-rows").replaceChildren(...data.items.map(inviteRow));
}

function renderAgentConfig(config) {
  const model = config.model;
  promptDefaults = config.defaults;
  openingDefaults = config.opening_defaults || {};
  $("#model-provider").value = model.provider;
  $("#model-id").value = model.model_id;
  $("#model-base-url").value = model.base_url;
  $("#model-timeout").value = model.timeout_seconds;
  $("#model-max-tokens").value = model.max_tokens;
  $("#model-temperature").value = model.temperature;
  $("#model-api-key").value = "";
  $("#model-api-key").placeholder = model.has_api_key ? model.api_key_hint : "未配置";
  $("#api-key-help").textContent = model.has_api_key
    ? `当前密钥 ${model.api_key_hint}；留空表示保留。`
    : "尚未配置密钥，保存后仍可使用演示模式。";
  $("#clear-api-key").checked = false;
  renderModelStatus(model);
  updateProviderHelp();
  renderInternetConfig(config.internet || {});
  renderVoiceConfig(config.voice || {});

  $("#discussion-prompt").value = config.prompts.discussion;
  $("#recommendation-prompt").value = config.prompts.recommendation;
  $("#discussion-opening").value = config.openings?.discussion || "";
  $("#discussion-movie-opening").value = config.openings?.discussion_movie || "";
  $("#recommendation-opening").value = config.openings?.recommendation || "";
  updatePromptCounts();
}

function renderInternetConfig(internet) {
  const tmdb = internet.tmdb || {};
  const search = internet.web_search || {};
  const reader = internet.web_reader || {};
  $("#tmdb-base-url").value = tmdb.base_url || "https://api.themoviedb.org/3";
  $("#tmdb-image-base-url").value = tmdb.image_base_url || "https://image.tmdb.org/t/p/w500";
  $("#tmdb-api-key").value = "";
  $("#tmdb-api-key").placeholder = tmdb.has_api_key ? tmdb.api_key_hint : "未配置";
  $("#clear-tmdb-api-key").checked = false;
  const inferredSearchProvider = search.provider || (String(search.base_url || "").includes("api.search.brave.com") ? "brave" : "tavily");
  $("#web-search-provider").value = inferredSearchProvider;
  $("#web-search-base-url").value = search.base_url || (inferredSearchProvider === "brave" ? "https://api.search.brave.com/res/v1" : "https://api.tavily.com");
  $("#web-search-api-key").value = "";
  $("#web-search-api-key").placeholder = search.has_api_key ? search.api_key_hint : "未配置";
  $("#clear-web-search-api-key").checked = false;
  updateWebSearchProviderUI(false);
  $("#web-reader-enabled").checked = reader.enabled !== false;
  $("#web-reader-base-url").value = reader.base_url || "https://r.jina.ai";
  const enabled = Boolean(tmdb.enabled || search.enabled);
  const status = $("#internet-status");
  status.className = `connection-status ${enabled ? "status-model" : "status-demo"}`;
  status.textContent = tmdb.enabled && search.enabled ? "电影与搜索已接入" : tmdb.enabled ? "电影与海报已接入" : search.enabled ? "Web Search 已接入" : "尚未配置";
}

function internetPayload() {
  return {
    tmdb_api_key: $("#tmdb-api-key").value.trim(),
    clear_tmdb_api_key: $("#clear-tmdb-api-key").checked,
    tmdb_base_url: $("#tmdb-base-url").value.trim(),
    tmdb_image_base_url: $("#tmdb-image-base-url").value.trim(),
    web_search_provider: $("#web-search-provider").value,
    web_search_api_key: $("#web-search-api-key").value.trim(),
    clear_web_search_api_key: $("#clear-web-search-api-key").checked,
    web_search_base_url: $("#web-search-base-url").value.trim(),
    web_reader_enabled: $("#web-reader-enabled").checked,
    web_reader_base_url: $("#web-reader-base-url").value.trim(),
  };
}

function updateWebSearchProviderUI(applyDefaults = true) {
  const tavily = $("#web-search-provider").value === "tavily";
  $("#web-search-base-url-label").textContent = tavily ? "Tavily Search 接口根地址" : "Brave Web Search 接口根地址";
  $("#web-search-api-key-label").textContent = tavily ? "Tavily API Key" : "Brave Web Search API 密钥";
  $("#web-search-provider-help").textContent = tavily
    ? "Tavily 免费版每月提供 1,000 credits，无需绑定信用卡。"
    : "保留已有 Brave Search 接入；使用前请确认当前套餐与额度。";
  $("#web-search-help").textContent = tavily
    ? "基础搜索每次使用 1 credit；可搜索全网，并限定检索豆瓣或知乎。留空表示保留当前密钥。"
    : "让 Agent 搜索全网，以及按站点检索豆瓣和知乎；留空表示保留当前密钥。";
  if (applyDefaults) {
    $("#web-search-base-url").value = tavily ? "https://api.tavily.com" : "https://api.search.brave.com/res/v1";
    $("#web-search-api-key").value = "";
    $("#clear-web-search-api-key").checked = false;
  }
}

function renderVoiceConfig(voice) {
  const inferredProvider = voice.provider || (String(voice.base_url || "").includes("openspeech.bytedance.com") ? "doubao" : "openai_compatible");
  $("#voice-provider").value = inferredProvider;
  $("#voice-base-url").value = voice.base_url || "https://openspeech.bytedance.com";
  $("#voice-stt-model").value = voice.stt_model || "volc.bigasr.auc_turbo";
  $("#voice-tts-model").value = voice.tts_model || "seed-tts-2.0";
  $("#voice-name").value = voice.voice_name || "zh_female_xiaohe_uranus_bigtts";
  $("#voice-audio-format").value = voice.audio_format || "mp3";
  $("#voice-timeout").value = voice.timeout_seconds || 60;
  $("#voice-api-key").value = "";
  $("#voice-api-key").placeholder = voice.has_api_key ? voice.api_key_hint : "未配置";
  $("#voice-key-help").textContent = voice.has_api_key ? `当前密钥 ${voice.api_key_hint}；留空表示保留。` : "尚未配置语音密钥。";
  $("#clear-voice-api-key").checked = false;
  $("#voice-app-id").value = voice.app_id || "";
  $("#voice-access-token").value = "";
  $("#voice-access-token").placeholder = voice.has_access_token ? voice.access_token_hint : "未配置";
  $("#voice-access-token-help").textContent = voice.has_access_token ? `当前 Access Token ${voice.access_token_hint}；留空表示保留。` : "新版控制台只需上面的 App Key；旧版控制台才填写这一组。";
  $("#clear-voice-access-token").checked = false;
  updateVoiceProviderUI(false);
  const status = $("#voice-status");
  status.className = `connection-status ${voice.enabled ? "status-model" : "status-demo"}`;
  status.textContent = voice.enabled ? `已接入 · ${voice.provider === "doubao" ? "豆包" : "OpenAI 兼容"}` : "尚未配置";
}

function voicePayload() {
  return {
    provider: $("#voice-provider").value,
    api_key: $("#voice-api-key").value.trim(),
    clear_api_key: $("#clear-voice-api-key").checked,
    app_id: $("#voice-app-id").value.trim(),
    access_token: $("#voice-access-token").value.trim(),
    clear_access_token: $("#clear-voice-access-token").checked,
    base_url: $("#voice-base-url").value.trim(),
    stt_model: $("#voice-stt-model").value.trim(),
    tts_model: $("#voice-tts-model").value.trim(),
    voice_name: $("#voice-name").value.trim(),
    audio_format: $("#voice-audio-format").value,
    timeout_seconds: Number($("#voice-timeout").value),
  };
}

function updateVoiceProviderUI(applyDefaults = true) {
  const doubao = $("#voice-provider").value === "doubao";
  $("#voice-legacy-fields").hidden = !doubao;
  $("#voice-api-key-label").textContent = doubao ? "App Key（新版控制台）" : "语音 API 密钥";
  $("#voice-stt-label").textContent = doubao ? "语音识别资源 ID" : "语音识别模型";
  $("#voice-tts-label").textContent = doubao ? "语音合成资源 ID" : "语音合成模型";
  $("#voice-name-label").textContent = doubao ? "豆包音色 ID" : "音色";
  $("#voice-provider-help").textContent = doubao
    ? "当前交互使用录音文件极速识别和单向流式语音合成，不启用全双工实时通话。"
    : "接口需要兼容 /v1/audio/transcriptions 与 /v1/audio/speech。";
  const aacOption = [...$("#voice-audio-format").options].find((option) => option.value === "aac");
  aacOption.disabled = doubao;
  if (doubao && !["mp3", "opus"].includes($("#voice-audio-format").value)) $("#voice-audio-format").value = "mp3";
  if (!applyDefaults) return;
  $("#voice-base-url").value = doubao ? "https://openspeech.bytedance.com" : "https://api.openai.com";
  $("#voice-stt-model").value = doubao ? "volc.bigasr.auc_turbo" : "whisper-1";
  $("#voice-tts-model").value = doubao ? "seed-tts-2.0" : "gpt-4o-mini-tts";
  $("#voice-name").value = doubao ? "zh_female_xiaohe_uranus_bigtts" : "alloy";
}

function renderModelStatus(model) {
  const status = $("#model-status");
  const active = model.mode === "model";
  status.className = `connection-status ${active ? "status-model" : "status-demo"}`;
  status.textContent = active ? `已接入 · ${model.model_id}` : "演示模式";
}

function updateProviderHelp() {
  const anthropic = $("#model-provider").value === "anthropic";
  $("#provider-help").textContent = anthropic
    ? "适用于 Claude 官方接口及兼容 Anthropic Messages 的服务。"
    : "适用于 OpenAI、DeepSeek、通义千问及其他兼容 Chat Completions 的服务。";
}

function modelPayload() {
  return {
    provider: $("#model-provider").value,
    api_key: $("#model-api-key").value.trim(),
    clear_api_key: $("#clear-api-key").checked,
    model_id: $("#model-id").value.trim(),
    base_url: $("#model-base-url").value.trim(),
    timeout_seconds: Number($("#model-timeout").value),
    max_tokens: Number($("#model-max-tokens").value),
    temperature: Number($("#model-temperature").value),
  };
}

function updatePromptCounts() {
  for (const mode of ["discussion", "recommendation"]) {
    const count = $(`#${mode}-prompt`).value.length;
    $(`#${mode}-count`).textContent = `${count.toLocaleString("zh-CN")} / 30,000 字符`;
  }
}

function showDashboardShell() {
  $("#admin-login").hidden = true;
  $("#admin-dashboard").hidden = false;
  $("#admin-logout").hidden = false;
}

function inviteRow(invite) {
  const row = document.createElement("tr");
  const cells = [
    `•••••-${invite.code_hint}`,
    invite.status,
    invite.account_id ? `…${invite.account_id.slice(-6)}` : "—",
    dateText(invite.created_at),
    dateText(invite.activated_at),
  ];
  cells.forEach((text, index) => {
    const cell = document.createElement("td");
    if (index === 1) {
      const status = document.createElement("span");
      status.className = `status status-${invite.status}`;
      status.textContent = { issued: "已发放", active: "已激活", revoked: "已停用" }[invite.status] || invite.status;
      cell.append(status);
    } else {
      cell.textContent = text;
    }
    row.append(cell);
  });
  const actionsCell = document.createElement("td");
  actionsCell.className = "table-actions";
  if (invite.status === "active") {
    actionsCell.append(
      actionButton("换发", () => rotateInvite(invite.id)),
      actionButton("停用", () => revokeInvite(invite.id))
    );
  } else if (invite.status === "issued") {
    actionsCell.append(actionButton("停用", () => revokeInvite(invite.id)));
  }
  row.append(actionsCell);
  return row;
}

function actionButton(label, action) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "mini-button";
  button.textContent = label;
  button.addEventListener("click", async () => {
    button.disabled = true;
    try { await action(); } catch (error) { toast(error.message); }
    finally { button.disabled = false; }
  });
  return button;
}

function dateText(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}

function showCodes(codes, title = "新生成的邀请码") {
  $("#new-codes").hidden = false;
  $("#new-codes h3").textContent = title;
  $("#codes-output").textContent = codes.join("\n");
  $("#new-codes").scrollIntoView({ behavior: "smooth", block: "center" });
}

async function revokeInvite(id) {
  if (!window.confirm("停用后，该邀请码和现有登录会话都会失效。继续吗？")) return;
  await api(`/api/admin/invites/${encodeURIComponent(id)}/revoke`, { method: "POST", body: "{}" });
  toast("邀请码已停用");
  await loadInvites();
}

async function rotateInvite(id) {
  if (!window.confirm("换发会让旧邀请码和现有登录会话立即失效，但保留原账户片单。继续吗？")) return;
  const data = await api(`/api/admin/invites/${encodeURIComponent(id)}/rotate`, { method: "POST", body: "{}" });
  showCodes([data.code], "换发后的新邀请码");
  toast("已换发，旧邀请码失效");
  await loadInvites();
}

$("#admin-login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button");
  button.disabled = true;
  $("#admin-error").textContent = "";
  try {
    await api("/api/admin/login", {
      method: "POST",
      body: JSON.stringify({ admin_token: $("#admin-token").value }),
    });
    showDashboard();
  } catch (error) {
    $("#admin-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

$("#model-provider").addEventListener("change", updateProviderHelp);

$("#model-config-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector('button[type="submit"]');
  button.disabled = true;
  $("#model-test-result").textContent = "";
  try {
    const data = await api("/api/admin/agent-config/model", {
      method: "POST",
      body: JSON.stringify(modelPayload()),
    });
    renderModelStatus(data.model);
    $("#model-api-key").value = "";
    $("#model-api-key").placeholder = data.model.has_api_key ? data.model.api_key_hint : "未配置";
    $("#api-key-help").textContent = data.model.has_api_key
      ? `当前密钥 ${data.model.api_key_hint}；留空表示保留。`
      : "尚未配置密钥，保存后仍可使用演示模式。";
    $("#clear-api-key").checked = false;
    $("#model-test-result").textContent = "配置已保存，并已立即生效";
    toast("模型配置已保存");
  } catch (error) {
    $("#model-test-result").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

$("#test-model").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  $("#model-test-result").textContent = "正在连接模型…";
  try {
    const data = await api("/api/admin/agent-config/test", {
      method: "POST",
      body: JSON.stringify(modelPayload()),
    });
    $("#model-test-result").textContent = `连接成功 · ${data.elapsed_ms}ms · ${data.reply}`;
  } catch (error) {
    $("#model-test-result").textContent = `连接失败：${error.message}`;
  } finally {
    button.disabled = false;
  }
});

$("#internet-config-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector('button[type="submit"]');
  button.disabled = true;
  $("#internet-test-result").textContent = "正在保存…";
  try {
    const data = await api("/api/admin/internet-config", {
      method: "POST",
      body: JSON.stringify(internetPayload()),
    });
    renderInternetConfig(data.internet);
    $("#internet-test-result").textContent = "联网配置已保存，并已立即生效";
    toast("联网与电影配置已保存");
  } catch (error) {
    $("#internet-test-result").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

for (const [selector, target, label] of [
  ["#test-tmdb", "tmdb", "TMDB"],
  ["#test-web-search", "web_search", "Web Search"],
]) {
  $(selector).addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    $("#internet-test-result").textContent = `正在测试 ${label}…`;
    try {
      const data = await api("/api/admin/internet-config/test", {
        method: "POST",
        body: JSON.stringify({ ...internetPayload(), target }),
      });
      $("#internet-test-result").textContent = `${label} 连接成功 · 返回 ${data.items} 项`;
    } catch (error) {
      $("#internet-test-result").textContent = `${label} 连接失败：${error.message}`;
    } finally {
      button.disabled = false;
    }
  });
}

$("#voice-config-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector('button[type="submit"]');
  button.disabled = true;
  $("#voice-test-result").textContent = "正在保存…";
  try {
    const data = await api("/api/admin/voice-config", {
      method: "POST",
      body: JSON.stringify(voicePayload()),
    });
    renderVoiceConfig(data.voice);
    $("#voice-test-result").textContent = "语音配置已保存，并已立即生效";
    toast("语音配置已保存");
  } catch (error) {
    $("#voice-test-result").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

$("#voice-provider").addEventListener("change", () => updateVoiceProviderUI(true));
$("#web-search-provider").addEventListener("change", () => updateWebSearchProviderUI(true));

$("#test-voice").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  $("#voice-test-result").textContent = "正在合成测试语音…";
  try {
    const data = await api("/api/admin/voice-config/test", {
      method: "POST",
      body: JSON.stringify(voicePayload()),
    });
    $("#voice-test-result").textContent = `语音连接成功 · ${data.content_type} · ${data.bytes} bytes`;
  } catch (error) {
    $("#voice-test-result").textContent = `语音连接失败：${error.message}`;
  } finally {
    button.disabled = false;
  }
});

for (const textarea of [$("#discussion-prompt"), $("#recommendation-prompt")]) {
  textarea.addEventListener("input", updatePromptCounts);
}

document.querySelectorAll("[data-reset-prompt]").forEach((button) => {
  button.addEventListener("click", () => {
    const mode = button.dataset.resetPrompt;
    const label = mode === "discussion" ? "聊电影" : "找电影";
    if (!window.confirm(`恢复${label} Agent 的默认提示词吗？保存前不会生效。`)) return;
    $(`#${mode}-prompt`).value = promptDefaults[mode];
    updatePromptCounts();
  });
});

$("#prompt-config-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector('button[type="submit"]');
  button.disabled = true;
  $("#prompt-save-result").textContent = "正在保存…";
  try {
    await api("/api/admin/agent-config/prompts", {
      method: "POST",
      body: JSON.stringify({
        discussion: $("#discussion-prompt").value,
        recommendation: $("#recommendation-prompt").value,
      }),
    });
    $("#prompt-save-result").textContent = "两套提示词已保存，并已立即生效";
    toast("Agent 提示词已保存");
  } catch (error) {
    $("#prompt-save-result").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

$("#reset-openings").addEventListener("click", () => {
  if (!window.confirm("恢复三条默认开场白吗？保存前不会生效。")) return;
  $("#discussion-opening").value = openingDefaults.discussion || "";
  $("#discussion-movie-opening").value = openingDefaults.discussion_movie || "";
  $("#recommendation-opening").value = openingDefaults.recommendation || "";
});

$("#opening-config-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector('button[type="submit"]');
  button.disabled = true;
  $("#opening-save-result").textContent = "正在保存…";
  try {
    const data = await api("/api/admin/agent-config/openings", {
      method: "POST",
      body: JSON.stringify({
        discussion: $("#discussion-opening").value,
        discussion_movie: $("#discussion-movie-opening").value,
        recommendation: $("#recommendation-opening").value,
      }),
    });
    $("#discussion-opening").value = data.openings.discussion;
    $("#discussion-movie-opening").value = data.openings.discussion_movie;
    $("#recommendation-opening").value = data.openings.recommendation;
    $("#opening-save-result").textContent = "三条开场白已保存，并已立即生效";
    toast("开场白已保存");
  } catch (error) {
    $("#opening-save-result").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

$("#generate-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button");
  button.disabled = true;
  try {
    const count = Number($("#invite-count").value);
    const data = await api("/api/admin/invites/generate", {
      method: "POST",
      body: JSON.stringify({ count }),
    });
    showCodes(data.codes);
    await loadInvites();
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
});

$("#copy-codes").addEventListener("click", async () => {
  await navigator.clipboard.writeText($("#codes-output").textContent);
  toast("已复制全部邀请码");
});

$("#admin-logout").addEventListener("click", async () => {
  await api("/api/admin/logout", { method: "POST", body: "{}" });
  showLogin();
});

loadDashboard();
