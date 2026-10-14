<script setup lang="ts">
import { computed } from 'vue'
import { useAuditStore } from '@/stores/audit'
import ScoreGauge from '@/components/charts/ScoreGauge.vue'
import RadarChart from '@/components/charts/RadarChart.vue'
import CategoryBar from '@/components/charts/CategoryBar.vue'
import CleaningFunnel from '@/components/charts/CleaningFunnel.vue'
import BlindspotList from '@/components/BlindspotList.vue'
import AiReportCard from '@/components/AiReportCard.vue'

const store = useAuditStore()
const report = computed(() => store.report)
</script>

<template>
  <aside class="col-report">
    <div v-if="store.errorMessage" class="notice err">
      <span>✕</span>
      <span>
        <b>/api/audit 调用失败，已启用离线数据。</b><br />
        {{ store.errorMessage }}
      </span>
    </div>
    <div v-else-if="store.dataSource === 'mock'" class="notice">
      <span>⚠</span>
      <span><b>当前为离线 mock 数据</b>（后端不可用时的回退），页面所有图表与地图交互均正常，可直接用于前端联调与演示。</span>
    </div>

    <template v-if="report">
      <ScoreGauge :score="report.score" :degraded="store.isDegraded" :center="report.meta.center" />
      <RadarChart :dimensions="report.score.dimensions" />
      <CategoryBar :categories="report.facilities.categories" :mode="store.barMode" />
      <CleaningFunnel :funnel="report.facilities.cleaning_funnel" />
      <BlindspotList />
      <AiReportCard />

      <section v-if="report.siting.length" class="card">
        <div class="card-head">
          <h3>补点建议（MCLP 贪心）</h3>
          <span class="hint">{{ report.siting.length }} 个候选点</span>
        </div>
        <div class="card-body">
          <div v-for="(s, i) in report.siting" :key="i" class="bs-item" style="background: rgba(49,208,165,0.06); border-color: rgba(49,208,165,0.24)" @click="store.selectPoi({ uid: 'siting-' + i, name: '建议补点 #' + (i + 1), lat: s.lat, lng: s.lng, address: '', tag: 'siting', walk_seconds: 0, category: 'market', categoryName: '补点', color: '#31d0a5' })">
            <span class="bs-idx" style="background: rgba(49,208,165,0.2); color: #b9f6e4">{{ i + 1 }}</span>
            <span class="bs-main">
              <span class="bs-title">{{ s.reason }}</span>
              <span class="bs-sub">
                <span class="tag-mini">{{ s.lat.toFixed(4) }}, {{ s.lng.toFixed(4) }}</span>
                <span class="tag-mini">收益 {{ Object.entries(s.gains).map(([k, v]) => k + ' +' + v).join('、') }}</span>
              </span>
            </span>
          </div>
        </div>
      </section>
    </template>
    <div v-else class="card"><div class="empty"><span class="spin" /> 正在生成体检报告…</div></div>
  </aside>
</template>
