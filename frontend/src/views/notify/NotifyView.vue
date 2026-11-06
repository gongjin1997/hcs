<template>
  <div class="notify-page">
    <PageHeader title="通知管理" :refresh="refresh" :refreshing="loading || refreshing || saving || smtpRef?.busy" />

    <el-collapse class="flow-help">
      <el-collapse-item title="通知流程" name="flow">
        <div class="flow">
          <div v-for="(s, i) in FLOW" :key="s.title" class="step">
            <div class="no">{{ i + 1 }}</div>
            <div><b>{{ s.title }}</b><span>{{ s.desc }}</span></div>
            <el-icon v-if="i < FLOW.length - 1" class="arrow"><Right /></el-icon>
          </div>
        </div>
      </el-collapse-item>
    </el-collapse>

    <div class="hi-card mt-16">
      <el-tabs v-model="tab" @tab-change="onTab">
        <el-tab-pane label="通知路由" name="routes">
          <div v-if="canManage" class="table-toolbar">
            <span class="text-3">按优先级从上到下匹配；调整顺序后优先级自动重排为 10、20、30…</span>
            <div class="flex gap-8">
              <el-button v-if="orderDirty" @click="loadAll">放弃排序</el-button>
              <el-button v-if="orderDirty" type="warning" :loading="saving" @click="saveOrder">保存排序</el-button>
              <el-button type="primary" :icon="Plus" @click="openRoute(null)">新建路由</el-button>
            </div>
          </div>
          <el-table v-loading="loading" :data="routes">
            <el-table-column label="优先级" width="80" align="center"><template #default="{ row }"><span class="prio num">{{ row.priority }}</span></template></el-table-column>
            <el-table-column label="路由名称" prop="name" min-width="220"><template #default="{ row }"><b>{{ row.name }}</b></template></el-table-column>
            <el-table-column label="匹配条件" min-width="240">
              <template #default="{ row }">
                <div class="match">
                  <template v-if="row.match.tenant_ids?.length"><span class="k">租户</span><TenantTag v-for="(tid, i) in row.match.tenant_ids" :key="tid" :id="tid" :name="row.tenant_names[i]" /></template>
                  <template v-if="row.match.severities?.length"><span class="k">级别</span><SeverityTag v-for="s in row.match.severities" :key="s" :level="s" /></template>
                  <template v-if="row.match.services?.length"><span class="k">服务</span><el-tag v-for="s in row.match.services" :key="s" size="small" effect="plain">{{ SERVICES[s] ?? s }}</el-tag></template>
                  <template v-if="row.match.resource_types?.length"><span class="k">类型</span><el-tag v-for="s in row.match.resource_types" :key="s" size="small" effect="plain" type="info">{{ RESOURCE_TYPES[s] ?? s }}</el-tag></template>
                  <el-tag v-if="!row.match.severities?.length && !row.match.services?.length && !row.match.tenant_ids?.length && !row.match.resource_types?.length" size="small" type="info">全部告警（兜底）</el-tag>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="联系组" min-width="130"><template #default="{ row }">{{ groupName(row.group_code) }}<div class="text-3 mono">{{ row.group_code }}</div></template></el-table-column>
            <el-table-column label="升级策略（未认领）" min-width="210">
              <template #default="{ row }">
                <div v-if="row.escalation.length" class="esc">
                  <span v-for="(e, i) in row.escalation" :key="i"><el-icon><Timer /></el-icon>{{ e.after_min }} 分钟 → {{ e.to === 'leader' ? '组长' : e.to }}
                    <em v-if="e.severities?.length" class="text-3">（仅 {{ e.severities.join(' / ') }}）</em></span>
                </div>
                <span v-else class="text-3">不升级</span>
              </template>
            </el-table-column>
            <el-table-column label="继续匹配" width="90" align="center"><template #default="{ row }"><el-icon v-if="row.continue_match" color="var(--hi-ok)"><Select /></el-icon><span v-else class="text-3">-</span></template></el-table-column>
            <el-table-column label="状态" width="80">
              <template #default="{ row }">
                <el-switch v-if="canManage" :model-value="row.enabled" size="small" @change="(v: string | number | boolean) => toggleRoute(row as NotifyRoute, !!v)" />
                <el-tag v-else :type="row.enabled ? 'success' : 'info'" size="small">{{ row.enabled ? '启用' : '停用' }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column v-if="canManage" label="操作" width="170" fixed="right">
              <template #default="{ row, $index }">
                <el-button link :icon="Top" :disabled="$index === 0" title="上移" @click="move($index, -1)" />
                <el-button link :icon="Bottom" :disabled="$index === routes.length - 1" title="下移" @click="move($index, 1)" />
                <el-button link type="primary" @click="openRoute(row as NotifyRoute)">编辑</el-button>
                <el-button link type="danger" @click="removeRoute(row as NotifyRoute)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
          <p class="text-3 tip">“继续匹配”可同时通知多个组。建议保留一条不设条件的兜底路由；未匹配到路由的告警不会发信。</p>
        </el-tab-pane>

        <el-tab-pane label="联系组" name="groups">
          <div v-if="canManage" class="table-toolbar">
            <span class="text-3">告警通知发给联系组全部启用的成员；升级策略“本组组长”通知组长</span>
            <el-button type="primary" :icon="Plus" @click="openGroup(null)">新建联系组</el-button>
          </div>
          <div class="groups">
            <div v-for="g in groups" :key="g.code" class="group">
              <div class="g-head">
                <b>{{ g.name }}</b><span class="mono text-3">{{ g.code }}</span>
                <span v-if="canManage" class="g-ops">
                  <el-button link type="primary" @click="openGroup(g)">编辑</el-button>
                  <el-button link type="danger" @click="removeGroup(g)">删除</el-button>
                </span>
              </div>
              <div class="g-used text-3">{{ g.routes?.length ? `被 ${g.routes.length} 条路由使用：${g.routes.join('、')}` : '未被路由使用' }}</div>
              <div class="g-sub">成员（{{ g.members.length }}）</div>
              <div v-for="m in g.members" :key="m.id" class="person"><span class="avatar">{{ m.name.slice(0, 1) }}</span><div><b>{{ m.name }}</b><span>{{ m.email }}</span></div></div>
              <div class="g-sub">组长 / 升级对象</div>
              <div v-for="m in g.leaders" :key="m.id" class="person leader"><span class="avatar">{{ m.name.slice(0, 1) }}</span><div><b>{{ m.name }}</b><span>{{ m.email }}</span></div></div>
            </div>
          </div>
        </el-tab-pane>

        <el-tab-pane label="联系人" name="contacts">
          <ContactPanel :contacts="contacts" />
        </el-tab-pane>

        <el-tab-pane label="路由模拟" name="simulate">
          <SimulatePanel />
        </el-tab-pane>

        <el-tab-pane label="邮件模板预览" name="preview">
          <div class="preview">
            <div class="p-side">
              <div class="p-label">选择告警</div>
              <el-select v-model="pv.alarmId" filterable class="w-full" @change="loadPreview">
                <el-option v-for="a in alarms" :key="a.id" :value="a.id" :label="`${a.severity} ${a.resource_name} ${a.title}`">
                  <SeverityTag :level="a.severity" short /> <span class="opt">{{ a.resource_name }} · {{ a.title }}</span>
                </el-option>
              </el-select>
              <div class="p-label">通知类型</div>
              <el-radio-group v-model="pv.kind" @change="loadPreview">
                <el-radio-button value="FIRING">告警</el-radio-button><el-radio-button value="UPGRADE">升级</el-radio-button>
                <el-radio-button value="REPEAT">重复</el-radio-button><el-radio-button value="ESCALATE">未处理升级</el-radio-button>
                <el-radio-button value="RESOLVED">恢复</el-radio-button>
              </el-radio-group>
              <template v-if="pv.data">
                <div class="p-label">邮件主题</div><div class="subject">{{ pv.data.subject }}</div>
                <div class="p-label">收件人</div><div class="rcpt"><el-tag v-for="r in pv.data.recipients" :key="r" size="small" effect="plain">{{ r }}</el-tag></div>
                <div class="p-label">命中路由</div><div class="rcpt"><el-tag v-for="r in pv.data.routes" :key="r" size="small">{{ r }}</el-tag></div>
                <div class="p-label">纯文本版本</div><pre class="plain">{{ pv.data.text }}</pre>
              </template>
            </div>
            <div class="p-main">
              <div class="mail-client">
                <div class="mc-bar"><i /><i /><i /><span>邮件预览</span></div>
                <iframe v-if="pv.data" :srcdoc="pv.data.html" sandbox="" title="邮件预览" />
                <el-empty v-else description="暂无邮件预览" :image-size="72" />
              </div>
            </div>
          </div>
        </el-tab-pane>

        <el-tab-pane label="邮件服务与测试" name="smtp" lazy>
          <SmtpPanel ref="smtpRef" :can-manage="canManage" />
        </el-tab-pane>

        <el-tab-pane label="发送记录" name="records">
          <div class="filter-bar mb-16">
            <el-radio-group v-model="recordStatus" @change="loadRecords">
              <el-radio-button value="">全部</el-radio-button>
              <el-radio-button v-for="(v, k) in NOTIFY_STATUS" :key="k" :value="k">{{ v.label }}</el-radio-button>
            </el-radio-group>
          </div>
          <el-table v-loading="loading" :data="records">
            <el-table-column label="创建时间" width="140"><template #default="{ row }">{{ fmtTime(row.created_at) }}</template></el-table-column>
            <el-table-column label="类型" width="90"><template #default="{ row }">{{ NOTIFY_KIND[row.kind] ?? row.kind }}</template></el-table-column>
            <el-table-column label="级别" width="90"><template #default="{ row }"><SeverityTag v-if="row.severity" :level="row.severity" short /></template></el-table-column>
            <el-table-column v-if="app.multiTenant" label="租户" width="110"><template #default="{ row }"><TenantTag :id="row.tenant_id" /></template></el-table-column>
            <el-table-column label="主题" prop="subject" min-width="320" show-overflow-tooltip />
            <el-table-column label="收件人" min-width="180" show-overflow-tooltip><template #default="{ row }">{{ row.recipients.join('，') }}</template></el-table-column>
            <el-table-column label="状态" width="96"><template #default="{ row }"><el-tag :type="NOTIFY_STATUS[row.status]?.type ?? 'info'" size="small">{{ NOTIFY_STATUS[row.status]?.label ?? row.status }}</el-tag></template></el-table-column>
            <el-table-column label="重试" width="64" align="center" prop="retry" />
            <el-table-column label="发送时间 / 错误" min-width="180" show-overflow-tooltip>
              <template #default="{ row }"><span v-if="row.error" class="err">{{ row.error }}</span><span v-else>{{ fmtTime(row.sent_at) }}</span></template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
      </el-tabs>
    </div>

    <RouteDialog v-model:visible="routeDlg.visible" :route="routeDlg.route" :groups="groups" :next-priority="nextPriority" @saved="loadAll" />
    <GroupDialog v-model:visible="groupDlg.visible" :group="groupDlg.group" :contacts="contacts" @saved="loadAll" />
  </div>
</template>

<script setup lang="ts">
import { Bottom, Plus, Right, Select, Timer, Top } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, onScopeDispose, reactive, ref } from 'vue'
import { useLatestRequest } from '@/composables/useLatestRequest'
import { useRoute, useRouter } from 'vue-router'
import {
  type ContactGroup, type NotifyRoute, deleteGroup, deleteRoute, listAlarms, listContacts, listGroups, listNotifications, listRoutes,
  previewMail, reorderRoutes, updateRoute,
} from '@/api'
import PageHeader from '@/components/PageHeader.vue'
import SeverityTag from '@/components/SeverityTag.vue'
import TenantTag from '@/components/TenantTag.vue'
import { useAppStore } from '@/stores/app'
import { useUserStore } from '@/stores/user'
import type { Alarm, Contact, NotificationRow } from '@/types'
import { NOTIFY_KIND, NOTIFY_STATUS, RESOURCE_TYPES, SERVICES } from '@/utils/dict'
import { fmtTime } from '@/utils/format'
import ContactPanel from './ContactPanel.vue'
import GroupDialog from './GroupDialog.vue'
import RouteDialog from './RouteDialog.vue'
import SimulatePanel from './SimulatePanel.vue'
import SmtpPanel from './SmtpPanel.vue'

const FLOW = [
  { title: '告警事件', desc: '新告警 / 升级 / 重复 / 恢复' },
  { title: '路由匹配', desc: '按优先级匹配租户、级别与服务' },
  { title: '联系组', desc: '成员 + 组长，去重合并收件人' },
  { title: '待发队列', desc: '保存待发送的通知' },
  { title: '发送邮件', desc: '失败后重试，最多尝试 5 次' },
  { title: '未认领升级', desc: '超时未认领上报组长（可限定级别）' },
]

const route = useRoute()
const router = useRouter()
const app = useAppStore()
const tab = ref(typeof route.query.tab === 'string' ? route.query.tab : 'routes')
const metadataRequest = useLatestRequest()
const recordRequest = useLatestRequest()
const loading = metadataRequest.loading
const recordLoading = recordRequest.loading
const refreshing = ref(false)
const smtpRef = ref<{ reload: () => Promise<void>; busy: boolean } | null>(null)
const routes = ref<NotifyRoute[]>([])
const groups = ref<ContactGroup[]>([])
const alarms = ref<Alarm[]>([])
const records = ref<NotificationRow[]>([])
const recordStatus = ref('')
const pv = reactive({ alarmId: null as number | null, kind: 'FIRING', data: null as Awaited<ReturnType<typeof previewMail>> | null })

const groupName = (code: string) => groups.value.find((g) => g.code === code)?.name ?? code
const canManage = computed(() => useUserStore().can('notify:manage'))
const contacts = ref<Contact[]>([])
const saving = ref(false)
const orderDirty = ref(false)

async function loadAll() {
  try {
    const applied = await metadataRequest.run(() => Promise.all([listRoutes(), listGroups(), listContacts()]), ([r, g, c]) => {
      routes.value = r; groups.value = g; contacts.value = c; orderDirty.value = false
    })
    if (applied) await onTab(tab.value)
  } catch { /* 请求层提示错误，保留上次配置。 */ }
}

async function refresh() {
  if (loading.value || refreshing.value || saving.value || smtpRef.value?.busy) return
  refreshing.value = true
  try {
    if (orderDirty.value) {
      try {
        await ElMessageBox.confirm('未保存的路由排序将丢失，是否刷新？', '刷新', {
          type: 'warning', confirmButtonText: '刷新', cancelButtonText: '取消',
        })
      } catch { return }
    }
    await Promise.allSettled([loadAll(), tab.value === 'smtp' ? smtpRef.value?.reload() : undefined])
  } finally { refreshing.value = false }
}

// ---- 路由维护 ----
const routeDlg = reactive({ visible: false, route: null as NotifyRoute | null })
const nextPriority = computed(() => {
  const fallback = routes.value.filter((r) => !Object.values(r.match).some((v) => (v as unknown[])?.length))
  const max = Math.max(0, ...routes.value.filter((r) => !fallback.includes(r)).map((r) => r.priority))
  return Math.min(max + 10, fallback.length ? Math.max(0, fallback[0].priority - 10) : max + 10)
})
const openRoute = (r: NotifyRoute | null) => Object.assign(routeDlg, { visible: true, route: r })
function move(i: number, d: number) {
  const list = [...routes.value]
  const [x] = list.splice(i, 1)
  list.splice(i + d, 0, x)
  routes.value = list
  orderDirty.value = true
}
async function saveOrder() {
  saving.value = true
  try {
    await reorderRoutes(routes.value.map((r) => r.id))
    ElMessage.success('排序已保存')
    await loadAll()
  } finally { saving.value = false }
}
async function toggleRoute(r: NotifyRoute, on: boolean) {
  await updateRoute(r.id, { name: r.name, priority: r.priority, match: r.match, group_code: r.group_code,
    escalation: r.escalation.map((e) => ({ after_min: e.after_min, to: e.to, severities: e.severities ?? [] })), continue_match: r.continue_match, enabled: on })
  ElMessage.success(on ? '已启用' : '已停用')
  await loadAll()
}
async function removeRoute(r: NotifyRoute) {
  const fallback = !Object.values(r.match).some((v) => (v as unknown[])?.length)
  await ElMessageBox.confirm(fallback ? `“${r.name}”是兜底路由，删除后未命中其他路由的告警将不会通知任何人。确认删除？` : `确认删除路由“${r.name}”？`,
    '删除路由', { type: 'warning' })
  await deleteRoute(r.id)
  ElMessage.success('已删除')
  await loadAll()
}

// ---- 联系组维护 ----
const groupDlg = reactive({ visible: false, group: null as ContactGroup | null })
const openGroup = (g: ContactGroup | null) => Object.assign(groupDlg, { visible: true, group: g })
async function removeGroup(g: ContactGroup) {
  await ElMessageBox.confirm(`确认删除联系组“${g.name}”？（仍被路由使用时不能删除）`, '删除联系组', { type: 'warning' })
  await deleteGroup(g.id)
  ElMessage.success('已删除')
  await loadAll()
}
async function loadRecords() {
  const status = recordStatus.value || undefined
  records.value = []
  return recordRequest.run(() => listNotifications(status), rows => { records.value = rows }).catch(() => false)
}
let previewRequest = 0
async function loadPreview() {
  const request = ++previewRequest
  const id = pv.alarmId
  const kind = pv.kind
  pv.data = null
  if (!id) return
  try {
    const data = await previewMail(id, kind)
    if (request === previewRequest && pv.alarmId === id && pv.kind === kind) pv.data = data
  } catch { /* 请求层提示错误，不显示上一次的邮件。 */ }
}
let previewListRequest = 0
onScopeDispose(() => { previewRequest++; previewListRequest++ })
async function onTab(name: string | number) {
  if (name !== 'records') recordRequest.invalidate()
  if (name !== 'preview') { previewRequest++; previewListRequest++; pv.data = null }
  router.replace({ query: { tab: String(name) } })
  if (name === 'records') await loadRecords()
  if (name === 'preview') {
    const request = ++previewListRequest
    ++previewRequest
    pv.data = null
    try {
      const data = await listAlarms({ status_group: 'active', page: 1, page_size: 100 })
      if (request !== previewListRequest) return
      alarms.value = data.items
      if (!alarms.value.some(a => a.id === pv.alarmId)) pv.alarmId = alarms.value[0]?.id ?? null
      await loadPreview()
    } catch { /* 请求层提示错误，不保留旧预览。 */ }
  }
}
onMounted(loadAll)
</script>

<style scoped lang="scss">
.flow { display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px; padding: 14px 20px;
  .step { position: relative; display: flex; align-items: center; gap: 10px; padding-right: 18px;
    .no { flex: none; width: 26px; height: 26px; border-radius: 50%; display: grid; place-items: center; font-size: 13px; font-weight: 700; color: #fff; background: var(--el-color-primary); }
    b { display: block; font-size: 13px; } span { font-size: 12px; color: var(--hi-text-3); }
    .arrow { position: absolute; right: 0; top: 50%; transform: translateY(-50%); color: var(--hi-text-3); } } }
.prio { display: inline-block; min-width: 36px; padding: 2px 8px; border-radius: 10px; font-weight: 700; color: var(--el-color-primary); background: var(--el-color-primary-light-9); }
.match { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; .k { font-size: 12px; color: var(--hi-text-3); } }
.esc { display: flex; flex-direction: column; gap: 2px; font-size: 13px; span { display: inline-flex; align-items: center; gap: 4px; } em { font-style: normal; font-size: 12px; } }
.tip { margin: 12px 0 0; }
.groups { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px;
  .group { padding: 16px; border: 1px solid var(--hi-border); border-radius: var(--hi-radius);
    .g-head { display: flex; align-items: center; gap: 8px; b { font-size: 15px; } .g-ops { margin-left: auto; } }
    .g-used { margin-top: 6px; font-size: 12px; line-height: 1.5; }
    .g-sub { margin: 14px 0 6px; font-size: 12px; color: var(--hi-text-3); }
    .person { display: flex; align-items: center; gap: 10px; padding: 6px 0;
      .avatar { width: 30px; height: 30px; border-radius: 50%; display: grid; place-items: center; color: #fff; font-size: 13px; background: linear-gradient(135deg, #1d63f0, #5b8ff7); }
      &.leader .avatar { background: linear-gradient(135deg, #f57c00, #f9a825); }
      div { display: flex; flex-direction: column; line-height: 1.3; span { font-size: 12px; color: var(--hi-text-3); } } } } }
.preview { display: grid; grid-template-columns: 360px 1fr; gap: 20px;
  .w-full { width: 100%; }
  .p-label { margin: 14px 0 6px; font-size: 12px; color: var(--hi-text-3); &:first-child { margin-top: 0; } }
  .opt { margin-left: 6px; }
  .subject { font-weight: 600; line-height: 1.5; }
  .rcpt { display: flex; flex-wrap: wrap; gap: 6px; }
  .plain { margin: 0; max-height: 260px; overflow: auto; padding: 10px 12px; font-size: 12px; line-height: 1.6; white-space: pre-wrap; border-radius: 6px; background: var(--hi-bg-soft); border: 1px solid var(--hi-border); font-family: var(--hi-font-mono); }
  .mail-client { border: 1px solid var(--hi-border); border-radius: var(--hi-radius); overflow: hidden; background: #f3f5f9;
    .mc-bar { display: flex; align-items: center; gap: 6px; height: 34px; padding: 0 12px; background: #fff; border-bottom: 1px solid var(--hi-border); font-size: 12px; color: var(--hi-text-3);
      i { width: 10px; height: 10px; border-radius: 50%; background: #ff5f57; &:nth-child(2) { background: #febc2e; } &:nth-child(3) { background: #28c840; } }
      span { margin-left: 8px; } }
    iframe { display: block; width: 100%; height: 720px; border: 0; } } }
.err { color: var(--hi-p1); }
@media (max-width: 1199px) {
  .flow { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .flow .step { min-width: 0; }
  .flow .step > div { min-width: 0; }
  .flow .step:nth-child(3n) .arrow { display: none; }
  .groups { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .preview { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 767px) {
  .flow { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px 16px; padding: 14px 16px; }
  .flow .step { gap: 8px; padding-right: 0; }
  .flow .step span, .flow .step .arrow { display: none; }
  .groups { grid-template-columns: minmax(0, 1fr); }
}
</style>
