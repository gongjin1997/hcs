<template>
  <div class="placeholder">
    <PageHeader :title="String($route.meta.title ?? '')" :desc="plan.desc" />
    <div class="hi-card body">
      <div class="hero">
        <el-icon :size="44" color="var(--el-color-primary)"><component :is="plan.icon" /></el-icon>
        <div><h3>{{ plan.title }}</h3><el-tag type="warning" effect="light" round>规划中 · {{ plan.version }}</el-tag></div>
      </div>
      <div class="features">
        <div v-for="f in plan.features" :key="f.title" class="feature">
          <b>{{ f.title }}</b><p>{{ f.desc }}</p>
        </div>
      </div>
      <p class="text-3">接口与数据表已在架构文档中预留（{{ plan.api }}），本期脚手架保留菜单与路由占位，便于按迭代交付。</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { MuteNotification } from '@element-plus/icons-vue'
import { computed, markRaw } from 'vue'
import { useRoute } from 'vue-router'
import PageHeader from '@/components/PageHeader.vue'

const PLANS = {
  '/silences': {
    icon: markRaw(MuteNotification), title: '维护窗口与告警静默', version: 'v1.1', api: 'silence 表 / /api/v1/silences',
    desc: '变更、发布、演练期间批量屏蔽告警，避免告警风暴；所有静默留痕可审计',
    features: [
      { title: '维护窗口', desc: '按资源、标签、服务、项目设置一次性或周期性窗口（如每周四 22:00–02:00 发布窗口）' },
      { title: '单条静默', desc: '在告警详情中一键静默 2 / 4 / 8 小时，必须填写原因；到期自动解除' },
      { title: '匹配预览', desc: '保存前预览将被静默的现有告警与资源，防止误伤核心告警' },
      { title: '审计与提醒', desc: '静默创建、修改、到期全部记录；窗口结束仍未恢复的告警自动重新通知' },
    ],
  },
} as const

const route = useRoute()
const plan = computed(() => PLANS[route.path as keyof typeof PLANS] ?? PLANS['/silences'])
</script>

<style scoped lang="scss">
.body { padding: 28px 32px; }
.hero { display: flex; align-items: center; gap: 18px; margin-bottom: 24px; h3 { margin: 0 0 8px; font-size: 20px; } }
.features { display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; margin-bottom: 20px;
  .feature { padding: 16px 18px; border-radius: var(--hi-radius); border: 1px dashed var(--el-color-primary-light-5); background: var(--el-color-primary-light-9);
    b { font-size: 15px; } p { margin: 6px 0 0; color: var(--hi-text-2); line-height: 1.7; font-size: 13px; } } }
</style>
