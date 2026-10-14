import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

/** Vite 配置：dev server 代理后端（http://localhost:8000），避免开发期跨域。 */
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) }
  },
  server: {
    // 显式绑定 IPv4：Windows 下 localhost 常解析到 ::1，绑定 127.0.0.1 更可控，
    // 也与浏览器端 AK 的 Referer 白名单项（127.0.0.1 / localhost）一致
    host: '127.0.0.1',
    port: 5173,
    strictPort: false,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true
      }
    },
    watch: {
      // 自测产物（无头 Chrome 的 profile 会频繁写文件）不要触发 HMR
      ignored: ['**/.smoke/**', '**/.tmp/**', '**/dist/**']
    }
  },
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 1600,
    rollupOptions: {
      output: {
        manualChunks: {
          echarts: ['echarts'],
          vendor: ['vue', 'pinia']
        }
      }
    }
  }
})
