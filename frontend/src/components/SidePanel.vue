<script setup lang="ts">
import { computed } from 'vue'
import { useAuditStore } from '@/stores/audit'
import { CATEGORY_COLORS, CATEGORY_NAMES, CATEGORY_KEYS, type MinuteKey } from '@/types/report'

const store = useAuditStore()

const BAND_STYLE: Record<MinuteKey, { color: string; label: string }> = {
  '5': { color: '#31d0a5', label: '5 分钟' },
  '10': { color: '#4ee0ff', label: '10 分钟' },
  '15': { color: '#5aa9ff', label: '15 分钟' }
}
const BAND_ORDER: MinuteKey[] = ['5', '10', '15']

const quota = computed(() => store.report?.meta.quota ?? null)
const meta = computed(() => store.report?.meta ?? null)
const pending = computed(() => store.pending)

function fmtArea(m2: number) {
  return m2 >= 1e6 ? (m2 / 1e6).toFixed(3) + ' km²' : Math.round(m2).toLocaleString('en-US') + ' m²'
}
const lat = computed(() => (pending.value ?? store.center).lat)
const lng = computed(() => (pending.value ?? store.center).lng)
</script>

<template>
  <aside class="col-side" :class="{ open: store.sideOpen }">
    <section class="card">
      <div class="card-head">
        <h3>分析中心点</h3>
        <span class="hint">{{ meta?.data_mode ?? '—' }}</span>
      </div>
      <div class="card-body">
        <div class="kv"><span class="k">经度 lng</span><span class="v">{{ lng.toFixed(6) }}</span></div>
        <div class="kv"><span class="k">纬度 lat</span><span class="v">{{ lat.toFixed(6) }}</span></div>
        <div class="kv"><span class="k">坐标系</span><span class="v">BD09</span></div>
      </div>
      <div class="card-body" style="border-top: 1px solid var(--line); padding-top: 9px">
        <div style="font-size: 11.5px; color: var(--text-2); line-height: 1.6">
          {{ meta?.address || '尚未获取地址' }}
        </div>
        <div style="display: flex; gap: 6px; margin-top: 10px; flex-wrap: wrap">
          <button class="btn sm" @click="store.resetView()">复位视野</button>
          <button class="btn sm" :disabled="store.loading" @click="store.runAudit()">重新体检</button>
        </div>
        <p style="font-size: 10.5px; color: var(--muted); margin: 9px 0 0; line-height: 1.55">
          三种选点方式：① 顶栏输入地址（走 <code style="font-family: var(--mono)">/api/geocode</code>）；
          ② <strong style="color: var(--text-2)">单击地图</strong>落点；③ <strong style="color: var(--text-2)">拖拽中心标记</strong>。
          落点后点击「在此处体检」即可。
        </p>
      </div>
    </section>

    <section class="card">
      <div class="card-head"><h3>时间档位与图层</h3></div>
      <div class="card-body">
        <div style="display: flex; gap: 6px; flex-wrap: wrap">
          <button
            v-for="m in BAND_ORDER"
            :key="m"
            class="chip"
            :class="{ on: store.bands.includes(m) }"
            @click="store.toggleBand(m)"
          >
            <span class="dot" :style="{ background: BAND_STYLE[m].color }" />
            {{ BAND_STYLE[m].label }}
          </button>
        </div>
        <div style="display: flex; gap: 6px; flex-wrap: wrap; margin-top: 9px">
          <button class="chip" :class="{ on: store.layers.pois }" @click="store.layers.pois = !store.layers.pois">POI 点位</button>
          <button class="chip" :class="{ on: store.layers.blindspots }" @click="store.layers.blindspots = !store.layers.blindspots">盲区</button>
          <button class="chip" :class="{ on: store.layers.mask }" @click="store.layers.mask = !store.layers.mask">圈外压暗</button>
        </div>
        <div style="margin-top: 11px">
          <div class="kv">
            <span class="k">15 分钟圈面积</span><span class="v">{{ fmtArea(store.area15) }}</span>
          </div>
          <div class="kv">
            <span class="k">盲区面积占比</span>
            <span class="v">{{ store.area15 ? ((store.totalBlindspotArea / store.area15) * 100).toFixed(1) + '%' : '—' }}</span>
          </div>
          <div class="kv"><span class="k">圈内 POI 总数</span><span class="v">{{ store.allPois.length }}</span></div>
        </div>
      </div>
    </section>

    <section class="card">
      <div class="card-head"><h3>数据来源与配额</h3></div>
      <div class="card-body">
        <div style="display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 9px">
          <span class="badge" :class="store.dataSource === 'api' ? 'live' : store.dataSource === 'mock' ? 'mock' : ''">
            {{ store.sourceLabel }}
          </span>
          <span class="badge" :class="store.backendOnline === false ? 'err' : 'live'">
            后端 {{ store.backendOnline === null ? '检测中' : store.backendOnline ? '在线' : '离线' }}
          </span>
          <!-- 区分「设计内的离线快照」与「真实故障降级」：replay 是 NFR-3 的特性，不该显示成故障 -->
          <span v-if="meta?.data_mode === 'replay'" class="badge live">离线快照模式</span>
          <span v-else-if="meta?.degraded" class="badge mock">已降级</span>
        </div>
        <template v-if="quota">
          <div class="kv">
            <span class="k">算路配额</span>
            <span class="v">{{ quota.routes_used }} / {{ quota.routes_limit }}</span>
          </div>
          <div class="meter" style="margin: 5px 0 10px">
            <i :style="{ width: Math.min(100, (quota.routes_used / quota.routes_limit) * 100) + '%' }" />
          </div>
          <div class="kv">
            <span class="k">地点检索配额</span>
            <span class="v">{{ quota.poi_used }} / {{ quota.poi_limit }}</span>
          </div>
          <div class="meter" style="margin-top: 5px">
            <i
              :style="{
                width: Math.min(100, (quota.poi_used / quota.poi_limit) * 100) + '%',
                background: 'linear-gradient(90deg,#ffb84d,#ff5d73)'
              }"
            />
          </div>
        </template>
        <div v-if="meta?.degrade_reasons?.length" class="notice" style="margin-top: 10px">
          <span>{{ meta?.data_mode === 'replay' ? 'ℹ' : '⚠' }}</span>
          <span>
            <template v-for="(r, i) in meta.degrade_reasons" :key="i">{{ r }}<br v-if="i < meta.degrade_reasons.length - 1" /></template>
          </span>
        </div>
      </div>
    </section>

    <section class="card">
      <div class="card-head"><h3>图例</h3></div>
      <div class="card-body tight">
        <div class="legend-title">步行等时圈</div>
        <div v-for="m in BAND_ORDER" :key="m" class="legend-row">
          <span class="legend-swatch" :style="{ background: BAND_STYLE[m].color + '55', borderColor: BAND_STYLE[m].color }" />
          <span>{{ BAND_STYLE[m].label }}可达范围</span>
        </div>
        <div class="legend-row">
          <span class="legend-swatch" style="background: rgba(255,77,109,0.4); border-color: #ff8fa3" />
          <span>服务盲区（灰色区域）</span>
        </div>
        <div class="legend-title" style="margin-top: 9px">设施类目</div>
        <div class="legend-grid">
          <div v-for="k in CATEGORY_KEYS" :key="k" class="legend-row">
            <span class="legend-dot" :style="{ background: CATEGORY_COLORS[k] }" />
            <span>{{ CATEGORY_NAMES[k] }}</span>
          </div>
        </div>
      </div>
    </section>
  </aside>
</template>
