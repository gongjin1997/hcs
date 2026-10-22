<template>
  <div class="notify-page">
    <PageHeader title="通知管理" desc="告警按优先级匹配通知路由 → 投递到联系组；未认领按升级策略逐级上报。通知先写 Outbox 再异步发送，失败自动退避重试">
      <el-button :icon="Refresh" @click="loadAll">刷新</el-button>
    </PageHeader>

    <!-- 投递链路示意 -->
    <div class="hi-card flow">
      <div v-for="(s, i) in FLOW" :key="s.title" class="step">
        <div class="no">{{ i + 1 }}</div>
        <div><b>{{ s.title }}</b><span>{{ s.desc }}</span></div>
        <el-icon v-if="i < FLOW.length - 1" class="arrow"><Right /></el-icon>
      </div>
    </div>

    <div class="hi-card mt-16">
      <el-tabs v-model="tab" @tab-change="onTab">
        <el-tab-pane label="通知路由" name="routes">
          <el-table v-loading="loading" :data="routes">
            <el-table-column label="优先级" width="80" align="center"><template #default="{ row }"><span class="prio num">{{ row.priority }}</span></template></el-table-column>
            <el-table-column label="路由名称" prop="name" min-width="160"><template #default="{ row }"><b>{{ row.name }}</b></template></el-table-column>
            <el-table-column label="匹配条件" min-width="240">
              <template #default="{ row }">
                <div class="match">
                  <template v-if="row.match.tenant_ids?.length"><span class="k">租户</span><TenantTag v-for="(tid, i) in row.match.tenant_ids" :key="tid" :id="tid" :name="row.tenant_names[i]" /></template>
                  <template v-if="row.match.severities?.length"><span class="k">级别</span><SeverityTag v-for="s in row.match.severities" :key="s" :level="s" /></template>
                  <template v-if="row.match.services?.length"><span class="k">服务</span><el-tag v-for="s in row.match.services" :key="s" size="small" effect="plain">{{ SERVICES[s] ?? s }}</el-tag></template>
                  <el-tag v-if="!row.match.severities?.length && !row.match.services?.length && !row.match.tenant_ids?.length" size="small" type="info">全部告警（兜底）</el-tag>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="联系组" min-width="150"><template #default="{ row }">{{ groupName(row.group_code) }}<div class="text-3 mono">{{ row.group_code }}</div></template></el-table-column>
            <el-table-column label="升级策略（未认领）" min-width="230">
              <template #default="{ row }">
                <div v-if="row.escalation.length" class="esc">
                  <span v-for="(e, i) in row.escalation" :key="i"><el-icon><Timer /></el-icon>{{ e.after_min }} 分钟 → {{ e.to === 'leader' ? '组长' : e.to }}
                    <em v-if="e.severities?.length" class="text-3">（仅 {{ e.severities.join(' / ') }}）</em></span>
                </div>
                <span v-else class="text-3">不升级</span>
              </template>
            </el-table-column>
            <el-table-column label="继续匹配" width="90" align="center"><template #default="{ row }"><el-icon v-if="row.continue_match" color="var(--hi-ok)"><Select /></el-icon><span v-else class="text-3">-</span></template></el-table-column>
            <el-table-column label="状态" width="80"><template #default="{ row }"><el-tag :type="row.enabled ? 'success' : 'info'" size="small">{{ row.enabled ? '启用' : '停用' }}</el-tag></template></el-table-column>
          </el-table>
          <p class="text-3 tip">“继续匹配”表示命中后继续向下匹配（如 P1 同时通知值班组与业务组）；未命中任何路由时使用兜底路由，保证告警不丢。</p>
        </el-tab-pane>

        <el-tab-pane label="联系组" name="groups">
          <div class="groups">
            <div v-for="g in groups" :key="g.code" class="group">
              <div class="g-head"><b>{{ g.name }}</b><span class="mono text-3">{{ g.code }}</span></div>
              <div class="g-sub">成员（{{ g.members.length }}）</div>
              <div v-for="m in g.members" :key="m.id" class="person"><span class="avatar">{{ m.name.slice(0, 1) }}</span><div><b>{{ m.name }}</b><span>{{ m.email }}</span></div></div>
              <div class="g-sub">组长 / 升级对象</div>
              <div v-for="m in g.leaders" :key="m.id" class="person leader"><span class="avatar">{{ m.name.slice(0, 1) }}</span><div><b>{{ m.name }}</b><span>{{ m.email }}</span></div></div>
            </div>
          </div>
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
                <div class="mc-bar"><i /><i /><i /><span>收件箱 · HTML 邮件渲染（与 SMTP 实际发送内容一致）</span></div>
                <iframe v-if="pv.data" :srcdoc="pv.data.html" sandbox="" title="邮件预览" />
              </div>
            </div>
          </div>
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
  </div>
</template>

<script setup lang="ts">
import { Refresh, Right, Select, Timer } from '@element-plus/icons-vue'
import { onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { type ContactGroup, type NotifyRoute, listAlarms, listGroups, listNotifications, listRoutes, previewMail } from '@/api'
import PageHeader from '@/components/PageHeader.vue'
import SeverityTag from '@/components/SeverityTag.vue'
import TenantTag from '@/components/TenantTag.vue'
import { useAppStore } from '@/stores/app'
import type { Alarm, NotificationRow } from '@/types'
import { NOTIFY_KIND, NOTIFY_STATUS, SERVICES } from '@/utils/dict'
import { fmtTime } from '@/utils/format'

const FLOW = [
  { title: '告警事件', desc: '新告警 / 升级 / 重复 / 恢复' },
  { title: '路由匹配', desc: '按优先级匹配租户、级别与服务' },
  { title: '联系组', desc: '成员 + 组长，去重合并收件人' },
  { title: '写入 Outbox', desc: '与告警同事务，保证不丢' },
  { title: '异步投递', desc: 'SMTP 发送，失败退避重试 5 次' },
  { title: '未认领升级', desc: '超时未认领上报组长（可限定级别）' },
]

const route = useRoute()
const router = useRouter()
const app = useAppStore()
const tab = ref(typeof route.query.tab === 'string' ? route.query.tab : 'routes')
const loading = ref(false)
const routes = ref<NotifyRoute[]>([])
const groups = ref<ContactGroup[]>([])
const alarms = ref<Alarm[]>([])
const records = ref<NotificationRow[]>([])
const recordStatus = ref('')
const pv = reactive({ alarmId: null as number | null, kind: 'FIRING', data: null as Awaited<ReturnType<typeof previewMail>> | null })

const groupName = (code: string) => groups.value.find((g) => g.code === code)?.name ?? code

async function loadAll() {
  loading.value = true
  try {
    ;[routes.value, groups.value] = await Promise.all([listRoutes(), listGroups()])
    await onTab(tab.value)
  } finally { loading.value = false }
}
async function loadRecords() { records.value = await listNotifications(recordStatus.value || undefined) }
async function loadPreview() { if (pv.alarmId) pv.data = await previewMail(pv.alarmId, pv.kind) }
async function onTab(name: string | number) {
  router.replace({ query: { tab: String(name) } })
  if (name === 'records') await loadRecords()
  if (name === 'preview' && !alarms.value.length) {
    alarms.value = (await listAlarms({ status_group: 'active', page: 1, page_size: 100 })).items
    pv.alarmId = alarms.value.find((a) => a.resource_name === 'redis-prod-01')?.id ?? alarms.value[0]?.id ?? null
    await loadPreview()
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
    .g-head { display: flex; justify-content: space-between; align-items: center; b { font-size: 15px; } }
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
</style>
