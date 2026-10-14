<script setup lang="ts">
import { computed } from 'vue'
import { useAuditStore } from '@/stores/audit'
import { categoryName } from '@/types/report'

const store = useAuditStore()
const list = computed(() => store.blindspots)
const total = computed(() => store.totalBlindspotArea)

function fmtArea(m2: number) {
  return m2 >= 1e6 ? (m2 / 1e6).toFixed(3) + ' km²' : Math.round(m2).toLocaleString('en-US') + ' m²'
}
function fmtPct(m2: number) {
  if (!store.area15) return ''
  return ((m2 / store.area15) * 100).toFixed(1) + '% 圈面积'
}
</script>

<template>
  <section class="card">
    <div class="card-head">
      <h3>服务盲区清单</h3>
      <span class="hint">{{ list.length }} 处 · 合计 {{ fmtArea(total) }}</span>
    </div>
    <div class="card-body">
      <div v-if="!list.length" class="empty">未识别到成片服务盲区</div>
      <div
        v-for="(b, i) in list"
        :key="i"
        class="bs-item"
        :class="{ on: store.selectedBlindspot === i }"
        @click="store.selectBlindspot(store.selectedBlindspot === i ? null : i)"
      >
        <span class="bs-idx">{{ i + 1 }}</span>
        <span class="bs-main">
          <span class="bs-title">灰色区域 #{{ i + 1 }} · {{ fmtArea(b.area_m2) }}</span>
          <span class="bs-sub">
            <span class="tag-mini">缺乏：{{ b.missing.map(categoryName).join(' / ') }}</span>
            <span class="tag-mini">{{ fmtPct(b.area_m2) }}</span>
            <span class="tag-mini">{{ b.sample_point.lat.toFixed(4) }}, {{ b.sample_point.lng.toFixed(4) }}</span>
          </span>
        </span>
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" style="color: var(--muted); flex: 0 0 auto">
          <path d="M5 12h13M12 5l7 7-7 7" />
        </svg>
      </div>
      <p style="font-size: 10.5px; color: var(--muted); margin: 10px 0 0; line-height: 1.6">
        盲区判定基于<strong style="color: var(--text-2)">真实步行耗时</strong>：关键类设施（菜市场 / 药店 / 小学）1 km 步行范围内全覆盖不到、
        且在 15 分钟等时圈内连片出现的栅格，聚合为多边形。点击条目地图会定位并高亮。
      </p>
    </div>
  </section>
</template>
