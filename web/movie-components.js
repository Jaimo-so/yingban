(function () {
  "use strict";

  // Presentation only: callers own movie data, API requests and navigation.
  function poster(movie) {
    const node = document.createElement("div");
    node.className = "poster";
    const year = document.createElement("span");
    year.className = "poster-year";
    const config = window.YingbanContent.get("poster");
    year.textContent = [
      config.showYear ? movie.year || "—" : "",
      config.showRegions ? (movie.regions || []).join(" / ") : "",
    ].filter(Boolean).join(" · ");
    const title = document.createElement("strong");
    title.className = "poster-title";
    title.textContent = movie.title_zh;
    if (movie.poster_url) {
      const image = document.createElement("img");
      image.className = "poster-image";
      image.src = movie.poster_url;
      image.alt = window.YingbanContent.format(config.alt, {title: movie.title_zh});
      image.loading = "lazy";
      image.decoding = "async";
      image.referrerPolicy = "no-referrer";
      node.classList.add("is-loading");
      image.addEventListener("load", () => {
        node.classList.remove("is-loading");
        node.classList.add("has-image");
      }, { once: true });
      image.addEventListener("error", () => {
        image.remove();
        node.classList.remove("has-image", "is-loading");
        node.classList.add("image-failed");
      }, { once: true });
      node.append(image);
    }
    node.append(year, title);
    return node;
  }

  // A native button keeps mouse, Enter and Space activation identical.
  function actionButton(label, action, {
    className = "mini-button", ariaLabel, disabled = false, onError,
  } = {}) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = className;
    button.textContent = label;
    button.disabled = disabled;
    if (ariaLabel) button.setAttribute("aria-label", ariaLabel);
    button.addEventListener("click", async () => {
      if (button.disabled) return;
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      try { await action(button); }
      catch (error) {
        if (onError) onError(error);
        else throw error;
      } finally {
        button.disabled = disabled;
        button.removeAttribute("aria-busy");
      }
    });
    return button;
  }

  /**
   * Shared card structure, with caller-owned layout and content.
   * posterAction wraps only the poster (and optional label/badge), never
   * other action buttons. showPoster=false preserves compact search rows.
   */
  function movieCard(movie, {
    className, body, showPoster = true, posterAction,
  }) {
    const card = document.createElement("article");
    card.className = className;
    if (showPoster) {
      const image = poster(movie);
      if (posterAction) {
        const { action, content, ...options } = posterAction;
        const button = actionButton("", action, options);
        button.append(image);
        if (content) button.append(content);
        card.append(button);
      } else {
        card.append(image);
      }
    }
    if (body) card.append(...body);
    return card;
  }

  function element(tag, className, value) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) node.textContent = value;
    return node;
  }

  function appendFields(parent, order, nodes) {
    parent.append(...order.map(key => nodes[key]));
  }

  function configuredActions(items, handlers, onError, values = {}, className = "history-item-actions") {
    const row = element("div", className);
    for (const item of items) {
      row.append(actionButton(window.YingbanContent.format(item.label, values), handlers[item.id], {onError}));
    }
    return row;
  }

  // View functions accept data and callbacks; all editable copy/order lives in JSON.
  function onboardingCard(movie, {onSelect, disabled, onError}) {
    const config = window.YingbanContent.get("cards.onboarding");
    return movieCard(movie, {
      className: "onboarding-movie",
      posterAction: {
        className: "onboarding-movie-select",
        ariaLabel: window.YingbanContent.format(config.selectAria, {title: movie.title_zh}),
        content: element("span", "onboarding-movie-select-label", config.select),
        disabled, onError, action: onSelect,
      },
    });
  }

  function recommendationCard(movie, {actions, onFeedback, onError}) {
    const config = window.YingbanContent.get("cards.recommendation");
    const body = element("div", "movie-card-body");
    appendFields(body, config.fields, {
      genres: element("span", "movie-meta", (movie.genres || []).join(" · ")),
      reason: element("p", "movie-reason", movie.match_reason || movie.summary),
      notes: element("p", "content-note", movie.content_notes?.length
        ? window.YingbanContent.format(config.notes, {notes: movie.content_notes.slice(0, 2).join("、")}) : ""),
    });
    const reasons = element("div", "feedback-reasons");
    reasons.hidden = true;
    for (const item of config.feedbackReasons) {
      reasons.append(actionButton(item.label, async () => {
        await onFeedback(item.id);
        reasons.replaceChildren(element("span", "feedback-note", config.feedbackRecorded));
      }, {onError}));
    }
    body.append(configuredActions(config.actions, {
      ...actions, reasons: () => { reasons.hidden = !reasons.hidden; },
    }, onError, {}, "movie-actions"), reasons);
    return movieCard(movie, {className: "movie-card", body: [body]});
  }

  function formatBoxOfficeAmount(value) {
    const amount = Number(value || 0);
    if (amount >= 10000) return `${(amount / 10000).toFixed(2).replace(/\.00$/, "")} 亿`;
    return `${amount.toLocaleString("zh-CN", {maximumFractionDigits: 2})} 万`;
  }

  function boxOfficeCard(movie, {actions, onError}) {
    const config = window.YingbanContent.get("cards.home");
    const format = window.YingbanContent.format;
    const body = element("div");
    appendFields(body, config.fields, {
      rank: element("span", "box-office-rank", format(config.rank, {rank: movie.box_office?.rank || "—"})),
      title: element("h3", "", movie.title_zh),
      amount: element("p", "box-office-amount", format(config.amount, {amount: formatBoxOfficeAmount(movie.box_office?.day_box_office_wan)})),
      meta: element("p", "box-office-meta", format(config.meta, {
        sessions: Number(movie.box_office?.sessions || 0).toLocaleString("zh-CN"),
        audience: Number(movie.box_office?.audience || 0).toLocaleString("zh-CN"),
      })),
    });
    body.append(configuredActions(config.actions, actions, onError));
    return movieCard(movie, {className: "weekly-movie", body: [body]});
  }

  function historyCard(movie, {historyState, conversationCount, actions, onOpenReflection, onError}) {
    const config = window.YingbanContent.get("cards.history");
    const format = window.YingbanContent.format;
    const body = element("div", "history-item-body");
    appendFields(body, config.fields, {
      title: element("h3", "", movie.title_zh),
      meta: element("p", "", format(config.meta, {originalTitle: movie.title_original, genres: (movie.genres || []).join(" / ")})),
    });
    const items = historyState === "watchlist" ? config.watchlistActions
      : conversationCount ? config.conversationActions : config.watchedActions;
    body.append(configuredActions(items, actions, onError, {count: conversationCount}));
    return movieCard(movie, {
      className: "history-item", body: [body],
      posterAction: historyState === "watched" ? {
        className: "history-poster-button",
        ariaLabel: format(config.posterAria, {title: movie.title_zh}),
        content: movie.note ? element("span", "reflection-badge", config.noteBadge) : null,
        action: onOpenReflection, onError,
      } : null,
    });
  }

  function searchCard(movie, {historyState, onAdd, onError}) {
    const config = window.YingbanContent.get("cards.search");
    const copy = element("div");
    appendFields(copy, config.fields, {
      title: element("strong", "", movie.title_zh),
      meta: element("span", "", window.YingbanContent.format(config.meta, {year: movie.year, originalTitle: movie.title_original})),
    });
    const button = actionButton(historyState === "watched" ? config.watchedLabel : config.otherLabel, onAdd, {onError});
    return movieCard(movie, {className: "search-item", showPoster: false, body: [copy, button]});
  }

  function emptyState(message) {
    const node = document.createElement("p");
    node.className = "empty-state";
    node.textContent = message;
    return node;
  }

  function searchEmptyState(status) {
    return emptyState(window.YingbanContent.get(status === "unavailable" ? "search.unavailable" : "search.empty"));
  }

  window.YingbanMovieUI = { poster, movieCard, actionButton, emptyState, searchEmptyState, onboardingCard, recommendationCard, boxOfficeCard, historyCard, searchCard };
}());
