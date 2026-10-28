import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The dev server proxies the API so the browser sees one origin and the
// session cookies keep their strict same-site policy without CORS.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: false,
      },
    },
  },
});
