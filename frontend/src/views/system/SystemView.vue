<template>
  <div class="system-page">
    <PageHeader title="系统管理" :refresh="refresh" :refreshing="refreshing || !!panels[tab]?.busy" />

    <div class="hi-card">
      <div v-if="info?.mock" class="system-info"><el-tag size="small" type="info" effect="plain">演示环境</el-tag></div>
      <nav class="system-sections" aria-label="系统功能分区">
        <button v-for="section in sections" :key="section.title" type="button"
                :class="{ active: activeSection === section.title }" :aria-pressed="activeSection === section.title"
                @click="goto(section.tabs[0].name)">{{ section.title }}</button>
      </nav>
      <el-tabs v-if="tabs.length" v-model="tab">
        <el-tab-pane v-for="t in sectionTabs" :key="t.name" :name="t.name" lazy>
          <template #label><span class="tab-label"><el-icon><component :is="t.icon" /></el-icon>{{ t.label }}</span></template>
          <TenantPanel v-if="t.name === 'tenants'" :ref="(el) => setPanel('tenants', el)" :info="info" />
          <CollectSwitchPanel v-else-if="t.name === 'switches'" :ref="(el) => setPanel('switches', el)" @changed="panels.tasks?.reload?.()" />
          <TaskPanel v-else-if="t.name === 'tasks'" :ref="(el) => setPanel('tasks', el)" />
          <PlatformPanel v-else-if="t.name === 'platform'" :ref="(el) => setPanel('platform', el)" @goto="goto" />
          <UserPanel v-else-if="t.name === 'users'" :ref="(el) => setPanel('users', el)" />
          <RolePanel v-else-if="t.name === 'roles'" :ref="(el) => setPanel('roles', el)" />
          <SettingsPanel v-else-if="t.name === 'settings'" :ref="(el) => setPanel('settings', el)" />
          <AuditPanel v-else-if="t.name === 'audit'" :ref="(el) => setPanel('audit', el)" />
        </el-tab-pane>
      </el-tabs>
      <el-empty v-if="!tabs.length" description="没有可访问的系统管理功能" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { Connection, Document, Key, Monitor, Open, Setting, Timer, User } from '@element-plus/icons-vue'
import { computed, markRaw, onMounted, ref, shallowReactive, type ComponentPublicInstance } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { systemInfo } from '@/api'
import PageHeader from '@/components/PageHeader.vue'
import { useUserStore } from '@/stores/user'
import type { SystemInfo } from '@/types'
import AuditPanel from './AuditPanel.vue'
import CollectSwitchPanel from './CollectSwitchPanel.vue'
import PlatformPanel from './PlatformPanel.vue'
import RolePanel from './RolePanel.vue'
import SettingsPanel from './SettingsPanel.vue'
import TaskPanel from './TaskPanel.vue'
import TenantPanel from './TenantPanel.vue'
import UserPanel from './UserPanel.vue'

const route = useRoute()
const router = useRouter()
const user = useUserStore()
/** perm 为数组 = 满足任一 */
const ALL: { name: string; label: string; perm: string | string[]; icon: object }[] = [
  { name: 'tenants', label: '租户接入', perm: 'tenant:view', icon: markRaw(Connection) },
  { name: 'switches', label: '采集开关', perm: ['task:view', 'collect:manage'], icon: markRaw(Open) },
  { name: 'tasks', label: '巡检任务', perm: 'task:view', icon: markRaw(Timer) },
  { name: 'platform', label: '平台状态', perm: 'task:view', icon: markRaw(Monitor) },
  { name: 'users', label: '用户管理', perm: 'user:manage', icon: markRaw(User) },
  { name: 'roles', label: '角色与权限', perm: 'role:manage', icon: markRaw(Key) },
  { name: 'settings', label: '系统设置', perm: 'setting:manage', icon: markRaw(Setting) },
  { name: 'audit', label: '操作审计', perm: 'audit:view', icon: markRaw(Document) },
]
const globalIdentityScope = computed(() => !!user.user && user.user.tenant_ids == null)
const tabs = computed(() => ALL.filter(t => (t.name !== 'users' || globalIdentityScope.value) && user.canAny(...(Array.isArray(t.perm) ? t.perm : [t.perm]))))
/** URL 为选中状态的唯一来源，兼容旧链接并随前进 / 后退和权限变更同步。 */
const tab = computed({
  get() {
    const want = route.query.tab === 'accounts' ? 'tenants' : route.query.tab
    return tabs.value.some(t => t.name === want) ? String(want) : tabs.value[0]?.name ?? ''
  },
  set(name: string) { goto(name) },
})
const sections = computed(() => [
  { title: '接入与采集', names: ['tenants', 'switches', 'tasks', 'platform'] },
  { title: '人员与权限', names: ['users', 'roles'] },
  { title: '系统设置', names: ['settings', 'audit'] },
].map(section => ({ title: section.title, tabs: tabs.value.filter(t => section.names.includes(t.name)) }))
  .filter(section => section.tabs.length))
const activeSection = computed(() => sections.value.find(section => section.tabs.some(t => t.name === tab.value))?.title)
const sectionTabs = computed(() => sections.value.find(section => section.title === activeSection.value)?.tabs ?? [])
const info = ref<SystemInfo | null>(null)
const refreshing = ref(false)
type Panel = { reload?: () => Promise<void> | void; busy?: boolean }
const panels = shallowReactive<Record<string, Panel | null>>({})
const setPanel = (name: string, el: Element | ComponentPublicInstance | null) => { panels[name] = el as Panel | null }

onMounted(async () => {
  try {
    if (user.canAny('task:view', 'tenant:view')) info.value = await systemInfo()
  } catch { /* 请求层统一提示错误，顶栏可重试。 */ }
})
function goto(name: string) {
  if (tab.value === name || !tabs.value.some(t => t.name === name)) return
  void router.push({ query: { ...route.query, tab: name }, hash: route.hash })
}
async function refresh() {
  if (refreshing.value || panels[tab.value]?.busy) return
  refreshing.value = true
  try {
    await Promise.allSettled([panels[tab.value]?.reload?.(), user.canAny('task:view', 'tenant:view') ? systemInfo().then((v) => (info.value = v)) : null])
  } finally { refreshing.value = false }
}
</script>

<style scoped lang="scss">
.system-info { margin-bottom: 12px; font-size: 12px; text-align: right; }
.system-sections { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }
.system-sections button { border: 1px solid var(--hi-border); border-radius: 6px; padding: 8px 16px; background: transparent; color: var(--hi-text-2); font: inherit; font-size: 13px; cursor: pointer; }
.system-sections button:hover, .system-sections button:focus-visible { color: var(--el-color-primary); border-color: var(--el-color-primary); }
.system-sections button.active { color: var(--el-color-primary); border-color: var(--el-color-primary); background: var(--el-color-primary-light-9); font-weight: 600; }
.tab-label { display: inline-flex; align-items: center; gap: 6px; }
</style>
