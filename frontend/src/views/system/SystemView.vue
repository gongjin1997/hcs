<template>
  <div class="system-page">
    <PageHeader title="系统管理" desc="租户接入（用户名密码 / AK/SK）、巡检调度、用户与角色权限、操作审计；凭据加密存储，所有变更写入审计">
      <span class="ver"><b>平台 v{{ info?.version ?? '0.3.0' }}</b><i />适配 HCS {{ info?.hcs_version ?? '8.5.1' }}<template v-if="info?.mock"><i />Mock 联调</template></span>
      <el-button :icon="Refresh" :loading="refreshing" @click="refresh">刷新</el-button>
    </PageHeader>

    <div class="hi-card">
      <el-tabs v-model="tab" @tab-change="onTab">
        <el-tab-pane v-for="t in tabs" :key="t.name" :name="t.name" lazy>
          <template #label><span class="tab-label"><el-icon><component :is="t.icon" /></el-icon>{{ t.label }}</span></template>
          <TenantPanel v-if="t.name === 'tenants'" :ref="(el) => setPanel('tenants', el)" :info="info" />
          <TaskPanel v-else-if="t.name === 'tasks'" :ref="(el) => setPanel('tasks', el)" />
          <UserPanel v-else-if="t.name === 'users'" :ref="(el) => setPanel('users', el)" />
          <RolePanel v-else-if="t.name === 'roles'" :ref="(el) => setPanel('roles', el)" />
          <AuditPanel v-else-if="t.name === 'audit'" :ref="(el) => setPanel('audit', el)" />
        </el-tab-pane>
      </el-tabs>
      <el-empty v-if="!tabs.length" description="没有可访问的系统管理功能" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { Connection, Document, Key, Refresh, Timer, User } from '@element-plus/icons-vue'
import { computed, markRaw, onMounted, ref, type ComponentPublicInstance } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { systemInfo } from '@/api'
import PageHeader from '@/components/PageHeader.vue'
import { useUserStore } from '@/stores/user'
import type { SystemInfo } from '@/types'
import AuditPanel from './AuditPanel.vue'
import RolePanel from './RolePanel.vue'
import TaskPanel from './TaskPanel.vue'
import TenantPanel from './TenantPanel.vue'
import UserPanel from './UserPanel.vue'

const route = useRoute()
const router = useRouter()
const user = useUserStore()
const ALL = [
  { name: 'tenants', label: '租户接入', perm: 'tenant:view', icon: markRaw(Connection) },
  { name: 'tasks', label: '巡检任务', perm: 'task:view', icon: markRaw(Timer) },
  { name: 'users', label: '用户管理', perm: 'user:manage', icon: markRaw(User) },
  { name: 'roles', label: '角色与权限', perm: 'role:manage', icon: markRaw(Key) },
  { name: 'audit', label: '操作审计', perm: 'audit:view', icon: markRaw(Document) },
]
const tabs = computed(() => ALL.filter((t) => user.can(t.perm)))
/** 兼容旧链接 ?tab=accounts */
const want = (typeof route.query.tab === 'string' ? route.query.tab : '').replace('accounts', 'tenants')
const tab = ref(tabs.value.some((t) => t.name === want) ? want : tabs.value[0]?.name ?? '')
const info = ref<SystemInfo | null>(null)
const refreshing = ref(false)
const panels: Record<string, { reload?: () => Promise<void> | void } | null> = {}
const setPanel = (name: string, el: Element | ComponentPublicInstance | null) => { panels[name] = el as { reload?: () => Promise<void> } | null }

onMounted(async () => { info.value = await systemInfo() })
function onTab(name: string | number) {
  router.replace({ query: { ...route.query, tab: String(name) } })
}
async function refresh() {
  refreshing.value = true
  try { await Promise.all([panels[tab.value]?.reload?.(), systemInfo().then((v) => (info.value = v))]) } finally { refreshing.value = false }
}
</script>

<style scoped lang="scss">
.ver { display: inline-flex; align-items: center; gap: 8px; height: 32px; padding: 0 12px; border-radius: 16px; font-size: 12px; color: var(--hi-text-2);
  background: var(--hi-bg-soft); border: 1px solid var(--hi-border); b { color: var(--el-color-primary); }
  i { width: 3px; height: 3px; border-radius: 50%; background: var(--hi-text-3); } }
.tab-label { display: inline-flex; align-items: center; gap: 6px; }
</style>
