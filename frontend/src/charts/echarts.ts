/**
 * ECharts 按需引入（只注册用到的图表与组件，显著减小打包体积）。
 */
import * as echarts from 'echarts/core'
import { BarChart, FunnelChart, GaugeChart, RadarChart } from 'echarts/charts'
import {
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { ComposeOption } from 'echarts/core'
import type {
  BarSeriesOption,
  FunnelSeriesOption,
  GaugeSeriesOption,
  RadarSeriesOption
} from 'echarts/charts'
import type {
  GridComponentOption,
  LegendComponentOption,
  TitleComponentOption,
  TooltipComponentOption
} from 'echarts/components'

echarts.use([
  RadarChart,
  BarChart,
  FunnelChart,
  GaugeChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  TitleComponent,
  CanvasRenderer
])

export type EChartsOption = ComposeOption<
  | RadarSeriesOption
  | BarSeriesOption
  | FunnelSeriesOption
  | GaugeSeriesOption
  | GridComponentOption
  | TooltipComponentOption
  | LegendComponentOption
  | TitleComponentOption
>

/** 深色主题下的图表公共样式 */
export const chartTheme = {
  text: '#c7d6ef',
  muted: '#8ea3c4',
  line: 'rgba(255,255,255,0.08)',
  axis: 'rgba(255,255,255,0.16)',
  font: 'system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif'
}

export { echarts }
