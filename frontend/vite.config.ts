import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Dev proxy: `npm run dev` (vite :5173) forwards API + WS to the backend
// started with `entropy-arb web --port 8000` in another terminal.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8000',
      '/ws': { target: 'ws://127.0.0.1:8000', ws: true },
    },
  },
})
