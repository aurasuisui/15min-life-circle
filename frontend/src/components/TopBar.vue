<script setup lang="ts">
import { ref } from 'vue'
import { useAuditStore } from '@/stores/audit'

const store = useAuditStore()
const input = ref(store.addressInput)

function submit() {
  store.addressInput = input.value
  void store.geocodeAndRun(input.value)
}
function pick(name: string) {
  input.value = name
  store.addressInput = name
  void store.geocodeAndRun(name)
}
</script>

<template>
  <header class="topbar">
    <div class="brand">
      <div class="brand-mark">
        <svg viewBox="0 0 24 24" fill="none" stroke="#04231a" stroke-width="2.1" stroke-linecap="round">
          <circle cx="12" cy="12" r="3.2" />
          <path d="M12 2.6v3M12 18.4v3M2.6 12h3M18.4 12h3" />
          <circle cx="12" cy="12" r="8.4" stroke-dasharray="2 3" />
        </svg>
      </div>
      <div class="brand-text">
        <b>15 分钟生活圈 · 智能体检</b>
        <span>Walkability Audit</span>
      </div>
    </div>

    <div class="searchbox">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
        <circle cx="11" cy="11" r="7" />
        <path d="M20 20l-3.6-3.6" />
      </svg>
      <input
        v-model="input"
        type="text"
        placeholder="输入社区/街道名称或地址，如：上海市浦东新区海科路1650号"
        @keyup.enter="submit"
      />
      <button class="btn primary" :disabled="store.loading" @click="submit">
        <span v-if="store.loading" class="spin" />
        {{ store.loading ? '体检中' : '开始体检' }}
      </button>
    </div>

    <div class="quick-chips">
      <button
        v-for="c in store.communities.slice(0, 3)"
        :key="c.name"
        class="chip"
        :title="'以「' + c.name + '」为中心体检'"
        @click="pick(c.name)"
      >
        {{ c.name }}
      </button>
    </div>

    <span v-if="store.dataSource === 'api'" class="badge live"><i class="pulse" />实时后端</span>
    <span v-else-if="store.dataSource === 'mock'" class="badge mock">离线 mock</span>
    <span v-else class="badge">加载中</span>

    <button class="btn ghost sm side-toggle" @click="store.sideOpen = !store.sideOpen">
      {{ store.sideOpen ? '收起面板' : '分析选项' }}
    </button>
  </header>
</template>
