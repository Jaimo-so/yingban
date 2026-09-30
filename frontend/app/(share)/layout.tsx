import type { Metadata } from "next";
import type { ReactNode } from "react";

import { BrandAssets } from "@/components/brand-assets";

export const metadata: Metadata = {
  title: "影伴 · 一张电影分享",
  robots: "noindex,nofollow,noarchive",
  referrer: "no-referrer",
  icons: { icon: "/brand/yingban-seal-clean.svg?v=2" },
};

export default function ShareLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-CN">
      <head>
        <BrandAssets />
        <link rel="stylesheet" href="/styles.css?v=34" />
      </head>
      <body className="share-page">{children}</body>
    </html>
  );
}
