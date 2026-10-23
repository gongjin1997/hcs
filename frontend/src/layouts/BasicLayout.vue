<template>
  <el-container class="layout" :class="{ collapsed: app.collapsed }">
    <!-- 侧边栏：浅色，按分组展示菜单；告警中心带“告警中”计数 -->
    <el-aside class="sider" :width="app.collapsed ? 'var(--hi-sider-collapsed)' : 'var(--hi-sider-width)'">
      <div class="logo" @click="$router.push('/dashboard')">
        <svg viewBox="0 0 32 32" width="30" height="30"><rect width="32" height="32" rx="8" fill="#1d63f0" />
          <path d="M16 6l8 3v6c0 5-3.4 9.3-8 11-4.6-1.7-8-6-8-11V9z" fill="none" stroke="#fff" stroke-width="2" />
          <path d="M12 16l3 3 5-6" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" /></svg>
        <div v-show="!app.collapsed" class="brand"><b>HCS 巡检平台</b><span>运维组内部 · Inspector</span></div>
      </div>
      <el-scrollbar class="menu-wrap">
        <nav class="menu">
          <template v-for="g in menu" :key="g.title">
            <div v-show="!app.collapsed" class="group-title">{{ g.title }}</div>
            <div v-show="app.collapsed" class="group-sep" />
            <el-tooltip v-for="m in g.items" :key="m.path" :content="m.title" placement="right" :disabled="!app.collapsed">
              <router-link :to="m.path" class="item" :class="{ active: activeMenu === m.path }">
                <el-icon :size="17"><component :is="m.icon" /></el-icon>
                <span v-show="!app.collapsed" class="label">{{ m.title }}</span>
                <em v-if="m.badge === 'firing' && app.stats?.FIRING" class="badge num">{{ app.stats.FIRING > 99 ? '99+' : app.stats.FIRING }}</em>
              </router-link>
            </el-tooltip>
          </template>
        </nav>
      </el-scrollbar>
      <div class="sider-foot">
        <span v-show="!app.collapsed" class="ver">v{{ APP_VERSION }} · HCS 8.5.1</span>
        <el-button text circle @click="app.toggle()"><el-icon :size="16"><component :is="app.collapsed ? Expand : Fold" /></el-icon></el-button>
      </div>
    </el-aside>

    <el-container direction="vertical" class="main-wrap">
      <el-header class="header" height="var(--hi-header-height)">
        <div class="left">
          <el-breadcrumb separator="/">
            <el-breadcrumb-item>{{ GROUP_OF[activeMenu] ?? '首页' }}</el-breadcrumb-item>
            <el-breadcrumb-item>{{ $route.meta.title }}</el-breadcrumb-item>
          </el-breadcrumb>
        </div>
        <!-- 全局搜索：按名称 / IP / 资源 ID 查台账（快捷键 /） -->
        <div v-if="user.can('ledger:view')" class="search">
        <el-autocomplete ref="searchRef" v-model="keyword" :fetch-suggestions="suggest" :trigger-on-focus="false"
                         placeholder="搜索资源：名称 / IP / 资源 ID" clearable :debounce="250" value-key="name"
                         popper-class="hi-search-pop" @select="onPick" @keyup.enter="goSearch">
          <template #prefix><el-icon><Search /></el-icon></template>
          <template #suffix><kbd v-show="!keyword">/</kbd></template>
          <template #default="{ item }">
            <div class="sg"><b>{{ item.name }}</b><span class="mono">{{ item.ip || item.resource_id.slice(0, 13) }}</span><em>{{ typeName(item.resource_type) }}</em></div>
          </template>
        </el-autocomplete>
        </div>
        <div class="right">
          <!-- 租户切换：全部租户 / 某个租户（请求头 X-Tenant-Id，后端与用户租户范围取交集） -->
          <el-dropdown v-if="app.tenants.length" trigger="click" :disabled="app.tenants.length < 2" @command="onTenant">
            <div class="tenant-switch" :class="{ single: app.tenants.length < 2 }" :style="{ '--tc': app.currentTenant?.color ?? (app.tenants.length === 1 ? app.tenants[0].color : '#4e5969') }">
              <i v-if="app.currentTenant || app.tenants.length === 1" class="dot" /><el-icon v-else :size="14"><OfficeBuilding /></el-icon>
              <span class="t-name">{{ app.tenantLabel }}</span>
              <el-icon v-if="app.tenants.length > 1" :size="12"><ArrowDown /></el-icon>
            </div>
            <template #dropdown>
              <el-dropdown-menu class="hi-tenant-menu">
                <div class="tm-title">切换租户<span>数据范围随之切换</span></div>
                <el-dropdown-item :command="0" :class="{ on: !app.tenantId }">
                  <span class="tm-item"><el-icon class="all"><OfficeBuilding /></el-icon><b>全部租户</b><em>{{ app.tenants.length }} 个租户合并查看</em></span>
                </el-dropdown-item>
                <el-dropdown-item v-for="t in app.tenants" :key="t.id" :command="t.id" :class="{ on: app.tenantId === t.id }">
                  <span class="tm-item"><i class="dot" :style="{ background: t.color }" /><b>{{ t.name }}</b><em>{{ AUTH_TYPE[t.auth_type]?.short ?? t.auth_type }}</em></span>
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
          <el-tooltip v-if="user.can('alarm:view')" :content="app.streamConnected ? '实时推送已连接' : '实时推送未连接，自动重连中'">
            <span class="live" :class="{ on: app.streamConnected }"><i />实时</span>
          </el-tooltip>
          <el-tooltip content="态势大屏（新窗口）"><el-button text circle @click="openBigScreen"><el-icon :size="17"><Monitor /></el-icon></el-button></el-tooltip>
          <el-badge v-if="user.can('alarm:view')" :value="app.stats?.FIRING || 0" :hidden="!app.stats?.FIRING" :max="99" class="bell">
            <el-button text circle @click="$router.push('/alarms?status_group=firing')"><el-icon :size="17"><Bell /></el-icon></el-button>
          </el-badge>
          <el-dropdown trigger="click" @command="onCommand">
            <div class="user">
              <span class="avatar">{{ user.displayName.slice(0, 1) }}</span>
              <div class="who"><b>{{ user.displayName }}</b><span>{{ user.roleName }}</span></div>
              <el-icon class="text-3"><ArrowDown /></el-icon>
            </div>
            <template #dropdown>
              <el-dropdown-menu>
                <div class="hi-user-card">
                  <b>{{ user.displayName }}<em>{{ user.roleName }}</em></b>
                  <span>{{ user.user?.username }} · {{ user.user?.email || '未设置邮箱' }}</span>
                  <span>租户范围：{{ user.scopeText }} · 权限点 {{ user.user?.permissions.length ?? 0 }} 个</span>
                </div>
                <el-dropdown-item command="password" divided :icon="Lock">修改密码</el-dropdown-item>
                <el-dropdown-item v-if="user.can('user:manage')" command="users" :icon="UserIcon">用户与权限</el-dropdown-item>
                <el-dropdown-item command="logout" divided :icon="SwitchButton">退出登录</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>
      <el-main class="content">
        <router-view v-slot="{ Component }">
          <transition name="fade" mode="out-in"><component :is="Component" :key="`${$route.path}#${app.tenantSeq}`" /></transition>
        </router-view>
      </el-main>
    </el-container>
    <PasswordDialog v-model="pwdVisible" />
  </el-container>
</template>

<script setup lang="ts">
import { ArrowDown, Bell, Expand, Fold, Lock, Monitor, OfficeBuilding, Search, SwitchButton, User as UserIcon } from '@element-plus/icons-vue'
import { ElMessage, ElNotification } from 'element-plus'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { listResources } from '@/api'
import PasswordDialog from '@/components/PasswordDialog.vue'
import { useAlarmStream } from '@/composables/useAlarmStream'
import { GROUP_OF, visibleMenu } from '@/config/menu'
import { useAppStore } from '@/stores/app'
import { useUserStore } from '@/stores/user'
import type { Resource } from '@/types'
import { AUTH_TYPE, SEVERITY, typeName } from '@/utils/dict'

const app = useAppStore()
const user = useUserStore()
const route = useRoute()
const router = useRouter()
const pwdVisible = ref(false)
const activeMenu = computed(() => '/' + route.path.split('/')[1])
const menu = computed(() => (user.user ? visibleMenu() : []))

// ---- 租户切换 ----
function onTenant(id: number) {
  const t = app.tenantOf(id)
  app.setTenant(id || null)
  ElMessage.success(`已切换到「${t?.name ?? '全部租户'}」`)
}

// ---- 全局搜索 ----
const keyword = ref('')
const searchRef = ref<{ focus: () => void }>()
async function suggest(q: string, cb: (items: Resource[]) => void) {
  if (!q.trim()) return cb([])
  const res = await listResources({ keyword: q.trim(), lifecycle: 'ALL', page_size: 8 }).catch(() => null)
  cb(res?.items ?? [])
}
const onPick = (r: Record<string, any>) => {
  router.push({ path: '/ledger', query: { type: r.resource_type, id: r.id } })
  keyword.value = ''
}
const goSearch = () => keyword.value.trim() && router.push({ path: '/ledger', query: { keyword: keyword.value.trim() } })
function onSlash(e: KeyboardEvent) {
  const tag = (e.target as HTMLElement)?.tagName
  if (e.key === '/' && !['INPUT', 'TEXTAREA'].includes(tag)) { e.preventDefault(); searchRef.value?.focus() }
}
onMounted(() => window.addEventListener('keydown', onSlash))
onBeforeUnmount(() => window.removeEventListener('keydown', onSlash))

// 实时推送：P1 / P2 新告警右下角弹窗提醒，并刷新铃铛计数
const { connected, reopen } = useAlarmStream((e) => {
  app.eventSeq++
  app.refreshStats()
  if (['FIRE', 'UPGRADE'].includes(e.type) && ['P1', 'P2'].includes(e.alarm.severity)) {
    ElNotification({
      title: `${e.alarm.severity} ${SEVERITY[e.alarm.severity].label}｜${e.type === 'FIRE' ? '新告警' : '告警升级'}`,
      message: `${e.alarm.resource_name} ${e.alarm.title}`,
      type: e.alarm.severity === 'P1' ? 'error' : 'warning',
      position: 'bottom-right',
      duration: 8000,
      onClick: () => router.push(`/alarms?id=${e.alarm.id}`),
    })
  }
})
watch(connected, (v) => (app.streamConnected = v))
watch(() => app.tenantSeq, () => reopen())  // 切换租户后按新范围重新订阅
onMounted(() => app.refreshStats())

function openBigScreen() {
  window.open(router.resolve('/bigscreen').href, '_blank')
}
function onCommand(cmd: string) {
  if (cmd === 'password') pwdVisible.value = true
  else if (cmd === 'users') router.push('/system?tab=users')
  else if (cmd === 'logout') {
    user.logout()
    router.push('/login')
  }
}
const APP_VERSION = __APP_VERSION__
</script>

<style scoped lang="scss">
.layout { height: 100vh; }
.sider {
  display: flex; flex-direction: column; background: var(--hi-sider-bg); border-right: 1px solid var(--hi-border);
  transition: width 0.2s; overflow: hidden;
  .logo {
    height: var(--hi-header-height); flex: none; display: flex; align-items: center; gap: 10px; padding: 0 17px; cursor: pointer;
    border-bottom: 1px solid var(--hi-border-light);
    svg { flex: none; }
    .brand { display: flex; flex-direction: column; line-height: 1.2; white-space: nowrap;
      b { color: var(--hi-text-1); font-size: 15px; letter-spacing: 0.3px; }
      span { color: var(--hi-text-3); font-size: 11px; } }
  }
  .menu-wrap { flex: 1; }
  .menu { padding: 6px 10px 12px; }
  .group-title { padding: 14px 10px 6px; font-size: 12px; color: var(--hi-text-3); }
  .group-sep { height: 1px; margin: 10px 6px; background: var(--hi-border-light); }
  .item {
    position: relative; display: flex; align-items: center; gap: 10px; height: 40px; padding: 0 12px; margin: 2px 0;
    border-radius: 8px; color: var(--hi-sider-text); font-size: 14px; white-space: nowrap; transition: background 0.15s, color 0.15s;
    &:hover { background: var(--hi-bg-soft); color: var(--hi-text-1); }
    &.active { background: var(--hi-sider-active-bg); color: var(--el-color-primary); font-weight: 600;
      &::before { content: ''; position: absolute; left: -10px; top: 10px; bottom: 10px; width: 3px; border-radius: 0 3px 3px 0; background: var(--el-color-primary); } }
    .label { flex: 1; }
    .badge { min-width: 20px; height: 18px; padding: 0 6px; border-radius: 9px; background: var(--hi-p1); color: #fff;
      font-size: 11px; font-style: normal; display: grid; place-items: center; font-weight: 600; }
  }
  .sider-foot {
    height: 48px; flex: none; display: flex; align-items: center; justify-content: space-between; padding: 0 12px 0 20px;
    border-top: 1px solid var(--hi-border-light); color: var(--hi-text-3); font-size: 12px;
  }
}
.layout.collapsed .sider {
  .logo { padding: 0 16px; }
  .item { justify-content: center; padding: 0; .badge { position: absolute; top: 2px; right: 2px; min-width: 16px; height: 16px; padding: 0 4px; font-size: 10px; } }
  .sider-foot { justify-content: center; padding: 0; }
}
.main-wrap { min-width: 0; }
.header {
  display: flex; align-items: center; gap: 16px; background: #fff; border-bottom: 1px solid var(--hi-border); padding: 0 20px;
  .left { flex: none; min-width: 160px; }
  .search { flex: 0 1 360px; min-width: 200px; margin-left: 8px; :deep(.el-autocomplete) { width: 100%; }
    kbd { font-family: var(--hi-font-mono); font-size: 11px; color: var(--hi-text-3); border: 1px solid var(--hi-border); border-radius: 4px; padding: 0 5px; line-height: 16px; }
    :deep(.el-input__wrapper) { background: var(--hi-bg-soft); box-shadow: none; border-radius: 8px; &.is-focus { background: #fff; box-shadow: 0 0 0 1px var(--el-color-primary) inset; } } }
  .right { margin-left: auto; display: flex; align-items: center; gap: 6px; flex: none; white-space: nowrap; }
  .tenant-switch { display: inline-flex; align-items: center; gap: 7px; height: 30px; padding: 0 12px; margin-right: 6px; border-radius: 15px; cursor: pointer;
    font-size: 13px; font-weight: 600; color: var(--tc); background: color-mix(in srgb, var(--tc) 8%, #fff); border: 1px solid color-mix(in srgb, var(--tc) 25%, #fff);
    transition: background 0.15s;
    &:hover { background: color-mix(in srgb, var(--tc) 14%, #fff); }
    &.single { cursor: default; }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--tc); box-shadow: 0 0 0 3px color-mix(in srgb, var(--tc) 18%, transparent); }
    .t-name { max-width: 180px; overflow: hidden; text-overflow: ellipsis; } }
  .live { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--hi-text-3); margin-right: 6px;
    i { width: 8px; height: 8px; border-radius: 50%; background: #c9cdd4; }
    &.on { color: var(--hi-ok); i { background: var(--hi-ok); box-shadow: 0 0 0 3px rgba(46, 125, 50, 0.18); animation: pulse 2s infinite; } } }
  .bell { margin-right: 8px; :deep(.el-badge__content) { top: 6px; right: 10px; } }
  .user { display: flex; align-items: center; gap: 8px; cursor: pointer; padding: 4px 8px; border-radius: 8px;
    &:hover { background: var(--hi-bg-soft); }
    .avatar { width: 30px; height: 30px; border-radius: 50%; display: grid; place-items: center; color: #fff; font-weight: 600; font-size: 13px;
      background: linear-gradient(135deg, #1d63f0, #5b8ff7); }
    .who { display: flex; flex-direction: column; line-height: 1.25; b { font-size: 13px; } span { font-size: 11px; color: var(--hi-text-3); } } }
}
.content { padding: 18px 22px 28px; background: var(--hi-bg-page); }
@keyframes pulse { 50% { box-shadow: 0 0 0 6px rgba(46, 125, 50, 0.05); } }
</style>

<style lang="scss">
.hi-tenant-menu { min-width: 280px; padding: 4px 0;
  .tm-title { padding: 6px 16px 8px; font-size: 12px; color: var(--hi-text-3); display: flex; justify-content: space-between; border-bottom: 1px solid var(--hi-border-light); margin-bottom: 4px; }
  .tm-item { display: flex; align-items: center; gap: 8px; width: 100%; line-height: 30px;
    .dot { width: 8px; height: 8px; border-radius: 50%; flex: none; } .all { color: var(--hi-text-2); }
    b { font-weight: 500; color: var(--hi-text-1); } em { margin-left: auto; padding-left: 16px; font-style: normal; font-size: 12px; color: var(--hi-text-3); } }
  .on { background: var(--hi-sider-active-bg); b { color: var(--el-color-primary); font-weight: 600; } } }
.hi-user-card { display: flex; flex-direction: column; gap: 3px; padding: 8px 16px 10px; min-width: 260px;
  b { font-size: 14px; color: var(--hi-text-1); em { margin-left: 8px; font-style: normal; font-size: 12px; font-weight: 500; padding: 1px 6px; border-radius: 4px; color: var(--el-color-primary); background: var(--el-color-primary-light-9); } }
  span { font-size: 12px; color: var(--hi-text-3); } }
.hi-search-pop .sg { display: flex; align-items: center; gap: 10px; line-height: 34px;
  b { font-weight: 500; } span { color: var(--hi-text-3); } em { margin-left: auto; font-style: normal; font-size: 12px; color: var(--hi-text-3); } }
</style>
