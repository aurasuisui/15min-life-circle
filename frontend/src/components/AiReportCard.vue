<script setup lang="ts">
import { computed } from 'vue'
import { useAuditStore } from '@/stores/audit'
import type { AiReport } from '@/types/report'

const store = useAuditStore()
const ai = computed<AiReport | null>(() => store.report?.ai_report ?? null)

/** 极简 markdown：只处理 **加粗**，且先做 HTML 转义，避免 XSS */
function safeBold(text: string): string {
  const escaped = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
  return escaped.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
}

const PRIORITY_LABEL: Record<string, string> = { high: '高', medium: '中', low: '低' }
</script>

<template>
  <section class="card">
    <div class="card-head">
      <h3>AI 体检解读</h3>
      <span v-if="ai" class="badge" :class="ai.degraded ? 'mock' : 'live'">
        {{ ai.degraded ? '模板降级' : 'LLM 生成' }}
      </span>
    </div>
    <div v-if="!ai" class="empty">暂无 AI 报告</div>
    <div v-else class="card-body">
      <p class="ai-diag" v-html="safeBold(ai.diagnosis)" />

      <template v-if="ai.evidence.length">
        <div style="margin-top: 12px; font-size: 11px; color: var(--muted); letter-spacing: 0.5px">规范依据</div>
        <div v-for="(e, i) in ai.evidence" :key="i" class="evidence">
          <div class="clause">{{ e.clause }}</div>
          <div class="source">—— {{ e.source }}</div>
        </div>
      </template>

      <template v-if="ai.suggestions.length">
        <div style="margin-top: 13px; font-size: 11px; color: var(--muted); letter-spacing: 0.5px">改善建议</div>
        <div v-for="(s, i) in ai.suggestions" :key="i" class="sug">
          <span class="pr" :class="s.priority">{{ PRIORITY_LABEL[s.priority] ?? s.priority }}</span>
          <span class="txt">{{ s.text }}</span>
        </div>
      </template>
    </div>
  </section>
</template>
