(function () {
  "use strict";

  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  };

  const appendText = (parent, tag, className, text) => {
    if (text === undefined || text === null || text === "") return null;
    const node = el(tag, className, text);
    parent.append(node);
    return node;
  };

  function renderPoster(movie, index) {
    const frame = el("div", "share-film-poster");
    const fallback = el("div", "share-film-poster-fallback");
    appendText(fallback, "span", "share-film-number", String(index + 1).padStart(2, "0"));
    appendText(fallback, "strong", "", movie.title || "电影");
    appendText(fallback, "small", "", movie.year || "影伴片单");
    frame.append(fallback);
    if (movie.poster_url) {
      const image = el("img", "");
      image.src = movie.poster_url;
      image.alt = `《${movie.title || "电影"}》封面`;
      image.loading = "eager";
      image.decoding = "async";
      image.referrerPolicy = "no-referrer";
      image.addEventListener("error", () => image.remove(), { once: true });
      frame.append(image);
    }
    return frame;
  }

  function renderMovie(movie, index) {
    const item = el("li", "share-film-item");
    item.append(renderPoster(movie, index));
    const copy = el("div", "share-film-copy");
    appendText(copy, "strong", "share-film-title", movie.title || "未命名电影");
    const details = [movie.year, ...(Array.isArray(movie.genres) ? movie.genres : [])].filter(Boolean);
    appendText(copy, "span", "share-film-meta", details.join(" · ") || "本月看过");
    item.append(copy);
    return item;
  }

  function renderMonthly(content, root) {
    const visual = content.visual || {};
    root.className = "share-artifact share-artifact-monthly";
    root.replaceChildren();

    const hero = el("header", "share-artifact-hero");
    const heroTop = el("div", "share-artifact-hero-top");
    appendText(heroTop, "p", "share-artifact-kicker", "MY MONTH IN FILMS");
    appendText(heroTop, "p", "share-artifact-month", visual.month_label || visual.month || "月度电影回顾");
    hero.append(heroTop);
    appendText(hero, "h1", "", visual.story_title || content.title);
    appendText(hero, "p", "share-artifact-story", visual.story || content.text);

    const stats = el("div", "share-artifact-stats");
    const movieStat = el("div", "");
    appendText(movieStat, "strong", "", visual.movie_count ?? (visual.movies || []).length);
    appendText(movieStat, "span", "", "部电影");
    const reflectionStat = el("div", "");
    appendText(reflectionStat, "strong", "", visual.reflection_count ?? 0);
    appendText(reflectionStat, "span", "", "篇观后感");
    stats.append(movieStat, reflectionStat);
    hero.append(stats);
    root.append(hero);

    const films = el("section", "share-artifact-section share-film-section");
    const heading = el("div", "share-section-heading");
    const headingCopy = el("div", "");
    appendText(headingCopy, "p", "share-section-index", "01 / WATCHED");
    appendText(headingCopy, "h2", "", "本月片单");
    appendText(heading, "p", "share-section-note", "每一张封面，都是这个月留下的一帧。");
    heading.prepend(headingCopy);
    films.append(heading);
    const grid = el("ol", "share-film-grid");
    const movies = Array.isArray(visual.movies) ? visual.movies : [];
    movies.forEach((movie, index) => grid.append(renderMovie(movie, index)));
    if (!movies.length) appendText(grid, "li", "share-film-empty", "这个月的片单仍在等待第一部电影。");
    films.append(grid);
    root.append(films);

    const afterword = el("section", "share-artifact-section share-afterword");
    const themeBlock = el("div", "share-theme-block");
    appendText(themeBlock, "p", "share-section-index", "02 / AFTERGLOW");
    appendText(themeBlock, "h2", "", "散场之后，留下这些关键词");
    const themes = el("div", "share-theme-list");
    const themeValues = Array.isArray(visual.themes) ? visual.themes : [];
    (themeValues.length ? themeValues : ["光影仍在继续"]).forEach((theme) => appendText(themes, "span", "", theme));
    themeBlock.append(themes);
    afterword.append(themeBlock);

    if (visual.next_direction) {
      const direction = el("blockquote", "share-next-direction");
      appendText(direction, "p", "share-section-index", "NEXT SCENE");
      appendText(direction, "strong", "", visual.next_direction);
      afterword.append(direction);
    }
    root.append(afterword);

    const footer = el("footer", "share-artifact-footer");
    const brand = el("div", "share-artifact-brand");
    appendText(brand, "span", "share-artifact-brand-mark", "映");
    appendText(brand, "strong", "", "影伴");
    appendText(footer, "p", "", content.attribution || "由影伴 AI 协助整理");
    footer.prepend(brand);
    root.append(footer);
  }

  function renderClassic(content, root) {
    root.className = "share-artifact share-artifact-classic";
    root.replaceChildren();
    appendText(root, "p", "share-artifact-kicker", "A FILM MEMORY");
    appendText(root, "h1", "", content.title || "一张电影分享");
    appendText(root, "p", "share-classic-text", content.text || "");
    appendText(root, "footer", "", content.attribution || "由影伴 AI 协助整理");
  }

  window.YingbanShareCard = {
    render(card, root) {
      const content = card?.content || {};
      if (content.visual?.variant === "monthly_recap") renderMonthly(content, root);
      else renderClassic(content, root);
    },
  };
}());
