import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const pages = [
  {
    source: "index.html",
    output: "index.html",
    bodyClass: null,
    assets: ["/styles.css?v=20", "/app.js?v=29"],
  },
  {
    source: "admin.html",
    output: "admin.html",
    bodyClass: "admin-body",
    assets: ["/styles.css?v=10", "/admin.css?v=16", "/admin.js?v=20"],
  },
  {
    source: "share.html",
    output: "share.html",
    bodyClass: "share-page",
    assets: ["/styles.css?v=15", "/share-card.js?v=12", "/share.js?v=12"],
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
  for (const asset of page.assets) {
    if (!output.includes(asset.replaceAll("&", "&amp;"))) {
      throw new Error(`${page.output}: missing versioned asset ${asset}`);
    }
  }
}

const share = readFileSync(resolve("out", "share.html"), "utf8");
if (!share.includes('content="noindex,nofollow,noarchive"')) {
  throw new Error("share.html: robots policy changed");
}
if (!share.includes('content="no-referrer"')) {
  throw new Error("share.html: referrer policy changed");
}

console.log("Framework export preserves all three legacy page bodies and public asset contracts.");
