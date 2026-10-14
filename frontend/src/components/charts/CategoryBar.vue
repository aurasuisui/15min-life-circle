<script setup lang="ts">
import { computed } from 'vue'
import { useEchart } from '@/composables/useEchart'
import { chartTheme, type EChartsOption } from '@/charts/echarts'
import { CATEGORY_COLORS, type CategoryKey, type FacilityCategory } from '@/types/report'
import { useAuditStore, type BarMode } from '@/stores/audit'

const props = defineProps<{ categories: FacilityCategory[]; mode: BarMode }>()
const store = useAuditStore()

const MODES: { key: BarMode; label: string }[] = [
  { key: 'count', label: '数量' },
  { key: 'density', label: '密度' },
  { key: 'walk', label: '最近步行' }
]

const unit = computed(() => (props.mode === 'count' ? '个' : props.mode === 'density' ? '个/km²' : '分钟'))

const values = computed(() =>
  props.categories.map((c) => {
    if (props.mode === 'count') return c.count
    if (props.mode === 'density') return Number(c.density_per_km2.toFixed(2))
    return Number((c.nearest_walk_seconds / 60).toFixed(1))
  })
)

const option = computed<EChartsOption>(() => ({
  grid: { left: 8, right: 14, top: 22, bottom: 6, containLabel: true },
  tooltip: {
    trigger: 'axis',
    axisPointer: { type: 'shadow', shadowStyle: { color: 'rgba(255,255,255,0.05)' } },
    backgroundColor: 'rgba(9,15,27,0.94)',
    borderColor: 'rgba(255,255,255,0.14)',
    textStyle: { color: '#e9f1ff', fontSize: 11.5 },
    valueFormatter: (v: unknown) => String(v) + ' ' + unit.value
  },
  xAxis: {
    type: 'category',
    data: props.categories.map((c) => c.name),
    axisLine: { lineStyle: { color: chartTheme.axis } },
    axisTick: { show: false },
    axisLabel: { color: chartTheme.muted, fontSize: 10, interval: 0, rotate: 30 }
  },
  yAxis: {
    type: 'value',
    name: unit.value,
    nameTextStyle: { color: chartTheme.muted, fontSize: 10, padding: [0, 0, 0, -18] },
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { color: chartTheme.muted, fontSize: 10 },
    splitLine: { lineStyle: { color: chartTheme.line } }
  },
  series: [
    {
      type: 'bar',
      barMaxWidth: 26,
      data: props.categories.map((c, i) => ({
        value: values.value[i],
        itemStyle: {
          borderRadius: [5, 5, 0, 0],
          color: CATEGORY_COLORS[c.key as CategoryKey] ?? '#5aa9ff',
          opacity: props.mode === 'walk' ? 0.82 : 0.95
        }
      })),
      label: {
        show: true,
        position: 'top',
        color: chartTheme.text,
        fontSize: 10,
        fontFamily: chartTheme.font
      },
      markLine:
        props.mode === 'walk'
          ? {
              silent: true,
              symbol: 'none',
              lineStyle: { color: 'rgba(255,184,77,0.7)', type: 'dashed', width: 1.2 },
              label: { formatter: '15 分钟阈值', color: 'rgba(255,184,77,0.9)', fontSize: 10, position: 'insideEndTop' },
              data: [{ yAxis: 15 }]
            }
          : undefined
    }
  ]
}))

const { el } = useEchart(option as any)
</script>

<template>
  <section class="card">
    <div class="card-head">
      <h3>各类设施覆盖</h3>
      <div class="seg">
        <button
          v-for="m in MODES"
          :key="m.key"
          :class="{ on: store.barMode === m.key }"
          @click="store.barMode = m.key"
        >
          {{ m.label }}
        </button>
      </div>
    </div>
    <div class="card-body" style="height: 250px">
      <div ref="el" class="chart" />
    </div>
  </section>
</template>
