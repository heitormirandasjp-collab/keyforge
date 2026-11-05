import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Em dev, o backend roda em 127.0.0.1:8787 (use `python -m agent_core.app --port 8787`).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8787",
        changeOrigin: false,
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
  },
});
