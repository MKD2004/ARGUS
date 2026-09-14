import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The backend runs separately (uvicorn on port 8000). In development, Vite
// forwards API calls and the progress WebSocket to it, so the browser only
// ever talks to one origin and the backend needs no CORS setup (DECISIONS.md D-047).
const backend = process.env.ARGUS_BACKEND_URL ?? 'http://localhost:8000'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': { target: backend, changeOrigin: true, rewrite: (path) => path.replace(/^\/api/, '') },
      '/ws': { target: backend.replace(/^http/, 'ws'), ws: true },
    },
  },
})
