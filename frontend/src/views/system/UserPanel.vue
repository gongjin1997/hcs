<template>
  <div v-loading="loading" class="user-panel">
    <div class="table-heading">
      <div class="intro text-3">
        统一维护人员信息，供登录、通知和资源负责人使用。
      </div>
      <div v-if="canManage" class="table-actions">
        <el-button :icon="Download" :loading="downloading === 'template'" :disabled="!!downloading" @click="downloadFile('template')">下载模板</el-button>
        <el-button :icon="Download" :loading="downloading === 'export'" :disabled="!!downloading" @click="downloadFile('export')">导出 Excel</el-button>
        <el-button :icon="Upload" @click="importVisible = true">导入 Excel</el-button>
        <el-button type="primary" :icon="Plus" @click="open()">新增用户</el-button>
      </div>
    </div>
    <div class="filters">
      <el-input v-model="kw" placeholder="账号 / 姓名 / 邮箱 / 电话" clearable :prefix-icon="Search" class="w-220" />
      <el-select v-model="roleFilter" placeholder="全部角色" clearable class="w-160">
        <el-option v-for="r in roles" :key="r.id" :value="r.id" :label="r.name" />
      </el-select>
      <span class="text-3">共 {{ rows.length }} 位人员</span>
    </div>
    <el-table :data="rows" row-key="id">
      <el-table-column label="账号" min-width="160">
        <template #default="{ row }"><div class="flex gap-8"><span class="avatar" :style="{ background: roleColor(row.role_code) }">{{ row.display_name.slice(0, 1) }}</span>
          <div><b>{{ row.display_name }}</b><span v-if="row.id === me?.id" class="me">我</span><div class="text-3 mono">{{ row.username }}</div></div></div></template>
      </el-table-column>
      <el-table-column label="邮箱" min-width="180"><template #default="{ row }">{{ row.email ?? '-' }}</template></el-table-column>
      <el-table-column label="电话" min-width="130"><template #default="{ row }">{{ row.phone || '-' }}</template></el-table-column>
      <el-table-column label="角色" width="120">
        <template #default="{ row }"><span class="role" :style="{ '--c': roleColor(row.role_code) }">{{ row.role_id == null && !row.login_enabled ? '无需角色' : row.role_name }}</span></template>
      </el-table-column>
      <el-table-column label="数据范围" min-width="170">
        <template #default="{ row }">
          <span v-if="row.tenant_ids == null" class="scope-all"><el-icon><Connection /></el-icon>全部租户</span>
          <div v-else class="flex gap-4 wrap"><TenantTag v-for="id in row.tenant_ids" :id="id" :key="id" :name="tenantName(id)" /><span v-if="!row.tenant_ids.length" class="text-3">未授权租户</span></div>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="140">
        <template #default="{ row }">
          <el-switch :model-value="row.enabled" size="small" :disabled="!canManage || row.id === me?.id || toggling != null" @change="(v: string | number | boolean) => toggle(row as UserAdmin, !!v)" />
          <span class="state" :class="{ off: !row.enabled }">{{ row.enabled ? '启用' : '停用' }}</span>
          <el-tag v-if="row.locked" type="danger" size="small" effect="plain" class="ml-4">已锁定</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="登录 / 通知" width="170">
        <template #default="{ row }">
          <el-tag size="small" :type="row.login_enabled ? 'primary' : 'info'" effect="plain">{{ row.login_enabled ? '已开通登录' : '仅登记人员' }}</el-tag>
          <div class="text-3">{{ row.notify_enabled ? (row.email ? '通知已开启' : row.phone ? '通知已开启 · 仅手机号' : '通知已开启 · 缺少联系方式') : '通知已关闭' }}</div>
        </template>
      </el-table-column>
      <el-table-column label="人员引用" min-width="150">
        <template #default="{ row }">
          <span>{{ row.owner_count }} 个资源</span><div class="text-3">{{ row.groups.length ? row.groups.join('、') : '未加入联系组' }}</div>
        </template>
      </el-table-column>
      <el-table-column label="最近登录" width="140">
        <template #default="{ row }">{{ row.last_login_at ? fmtTime(row.last_login_at, 'MM-DD HH:mm') : '从未登录' }}<div v-if="row.last_login_at" class="text-3">{{ fromNow(row.last_login_at) }}</div></template>
      </el-table-column>
      <el-table-column v-if="canManage" label="操作" width="140" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="open(row as UserAdmin)">编辑</el-button>
          <el-button v-if="row.login_enabled" link type="primary" @click="onReset(row as UserAdmin)">{{ row.locked ? '解锁 / 重置' : '重置密码' }}</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dlg.visible" :title="dlg.id ? '编辑用户' : '新增用户'" width="min(560px, calc(100vw - 24px))" :close-on-click-modal="!dlg.saving" :close-on-press-escape="!dlg.saving" :show-close="!dlg.saving" @closed="clearForm">
      <el-form ref="formRef" :model="dlg.form" :rules="rules" label-width="88px" :disabled="dlg.saving" @submit.prevent>
        <el-form-item label="账号" prop="username"><el-input v-model="dlg.form.username" :disabled="!!dlg.id" placeholder="字母开头，3~32 位" autocomplete="off" /></el-form-item>
        <el-form-item label="姓名" prop="display_name"><el-input v-model="dlg.form.display_name" /></el-form-item>
        <el-form-item label="邮箱" prop="email"><el-input v-model="dlg.form.email" placeholder="接收告警 / 报表邮件" /></el-form-item>
        <el-form-item label="电话" prop="phone"><el-input v-model="dlg.form.phone" maxlength="32" placeholder="可选，便于联系与升级" /></el-form-item>
        <el-form-item label="人员启用"><el-switch v-model="dlg.form.enabled" :disabled="dlg.id === me?.id" /><div class="text-3 scope-tip">总开关；停用后无法登录或接收通知，也不能作为新负责人选择，历史引用保留。</div></el-form-item>
        <el-form-item label="开通登录"><el-switch v-model="dlg.form.login_enabled" :disabled="dlg.id === me?.id" /><div class="text-3 scope-tip">{{ dlg.form.login_enabled ? '开通登录须分配角色；首次开通须设置初始密码。' : '仅登记人员：无需角色和密码，仍可作为资源负责人或接收通知。' }}</div></el-form-item>
        <el-form-item label="接收通知"><el-switch v-model="dlg.form.notify_enabled" /><div class="text-3 scope-tip">邮件需要有效邮箱，升级短信需要手机号，并配置对应联系组、路由及通知开关。</div></el-form-item>
        <el-form-item label="角色" prop="role_id">
          <el-select v-model="dlg.form.role_id" clearable class="w-full" :disabled="dlg.id === me?.id" :placeholder="dlg.form.login_enabled ? '选择角色' : '可选，不开通登录时无需角色'">
            <el-option v-for="r in roles" :key="r.id" :value="r.id" :label="r.name">
              <div class="role-opt"><span class="role" :style="{ '--c': roleColor(r.code) }">{{ r.name }}</span><span class="text-3">{{ r.description || `${r.permissions.length} 项权限` }}</span></div>
            </el-option>
          </el-select>
          <div v-if="selRole" class="role-desc text-3">{{ selRole.permissions.includes('*') ? '全部权限' : `${selRole.permissions.length} 项权限：${permNames(selRole.permissions)}` }}</div>
        </el-form-item>
        <el-form-item label="数据范围">
          <el-radio-group v-model="dlg.form.all" :disabled="dlg.id === me?.id">
            <el-radio :value="true">全部租户</el-radio><el-radio :value="false">指定租户</el-radio>
          </el-radio-group>
          <el-checkbox-group v-if="!dlg.form.all" v-model="dlg.form.tenant_ids" class="tenant-checks">
            <el-checkbox v-for="t in tenantOptions" :key="t.id" :value="t.id" border><TenantTag :id="t.id" :name="t.short_name" :color="t.color" plain />
              <span class="text-3 ml-4">{{ t.name }}</span></el-checkbox>
          </el-checkbox-group>
          <div class="text-3 scope-tip">{{ dlg.form.all ? '包括以后新增的租户' : '只能看到 / 处理所选租户的数据；顶部租户切换器只显示这些租户' }}</div>
        </el-form-item>
        <el-form-item v-if="needsInitialPassword" label="初始密码" prop="password">
          <el-input v-model="dlg.form.password" type="password" show-password autocomplete="new-password" placeholder="至少 8 位，首次登录后请自行修改" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button :disabled="dlg.saving" @click="dlg.visible = false">取消</el-button>
        <el-button type="primary" :loading="dlg.saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
    <UserImportDialog v-model:visible="importVisible" :can-manage="canManage" @imported="load" />
  </div>
</template>

<script setup lang="ts">
import { Connection, Download, Plus, Search, Upload } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import { createUser, downloadUserImportTemplate, exportUsers, listPermissions, listRoles, listTenants, listUsers, patchUser, resetPassword } from '@/api'
import TenantTag from '@/components/TenantTag.vue'
import { useAppStore } from '@/stores/app'
import { useUserStore } from '@/stores/user'
import type { PermissionGroup, Role, TenantBrief, UserAdmin } from '@/types'
import { fmtTime, fromNow } from '@/utils/format'
import UserImportDialog from './UserImportDialog.vue'

const app = useAppStore()
const userStore = useUserStore()
const me = computed(() => userStore.user)
const canManage = computed(() => !!me.value && me.value.tenant_ids == null && userStore.can('user:manage'))
const loading = ref(false)
const toggling = ref<number | null>(null)
const downloading = ref('')
const importVisible = ref(false)
const users = ref<UserAdmin[]>([])
const roles = ref<Role[]>([])
const perms = ref<PermissionGroup[]>([])
const tenantOptions = ref<Pick<TenantBrief, 'id' | 'name' | 'short_name' | 'color'>[]>([])
const kw = ref('')
const roleFilter = ref<number | undefined>()

async function load() {
  if (!canManage.value || loading.value) return
  loading.value = true
  try {
    const [u, r, p, t] = await Promise.all([
      listUsers(), listRoles(), listPermissions(),
      userStore.can('tenant:view') ? listTenants() : Promise.resolve(null),
    ])
    users.value = u
    roles.value = r
    perms.value = p
    tenantOptions.value = t ? t.map(({ id, name, short_name, color }) => ({ id, name, short_name, color })) : app.tenants
  } finally { loading.value = false }
}
onMounted(load)
defineExpose({ reload: load, busy: computed(() => loading.value || dlg.saving || toggling.value != null) })

async function downloadFile(kind: 'export' | 'template') {
  if (!canManage.value || downloading.value) return
  downloading.value = kind
  try { await (kind === 'export' ? exportUsers() : downloadUserImportTemplate()) }
  catch { /* 请求层提示下载错误。 */ }
  finally { downloading.value = '' }
}

const rows = computed(() => {
  const k = kw.value.trim().toLowerCase()
  return users.value.filter((u) => (!roleFilter.value || u.role_id === roleFilter.value)
    && (!k || [u.username, u.display_name, u.email ?? '', u.phone ?? ''].some((s) => s.toLowerCase().includes(k))))
})
const ROLE_COLOR: Record<string, string> = { admin: '#1d63f0', ops_lead: '#7a5af8', oncall: '#0e9f8f', viewer: '#8c8c8c' }
const roleColor = (code?: string | null) => ROLE_COLOR[code ?? ''] ?? '#f08a24'
const tenantName = (id: number) => tenantOptions.value.find((t) => t.id === id)?.short_name ?? `#${id}`
const permName = computed(() => Object.fromEntries(perms.value.flatMap((g) => g.items.map((i) => [i.code, i.name]))))
const permNames = (codes: string[]) => codes.map((c) => permName.value[c] ?? c).join('、')

// ---- 新增 / 编辑 ----
const formRef = ref<FormInstance>()
const blank = () => ({ username: '', display_name: '', email: '', phone: '', enabled: true, login_enabled: false, notify_enabled: true,
  role_id: undefined as number | undefined, all: true, tenant_ids: [] as number[], password: '' })
const dlg = reactive({ visible: false, saving: false, id: null as number | null, originalLoginEnabled: true, originalHasPassword: true, form: blank() })
const selRole = computed(() => roles.value.find((r) => r.id === dlg.form.role_id))
const needsInitialPassword = computed(() => dlg.form.login_enabled && (!dlg.id || (!dlg.originalLoginEnabled && !dlg.originalHasPassword)))
const rules = computed<FormRules>(() => ({
  username: [{ required: true, message: '请输入账号' }, { pattern: /^[a-zA-Z][a-zA-Z0-9_.-]{2,31}$/, message: '字母开头，3~32 位字母 / 数字 / _ . -' }],
  display_name: [{ required: true, message: '请输入姓名' }],
  email: [{ type: 'email', message: '邮箱格式不正确' }],
  role_id: [{ required: dlg.form.login_enabled, message: '开通登录请选择角色' }],
  password: needsInitialPassword.value ? [{ required: true, message: '请输入初始密码' }, { min: 8, max: 64, message: '长度 8~64 位' }] : [],
}))
function clearForm() { dlg.form.password = ''; formRef.value?.clearValidate() }
function open(u?: UserAdmin) {
  if (!canManage.value) return
  dlg.id = u?.id ?? null
  dlg.originalLoginEnabled = u?.login_enabled ?? true
  dlg.originalHasPassword = u?.has_password ?? true
  dlg.form = u
    ? { username: u.username, display_name: u.display_name, email: u.email ?? '', phone: u.phone ?? '', enabled: u.enabled,
      login_enabled: u.login_enabled, notify_enabled: u.notify_enabled, role_id: u.role_id ?? undefined,
      all: u.tenant_ids == null, tenant_ids: [...(u.tenant_ids ?? [])], password: '' }
    : blank()
  dlg.visible = true
}
async function save() {
  if (!canManage.value || dlg.saving) return
  if (!(await formRef.value?.validate().catch(() => false))) return
  if (!canManage.value || dlg.saving) return
  const f = dlg.form
  if (f.login_enabled && !f.all && !f.tenant_ids.length) return ElMessage.warning('开通登录请至少选择一个租户，或选择“全部租户”')
  dlg.saving = true
  try {
    if (dlg.id) {
      await patchUser(dlg.id, {
        display_name: f.display_name.trim(), email: f.email.trim() || null, phone: f.phone.trim() || null, role_id: f.role_id || null,
        enabled: f.enabled, login_enabled: f.login_enabled, notify_enabled: f.notify_enabled,
        ...(needsInitialPassword.value ? { password: f.password } : {}),
        ...(f.all ? { all_tenants: true } : { tenant_ids: f.tenant_ids }),
      })
    } else {
      await createUser({ username: f.username, display_name: f.display_name.trim(), email: f.email.trim() || null, phone: f.phone.trim() || null,
        role_id: f.role_id || null, tenant_ids: f.all ? null : f.tenant_ids, enabled: f.enabled, login_enabled: f.login_enabled,
        notify_enabled: f.notify_enabled, ...(needsInitialPassword.value ? { password: f.password } : {}) })
    }
    ElMessage.success('已保存')
    dlg.visible = false
    clearForm()
    await load()
  } catch { /* 请求层提示错误，保留表单供重试。 */ }
  finally { dlg.saving = false }
}
async function toggle(u: UserAdmin, enabled: boolean) {
  if (!canManage.value || u.id === me.value?.id || toggling.value != null) return
  toggling.value = u.id
  try {
    if (!enabled) await ElMessageBox.confirm(`停用后「${u.display_name}」无法登录、接收通知或被选为新负责人，历史引用保留。确定停用？`, '停用人员', { type: 'warning' })
    if (!canManage.value) return
    await patchUser(u.id, { enabled })
    ElMessage.success(enabled ? '已启用' : '已停用')
    await load()
  } catch { /* 取消或请求层已提示错误。 */ }
  finally { toggling.value = null }
}
async function onReset(u: UserAdmin) {
  if (!canManage.value || !u.login_enabled) return
  const { value } = await ElMessageBox.prompt(`为「${u.display_name}」设置新密码（同时解除登录锁定）`, '重置密码', {
    inputType: 'password', inputPlaceholder: '至少 8 位', inputPattern: /^.{8,64}$/, inputErrorMessage: '长度 8~64 位',
  })
  await resetPassword(u.id, value)
  ElMessage.success('密码已重置')
  await load()
}
</script>

<style scoped lang="scss">
.table-heading { align-items: flex-start; flex-wrap: wrap; .intro { flex: 1; font-size: 12px; line-height: 1.8; b { color: var(--hi-text-2); } } }
.filters { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; .w-220 { width: 220px; } .w-160 { width: 160px; } }
.avatar { width: 30px; height: 30px; border-radius: 50%; display: grid; place-items: center; color: #fff; font-weight: 600; font-size: 13px; flex: none; }
.me { margin-left: 6px; padding: 0 5px; border-radius: 4px; font-size: 11px; color: var(--el-color-primary); background: var(--el-color-primary-light-9); }
.role { display: inline-flex; align-items: center; height: 22px; padding: 0 8px; border-radius: 4px; font-size: 12px; font-weight: 500;
  color: var(--c); background: color-mix(in srgb, var(--c) 10%, #fff); border: 1px solid color-mix(in srgb, var(--c) 25%, #fff); }
.role-opt { display: flex; align-items: center; gap: 10px; .text-3 { font-size: 12px; overflow: hidden; text-overflow: ellipsis; } }
.role-desc { margin-top: 4px; font-size: 12px; line-height: 1.6; }
.scope-all { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; color: var(--el-color-primary); }
.gap-4 { gap: 4px; } .wrap { flex-wrap: wrap; }
.state { margin-left: 8px; font-size: 12px; color: var(--hi-ok); &.off { color: var(--hi-text-3); } }
.ml-4 { margin-left: 4px; }
.w-full { width: 100%; }
.tenant-checks { display: flex; flex-direction: column; gap: 6px; width: 100%; margin-top: 6px; :deep(.el-checkbox) { margin-right: 0; height: 34px; } }
.scope-tip { width: 100%; font-size: 12px; margin-top: 2px; }
@media (max-width: 600px) {
  .filters { flex-wrap: wrap; .w-220 { width: 100%; flex-basis: 100%; } }
  :deep(.el-table__cell.is-fixed-column--right) { position: static !important; right: auto !important; }
  :deep(.el-table__fixed-right-patch) { display: none; }
}
</style>
