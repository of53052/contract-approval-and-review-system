import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 开发期把 /api 与 /health 代理到后端（127.0.0.1:8000），
// 前端代码里统一用相对路径，生产部署时换成同域反向代理即可。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/health": { target: "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
  // pdfjs-dist 的 worker 体积大，单独分包，避免主包过大
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          pdfjs: ["pdfjs-dist"],
          antd: ["antd", "@ant-design/icons"],
        },
      },
    },
  },
});
