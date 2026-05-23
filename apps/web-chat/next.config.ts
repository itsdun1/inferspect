import path from "node:path";
import type { NextConfig } from "next";

const monorepoRoot = path.join(__dirname, "../..");

const nextConfig: NextConfig = {
  output: "standalone",
  // Standalone bundles the workspace package (@ollive/web-shared) into the
  // output server, otherwise the symlinked source files don't ship. Both
  // turbopack.root and outputFileTracingRoot must match per Next 16 warning.
  outputFileTracingRoot: monorepoRoot,
  turbopack: {
    root: monorepoRoot,
  },
  transpilePackages: ["@ollive/web-shared"],
};

export default nextConfig;
