import { defineConfig, Plugin } from "vite";
import react from "@vitejs/plugin-react";

function healthPlugin(): Plugin {
  return {
    name: "health-endpoint",
    configureServer(server) {
      server.middlewares.use("/health", (_req, res) => {
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify({ status: "ok" }));
      });
    },
    configurePreviewServer(server) {
      server.middlewares.use("/health", (_req, res) => {
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify({ status: "ok" }));
      });
    },
  };
}

export default defineConfig({
  plugins: [react(), healthPlugin()],
});
