import type { Metadata } from "next";
import type { ReactNode } from "react";

export const metadata: Metadata = {
  title: "影伴｜让电影关系继续发生",
  description:
    "影伴是记得你电影经验的 AI 电影伙伴。聊聊看完的电影，找到下一部，留下由你决定的观影轨迹。",
  icons: { icon: "/brand/yingban-seal-clean.svg?v=2" },
};

export default function LandingLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-CN">
      <head>
        <meta name="referrer" content="no-referrer" />
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
        <link
          href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Noto+Sans+SC:wght@400;500;600;700&family=Noto+Serif+SC:wght@400;500;600&display=swap"
          rel="stylesheet"
        />
      </head>
      <body style={{ margin: 0 }}>{children}</body>
    </html>
  );
}
