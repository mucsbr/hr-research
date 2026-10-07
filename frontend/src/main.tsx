import '@ant-design/v5-patch-for-react-19'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { App as AntApp, ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import App from './App'
import './style.css'

const queryClient = new QueryClient({ defaultOptions: { queries: {
  retry: false, refetchOnWindowFocus: false, staleTime: 5000,
}}})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <ConfigProvider locale={zhCN} theme={{ token: {
        colorPrimary: '#bc5535', colorText: '#263333', colorBgLayout: '#f5f4f0',
        borderRadius: 9, fontFamily: '"Inter", -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif',
      } }}>
        <AntApp><BrowserRouter><App /></BrowserRouter></AntApp>
      </ConfigProvider>
    </QueryClientProvider>
  </StrictMode>,
)
