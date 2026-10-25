import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev:   `npm run dev` on :5173, every /api/* call is proxied to FastAPI on :8000
// Build: `npm run build` writes into ../backend/static so FastAPI serves the UI itself.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://localhost:8000',
    },
  },
  build: {
    outDir: '../backend/static',
    emptyOutDir: true,
  },
})
