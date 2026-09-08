import Script from "next/script";

import { LegacyMarkup } from "@/components/legacy-markup";
import { readLegacyBody } from "@/lib/legacy-template";

export default function AdminPage() {
  return (
    <>
      <LegacyMarkup html={readLegacyBody("admin.html")} />
      <Script src="/admin.js?v=20" strategy="afterInteractive" />
    </>
  );
}
