import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      // /api/... 로 부르면 Vite 가 8005 로 넘긴다.
      // 브라우저는 5173 만 보므로 CORS 가 생기지 않는다.
      '/api': {
        target: 'http://127.0.0.1:8005',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
})
