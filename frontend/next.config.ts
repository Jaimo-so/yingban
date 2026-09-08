import type { NextConfig } from "next";

const legacyAssets = [
  "styles.css",
  "admin.css",
  "app.js",
  "admin.js",
  "share.js",
  "share-card.js",
];

const developmentProxy: Partial<NextConfig> =
  process.env.NODE_ENV === "development"
    ? {
        async rewrites() {
          return [
            {
              source: "/api/:path*",
              destination: "http://127.0.0.1:8765/api/:path*",
            },
            ...legacyAssets.map((asset) => ({
              source: `/${asset}`,
              destination: `http://127.0.0.1:8765/${asset}`,
            })),
          ];
        },
      }
    : {};

const nextConfig: NextConfig = {
  output: "export",
  trailingSlash: false,
  images: { unoptimized: true },
  ...developmentProxy,
};

export default nextConfig;
