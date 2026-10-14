/** 极简事件总线：解耦「报告面板 → 地图」的指令（flyTo / 高亮 / 重新定位）。 */
export type MapCommand =
  | { type: 'focus-blindspot'; index: number; lat: number; lng: number }
  | { type: 'focus-poi'; lat: number; lng: number; zoom?: number }
  | { type: 'fly-to'; lat: number; lng: number; zoom?: number }
  | { type: 'reset-view' }
  | { type: 'clear-highlight' }

type Handler = (cmd: MapCommand) => void

const handlers = new Set<Handler>()

export const mapBus = {
  on(h: Handler) {
    handlers.add(h)
    return () => handlers.delete(h)
  },
  emit(cmd: MapCommand) {
    handlers.forEach((h) => h(cmd))
  }
}
