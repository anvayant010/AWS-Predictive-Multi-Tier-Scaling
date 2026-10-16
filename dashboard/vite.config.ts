import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// base "./" keeps the built site relocatable (GitHub Pages sub-path, a file server, or `vite preview`).
export default defineConfig({
  base: "./",
  plugins: [react()],
  test: { environment: "node", include: ["src/**/*.test.ts"] },
});
