import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { App } from './App';
import { ConfigDraftProvider } from './config';
import './styles.css';

// 查询缓存只保存无密钥的任务与能力快照；明确禁用业务错误的自动重试。
const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: false, refetchOnWindowFocus: true },
    mutations: { retry: false },
  },
});

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ConfigProvider
      locale={zhCN}
      theme={{
        token: {
          colorPrimary: '#c6653d',
          colorInfo: '#c6653d',
          colorSuccess: '#3e7759',
          colorWarning: '#a67529',
          colorError: '#b7473f',
          colorText: '#292d29',
          colorTextSecondary: '#777d75',
          colorBgContainer: '#ffffff',
          colorBorder: '#dedfd8',
          borderRadius: 5,
          fontFamily: 'Inter, "Segoe UI", "Microsoft YaHei", sans-serif',
          controlHeight: 36,
        },
        components: {
          Button: { primaryShadow: 'none' },
          Tabs: { horizontalItemGutter: 28 },
          Tag: { defaultBg: '#eeefe9' },
        },
      }}
    >
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <ConfigDraftProvider>
            <App />
          </ConfigDraftProvider>
        </BrowserRouter>
      </QueryClientProvider>
    </ConfigProvider>
  </StrictMode>,
);
