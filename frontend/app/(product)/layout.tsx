import type { Metadata } from "next";
import type { ReactNode } from "react";

export const metadata: Metadata = {
  title: "影伴 · 你的电影搭子",
  description: "影伴——记得你看过什么的 AI 电影伙伴。",
};

export default function ProductLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-CN">
      <head>
        <script
          dangerouslySetInnerHTML={{
            __html:
              'if(window.location.protocol==="file:"){window.location.replace("http://127.0.0.1:8765/")}',
          }}
        />
        <link rel="stylesheet" href="/styles.css?v=20" />
      </head>
      <body>{children}</body>
    </html>
  );
}
