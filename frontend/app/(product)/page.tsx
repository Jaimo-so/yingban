import Script from "next/script";

import { LegacyMarkup } from "@/components/legacy-markup";
import { readLegacyBody } from "@/lib/legacy-template";

export default function ProductPage() {
  return (
    <>
      <LegacyMarkup html={readLegacyBody("index.html")} />
      <Script src="/app.js?v=29" strategy="afterInteractive" />
    </>
  );
}
