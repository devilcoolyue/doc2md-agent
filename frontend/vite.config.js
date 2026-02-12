import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  server: {
    host: "0.0.0.0",
    port: 10086,
    proxy: {
      "/api": {
        target: "http://localhost:9999",
        changeOrigin: true
      }
    }
  },
  preview: {
    host: "0.0.0.0",
    port: 10086
  }
});
