import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// 前端开发代理与离线测试配置；API 和 SSE 均转给本机 Python 服务。
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, strictPort: true, proxy: { '/api': 'http://127.0.0.1:8765' } },
  test: {
    // 完整工作台包含多层抽屉和路由；串行运行避免 Windows 上布局计算争用造成误超时。
    maxWorkers: 1,
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    css: false,
    restoreMocks: true,
  },
});
