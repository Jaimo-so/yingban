import type { Metadata } from "next";
import type { ReactNode } from "react";

export const metadata: Metadata = {
  title: "影伴 · 管理后台",
  robots: { index: false, follow: false },
};

export default function AdminLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-CN">
      <head>
        <link rel="icon" type="image/png" href="/brand/logo-screen-dialogue.png?v=1" />
        <link rel="stylesheet" href="/styles.css?v=26" />
        <link rel="stylesheet" href="/admin.css?v=19" />
      </head>
      <body className="admin-body">{children}</body>
    </html>
  );
}
