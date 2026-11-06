<template>
  <el-dialog :model-value="modelValue" width="980px" top="4vh" class="tenant-dialog" :close-on-click-modal="false"
             @update:model-value="(v: boolean) => emit('update:modelValue', v)" @open="init">
    <template #header>
      <div class="dlg-title">
        <span class="dot" :style="{ background: form.color }" />
        <b>{{ tenant ? `编辑租户 · ${tenant.name}` : '新增租户' }}</b>
      </div>
    </template>

    <el-form ref="formRef" :model="form" :rules="rules" label-position="top" class="tenant-form" :disabled="saving" @submit.prevent>
      <el-alert v-if="!tenant" title="新租户默认关闭全部采集，保存后请在采集开关中按需启用。" type="info" :closable="false" show-icon class="mb-16" />
      <!-- 基本信息 -->
      <div class="sec"><span class="no">1</span>基本信息</div>
      <el-row :gutter="16">
        <el-col :span="5"><el-form-item label="租户编码" prop="code"><el-input v-model="form.code" placeholder="如 tenant-a" :disabled="!!tenant" class="mono-input" /></el-form-item></el-col>
        <el-col :span="7"><el-form-item label="租户名称" prop="name"><el-input v-model="form.name" placeholder="如 租户A · 核心生产" /></el-form-item></el-col>
        <el-col :span="5"><el-form-item label="简称（标签展示）"><el-input v-model="form.short_name" maxlength="16" placeholder="默认同名称" /></el-form-item></el-col>
        <el-col :span="7">
          <el-form-item label="标识色">
            <div class="colors">
              <span v-for="c in TENANT_COLORS" :key="c" class="color" :class="{ on: form.color === c }" :style="{ background: c }" @click="form.color = c" />
            </div>
          </el-form-item>
        </el-col>
      </el-row>

      <!-- HCS 接入 -->
      <div class="sec"><span class="no">2</span>HCS 接入认证<span class="sec-tip">平台以该租户身份只读调用 HCS 云服务 API，建议使用仅授予只读策略的 IAM 用户</span></div>
      <div class="auth-cards">
        <div v-for="(a, k) in AUTH_TYPE" :key="k" class="auth-card" :class="{ on: form.auth_type === k }" :style="{ '--c': a.color }" @click="form.auth_type = k as AuthType">
          <el-radio :model-value="form.auth_type" :value="k" @click.stop.prevent="form.auth_type = k as AuthType">{{ a.label }}</el-radio>
          <div class="desc">{{ a.desc }}</div>
        </div>
      </div>
      <el-row :gutter="16">
        <el-col :span="12"><el-form-item label="IAM 地址" prop="iam_endpoint"><el-input v-model="form.iam_endpoint" class="mono-input" placeholder="https://iam-apigateway-proxy.{外部域名}" /></el-form-item></el-col>
        <el-col :span="6"><el-form-item label="账号名（domain）" prop="domain_name"><el-input v-model="form.domain_name" placeholder="选填，可使用账号 ID" /></el-form-item></el-col>
        <el-col :span="6">
          <el-form-item>
            <template #label>账号 ID（domain_id）<el-tooltip content="可代替账号名参与认证；调用全局服务时也用于标识账号" placement="top"><el-icon class="tip"><QuestionFilled /></el-icon></el-tooltip></template>
            <el-input v-model="form.domain_id" class="mono-input" placeholder="可选" />
          </el-form-item>
        </el-col>
      </el-row>
      <el-row :gutter="16">
        <el-col v-if="form.auth_type === 'PASSWORD'" :span="12">
          <el-form-item label="IAM 用户名" prop="username"><el-input v-model="form.username" placeholder="只读 IAM 用户，如 inspector_ro" autocomplete="off" /></el-form-item>
        </el-col>
        <el-col v-else :span="12">
          <el-form-item prop="access_key">
            <template #label>Access Key（AK）<span v-if="keepAk" class="text-3">· 当前 {{ tenant?.access_key_mask }}，留空不修改</span></template>
            <el-input v-model="form.access_key" class="mono-input" :placeholder="keepAk ? '留空表示不修改' : '20 位左右的 AK'" autocomplete="off" />
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item prop="secret">
            <template #label>{{ form.auth_type === 'PASSWORD' ? 'IAM 密码' : 'Secret Key（SK）' }}<span v-if="keepSecret" class="text-3">· 已设置，留空不修改</span></template>
            <el-input v-model="form.secret" type="password" show-password autocomplete="new-password"
                      :placeholder="keepSecret ? '留空表示不修改' : authChanged ? '切换认证方式后需重新填写' : '必填'" />
          </el-form-item>
        </el-col>
      </el-row>

      <!-- 纳管项目 -->
      <div class="sec"><span class="no">3</span>纳管项目（Region / Project）<span class="sec-tip">一个租户可纳管多个项目；巡检与台账同步按项目逐个调用</span>
        <el-button class="sec-act" size="small" :icon="Plus" @click="form.projects.push(blankProject(form.projects[0]?.external_domain))">添加项目</el-button>
      </div>
      <p class="text-3">Region ID 填底座导出表的 region0_id；外部域名填 external_global_domain_name，不填控制台地址或资源空间名称。</p>
      <el-table :data="form.projects" size="small" border class="proj-table">
        <el-table-column label="Region ID" min-width="120"><template #default="{ row }"><el-input v-model="row.region_id" size="small" class="mono-input" placeholder="region-01" /></template></el-table-column>
        <el-table-column label="Project ID" min-width="210"><template #default="{ row }"><el-input v-model="row.project_id" size="small" class="mono-input" placeholder="32 位项目 ID" /></template></el-table-column>
        <el-table-column label="项目名称" min-width="150"><template #default="{ row }"><el-input v-model="row.project_name" size="small" placeholder="可选" /></template></el-table-column>
        <el-table-column label="外部域名" min-width="150"><template #default="{ row }"><el-input v-model="row.external_domain" size="small" class="mono-input" placeholder="hcs.example.com" /></template></el-table-column>
        <el-table-column label="环境" width="90">
          <template #default="{ row }"><el-select v-model="row.env" size="small"><el-option v-for="(e, k) in ENV_TYPE" :key="k" :value="k" :label="e.label" /></el-select></template>
        </el-table-column>
        <el-table-column label="DataArts 工作空间" min-width="160">
          <template #default="{ row }">
            <el-select v-model="row.workspaces" size="small" multiple filterable allow-create default-first-option collapse-tags placeholder="回车添加" class="w-full" />
          </template>
        </el-table-column>
        <el-table-column label="启用" width="60" align="center"><template #default="{ row }"><el-switch v-model="row.enabled" size="small" /></template></el-table-column>
        <el-table-column width="44" align="center">
          <template #default="{ $index }"><el-button link type="danger" :icon="Delete" :disabled="form.projects.length <= 1" @click="form.projects.splice($index, 1)" /></template>
        </el-table-column>
      </el-table>

      <!-- 监控指标 -->
      <div class="sec"><span class="no">4</span>外部指标通道<span class="sec-tip">可选 ECS 使用率来源；DCS、GaussDB 使用原生接口</span></div>
      <el-alert v-if="tenant?.metrics_source === 'AOM'" title="旧 AOM 来源已停用，保存时改为“仅服务原生”；如需 Prometheus，请手动选择。" type="warning" :closable="false" class="mb-16" />
      <div class="src-grid">
        <div v-for="(m, k) in metricsSources" :key="k" class="src" :class="{ on: form.metrics_source === k }" :style="{ '--c': m.color }" @click="form.metrics_source = k as MetricsSource">
          <div class="src-head"><el-radio :model-value="form.metrics_source" :value="k" @click.stop.prevent="form.metrics_source = k as MetricsSource">{{ m.label }}</el-radio>
            <el-tag v-if="k === 'AUTO'" size="small" effect="plain" round>推荐</el-tag></div>
          <div class="desc">{{ m.desc }}</div>
        </div>
      </div>
      <el-row :gutter="16" class="mt-12">
        <el-col :span="12">
          <el-form-item prop="prometheus_url">
            <template #label>Prometheus 地址<span class="text-3">{{ form.metrics_source === 'PROMETHEUS' ? '（必填）' : '（可选，供自动选择使用）' }}</span></template>
            <el-input v-model="form.prometheus_url" class="mono-input" placeholder="填写内网实际 Prometheus 地址" />
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <div class="metrics-tip">
            <el-icon><InfoFilled /></el-icon>
            <div>外部指标仅支持 Prometheus，按需配置。DCS 本地历史仅包含实际接口采样；GaussDB 的磁盘、慢 SQL、会话等使用原生接口。</div>
          </div>
        </el-col>
      </el-row>

      <!-- Endpoint 覆盖 -->
      <el-collapse v-model="advanced" class="adv">
        <el-collapse-item name="ep">
          <template #title>
            <div class="sec flat"><span class="no">5</span>服务 Endpoint 覆盖（高级）
              <span class="sec-tip">按局点实际地址覆盖；旧 AOM 地址已停用</span>
              <el-tag v-if="epRows.length" size="small" round class="ml-6">{{ epRows.length }} 项</el-tag></div>
          </template>
          <div v-for="(r, i) in epRows" :key="i" class="ep-row">
            <el-select v-model="r.k" size="small" filterable class="ep-k" placeholder="服务" :disabled="r.k === 'aom'">
              <el-option v-if="r.k === 'aom'" value="aom" label="AOM（已停用）" disabled />
              <el-option v-for="s in endpointServices" :key="s" :value="s" :label="`${SERVICES[s] ?? s.toUpperCase()}（${s}）`" :disabled="epRows.some((x) => x !== r && x.k === s)" />
            </el-select>
            <el-input v-model="r.v" size="small" class="mono-input flex-1" :placeholder="endpointHint(r.k)" :disabled="r.k === 'aom'" />
            <el-button link type="danger" :icon="Delete" @click="epRows.splice(i, 1)" />
          </div>
          <el-button size="small" :icon="Plus" @click="epRows.push({ k: '', v: '' })">添加覆盖</el-button>
        </el-collapse-item>
      </el-collapse>

      <!-- 报表 & 备注 -->
      <div class="sec"><span class="no">6</span>报表与备注</div>
      <el-row :gutter="16">
        <el-col :span="12">
          <el-form-item label="租户报表收件人（日报 / 周报按租户发送）">
            <RecipientSelect v-model="form.report_user_ids" :tenant-id="tenant?.id ?? null" :global-only="!tenant" :active="modelValue" :disabled="saving" />
          </el-form-item>
        </el-col>
        <el-col :span="9"><el-form-item label="备注"><el-input v-model="form.remark" placeholder="如 接口负责人、接入说明" /></el-form-item></el-col>
        <el-col :span="3"><el-form-item label="启用"><el-switch v-model="form.enabled" inline-prompt active-text="启用" inactive-text="停用" /></el-form-item></el-col>
      </el-row>
    </el-form>

    <template #footer>
      <span class="foot-tip text-3">保存后可测试认证、服务接口与已配置的指标来源</span>
      <el-button @click="emit('update:modelValue', false)">取消</el-button>
      <el-button type="primary" :loading="saving" @click="save">{{ tenant ? '保存修改' : '创建租户' }}</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { Delete, InfoFilled, Plus, QuestionFilled } from '@element-plus/icons-vue'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { computed, ref } from 'vue'
import { createTenant, updateTenant } from '@/api'
import RecipientSelect from '@/components/RecipientSelect.vue'
import { useUserStore } from '@/stores/user'
import type { AuthType, MetricsSource, SystemInfo, Tenant, TenantIn } from '@/types'
import { AUTH_TYPE, ENV_TYPE, SERVICES } from '@/utils/dict'
import { ENDPOINT_SERVICES, TENANT_COLORS, TENANT_METRICS_SOURCES, blankProject, blankTenant, cleanTenantIn, endpointHint, toTenantIn } from './tenant-form'

const props = defineProps<{ modelValue: boolean; tenant: Tenant | null; info: SystemInfo | null }>()
const emit = defineEmits<{ (e: 'update:modelValue', v: boolean): void; (e: 'saved'): void }>()
const user = useUserStore()

const formRef = ref<FormInstance>()
const form = ref<TenantIn>(blankTenant())
const epRows = ref<{ k: string; v: string }[]>([])
const advanced = ref<string[]>([])
const saving = ref(false)
const metricsSources = computed(() => Object.fromEntries(Object.entries(TENANT_METRICS_SOURCES).filter(([key]) => key !== 'CES' || props.tenant?.metrics_source === 'CES')))
const endpointServices = computed(() => ENDPOINT_SERVICES.filter(key => key !== 'ces' || epRows.value.some(row => row.k === 'ces')))

const authChanged = computed(() => !!props.tenant && props.tenant.auth_type !== form.value.auth_type)
const keepSecret = computed(() => !!props.tenant?.secret_set && !authChanged.value)
const keepAk = computed(() => props.tenant?.auth_type === 'AKSK' && form.value.auth_type === 'AKSK' && !!props.tenant.access_key_mask)

function init() {
  form.value = props.tenant ? toTenantIn(props.tenant) : blankTenant()
  epRows.value = Object.entries(form.value.endpoints).map(([k, v]) => ({ k, v }))
  advanced.value = epRows.value.length ? ['ep'] : []
  formRef.value?.clearValidate()
}

const required = (msg: string) => ({ required: true, message: msg, trigger: 'blur' })
const rules: FormRules = {
  code: [required('请填写租户编码'), { pattern: /^[a-z][a-z0-9-]{1,31}$/, message: '小写字母开头，可含数字和 -，2~32 位', trigger: 'blur' }],
  name: [required('请填写租户名称')],
  iam_endpoint: [required('请填写 IAM 地址'), { pattern: /^https?:\/\/.{4,}/, message: '以 http(s):// 开头', trigger: 'blur' }],
  username: [{ validator: (_r, v, cb) => (form.value.auth_type === 'PASSWORD' && !v?.trim() ? cb(new Error('请填写 IAM 用户名')) : cb()), trigger: 'blur' }],
  access_key: [{ validator: (_r, v, cb) => (form.value.auth_type === 'AKSK' && !keepAk.value && !v?.trim() ? cb(new Error('请填写 Access Key')) : cb()), trigger: 'blur' }],
  secret: [{ validator: (_r, v, cb) => (!keepSecret.value && !v?.trim() ? cb(new Error(form.value.auth_type === 'PASSWORD' ? '请填写密码' : '请填写 Secret Key')) : cb()), trigger: 'blur' }],
  prometheus_url: [{ validator: (_r, v, cb) => (form.value.metrics_source === 'PROMETHEUS' && !v?.trim() ? cb(new Error('指标来源为 Prometheus 时必填')) : cb()), trigger: 'blur' }],
}

async function save() {
  if (saving.value) return
  if (!(await formRef.value?.validate().catch(() => false))) return
  if (saving.value) return
  const bad = form.value.projects.find((p) => !p.region_id.trim() || !p.project_id.trim() || !p.external_domain.trim())
  if (bad) return ElMessage.warning('纳管项目的 Region ID、Project ID、外部域名均为必填')
  form.value.endpoints = Object.fromEntries(epRows.value.filter((r) => r.k && r.v.trim()).map((r) => [r.k, r.v]))
  const body = cleanTenantIn(form.value)
  saving.value = true
  try {
    if (props.tenant) await updateTenant(props.tenant.id, body)
    else await createTenant(body)
    ElMessage.success(props.tenant ? '已保存' : '租户已创建，建议立即执行连通性测试')
    await user.fetchMe()                     // 刷新顶部租户切换器
    emit('update:modelValue', false)
    emit('saved')
  } finally { saving.value = false }
}
</script>

<style scoped lang="scss">
.dlg-title { display: flex; align-items: center; gap: 10px; b { font-size: 16px; }
  .dot { width: 10px; height: 10px; border-radius: 50%; } }
.tenant-form { max-height: 72vh; overflow-y: auto; padding-right: 6px; }
.sec { display: flex; align-items: center; gap: 8px; margin: 18px 0 10px; font-weight: 600; font-size: 14px; color: var(--hi-text-1);
  &:first-child { margin-top: 0; }
  &.flat { margin: 0; }
  .no { width: 20px; height: 20px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; font-size: 12px;
    color: #fff; background: var(--el-color-primary); }
  .sec-tip { font-weight: 400; font-size: 12px; color: var(--hi-text-3); }
  .sec-act { margin-left: auto; } }
.mono-input :deep(input) { font-family: var(--hi-font-mono); font-size: 13px; }
.w-full { width: 100%; }
.mt-12 { margin-top: 12px; }
.ml-6 { margin-left: 6px; }
.tip { margin-left: 4px; color: var(--hi-text-3); cursor: help; vertical-align: -2px; }
.colors { display: flex; flex-wrap: wrap; gap: 6px; padding-top: 4px;
  .color { width: 20px; height: 20px; border-radius: 6px; cursor: pointer; border: 2px solid transparent; box-shadow: 0 0 0 1px var(--hi-border);
    &.on { border-color: #fff; box-shadow: 0 0 0 2px var(--el-color-primary); } } }
.auth-cards, .src-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; margin-bottom: 14px; }
.src-grid { grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); margin-bottom: 0; }
.auth-card, .src { padding: 10px 14px; border-radius: 8px; border: 1px solid var(--hi-border); cursor: pointer; transition: all .15s;
  &:hover { border-color: var(--c); }
  &.on { border-color: var(--c); background: color-mix(in srgb, var(--c) 6%, #fff); box-shadow: 0 0 0 2px color-mix(in srgb, var(--c) 18%, transparent); }
  .desc { font-size: 12px; line-height: 1.6; color: var(--hi-text-3); margin-top: 2px; }
  :deep(.el-radio) { height: 22px; font-weight: 600; } }
.src-head { display: flex; align-items: center; justify-content: space-between; }
.metrics-tip { display: flex; gap: 8px; margin-top: 26px; padding: 8px 12px; border-radius: 6px; font-size: 12px; line-height: 1.7; color: var(--hi-text-2);
  background: var(--el-color-primary-light-9); .el-icon { color: var(--el-color-primary); margin-top: 3px; } }
.proj-table :deep(.el-input__wrapper) { box-shadow: none; padding: 0 4px; }
.proj-table :deep(.el-select__wrapper) { box-shadow: none; }
.adv { margin-top: 14px; border-top: none; :deep(.el-collapse-item__header) { height: 40px; } }
.ep-row { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; .ep-k { width: 220px; } }
.flex-1 { flex: 1; }
.foot-tip { float: left; line-height: 32px; font-size: 12px; }
</style>
