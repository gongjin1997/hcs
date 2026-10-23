<template>
  <div v-loading="loading" class="platform-panel">
    <template v-if="st">
      <el-row :gutter="16">
        <!-- 后台进程心跳 -->
        <el-col :span="8">
          <div class="box">
            <div class="box-title"><el-icon><Cpu /></el-icon>后台进程<span class="text-3">心跳超时 {{ st.heartbeat_timeout_min }} 分钟判定异常</span></div>
            <div v-for="w in st.workers" :key="w.name" class="worker" :class="w.status.toLowerCase()">
              <div class="w-head">
                <span class="dot" /><b>{{ w.label }}</b><span class="mono text-3">{{ w.name }}</span>
                <el-tag size="small" :type="WORKER_STATUS[w.status]?.type" class="ml-auto">{{ WORKER_STATUS[w.status]?.label ?? w.status }}</el-tag>
              </div>
              <div v-if="w.status !== 'NEVER'" class="text-3 w-meta">
                {{ w.host }} · pid {{ w.pid }} · v{{ w.version }} · 心跳 {{ fromNow(w.beat_at) }}<template v-if="w.started_at"> · 启动于 {{ fmtTime(w.started_at, 'MM-DD HH:mm') }}</template>
              </div>
              <div v-else class="text-3 w-meta">未收到心跳：在部署目录执行 <span class="mono">make status</span> / <span class="mono">make logs S={{ w.name }}</span> 检查容器</div>
            </div>
            <div class="kv-line"><span>API 启动</span><b>{{ fmtTime(st.api_started_at, 'MM-DD HH:mm') }}</b></div>
            <div class="kv-line"><span>实时推送连接</span><b class="num">{{ st.sse_subscribers }}</b></div>
          </div>
        </el-col>
        <!-- 通知投递 -->
        <el-col :span="8">
          <div class="box">
            <div class="box-title"><el-icon><Message /></el-icon>通知投递<span class="text-3">近 24 小时</span></div>
            <div class="nums">
              <div><span>已发送</span><b class="num ok">{{ st.outbox.sent_24h }}</b></div>
              <div><span>失败</span><b class="num" :class="{ bad: st.outbox.failed_24h }">{{ st.outbox.failed_24h }}</b></div>
              <div><span>合并</span><b class="num">{{ st.outbox.merged_24h }}</b></div>
              <div><span>待发送</span><b class="num" :class="{ warn: st.outbox.pending }">{{ st.outbox.pending }}</b></div>
              <div><span>重试中</span><b class="num" :class="{ warn: st.outbox.retrying }">{{ st.outbox.retrying }}</b></div>
            </div>
            <div class="kv-line"><span>最近发送</span><b>{{ st.outbox.last_sent_at ? fromNow(st.outbox.last_sent_at) : '-' }}</b></div>
            <div class="kv-line"><span>最早待发</span><b :class="{ 'text-warn': st.outbox.oldest_pending_at }">{{ st.outbox.oldest_pending_at ? fromNow(st.outbox.oldest_pending_at) : '无积压' }}</b></div>
            <div class="kv-line"><span>生效中的静默</span><b class="num">{{ st.active_silences }}</b><el-link class="ml-auto" type="primary" :underline="false" @click="router.push('/silences')">查看</el-link></div>
          </div>
        </el-col>
        <!-- 运行环境 -->
        <el-col :span="8">
          <div class="box">
            <div class="box-title"><el-icon><Monitor /></el-icon>运行环境</div>
            <div class="kv-line"><span>版本</span><b>v{{ st.version }}</b><el-tag size="small" effect="plain" class="ml-auto">{{ st.env }}</el-tag></div>
            <div class="kv-line"><span>时区</span><b>{{ st.timezone }}</b></div>
            <div class="kv-line"><span>数据库</span><b>{{ st.database ?? '-' }}</b></div>
            <div class="kv-line"><span>Redis</span>
              <b v-if="!st.redis.configured" class="text-3">未配置（单实例部署可不配）</b>
              <b v-else :class="st.redis.ok ? 'text-ok' : 'text-danger'">{{ st.redis.ok ? '连接正常' : '连接失败' }}</b>
            </div>
            <div class="kv-line"><span>Prometheus 指标</span><b class="mono">{{ st.metrics.path }}</b>
              <el-tag size="small" :type="st.metrics.token_required ? 'success' : 'warning'" effect="plain" class="ml-auto">{{ st.metrics.token_required ? '需令牌' : '未设令牌' }}</el-tag>
            </div>
            <div class="text-3 small">/metrics 不经 Nginx 对外暴露，供内网 Prometheus 直接抓取 API 容器</div>
          </div>
        </el-col>
      </el-row>

      <!-- 平台自身告警 -->
      <div class="box mt-16">
        <div class="box-title">
          <el-icon><WarningFilled /></el-icon>平台自身告警
          <span class="text-3">{{ st.platform_alarm.enabled ? `任务连续失败 ≥ ${st.task_fail_threshold} 次、认证失败、通知发送失败、进程心跳超时时产生（${st.platform_alarm.severity}）` : '已关闭（系统设置 → 平台自身告警）' }}</span>
          <el-link v-if="canSetting" class="ml-auto" type="primary" :underline="false" @click="emit('goto', 'settings')">设置</el-link>
        </div>
        <el-table v-if="st.platform_alarms.length" :data="st.platform_alarms" size="small" @row-click="(r: Alarm) => router.push({ path: '/alarms', query: { id: String(r.id) } })" class="clickable">
          <el-table-column width="80"><template #default="{ row }"><SeverityTag :level="row.severity" short /></template></el-table-column>
          <el-table-column label="规则" width="110"><template #default="{ row }"><span class="mono">{{ row.rule_id }}</span></template></el-table-column>
          <el-table-column label="告警" min-width="320" show-overflow-tooltip prop="title" />
          <el-table-column label="对象" min-width="140" prop="resource_name" />
          <el-table-column label="状态" width="90"><template #default="{ row }"><StatusTag :status="row.status" /></template></el-table-column>
          <el-table-column label="首次发生" width="130"><template #default="{ row }">{{ fmtTime(row.first_seen, 'MM-DD HH:mm') }}</template></el-table-column>
        </el-table>
        <div v-else class="all-ok"><el-icon><CircleCheckFilled /></el-icon>平台运行正常，没有活动的平台自身告警</div>
      </div>

      <!-- 任务健康 -->
      <div class="box mt-16">
        <div class="box-title"><el-icon><Timer /></el-icon>任务健康<span class="text-3">按租户统计连续失败次数；单个租户失败不影响其他租户</span></div>
        <el-table :data="st.tasks" size="small">
          <el-table-column label="任务" min-width="200"><template #default="{ row }"><b>{{ row.name }}</b><span v-if="!row.enabled" class="text-3">（已停用）</span></template></el-table-column>
          <el-table-column label="状态" width="100"><template #default="{ row }"><el-tag size="small" :type="TASK_HEALTH[row.status]?.type">{{ TASK_HEALTH[row.status]?.label ?? row.status }}</el-tag></template></el-table-column>
          <el-table-column label="各租户" min-width="520">
            <template #default="{ row }">
              <div class="tenants">
                <span v-for="t in row.tenants" :key="t.tenant_id ?? 0" class="tt" :class="{ bad: t.fails }">
                  <TenantTag v-if="t.tenant_id" :id="t.tenant_id" :name="t.tenant_name ?? undefined" /><span v-else class="text-3">全局</span>
                  <el-tag size="small" effect="plain" :type="TASK_STATUS[t.last_status]?.type ?? 'info'">{{ TASK_STATUS[t.last_status]?.label ?? t.last_status }}</el-tag>
                  <span class="text-3">{{ fromNow(t.last_at) }}</span>
                  <span v-if="t.fails" class="fails">连续失败 {{ t.fails }} 次<template v-if="t.last_success_at">，上次成功 {{ fromNow(t.last_success_at) }}</template></span>
                </span>
                <span v-if="!row.tenants.length" class="text-3">未运行</span>
              </div>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <!-- 数据清理 -->
      <div class="box mt-16">
        <div class="box-title">
          <el-icon><Delete /></el-icon>数据清理
          <span class="text-3">按“系统设置 → 数据保留”每天低峰执行（任务 data_cleanup）</span>
          <el-link v-if="canSetting" class="ml-auto" type="primary" :underline="false" @click="emit('goto', 'settings')">保留期设置</el-link>
        </div>
        <template v-if="st.cleanup">
          <div class="cleanup-head">
            最近一次 <b>{{ fmtTime(st.cleanup.started_at, 'YYYY-MM-DD HH:mm') }}</b>
            <el-tag size="small" :type="TASK_STATUS[st.cleanup.status]?.type ?? 'info'">{{ TASK_STATUS[st.cleanup.status]?.label ?? st.cleanup.status }}</el-tag>
            共删除 <b class="num">{{ st.cleanup.deleted }}</b> 行
          </div>
          <div class="chips">
            <span v-for="(n, k) in st.cleanup.stats" :key="k" class="chip" :class="{ zero: !n }">{{ CLEANUP_TABLE[k] ?? k }} <b class="num">{{ n }}</b></span>
          </div>
        </template>
        <div v-else class="text-3">尚未执行过数据清理</div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { CircleCheckFilled, Cpu, Delete, Message, Monitor, Timer, WarningFilled } from '@element-plus/icons-vue'
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { platformStatus } from '@/api'
import SeverityTag from '@/components/SeverityTag.vue'
import StatusTag from '@/components/StatusTag.vue'
import TenantTag from '@/components/TenantTag.vue'
import { useUserStore } from '@/stores/user'
import type { Alarm, PlatformStatus } from '@/types'
import { CLEANUP_TABLE, TASK_HEALTH, TASK_STATUS, WORKER_STATUS } from '@/utils/dict'
import { fmtTime, fromNow } from '@/utils/format'

const emit = defineEmits<{ goto: [tab: string] }>()
const router = useRouter()
const user = useUserStore()
const canSetting = computed(() => user.can('setting:manage'))
const loading = ref(false)
const st = ref<PlatformStatus | null>(null)

async function load() {
  loading.value = true
  try { st.value = await platformStatus() } finally { loading.value = false }
}
onMounted(load)
defineExpose({ reload: load })
</script>

<style scoped lang="scss">
.box { height: 100%; padding: 14px 16px; border: 1px solid var(--hi-border); border-radius: var(--hi-radius); background: #fff;
  .box-title { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; font-weight: 600;
    .el-icon { color: var(--el-color-primary); } .text-3 { font-weight: 400; font-size: 12px; } } }
.worker { padding: 8px 10px; margin-bottom: 8px; border-radius: 8px; background: var(--hi-bg-soft);
  .w-head { display: flex; align-items: center; gap: 8px; .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--hi-unknown); } }
  .w-meta { margin-top: 4px; font-size: 12px; line-height: 1.6; }
  &.ok .dot { background: var(--hi-ok); box-shadow: 0 0 0 3px rgba(46, 125, 50, 0.15); }
  &.late { background: #fff4f2; .dot { background: var(--hi-p1); } } }
.kv-line { display: flex; align-items: center; gap: 8px; padding: 5px 0; font-size: 13px; border-top: 1px dashed var(--hi-border-light);
  > span:first-child { width: 110px; color: var(--hi-text-3); } }
.small { margin-top: 6px; font-size: 12px; }
.nums { display: grid; grid-template-columns: repeat(5, 1fr); gap: 4px; margin-bottom: 8px; text-align: center;
  > div { padding: 8px 0; border-radius: 8px; background: var(--hi-bg-soft); display: flex; flex-direction: column; gap: 2px; }
  span { font-size: 12px; color: var(--hi-text-3); } b { font-size: 20px; }
  .ok { color: var(--hi-ok); } .bad { color: var(--hi-p1); } .warn { color: var(--hi-p2); } }
.all-ok { display: flex; align-items: center; gap: 8px; padding: 10px 12px; border-radius: 8px; color: var(--hi-ok); background: var(--hi-ok-bg); font-size: 13px; }
.clickable :deep(tr) { cursor: pointer; }
.tenants { display: flex; flex-wrap: wrap; gap: 6px 16px; padding: 2px 0;
  .tt { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; }
  .fails { color: var(--hi-p1); } }
.cleanup-head { display: flex; align-items: center; gap: 8px; font-size: 13px; margin-bottom: 10px; }
.chips { display: flex; flex-wrap: wrap; gap: 6px;
  .chip { padding: 2px 10px; border-radius: 12px; font-size: 12px; background: var(--el-color-primary-light-9); color: var(--hi-text-2);
    b { margin-left: 4px; color: var(--el-color-primary); }
    &.zero { background: var(--hi-bg-soft); b { color: var(--hi-text-3); } } } }
</style>
