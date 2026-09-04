import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // Makes the dev server and the local backend look same-origin to the
    // browser, so the auth session cookie works locally without needing
    // HTTPS - see SETUP.md. Only used when VITE_API_BASE is unset (local
    // dev); production talks to the real cross-origin backend URL instead.
    proxy: {
      '/api': 'http://localhost:8001',
      '/auth': 'http://localhost:8001',
    },
  },
})
