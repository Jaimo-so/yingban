import Script from "next/script";

/** Load the same brand module used by the standalone HTML pages. */
export function BrandAssets() {
  return <Script src="/brand.js?v=1" strategy="beforeInteractive" />;
}
