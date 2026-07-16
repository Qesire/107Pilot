import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const currentDir = dirname(fileURLToPath(import.meta.url));
const projectRoot = resolve(currentDir, "../..");

export default defineConfig({
  root: currentDir,
  plugins: [react()],
  build: {
    outDir: resolve(projectRoot, "src/pilot107/web/static"),
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: {
      output: {
        entryFileNames: "assets/app.js",
        chunkFileNames: "assets/chunk-[hash].js",
        assetFileNames: (assetInfo) =>
          assetInfo.name?.endsWith(".css")
            ? "assets/styles.css"
            : "assets/[name]-[hash][extname]",
      },
    },
  },
  server: {
    host: "127.0.0.1",
    port: 3197,
    proxy: {
      "/api": "http://127.0.0.1:8070",
    },
  },
  preview: {
    host: "127.0.0.1",
    port: 3197,
  },
});
