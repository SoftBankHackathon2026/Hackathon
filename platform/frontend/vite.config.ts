import { defineConfig } from "vite";
export default defineConfig({
  server: {
    proxy: {
      "/api": process.env.ONEPORT_API_TARGET ?? "http://127.0.0.1:8320",
      "/docs": process.env.ONEPORT_API_TARGET ?? "http://127.0.0.1:8320",
      "/openapi.json":
        process.env.ONEPORT_API_TARGET ?? "http://127.0.0.1:8320",
      "/healthz": process.env.ONEPORT_API_TARGET ?? "http://127.0.0.1:8320",
    },
  },
});
