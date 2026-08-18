import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The API is proxied in development so the browser sees one origin and there is no CORS
// configuration to get wrong. The compose stack serves the built assets behind the same path.
export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.LEDGERLINE_API ?? "http://localhost:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
