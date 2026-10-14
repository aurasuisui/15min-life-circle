/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 百度地图浏览器端 AK（仅浏览器端 AK 可用，需配置 Referer 白名单） */
  readonly VITE_BAIDU_MAP_BROWSER_AK?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<{}, {}, any>
  export default component
}
