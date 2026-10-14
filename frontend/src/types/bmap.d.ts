/**
 * 百度地图 JSAPI 的最小类型声明。
 * 载入的是 JSAPI 4.0（type=webgl&v=4.0）；该版本同时挂载 window.BMapGL，
 * 因此全项目统一使用 BMapGL.* 命名空间。为避免与官方 @types 冲突，这里只声明用到的成员。
 */
export interface BMapGLPoint {
  lng: number
  lat: number
}
export interface BMapGLSize {
  width: number
  height: number
}
export interface BMapGLMap {
  centerAndZoom(point: unknown, zoom: number): void
  setCenter(point: unknown): void
  panTo(point: unknown): void
  flyTo?(point: unknown, zoom?: number): void
  getZoom(): number
  getCenter(): BMapGLPoint
  getSize(): BMapGLSize
  getContainer(): HTMLElement
  pointToPixel?(point: unknown): { x: number; y: number }
  pointToOverlayPixel?(point: unknown): { x: number; y: number }
  addOverlay(overlay: unknown): void
  removeOverlay(overlay: unknown): void
  clearOverlays(): void
  addEventListener(event: string, handler: (e: never) => void): void
  removeEventListener(event: string, handler: (e: never) => void): void
  enableScrollWheelZoom(enable?: boolean): void
  [key: string]: unknown
}

declare global {
  interface Window {
    BMapGL?: Record<string, never>
    _BMapSecurityConfig?: { serviceHost: string }
  }
}

export {}
