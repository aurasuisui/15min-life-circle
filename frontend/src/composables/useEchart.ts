import { onBeforeUnmount, onMounted, ref, watch, type Ref } from 'vue'
import { echarts, type EChartsOption } from '@/charts/echarts'

/**
 * 把一个响应式的 ECharts option 绑定到 DOM 上，自动处理初始化 / 更新 / 尺寸变化 / 销毁。
 */
export function useEchart(option: Ref<EChartsOption>) {
  const el = ref<HTMLDivElement | null>(null)
  let chart: echarts.ECharts | null = null
  let ro: ResizeObserver | null = null

  const render = () => {
    if (!chart) return
    chart.setOption(option.value, true)
  }

  onMounted(() => {
    if (!el.value) return
    chart = echarts.init(el.value, undefined, { renderer: 'canvas' })
    render()
    ro = new ResizeObserver(() => chart?.resize())
    ro.observe(el.value)
  })

  watch(option, render, { deep: true })

  onBeforeUnmount(() => {
    ro?.disconnect()
    chart?.dispose()
    chart = null
  })

  return { el }
}
