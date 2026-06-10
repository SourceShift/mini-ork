import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

// Build output goes straight into the FastAPI app's static dir so
// `mini-ork serve` serves the SPA without extra steps.
const STATIC_OUT = path.resolve(__dirname, "../mini_ork/web/static");

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  build: {
    outDir: STATIC_OUT,
    emptyOutDir: true,
    sourcemap: true,
  },
  server: {
    port: 7070,
    strictPort: false,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:7090",
        changeOrigin: true,
      },
    },
  },
});
