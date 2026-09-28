import Script from "next/script";
import type { Metadata } from "next";
import type { ReactNode } from "react";

import { BrandAssets } from "@/components/brand-assets";

export const metadata: Metadata = {
  title: "影伴 · 你的电影搭子",
  description: "影伴——记得你看过什么的 AI 电影伙伴。",
};

export default function ProductLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-CN">
      <head>
        <meta name="referrer" content="no-referrer" />
        <BrandAssets />
        <Script src="/ui-content.js?v=1" strategy="beforeInteractive" />
        <Script src="/movie-components.js?v=2" strategy="beforeInteractive" />
        <script
          dangerouslySetInnerHTML={{
            __html:
              'if(window.location.search){const params=new URLSearchParams(window.location.search);if([...params.keys()].some(key=>["username","password","confirm_password","invite_code"].includes(key.toLowerCase()))){window.history.replaceState(null,"",window.location.pathname+window.location.hash)}}if(window.location.protocol==="file:"){window.location.replace("http://127.0.0.1:8765/")}',
          }}
        />
        <link rel="stylesheet" href="/styles.css?v=31" />
      </head>
      <body>{children}</body>
    </html>
  );
}
