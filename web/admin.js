const $ = (selector) => document.querySelector(selector);
let promptDefaults = { discussion: "", recommendation: "" };
let openingDefaults = { discussion: "", discussion_movie: "", recommendation: "" };
let skillDefaults = {};
let activeSkillKey = "";
const STEPFUN_BASE_URL = "https://api.stepfun.com/step_plan/v1";
const CUSTOM_BASE_URL = "__custom__";
const MODEL_PROVIDER_LABELS = {
  anthropic: "Anthropic Messages",
  openai_compatible: "OpenAI 兼容",
  stepfun: "阶跃星辰 Step Plan",
};
const VOICE_PROVIDER_LABELS = {
  doubao: "豆包语音",
  openai_compatible: "OpenAI 兼容语音",
  stepfun: "阶跃星辰 StepAudio 2.5",
};
const BASE_URL_PRESETS = {
  model: {
    anthropic: [
      { label: "Anthropic 官方", value: "https://api.anthropic.com" },
      { label: "DeepSeek Anthropic 兼容", value: "https://api.deepseek.com/anthropic" },
      { label: "MiniMax Anthropic 兼容", value: "https://api.minimaxi.com/anthropic" },
    ],
    openai_compatible: [
      { label: "OpenAI 官方", value: "https://api.openai.com" },
      { label: "TokenRouter 聚合服务", value: "https://api.tokenrouter.com/v1" },
      { label: "OpenRouter 聚合服务", value: "https://openrouter.ai/api/v1" },
      { label: "DeepSeek", value: "https://api.deepseek.com" },
      { label: "Google Gemini", value: "https://generativelanguage.googleapis.com/v1beta/openai" },
      { label: "智谱 BigModel", value: "https://open.bigmodel.cn/api/paas/v4" },
      { label: "阿里云百炼（默认空间）", value: "https://dashscope.aliyuncs.com/compatible-mode/v1" },
      { label: "MiniMax", value: "https://api.minimaxi.com/v1" },
      { label: "火山方舟", value: "https://ark.cn-beijing.volces.com/api/v3" },
      { label: "硅基流动", value: "https://api.siliconflow.cn/v1" },
    ],
    stepfun: [{ label: "阶跃星辰 Step Plan", value: STEPFUN_BASE_URL }],
  },
  voice: {
    doubao: [{ label: "豆包语音官方", value: "https://openspeech.bytedance.com" }],
    openai_compatible: [{ label: "OpenAI 官方", value: "https://api.openai.com" }],
    stepfun: [{ label: "阶跃星辰 StepAudio", value: STEPFUN_BASE_URL }],
  },
  image: {
    stepfun: [{ label: "阶跃星辰 Step Image", value: STEPFUN_BASE_URL }],
  },
};
const BASE_URL_PICKERS = {
  model: {
    select: "#model-base-url-preset",
    input: "#model-base-url",
    customField: "#model-base-url-custom-field",
  },
  voice: {
    select: "#voice-base-url-preset",
    input: "#voice-base-url",
    customField: "#voice-base-url-custom-field",
  },
  image: {
    select: "#image-base-url-preset",
    input: "#image-base-url",
    customField: "#image-base-url-custom-field",
  },
};
const fallbackModelCatalog = [
  ["step-3.5-flash-2603", "text", "Step 3.5 Flash 2603"],
  ["step-3.7-flash", "text", "Step 3.7 Flash"],
  ["step-router-v1", "text", "Step Router V1"],
  ["step-image-edit-2", "image", "Step Image Edit 2"],
  ["stepaudio-2.5-asr", "asr", "StepAudio 2.5 ASR"],
  ["stepaudio-2.5-chat", "audio_chat", "StepAudio 2.5 Chat"],
  ["stepaudio-2.5-realtime", "realtime", "StepAudio 2.5 Realtime"],
  ["stepaudio-2.5-tts", "tts", "StepAudio 2.5 TTS"],
].map(([id, capability, label]) => ({ id, capability, label }));
let modelCatalog = fallbackModelCatalog;

function modelsFor(...capabilities) {
  const selected = new Set(capabilities);
  return modelCatalog.filter((model) => selected.has(model.capability));
}

function renderDataList(selector, models) {
  $(selector).replaceChildren(...models.map((model) => {
    const option = document.createElement("option");
    option.value = model.id;
    option.label = model.label || model.id;
    return option;
  }));
}

function renderSelectOptions(selector, models, selectedValue = "") {
  const select = $(selector);
  select.replaceChildren(...models.map((model) => {
    const option = document.createElement("option");
    option.value = model.id;
    option.textContent = `${model.label || model.id} · ${model.id}`;
    return option;
  }));
  if (selectedValue) select.value = selectedValue;
}

function applyBaseUrlSelection(kind) {
  const picker = BASE_URL_PICKERS[kind];
  const select = $(picker.select);
  const input = $(picker.input);
  const custom = select.value === CUSTOM_BASE_URL;
  if (!custom) {
    input.value = select.value;
    const targetProvider = ["model", "voice"].includes(kind)
      ? select.selectedOptions[0]?.dataset.provider
      : "";
    const providerSelector = kind === "model" ? "#model-provider" : "#voice-provider";
    if (targetProvider && $(providerSelector).value !== targetProvider) {
      $(providerSelector).value = targetProvider;
      if (kind === "model") updateProviderHelp(false);
      if (kind === "voice") updateVoiceProviderUI(true);
      return;
    }
  }
  $(picker.customField).hidden = !custom;
}

function renderBaseUrlPicker(kind, provider, currentUrl = "", applyDefault = false) {
  const picker = BASE_URL_PICKERS[kind];
  const providerPresets = BASE_URL_PRESETS[kind]?.[provider] || [];
  const isGroupedPicker = ["model", "voice"].includes(kind);
  const providerLabels = kind === "model" ? MODEL_PROVIDER_LABELS : VOICE_PROVIDER_LABELS;
  const allPresets = isGroupedPicker
    ? Object.values(BASE_URL_PRESETS[kind]).flat()
    : providerPresets;
  const select = $(picker.select);
  const input = $(picker.input);
  const normalizedCurrent = String(currentUrl || "").trim();
  const createPresetOption = (preset, presetProvider = "") => {
    const option = document.createElement("option");
    option.value = preset.value;
    option.textContent = `${preset.label} · ${preset.value}`;
    if (presetProvider) option.dataset.provider = presetProvider;
    return option;
  };
  const presetNodes = isGroupedPicker
    ? Object.entries(BASE_URL_PRESETS[kind]).map(([presetProvider, presets]) => {
      const group = document.createElement("optgroup");
      group.label = providerLabels[presetProvider];
      group.append(...presets.map((preset) => createPresetOption(preset, presetProvider)));
      return group;
    })
    : providerPresets.map((preset) => createPresetOption(preset));
  const customOption = document.createElement("option");
  customOption.value = CUSTOM_BASE_URL;
  customOption.textContent = "自定义接口根地址";
  select.replaceChildren(...presetNodes, customOption);

  const hasPreset = allPresets.some((preset) => preset.value === normalizedCurrent);
  if (applyDefault || !normalizedCurrent) {
    input.value = providerPresets[0]?.value || normalizedCurrent;
    select.value = providerPresets[0]?.value || CUSTOM_BASE_URL;
  } else {
    input.value = normalizedCurrent;
    select.value = hasPreset ? normalizedCurrent : CUSTOM_BASE_URL;
  }
  applyBaseUrlSelection(kind);
}

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
    const [invites, config, metrics, pricing, skills] = await Promise.all([
      api("/api/admin/invites"),
      api("/api/admin/agent-config"),
      api("/api/admin/metrics/summary"),
      api("/api/admin/usage-pricing"),
      api("/api/admin/skills"),
    ]);
    renderInvites(invites);
    renderAgentConfig(config);
    renderMetrics(metrics);
    renderUsagePricing(pricing);
    skillDefaults = skills.defaults || {};
    renderSkillManagement(skills.items || []);
    showDashboardShell();
  } catch (error) {
    if (error.message.includes("未登录")) showLogin();
    else toast(error.message);
  }
}

function renderUsagePricing(pricing) {
  const rates = pricing.rates || {};
  const modelRate = rates.chat || rates.reflection || rates.taste_profile || {};
  $("#pricing-version").value = pricing.version || "unpriced-v1";
  $("#pricing-model-input").value = Number(modelRate.input_per_million || 0);
  $("#pricing-model-output").value = Number(modelRate.output_per_million || 0);
  for (const [operation, inputId] of [
    ["stt", "#pricing-stt-call"], ["tts", "#pricing-tts-call"],
    ["tmdb", "#pricing-tmdb-call"], ["web_search", "#pricing-search-call"],
    ["web_reader", "#pricing-reader-call"],
  ]) {
    $(inputId).value = Number((rates[operation] || {}).call_cost || 0);
  }
}

function renderMetrics(metrics) {
  const onboarding = metrics.onboarding || {};
  const recommendations = metrics.recommendations || {};
  const feedbackCount = Object.values(recommendations.feedback || {}).reduce((sum, value) => sum + Number(value || 0), 0);
  const reflections = metrics.reflections || {};
  const cards = [
    ["账户", metrics.accounts || 0, "当前账户总数"],
    ["冷启动完成", onboarding.completed || 0, `进行中 ${onboarding.in_progress || 0}`],
    ["推荐曝光", recommendations.impressions || 0, `已反馈 ${feedbackCount}`],
    ["观后感草稿", reflections.draft || 0, `确认 ${reflections.confirmed || 0} · 锁定 ${reflections.locked || 0}`],
  ];
  $("#metrics-grid").replaceChildren(...cards.map(([label, value, detail]) => {
    const card = document.createElement("article");
    card.className = "metric-card";
    const title = document.createElement("span");
    title.textContent = label;
    const number = document.createElement("strong");
    number.textContent = Number(value).toLocaleString("zh-CN");
    const copy = document.createElement("small");
    copy.textContent = detail;
    card.append(title, number, copy);
    return card;
  }));
  $("#metrics-usage-rows").replaceChildren(...(metrics.usage || []).map((usage) => {
    const row = document.createElement("tr");
    for (const value of [
      usage.operation, usage.calls, usage.failures,
      `${usage.average_latency_ms || 0} ms`, `¥${Number(usage.estimated_cost || 0).toFixed(6)}`,
    ]) {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.append(cell);
    }
    return row;
  }));
}

async function loadMetrics() {
  const metrics = await api("/api/admin/metrics/summary");
  renderMetrics(metrics);
}

async function loadInvites() {
  const data = await api("/api/admin/invites");
  renderInvites(data);
}

function renderInvites(data) {
  $("#invite-rows").replaceChildren(...(data.items || []).map(inviteRow));
  const storageNote = $("#invite-storage-note");
  const persistent = data.storage?.persistent !== false;
  storageNote.hidden = persistent;
  storageNote.textContent = persistent
    ? ""
    : "当前数据库位于临时存储：更新配置、重新发布或实例回收后，邀请码和账户数据仍会丢失。请挂载 NAS，并将 YINGBAN_DATABASE 指向挂载目录。";
}

function renderAgentConfig(config) {
  const model = config.model;
  modelCatalog = config.model_catalog?.models?.length ? config.model_catalog.models : fallbackModelCatalog;
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
  renderImageConfig(config.image || {});

  $("#discussion-prompt").value = config.prompts.discussion;
  $("#recommendation-prompt").value = config.prompts.recommendation;
  $("#core-prompt").value = config.core_prompt || "";
  $("#discussion-opening").value = config.openings?.discussion || "";
  $("#discussion-movie-opening").value = config.openings?.discussion_movie || "";
  $("#recommendation-opening").value = config.openings?.recommendation || "";
  updatePromptCounts();
}

const skillModuleLabels = { discussion: "聊电影", recommendation: "选电影" };

function selectApiPanel(apiKey) {
  document.querySelectorAll("[data-api-target]").forEach((button) => {
    const selected = button.dataset.apiTarget === apiKey;
    button.classList.toggle("is-active", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
  document.querySelectorAll("[data-api-panel]").forEach((panel) => {
    panel.hidden = panel.dataset.apiPanel !== apiKey;
  });
}

function selectPromptPanel(promptKey) {
  document.querySelectorAll("[data-prompt-target]").forEach((button) => {
    const selected = button.dataset.promptTarget === promptKey;
    button.classList.toggle("is-active", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
  document.querySelectorAll("[data-prompt-panel]").forEach((panel) => {
    panel.hidden = panel.dataset.promptPanel !== promptKey;
  });
  $("#prompt-actions").hidden = promptKey === "core";
}

async function loadSkills() {
  const data = await api("/api/admin/skills");
  skillDefaults = data.defaults || skillDefaults;
  renderSkillManagement(data.items || []);
}

function skillActionButton(label, action, className = "button button-secondary") {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = label;
  button.addEventListener("click", async () => {
    button.disabled = true;
    try { await action(); } catch (error) { toast(error.message); }
    finally { button.disabled = false; }
  });
  return button;
}

function selectSkillPanel(skillKey) {
  activeSkillKey = skillKey;
  document.querySelectorAll("[data-skill-target]").forEach((button) => {
    const selected = button.dataset.skillTarget === skillKey;
    button.classList.toggle("is-active", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
  document.querySelectorAll("[data-skill-panel]").forEach((panel) => {
    panel.hidden = panel.dataset.skillPanel !== skillKey;
  });
}

function renderSkillManagement(skills) {
  const list = $("#skill-management-list");
  const detail = $("#skill-management-detail");
  if (!skills.length) {
    activeSkillKey = "";
    list.replaceChildren();
    const empty = document.createElement("p");
    empty.className = "config-card management-empty-state";
    empty.textContent = "当前没有可管理的 Skill。";
    detail.replaceChildren(empty);
    return;
  }
  if (!skills.some((skill) => skill.skill_key === activeSkillKey)) {
    activeSkillKey = skills[0].skill_key;
  }

  const selectors = skills.map((skill) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "management-selector";
    button.dataset.skillTarget = skill.skill_key;
    button.setAttribute("aria-controls", `skill-panel-${skill.skill_key}`);
    const name = document.createElement("strong");
    name.textContent = skill.name;
    const meta = document.createElement("small");
    meta.textContent = `${skillModuleLabels[skill.module] || skill.module} · ${skill.enabled ? "已启用" : "已停用"}`;
    button.append(name, meta);
    button.addEventListener("click", () => selectSkillPanel(skill.skill_key));
    return button;
  });

  const cards = skills.map((skill) => {
    const card = document.createElement("article");
    card.className = "config-card skill-admin-card";
    card.dataset.skillKey = skill.skill_key;
    card.dataset.skillPanel = skill.skill_key;
    card.id = `skill-panel-${skill.skill_key}`;

    const head = document.createElement("div");
    head.className = "skill-admin-head";
    const titleWrap = document.createElement("div");
    titleWrap.className = "skill-admin-title";
    const moduleBadge = document.createElement("span");
    moduleBadge.className = "skill-module-badge";
    moduleBadge.textContent = skillModuleLabels[skill.module] || skill.module;
    const titleCopy = document.createElement("div");
    const title = document.createElement("h3");
    title.textContent = skill.name;
    const description = document.createElement("p");
    description.textContent = skill.description;
    titleCopy.append(title, description);
    titleWrap.append(moduleBadge, titleCopy);

    const enabledLabel = document.createElement("label");
    enabledLabel.className = "skill-enabled-label";
    const enabled = document.createElement("input");
    enabled.type = "checkbox";
    enabled.checked = skill.enabled;
    enabled.addEventListener("change", async () => {
      enabled.disabled = true;
      try {
        await api(`/api/admin/skills/${encodeURIComponent(skill.skill_key)}/enabled`, {
          method: "POST", body: JSON.stringify({ enabled: enabled.checked }),
        });
        toast(enabled.checked ? "Skill 已启用" : "Skill 已停用，历史数据仍保留");
        await loadSkills();
      } catch (error) {
        enabled.checked = !enabled.checked;
        toast(error.message);
      } finally { enabled.disabled = false; }
    });
    enabledLabel.append(enabled, document.createTextNode("运行时启用"));
    head.append(titleWrap, enabledLabel);

    const versionLine = document.createElement("div");
    versionLine.className = "skill-version-line";
    const activeText = document.createElement("span");
    activeText.textContent = `当前生效：v${skill.active_version}`;
    const activation = document.createElement("span");
    activation.textContent = `激活：${skill.activation_mode === "module_default" ? "模块默认" : "显式入口或意图识别"}`;
    const draftText = document.createElement("span");
    draftText.textContent = skill.draft ? `草稿：v${skill.draft.version}` : "没有未发布草稿";
    versionLine.append(activeText, activation, draftText);

    const editor = document.createElement("div");
    editor.className = "skill-editor";
    const editorVersion = skill.draft || skill.active;
    const inputContract = editorVersion?.input_contract || {};
    const outputContract = editorVersion?.output_contract || {};
    const instructionLabel = document.createElement("label");
    instructionLabel.textContent = "Skill 指令";
    const instruction = document.createElement("textarea");
    instruction.className = "skill-instructions";
    instruction.maxLength = 30000;
    instruction.value = editorVersion?.instructions || "";
    instructionLabel.append(instruction);

    const noteLabel = document.createElement("label");
    noteLabel.className = "skill-change-note";
    noteLabel.textContent = "版本修改说明";
    const note = document.createElement("input");
    note.maxLength = 500;
    note.placeholder = "说明本次为什么修改，便于以后回滚";
    note.value = skill.draft?.change_note || "";
    noteLabel.append(note);

    const preview = document.createElement("pre");
    preview.className = "skill-preview";
    preview.hidden = true;
    const result = document.createElement("span");
    result.className = "action-result";

    const actions = document.createElement("div");
    actions.className = "skill-admin-actions";
    actions.append(
      skillActionButton("保存草稿", async () => {
        await api(`/api/admin/skills/${encodeURIComponent(skill.skill_key)}/draft`, {
          method: "POST",
          body: JSON.stringify({
            instructions: instruction.value,
            input_contract: inputContract,
            output_contract: outputContract,
            change_note: note.value,
          }),
        });
        toast("Skill 草稿已保存，尚未进入运行时");
        await loadSkills();
      }),
      skillActionButton("发布生效", async () => {
        await api(`/api/admin/skills/${encodeURIComponent(skill.skill_key)}/publish`, {
          method: "POST", body: "{}",
        });
        toast("Skill 新版本已发布并立即生效");
        await loadSkills();
      }, "button button-primary"),
      skillActionButton("预览生效 Prompt", async () => {
        const data = await api(`/api/admin/skills/${encodeURIComponent(skill.skill_key)}/preview`, {
          method: "POST", body: "{}",
        });
        preview.textContent = data.preview.prompt;
        preview.hidden = !preview.hidden;
      }),
      skillActionButton("恢复默认到草稿", async () => {
        await api(`/api/admin/skills/${encodeURIComponent(skill.skill_key)}/restore`, {
          method: "POST", body: "{}",
        });
        toast("内置默认已恢复为未发布草稿");
        await loadSkills();
      }),
      skillActionButton("回滚上一版本", async () => {
        if (!window.confirm("将当前生效版本切换到上一份已发布版本，继续吗？")) return;
        await api(`/api/admin/skills/${encodeURIComponent(skill.skill_key)}/rollback`, {
          method: "POST", body: "{}",
        });
        toast("Skill 已回滚到上一版本");
        await loadSkills();
      }),
      result,
    );
    actions.children[1].disabled = !skill.draft;
    actions.children[4].disabled = !(skill.versions || []).some(
      (version) => Number(version.version) < Number(skill.active_version)
    );
    editor.append(instructionLabel, noteLabel, actions, preview);
    card.append(head, versionLine, editor);
    return card;
  });
  list.replaceChildren(...selectors);
  detail.replaceChildren(...cards);
  selectSkillPanel(activeSkillKey);
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
  const baseUrl = String(search.base_url || "");
  const inferredSearchProvider = search.provider
    || (baseUrl.includes("api.bochaai.com") ? "bocha" : baseUrl.includes("api.search.brave.com") ? "brave" : "tavily");
  $("#web-search-provider").value = inferredSearchProvider;
  const searchDefaults = {
    bocha: "https://api.bochaai.com/v1",
    tavily: "https://api.tavily.com",
    brave: "https://api.search.brave.com/res/v1",
  };
  $("#web-search-base-url").value = search.base_url || searchDefaults[inferredSearchProvider];
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
  const provider = $("#web-search-provider").value;
  const providers = {
    bocha: {
      baseUrl: "https://api.bochaai.com/v1",
      baseLabel: "博查 Web Search 接口根地址",
      keyLabel: "博查 API 密钥",
      providerHelp: "博查支持中文全网搜索，并可限定检索豆瓣或知乎公开页面。",
      keyHelp: "从博查开放平台复制 API 密钥。401 表示密钥无效，403 表示余额或额度不足；留空表示保留当前密钥。",
    },
    tavily: {
      baseUrl: "https://api.tavily.com",
      baseLabel: "Tavily Search 接口根地址",
      keyLabel: "Tavily API Key",
      providerHelp: "保留已有 Tavily 接入；使用前请确认当前套餐与额度。",
      keyHelp: "可搜索全网，并限定检索豆瓣或知乎；留空表示保留当前密钥。",
    },
    brave: {
      baseUrl: "https://api.search.brave.com/res/v1",
      baseLabel: "Brave Web Search 接口根地址",
      keyLabel: "Brave Web Search API 密钥",
      providerHelp: "保留已有 Brave Search 接入；使用前请确认当前套餐与额度。",
      keyHelp: "可搜索全网，并按站点检索豆瓣和知乎；留空表示保留当前密钥。",
    },
  };
  const current = providers[provider] || providers.bocha;
  $("#web-search-base-url-label").textContent = current.baseLabel;
  $("#web-search-api-key-label").textContent = current.keyLabel;
  $("#web-search-provider-help").textContent = current.providerHelp;
  $("#web-search-help").textContent = current.keyHelp;
  if (applyDefaults) {
    $("#web-search-base-url").value = current.baseUrl;
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
  $("#voice-chat-model").value = voice.chat_model || "stepaudio-2.5-chat";
  $("#voice-realtime-model").value = voice.realtime_model || "stepaudio-2.5-realtime";
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
  const providerLabel = { doubao: "豆包", stepfun: "阶跃星辰", openai_compatible: "OpenAI 兼容" }[voice.provider] || voice.provider;
  status.textContent = voice.enabled ? `已接入 · ${providerLabel}` : "尚未配置";
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
    chat_model: $("#voice-chat-model").value.trim(),
    realtime_model: $("#voice-realtime-model").value.trim(),
    voice_name: $("#voice-name").value.trim(),
    audio_format: $("#voice-audio-format").value,
    timeout_seconds: Number($("#voice-timeout").value),
  };
}

function updateVoiceProviderUI(applyDefaults = true) {
  const provider = $("#voice-provider").value;
  const doubao = provider === "doubao";
  const stepfun = provider === "stepfun";
  $("#voice-legacy-fields").hidden = !doubao;
  $("#voice-chat-field").hidden = !stepfun;
  $("#voice-realtime-field").hidden = !stepfun;
  $("#voice-api-key-label").textContent = doubao ? "App Key（新版控制台）" : stepfun ? "阶跃星辰 API 密钥" : "语音 API 密钥";
  $("#voice-stt-label").textContent = doubao ? "语音识别资源 ID" : "语音识别模型选择";
  $("#voice-tts-label").textContent = doubao ? "语音合成资源 ID" : "语音合成模型选择";
  $("#voice-name-label").textContent = doubao ? "豆包音色 ID" : stepfun ? "StepAudio 音色 ID" : "音色";
  $("#voice-provider-help").textContent = doubao
    ? "当前交互使用录音文件极速识别和单向流式语音合成，不启用全双工实时通话。"
    : stepfun
      ? "录音式语音已接入 StepAudio 2.5 ASR 与 TTS；Chat 与 Realtime 模型可分别选择，Realtime 仍需后续 WebSocket 通话界面。"
      : "接口需要兼容 /v1/audio/transcriptions 与 /v1/audio/speech。";
  renderDataList("#voice-stt-options", stepfun ? modelsFor("asr") : []);
  renderDataList("#voice-tts-options", stepfun ? modelsFor("tts") : []);
  renderDataList("#voice-chat-options", modelsFor("audio_chat"));
  renderDataList("#voice-realtime-options", modelsFor("realtime"));
  renderBaseUrlPicker("voice", provider, $("#voice-base-url").value, applyDefaults);
  const aacOption = [...$("#voice-audio-format").options].find((option) => option.value === "aac");
  aacOption.disabled = doubao || stepfun;
  if ((doubao || stepfun) && !["mp3", "opus", ...(stepfun ? ["wav"] : [])].includes($("#voice-audio-format").value)) $("#voice-audio-format").value = "mp3";
  if (!applyDefaults) return;
  $("#voice-stt-model").value = doubao ? "volc.bigasr.auc_turbo" : stepfun ? "stepaudio-2.5-asr" : "whisper-1";
  $("#voice-tts-model").value = doubao ? "seed-tts-2.0" : stepfun ? "stepaudio-2.5-tts" : "gpt-4o-mini-tts";
  $("#voice-chat-model").value = "stepaudio-2.5-chat";
  $("#voice-realtime-model").value = "stepaudio-2.5-realtime";
  $("#voice-name").value = doubao ? "zh_female_xiaohe_uranus_bigtts" : stepfun ? "cixingnansheng" : "alloy";
}

function renderImageConfig(image) {
  renderSelectOptions("#image-model", modelsFor("image"), image.model || "step-image-edit-2");
  $("#image-base-url").value = image.base_url || STEPFUN_BASE_URL;
  renderBaseUrlPicker("image", "stepfun", $("#image-base-url").value);
  $("#image-timeout").value = image.timeout_seconds || 60;
  $("#image-api-key").value = "";
  $("#image-api-key").placeholder = image.has_api_key ? image.api_key_hint : "未配置";
  $("#image-key-help").textContent = image.has_api_key ? `当前密钥 ${image.api_key_hint}；留空表示保留。` : "尚未配置阶跃星辰 API 密钥。";
  $("#clear-image-api-key").checked = false;
  const status = $("#image-status");
  status.className = `connection-status ${image.enabled ? "status-model" : "status-demo"}`;
  status.textContent = image.enabled ? `已接入 · ${image.model}` : "尚未配置";
}

function imagePayload() {
  return {
    api_key: $("#image-api-key").value.trim(),
    clear_api_key: $("#clear-image-api-key").checked,
    base_url: $("#image-base-url").value.trim(),
    model: $("#image-model").value,
    timeout_seconds: Number($("#image-timeout").value),
  };
}

function renderModelStatus(model) {
  const status = $("#model-status");
  const active = model.mode === "model";
  status.className = `connection-status ${active ? "status-model" : "status-demo"}`;
  status.textContent = active ? `已接入 · ${model.model_id}` : "演示模式";
}

function updateProviderHelp(applyDefaults = false) {
  const provider = $("#model-provider").value;
  const stepfun = provider === "stepfun";
  $("#provider-help").textContent = provider === "anthropic"
    ? "适用于 Claude 官方接口及兼容 Anthropic Messages 的服务。"
    : stepfun
      ? "使用 Step Plan 的 OpenAI 兼容 Chat Completions；Router 仅可通过该通道调用。"
      : "适用于 OpenAI、DeepSeek、通义千问及其他兼容 Chat Completions 的服务。";
  $("#model-id-help").textContent = stepfun
    ? "可选择三款文本模型或返回文本的 StepAudio 2.5 Chat。"
    : "输入当前服务商支持的模型 ID。";
  renderDataList("#model-id-options", stepfun ? modelsFor("text", "audio_chat") : []);
  renderBaseUrlPicker("model", provider, $("#model-base-url").value, applyDefaults);
  if (applyDefaults) {
    if (provider === "anthropic") {
      $("#model-id").value = "claude-sonnet-4-6";
    } else if (stepfun) {
      $("#model-id").value = "step-3.5-flash-2603";
    } else {
      $("#model-id").value = "gpt-4.1-mini";
    }
  }
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
  const codeCell = document.createElement("td");
  const codeControl = document.createElement("div");
  codeControl.className = "invite-code-control";
  const codeHint = document.createElement("code");
  codeHint.textContent = `•••••-${invite.code_hint}`;
  codeControl.append(codeHint);
  if (invite.copy_available) {
    const copy = actionButton("复制", async () => {
      await copyInviteCode(invite.id, invite.code_hint);
    });
    copy.setAttribute("aria-label", `复制尾号 ${invite.code_hint} 的完整邀请码`);
    codeControl.append(copy);
  } else {
    const unavailable = document.createElement("small");
    unavailable.textContent = invite.status === "revoked" ? "已停用" : "旧码不可恢复";
    codeControl.append(unavailable);
  }
  codeCell.append(codeControl);
  row.append(codeCell);

  const cells = [
    invite.status,
    invite.account_id ? `…${invite.account_id.slice(-6)}` : "—",
    invite.note || "",
    Number(invite.model_calls || 0).toLocaleString("zh-CN"),
    `${Number(invite.movie_count || 0)} / ${Number(invite.conversation_count || 0)}`,
    dateText(invite.created_at),
    dateText(invite.activated_at),
  ];
  cells.forEach((text, index) => {
    const cell = document.createElement("td");
    if (index === 0) {
      const status = document.createElement("span");
      status.className = `status status-${invite.status}`;
      status.textContent = { issued: "已发放", active: "已激活", revoked: "已停用" }[invite.status] || invite.status;
      cell.append(status);
    } else if (index === 2) {
      const editor = document.createElement("div");
      editor.className = "invite-note-editor";
      const input = document.createElement("input");
      input.type = "text";
      input.maxLength = 200;
      input.value = text;
      input.placeholder = "添加备注";
      input.setAttribute("aria-label", `邀请码 •••••-${invite.code_hint} 的备注`);
      const save = actionButton("保存", async () => {
        await saveInviteNote(invite.id, input.value);
      });
      editor.append(input, save);
      cell.append(editor);
    } else {
      cell.textContent = text;
    }
    row.append(cell);
  });
  const actionsCell = document.createElement("td");
  actionsCell.className = "table-actions";
  if (invite.account_id) {
    actionsCell.append(actionButton("查看对话", () => openInviteConversations(invite)));
  }
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

async function copyInviteCode(id, codeHint) {
  const data = await api(`/api/admin/invites/${encodeURIComponent(id)}/code`, {
    method: "POST",
    body: "{}",
  });
  await navigator.clipboard.writeText(data.code);
  toast(`尾号 ${codeHint} 的邀请码已复制`);
}

async function saveInviteNote(id, note) {
  await api(`/api/admin/invites/${encodeURIComponent(id)}/note`, {
    method: "POST",
    body: JSON.stringify({ note }),
  });
  toast("邀请码备注已保存");
  await loadInvites();
}

async function openInviteConversations(invite) {
  const dialog = $("#invite-conversations-dialog");
  $("#invite-conversations-title").textContent = `邀请码 •••••-${invite.code_hint}`;
  $("#invite-conversations-summary").textContent = "正在加载按电影归档的对话记录…";
  $("#invite-conversations-content").replaceChildren();
  dialog.showModal();
  try {
    const data = await api(`/api/admin/invites/${encodeURIComponent(invite.id)}/conversations`);
    renderInviteConversations(data);
  } catch (error) {
    $("#invite-conversations-summary").textContent = error.message;
  }
}

function renderInviteConversations(data) {
  const items = data.items || [];
  const invite = data.invite || {};
  const groups = new Map();
  for (const item of items) {
    const key = item.movie_id;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  }
  $("#invite-conversations-summary").textContent = `${invite.note || "无备注"} · ${groups.size} 部电影 · ${items.length} 段对话`;
  const content = $("#invite-conversations-content");
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "conversation-empty";
    empty.textContent = "这个邀请码账户还没有已同步的电影对话。";
    content.replaceChildren(empty);
    return;
  }
  content.replaceChildren(...[...groups.values()].map((conversations) => {
    const first = conversations[0];
    const section = document.createElement("section");
    section.className = "conversation-movie-group";
    const heading = document.createElement("h3");
    heading.textContent = `《${first.title_zh}》${first.year ? `（${first.year}）` : ""}`;
    section.append(heading, ...conversations.map(conversationRecord));
    return section;
  }));
}

function conversationRecord(conversation) {
  const details = document.createElement("details");
  details.className = "conversation-record";
  const summary = document.createElement("summary");
  const userTurns = (conversation.messages || []).filter((item) => item.role === "user").length;
  const skill = conversation.skill_key ? ` · Skill ${conversation.skill_key}` : "";
  summary.textContent = `${dateText(conversation.updated_at)} · ${userTurns} 次用户发言${skill}`;
  const list = document.createElement("div");
  list.className = "conversation-message-list";
  list.append(...(conversation.messages || []).map((message) => {
    const item = document.createElement("div");
    item.className = `conversation-message conversation-message-${message.role}`;
    item.textContent = message.content;
    return item;
  }));
  details.append(summary, list);
  return details;
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

$("#model-provider").addEventListener("change", () => updateProviderHelp(true));
$("#model-base-url-preset").addEventListener("change", () => applyBaseUrlSelection("model"));

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
$("#voice-base-url-preset").addEventListener("change", () => applyBaseUrlSelection("voice"));
$("#image-base-url-preset").addEventListener("change", () => applyBaseUrlSelection("image"));
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

$("#image-config-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector('button[type="submit"]');
  button.disabled = true;
  $("#image-test-result").textContent = "正在保存…";
  try {
    const data = await api("/api/admin/image-config", {
      method: "POST",
      body: JSON.stringify(imagePayload()),
    });
    renderImageConfig(data.image);
    $("#image-test-result").textContent = "图像模型配置已保存，并已立即生效";
    toast("图像模型配置已保存");
  } catch (error) {
    $("#image-test-result").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

$("#test-image").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  $("#image-test-result").textContent = "正在查询模型权限…";
  try {
    const data = await api("/api/admin/image-config/test", {
      method: "POST",
      body: JSON.stringify(imagePayload()),
    });
    $("#image-test-result").textContent = `模型权限正常 · ${data.model}`;
  } catch (error) {
    $("#image-test-result").textContent = `模型权限测试失败：${error.message}`;
  } finally {
    button.disabled = false;
  }
});

for (const textarea of [$("#discussion-prompt"), $("#recommendation-prompt")]) {
  textarea.addEventListener("input", updatePromptCounts);
}

document.querySelectorAll("[data-prompt-target]").forEach((button) => {
  button.addEventListener("click", () => selectPromptPanel(button.dataset.promptTarget));
});

document.querySelectorAll("[data-api-target]").forEach((button) => {
  button.addEventListener("click", () => selectApiPanel(button.dataset.apiTarget));
});

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
      body: JSON.stringify({ count, note: $("#invite-note").value }),
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

$("#invite-conversations-close").addEventListener("click", () => {
  $("#invite-conversations-dialog").close();
});

$("#refresh-metrics").addEventListener("click", async (event) => {
  event.currentTarget.disabled = true;
  try {
    await loadMetrics();
    toast("产品健康汇总已刷新");
  } catch (error) {
    toast(error.message);
  } finally {
    event.currentTarget.disabled = false;
  }
});

$("#usage-pricing-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type='submit']");
  button.disabled = true;
  try {
    const inputRate = Number($("#pricing-model-input").value || 0);
    const outputRate = Number($("#pricing-model-output").value || 0);
    const modelRate = { input_per_million: inputRate, output_per_million: outputRate, call_cost: 0 };
    const callRate = (selector) => ({ input_per_million: 0, output_per_million: 0, call_cost: Number($(selector).value || 0) });
    const data = await api("/api/admin/usage-pricing", {
      method: "POST",
      body: JSON.stringify({
        version: $("#pricing-version").value.trim(),
        rates: {
          chat: modelRate,
          reflection: modelRate,
          taste_profile: modelRate,
          stt: callRate("#pricing-stt-call"),
          tts: callRate("#pricing-tts-call"),
          tmdb: callRate("#pricing-tmdb-call"),
          web_search: callRate("#pricing-search-call"),
          web_reader: callRate("#pricing-reader-call"),
        },
      }),
    });
    renderUsagePricing(data.pricing);
    await loadMetrics();
    toast("价格表已保存，后续调用将使用新版本估算");
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
});

$("#admin-logout").addEventListener("click", async () => {
  await api("/api/admin/logout", { method: "POST", body: "{}" });
  showLogin();
});

loadDashboard();
