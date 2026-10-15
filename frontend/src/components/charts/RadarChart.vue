<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useEchart } from '@/composables/useEchart'
import { chartTheme, type EChartsOption } from '@/charts/echarts'
import type { ScoreDimension } from '@/types/report'

const props = defineProps<{ dimensions: ScoreDimension[] }>()

/**
 * 达标基准线。
 * 注意量纲：后端 app/services/score.py 的维度分是【百分制】
 *   score_c = 100 · (0.45·access + 0.35·present + 0.20·density)
 * 所以基准线是 75（≈ 原「7.5/10」的同一含义），坐标轴满分是 100。
 * 历史 bug：这里曾写 BENCHMARK = 7.5 且 indicator max = 10，
 * 导致所有非零维度都被顶到最外圈，雷达图完全失真。
 */
const BENCHMARK = 75

const option = computed<EChartsOption>(() => ({
  tooltip: { trigger: 'item', backgroundColor: 'rgba(9,15,27,0.94)', borderColor: 'rgba(255,255,255,0.14)', textStyle: { color: '#e9f1ff', fontSize: 11.5 } },
  legend: {
    bottom: 0,
    itemWidth: 9,
    itemHeight: 9,
    textStyle: { color: chartTheme.muted, fontSize: 10.5 },
    data: ['现状得分', '达标基准']
  },
  radar: {
    center: ['50%', '47%'],
    radius: '64%',
    splitNumber: 5,
    axisName: { color: chartTheme.muted, fontSize: 10.5 },
    indicator: props.dimensions.map((d) => ({ name: d.name, max: 100 })),
    splitLine: { lineStyle: { color: 'rgba(255,255,255,0.09)' } },
    axisLine: { lineStyle: { color: 'rgba(255,255,255,0.09)' } },
    splitArea: { areaStyle: { color: ['rgba(255,255,255,0.014)', 'rgba(255,255,255,0.035)'] } }
  },
  series: [
    {
      type: 'radar',
      symbolSize: 4,
      data: [
        {
          value: props.dimensions.map((d) => d.score),
          name: '现状得分',
          lineStyle: { color: '#31d0a5', width: 2 },
          itemStyle: { color: '#31d0a5' },
          areaStyle: { color: 'rgba(49,208,165,0.24)' }
        },
        {
          value: props.dimensions.map(() => BENCHMARK),
          name: '达标基准',
          lineStyle: { color: 'rgba(255,184,77,0.85)', width: 1.4, type: 'dashed' },
          itemStyle: { color: 'rgba(255,184,77,0.9)' },
          areaStyle: { color: 'rgba(255,184,77,0.05)' },
          symbolSize: 0
        }
      ]
    }
  ]
}))

const { el } = useEchart(option as any)
const ready = ref(true)
watch(option, () => (ready.value = true))
</script>

<template>
  <section class="card">
    <div class="card-head">
      <h3>8 维设施得分雷达</h3>
      <span class="hint">满分 100 · 虚线为达标基准 {{ BENCHMARK }}</span>
    </div>
    <div class="card-body" style="height: 268px">
      <div ref="el" class="chart" />
    </div>
  </section>
</template>
