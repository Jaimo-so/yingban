/* Shared brand icon for legacy HTML, dynamic chat, and the Next.js shell. */
(function () {
  "use strict";

  // The presentation asset clips the screenshot background around the original seal.
  const iconUrl = "/brand/yingban-seal-clean.svg?v=2";
  const tagName = "yingban-icon";

  class YingbanIcon extends HTMLElement {
    constructor() {
      super();
      const root = this.attachShadow({ mode: "open" });
      const style = document.createElement("style");
      style.textContent = `
        :host {
          display: inline-block;
          width: var(--yingban-icon-size, 40px);
          height: var(--yingban-icon-size, 40px);
          flex: 0 0 auto;
          vertical-align: middle;
        }
        .frame {
          width: 100%;
          height: 100%;
        }
        img {
          width: 100%;
          height: 100%;
          display: block;
        }
      `;
      const frame = document.createElement("span");
      frame.className = "frame";
      frame.style.display = "block";
      const image = document.createElement("img");
      image.src = iconUrl;
      image.alt = "";
      image.draggable = false;
      frame.append(image);
      root.append(style, frame);
    }
  }

  if (!customElements.get(tagName)) customElements.define(tagName, YingbanIcon);

  // The favicon uses the same source as the component, without a second config.
  const favicon = document.createElement("link");
  favicon.rel = "icon";
  favicon.type = "image/svg+xml";
  favicon.href = iconUrl;
  favicon.dataset.yingbanBrand = "";
  document.head.append(favicon);
}());
