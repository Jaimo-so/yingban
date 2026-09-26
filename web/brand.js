/* Shared brand icon for legacy HTML, dynamic chat, and the Next.js shell. */
(function () {
  "use strict";

  // Replace the image here to update every brand icon, avatar, and favicon.
  const iconUrl = "/brand/yingban-seal.png?v=1";
  const tagName = "yingban-icon";

  class YingbanIcon extends HTMLElement {
    constructor() {
      super();
      const root = this.attachShadow({ mode: "open" });
      const style = document.createElement("style");
      // Display only the seal area of the supplied 368 x 338 image. The source
      // pixels are unchanged; the small screenshot margin is clipped in CSS.
      style.textContent = `
        :host {
          display: inline-block;
          width: var(--yingban-icon-size, 40px);
          height: var(--yingban-icon-size, 40px);
          flex: 0 0 auto;
          vertical-align: middle;
        }
        .frame {
          position: relative;
          width: 100%;
          height: 100%;
          overflow: hidden;
          border-radius: 12%;
        }
        img {
          position: absolute;
          top: 0;
          left: -4.79042%;
          width: 110.17964%;
          height: 101.19760%;
          max-width: none;
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
  favicon.type = "image/png";
  favicon.href = iconUrl;
  favicon.dataset.yingbanBrand = "";
  document.head.append(favicon);
}());
