import type { Metadata } from "next";
import type { ReactNode } from "react";

import { BrandAssets } from "@/components/brand-assets";

export const metadata: Metadata = {
  title: "影伴 · 管理后台",
  robots: { index: false, follow: false },
  icons: { icon: "/brand/yingban-seal-clean.svg?v=2" },
};

export default function AdminLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-CN">
      <head>
        <BrandAssets />
        <link rel="stylesheet" href="/styles.css?v=32" />
        <link rel="stylesheet" href="/admin.css?v=20" />
      </head>
      <body className="admin-body">{children}</body>
    </html>
  );
}
