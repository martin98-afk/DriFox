import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Tauri 前端构建配置：固定端口供 tauri dev 注入 devUrl，禁用浏览器自动打开
export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  server: {
    port: 5173,
    strictPort: true,
  },
  build: {
    target: "es2020",
  },
  envPrefix: ["VITE_", "TAURI_"],
});
