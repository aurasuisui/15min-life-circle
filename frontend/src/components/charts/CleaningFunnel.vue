<script setup lang="ts">
import { computed } from 'vue'
import { useEchart } from '@/composables/useEchart'
import type { EChartsOption } from '@/charts/echarts'
import type { CleaningFunnel } from '@/types/report'

const props = defineProps<{ funnel: CleaningFunnel }>()

const STAGES = [
  { key: 'raw', name: '检索原始', color: '#5aa9ff', desc: '2 组多关键字周边检索召回' },
  { key: 'deduped', name: '去重', color: '#4ee0ff', desc: 'uid + 名称/坐标近邻去重' },
  { key: 'corrected', name: '类型纠错', color: '#31d0a5', desc: '分类表复核 + 坐标漂移校正' },
  { key: 'inside', name: '圈内有效', color: '#ffb84d', desc: '等时圈内 point-in-polygon 裁剪' }
] as const

const steps = computed(() =>
  STAGES.map((s, i) => {
    const value = props.funnel[s.key]
    const prev = i === 0 ? value : props.funnel[STAGES[i - 1].key]
    return {
      ...s,
      value,
      ratio: props.funnel.raw ? value / props.funnel.raw : 0,
      drop: i === 0 ? 0 : prev - value,
      dropRatio: i === 0 || !prev ? 0 : (prev - value) / prev
    }
  })
)

const option = computed<EChartsOption>(() => ({
  tooltip: {
    trigger: 'item',
    backgroundColor: 'rgba(9,15,27,0.94)',
    borderColor: 'rgba(255,255,255,0.14)',
    textStyle: { color: '#e9f1ff', fontSize: 11.5 },
    formatter: (p: any) => p.name + '：<b>' + p.value + '</b> 条（占原始 ' + ((p.value / props.funnel.raw) * 100).toFixed(1) + '%）'
  },
  series: [
    {
      type: 'funnel',
      left: '2%',
      right: '2%',
      top: 6,
      bottom: 6,
      minSize: '38%',
      maxSize: '100%',
      sort: 'descending',
      gap: 3,
      label: {
        show: true,
        position: 'inside',
        color: '#06131f',
        fontSize: 11,
        fontWeight: 650,
        formatter: (p: any) => p.name + ' ' + p.value
      },
      labelLine: { show: false },
      itemStyle: { borderWidth: 0 },
      emphasis: { label: { fontSize: 12 } },
      data: steps.value.map((s) => ({
        name: s.name,
        value: s.value,
        itemStyle: { color: s.color, opacity: 0.9 }
      }))
    }
  ]
}))

const { el } = useEchart(option as any)
</script>

<template>
  <section class="card">
    <div class="card-head">
      <h3>多源 POI 清洗漏斗</h3>
      <span class="hint">检索 → 去重 → 纠错 → 圈内</span>
    </div>
    <div class="card-body" style="padding-bottom: 6px">
      <div style="height: 190px"><div ref="el" class="chart" /></div>
      <div class="funnel-steps" style="margin-top: 10px">
        <div v-for="s in steps" :key="s.key" class="fstep">
          <span style="color: var(--muted)">{{ s.name }}</span>
          <span class="bar"><i :style="{ width: (s.ratio * 100).toFixed(1) + '%', background: s.color }" /></span>
          <span class="n">{{ s.value }}</span>
          <span v-if="s.drop > 0" class="drop" style="grid-column: 2 / 4; text-align: right; margin-top: -3px">
            −{{ s.drop }}（{{ (s.dropRatio * 100).toFixed(1) }}%）
          </span>
        </div>
      </div>
    </div>
  </section>
</template>
