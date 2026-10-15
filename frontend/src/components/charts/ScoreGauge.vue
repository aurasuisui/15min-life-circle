<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useEchart } from '@/composables/useEchart'
import { chartTheme, type EChartsOption } from '@/charts/echarts'
import { GRADE_COLORS, type Score } from '@/types/report'

const props = defineProps<{ score: Score; degraded: boolean; center: { lat: number; lng: number } }>()

const gradeColor = computed(() => GRADE_COLORS[props.score.grade] ?? '#31d0a5')

const option = ref<EChartsOption>({
  series: [
    {
      type: 'gauge',
      startAngle: 212,
      endAngle: -32,
      min: 0,
      max: 100,
      radius: '96%',
      center: ['50%', '54%'],
      progress: { show: true, width: 11, roundCap: true, itemStyle: { color: gradeColor.value } },
      axisLine: { lineStyle: { width: 11, color: [[1, 'rgba(255,255,255,0.075)']] } },
      pointer: { show: false },
      axisTick: { show: false },
      splitLine: { show: false },
      axisLabel: { show: false },
      anchor: { show: false },
      title: { show: false },
      detail: {
        valueAnimation: true,
        offsetCenter: [0, '2%'],
        formatter: (v: number) => v.toFixed(1),
        color: '#e9f1ff',
        fontSize: 30,
        fontWeight: 700,
        fontFamily: chartTheme.font
      },
      data: [{ value: props.score.total }]
    }
  ]
})

watch(
  () => props.score.total,
  (v) => {
    option.value = {
      series: [
        {
          ...(option.value.series as any[])[0],
          progress: { show: true, width: 11, roundCap: true, itemStyle: { color: gradeColor.value } },
          data: [{ value: v }]
        }
      ]
    }
  }
)

const { el } = useEchart(option)

const grades = ['A', 'B', 'C', 'D'] as const
const gradeText: Record<string, string> = { A: '优秀', B: '良好', C: '达标待改进', D: '明显不足' }
</script>

<template>
  <section class="card">
    <div class="score-hero">
      <div ref="el" class="score-gauge chart" />
      <div class="score-meta">
        <div class="badge" :style="{ color: gradeColor, borderColor: gradeColor + '66', background: gradeColor + '1f' }">
          综合等级 {{ score.grade }} · {{ gradeText[score.grade] }}
        </div>
        <div class="score-total" style="margin-top: 8px">
          {{ score.total.toFixed(1) }} <span style="color: var(--muted); font-weight: 400; font-size: 11px">/ 100</span>
        </div>
        <div class="grade-band">
          <i v-for="g in grades" :key="g" :class="{ on: g === score.grade }" :style="{ background: GRADE_COLORS[g] }" />
        </div>
        <div style="font-size: 10.5px; color: var(--muted); line-height: 1.55">
          A ≥ 90 · B ≥ 75 · C ≥ 60 · D &lt; 60<br />
          中心点 {{ center.lat.toFixed(4) }}, {{ center.lng.toFixed(4) }}
          <span v-if="degraded" style="color: var(--warn)"> · 数据已降级</span>
        </div>
      </div>
    </div>
    <div class="card-body" style="border-top: 1px solid var(--line)">
      <div class="dim-list">
        <div v-for="d in score.dimensions" :key="d.key" class="dim-row">
          <span class="name">{{ d.name }}</span>
          <span class="bar">
            <i
              :style="{
                // 维度分是百分制（见后端 app/services/score.py），直接当百分比宽度用。
                // 历史 bug：曾写成 (d.score / 10) * 100，得分 52.38 会算出 523% 宽度，进度条溢出。
                width: Math.max(0, Math.min(100, d.score)) + '%',
                background:
                  d.score >= 80
                    ? 'linear-gradient(90deg,#31d0a5,#4ee0ff)'
                    : d.score >= 60
                      ? 'linear-gradient(90deg,#ffb84d,#ffd98a)'
                      : 'linear-gradient(90deg,#ff5d73,#ff9aa8)'
              }"
            />
          </span>
          <span class="val">{{ d.score.toFixed(1) }}</span>
        </div>
      </div>
    </div>
  </section>
</template>
