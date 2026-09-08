import type { Metadata } from "next";
import type { ReactNode } from "react";

export const metadata: Metadata = {
  title: "影伴 · 一张电影分享",
  robots: "noindex,nofollow,noarchive",
  referrer: "no-referrer",
};

export default function ShareLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-CN">
      <head>
        <link rel="stylesheet" href="/styles.css?v=15" />
      </head>
      <body className="share-page">{children}</body>
    </html>
  );
}
