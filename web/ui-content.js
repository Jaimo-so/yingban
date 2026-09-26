(function () {
  "use strict";

  function fail(path, message) {
    throw new Error(`ui-content.json: ${path} ${message}`);
  }

  const text = (...tokens) => (value, path) => {
    if (typeof value !== "string") fail(path, "必须是文字");
    for (const match of value.matchAll(/\{([^{}]+)\}/g)) {
      if (!tokens.includes(match[1])) fail(path, `不支持占位符 {${match[1]}}`);
    }
  };
  const boolean = (value, path) => {
    if (typeof value !== "boolean") fail(path, "必须是 true 或 false");
  };
  const fields = (...allowed) => (value, path) => {
    if (!Array.isArray(value)) fail(path, "必须是字段列表");
    if (new Set(value).size !== value.length) fail(path, "不能重复字段");
    for (const field of value) if (!allowed.includes(field)) fail(path, `不支持字段 ${field}`);
  };
  const actions = (allowed, tokens = []) => (value, path) => {
    if (!Array.isArray(value)) fail(path, "必须是按钮列表");
    const seen = new Set();
    value.forEach((item, index) => {
      const itemPath = `${path}[${index}]`;
      check(item, {id: text(), label: text(...tokens)}, itemPath);
      if (!allowed.includes(item.id)) fail(`${itemPath}.id`, `不支持动作 ${item.id}`);
      if (seen.has(item.id)) fail(`${itemPath}.id`, "不能重复动作");
      if (!item.label.trim()) fail(`${itemPath}.label`, "按钮名称不能为空");
      seen.add(item.id);
    });
  };
  const mode = {kicker: text(), title: text(), description: text()};
  const schema = {
    home: {
      eyebrow: text(), title: text(), description: text(), discussion: mode, recommendation: mode,
      boxOffice: {eyebrow: text(), title: text(), sourceLabel: text(), current: text("date"), stale: text("date"), unavailable: text()},
    },
    poster: {showYear: boolean, showRegions: boolean, alt: text("title")},
    search: {submit: text(), submitting: text(), loading: text(), loadingCandidates: text(), empty: text(), unavailable: text(), failed: text()},
    cards: {
      onboarding: {select: text(), selectAria: text("title")},
      home: {
        fields: fields("rank", "title", "amount", "meta"), rank: text("rank"), amount: text("amount"), meta: text("sessions", "audience"),
        actions: actions(["watchlist", "discuss"]),
      },
      recommendation: {
        fields: fields("genres", "reason", "notes"), notes: text("notes"), feedbackRecorded: text(),
        actions: actions(["watchlist", "watched", "discuss", "reasons"]),
        feedbackReasons: actions(["not_now", "wrong_tone", "wrong_genre", "too_heavy", "not_interested"]),
      },
      history: {
        fields: fields("title", "meta"), meta: text("originalTitle", "genres"), noteBadge: text(), posterAria: text("title"),
        watchlistActions: actions(["keep", "watched", "not_now", "not_interested", "remove"]),
        watchedActions: actions(["discuss", "remove"]),
        conversationActions: actions(["continue", "conversations", "discuss", "remove"], ["count"]),
      },
      search: {fields: fields("title", "meta"), meta: text("year", "originalTitle"), watchedLabel: text(), otherLabel: text()},
    },
  };

  function check(value, rule, path) {
    if (typeof rule === "function") return rule(value, path);
    if (!value || typeof value !== "object" || Array.isArray(value)) fail(path, "必须是对象");
    for (const key of Object.keys(value)) if (!Object.hasOwn(rule, key)) fail(`${path}.${key}`, "不是可编辑配置项");
    for (const [key, child] of Object.entries(rule)) check(value[key], child, path ? `${path}.${key}` : key);
  }

  function validate(value) {
    check(value, schema, "");
    return value;
  }

  let content;
  async function load() {
    const response = await fetch("/ui-content.json", {cache: "no-store"});
    if (!response.ok) throw new Error(`ui-content.json 加载失败（${response.status}）`);
    let value;
    try { value = await response.json(); }
    catch { throw new Error("ui-content.json 格式不正确，请检查引号和逗号"); }
    content = validate(value);
    return content;
  }

  function get(path) {
    if (!content) throw new Error("界面配置尚未加载");
    return path.split(".").reduce((value, key) => value[key], content);
  }

  function format(template, values = {}) {
    return template.replace(/\{([^{}]+)\}/g, (_, key) => String(values[key] ?? ""));
  }

  function applyPage() {
    const bindings = {
      ".home-intro .eyebrow": "home.eyebrow",
      ".home-intro h1": "home.title",
      ".home-intro > p:last-child": "home.description",
      "#discussion-card .mode-kicker": "home.discussion.kicker",
      "#discussion-card strong": "home.discussion.title",
      "#discussion-card .mode-copy > span:last-child": "home.discussion.description",
      "#recommendation-card .mode-kicker": "home.recommendation.kicker",
      "#recommendation-card strong": "home.recommendation.title",
      "#recommendation-card .mode-copy > span:last-child": "home.recommendation.description",
      "#weekly-card .eyebrow": "home.boxOffice.eyebrow",
      "#weekly-title": "home.boxOffice.title",
      "#box-office-source": "home.boxOffice.sourceLabel",
      "#movie-search-form button": "search.submit",
      "#onboarding-search-form button": "search.submit",
    };
    for (const [selector, key] of Object.entries(bindings)) document.querySelector(selector).textContent = get(key);
  }

  window.YingbanContent = {load, get, format, applyPage, validate};
}());
