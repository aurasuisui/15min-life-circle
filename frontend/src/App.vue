<script setup lang="ts">
import { onMounted } from 'vue'
import TopBar from '@/components/TopBar.vue'
import SidePanel from '@/components/SidePanel.vue'
import MapView from '@/components/MapView.vue'
import ReportPanel from '@/components/ReportPanel.vue'
import { useAuditStore } from '@/stores/audit'

const store = useAuditStore()

onMounted(() => {
  void store.bootstrap()
})
</script>

<template>
  <div class="app">
    <TopBar />
    <div class="workspace">
      <SidePanel />
      <MapView />
      <ReportPanel />
    </div>
  </div>
</template>
