import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

const pages = [
  {
    source: "index.html",
    output: "index.html",
    bodyClass: null,
    assets: ["/styles.css?v=32", "/ui-content.js?v=1", "/movie-components.js?v=2", "/app.js?v=44"],
  },
  {
    source: "admin.html",
    output: "admin.html",
    bodyClass: "admin-body",
    assets: ["/styles.css?v=32", "/admin.css?v=20", "/admin.js?v=26"],
  },
  {
    source: "share.html",
    output: "share.html",
    bodyClass: "share-page",
    assets: ["/styles.css?v=32", "/share-card.js?v=14", "/share.js?v=12"],
  },
];

const bodyPattern = /<body(?:\s+[^>]*)?>([\s\S]*?)<\/body>/i;
const scriptPattern = /<script\b[^>]*>[\s\S]*?<\/script>/gi;

for (const page of pages) {
  const source = readFileSync(resolve("..", "web", page.source), "utf8");
  const output = readFileSync(resolve("out", page.output), "utf8");
  const body = bodyPattern.exec(source)?.[1].replace(scriptPattern, "").trim();

  if (!body || !output.includes(body)) {
    throw new Error(`${page.output}: exported page changed the legacy body markup`);
  }
  if (!output.includes('data-framework-migration="legacy-markup"')) {
    throw new Error(`${page.output}: missing migration boundary`);
  }
  if (page.bodyClass && !output.includes(`<body class="${page.bodyClass}">`)) {
    throw new Error(`${page.output}: body class changed`);
  }
  for (const asset of [...page.assets, "/brand.js?v=2"]) {
    if (!output.includes(asset.replaceAll("&", "&amp;"))) {
      throw new Error(`${page.output}: missing versioned asset ${asset}`);
    }
  }
}

const share = readFileSync(resolve("out", "share.html"), "utf8");
if (!existsSync(resolve("..", "web", "brand", "yingban-seal-clean.svg"))) {
  throw new Error("Shared clean Yingban logo is missing");
}
if (!share.includes('content="noindex,nofollow,noarchive"')) {
  throw new Error("share.html: robots policy changed");
}
if (!share.includes('content="no-referrer"')) {
  throw new Error("share.html: referrer policy changed");
}

const landing = readFileSync(resolve("out", "landing.html"), "utf8");
for (const text of ["下一部电影，", "电影很多", "没想好怎么说。", "留下观后感笔记", "我的电影"]) {
  if (!landing.includes(text)) {
    throw new Error(`landing.html: missing product section ${text}`);
  }
}
if (landing.includes("一段会继续的关系。") || landing.includes("github.com/")) {
  throw new Error("landing.html: removed content must not be visible");
}
if (!landing.includes('href="/"')) {
  throw new Error("landing.html: missing product login entry");
}
for (const asset of ["hero-screen.jpg", "story-discover.jpg", "story-discuss.jpg", "story-remember.jpg", "yingban-home.jpg", "yingban-recommend.jpg", "yingban-chat.jpg", "yingban-my-movies.jpg"]) {
  if (!existsSync(resolve("out", "landing-assets", asset))) {
    throw new Error(`landing.html: missing local image ${asset}`);
  }
}

console.log("Framework export preserves three legacy pages and includes the Yingban landing page.");
