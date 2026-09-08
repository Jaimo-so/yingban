import Script from "next/script";

import { LegacyMarkup } from "@/components/legacy-markup";
import { readLegacyBody } from "@/lib/legacy-template";

export default function SharePage() {
  return (
    <>
      <LegacyMarkup html={readLegacyBody("share.html")} />
      <Script src="/share-card.js?v=12" strategy="afterInteractive" />
      <Script src="/share.js?v=12" strategy="afterInteractive" />
    </>
  );
}
