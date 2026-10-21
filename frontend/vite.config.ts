import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// 前端开发代理与离线测试配置；API 和 SSE 均转给本机 Python 服务。
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, strictPort: true, proxy: { '/api': 'http://127.0.0.1:8765' } },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    css: false,
    restoreMocks: true,
  },
});
