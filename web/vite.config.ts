import path from "node:path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

// Built files go into the Python package (board/static) so `pip install .` needs no Node.
// vendor/ and icons/ already live there, so don't empty the folder.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: "/static/",
  resolve: { alias: { "@": path.resolve(__dirname, "./src") } },
  build: { outDir: "../board/static", emptyOutDir: false, assetsDir: "assets" },
  server: { proxy: { "/api": "http://127.0.0.1:8000" } },
})
