<template>
  <div v-loading="loading" class="role-panel">
    <div class="table-toolbar">
      <div class="intro text-3">角色决定<b>能做什么</b>（菜单、按钮和后端接口统一按权限点校验）；<b>能看哪些租户</b>在「用户管理」里按用户设置。
        内置角色可调整权限，平台管理员固定拥有全部权限；自定义角色在没有用户使用时可删除。</div>
      <el-button type="primary" :icon="Plus" @click="open()">新增角色</el-button>
    </div>

    <div class="role-cards">
      <div v-for="r in roles" :key="r.id" class="rc" :style="{ '--c': roleColor(r.code) }">
        <div class="rc-head">
          <span class="rc-name">{{ r.name }}</span>
          <el-tag v-if="r.builtin" size="small" effect="plain" round>内置</el-tag>
          <el-tag v-else size="small" type="warning" effect="plain" round>自定义</el-tag>
          <span class="mono text-3 rc-code">{{ r.code }}</span>
        </div>
        <div class="rc-desc text-3">{{ r.description || '—' }}</div>
        <div class="rc-foot">
          <span><b>{{ isAll(r) ? '全部' : r.permissions.length }}</b> 项权限</span><span><b>{{ r.user_count }}</b> 个用户</span>
          <span class="flex-1" />
          <el-button v-if="!isAll(r)" link type="primary" size="small" @click="open(r)">编辑</el-button>
          <el-tooltip v-else content="平台管理员固定拥有全部权限，不可修改" placement="top"><el-icon class="lock"><Lock /></el-icon></el-tooltip>
          <el-button v-if="!r.builtin" link type="danger" size="small" :disabled="r.user_count > 0" @click="remove(r)">删除</el-button>
        </div>
      </div>
    </div>

    <div class="sub-title">权限矩阵 <span class="text-3">（{{ flat.length }} 个权限点 · {{ perms.length }} 个模块）</span></div>
    <el-table :data="flat" :span-method="spanMethod" border size="small" class="matrix">
      <el-table-column label="模块" width="110" align="center"><template #default="{ row }"><b>{{ row.group }}</b></template></el-table-column>
      <el-table-column label="权限" min-width="220">
        <template #default="{ row }">{{ row.name }}<span class="mono text-3 code">{{ row.code }}</span></template>
      </el-table-column>
      <el-table-column v-for="r in roles" :key="r.id" :label="r.name" width="108" align="center">
        <template #header><span class="col-head" :style="{ color: roleColor(r.code) }">{{ r.name }}</span></template>
        <template #default="{ row }">
          <el-icon v-if="has(r, row.code)" class="yes" :style="{ color: roleColor(r.code) }"><Select /></el-icon>
          <span v-else class="no">—</span>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="dlg.visible" :title="dlg.role ? `编辑角色 · ${dlg.role.name}` : '新增角色'" width="720px" top="6vh">
      <el-form ref="formRef" :model="dlg.form" :rules="rules" label-width="72px" :disabled="dlg.saving" @submit.prevent>
        <el-row :gutter="16">
          <el-col :span="10"><el-form-item label="编码" prop="code"><el-input v-model="dlg.form.code" :disabled="!!dlg.role" class="mono-input" placeholder="如 dba_readonly" /></el-form-item></el-col>
          <el-col :span="14"><el-form-item label="名称" prop="name"><el-input v-model="dlg.form.name" placeholder="如 数据库值班" /></el-form-item></el-col>
        </el-row>
        <el-form-item label="说明"><el-input v-model="dlg.form.description" placeholder="一句话说明适用人员" /></el-form-item>
        <el-form-item label="权限">
          <div class="presets">
            <span class="text-3">快速选择：</span>
            <el-button size="small" @click="preset('view')">只读（全部查看）</el-button>
            <el-button size="small" @click="preset('oncall')">同值班运维</el-button>
            <el-button size="small" @click="preset('none')">清空</el-button>
            <span class="flex-1" /><span class="text-3">已选 {{ dlg.form.permissions.length }} / {{ flat.length }}</span>
          </div>
          <div class="perm-groups">
            <div v-for="g in perms" :key="g.group" class="pg">
              <el-checkbox :model-value="groupState(g) === 'all'" :indeterminate="groupState(g) === 'some'" class="pg-head" @change="(v: any) => toggleGroup(g, !!v)">{{ g.group }}</el-checkbox>
              <el-checkbox-group v-model="dlg.form.permissions" class="pg-items">
                <el-checkbox v-for="i in g.items" :key="i.code" :value="i.code">{{ i.name }}<span class="mono text-3 code">{{ i.code }}</span></el-checkbox>
              </el-checkbox-group>
            </div>
          </div>
          <div v-if="warn" class="warn"><el-icon><WarningFilled /></el-icon>{{ warn }}</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dlg.visible = false">取消</el-button>
        <el-button type="primary" :loading="dlg.saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { Lock, Plus, Select, WarningFilled } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import { createRole, deleteRole, listPermissions, listRoles, patchRole } from '@/api'
import { useUserStore } from '@/stores/user'
import type { PermissionGroup, Role } from '@/types'

const userStore = useUserStore()
const canManage = computed(() => !!userStore.user && userStore.user.tenant_ids == null && userStore.can('role:manage'))
const loading = ref(false)
const roles = ref<Role[]>([])
const perms = ref<PermissionGroup[]>([])

async function load() {
  loading.value = true
  try { [roles.value, perms.value] = await Promise.all([listRoles(), listPermissions()]) } finally { loading.value = false }
}
onMounted(load)
defineExpose({ reload: load })

const ROLE_COLOR: Record<string, string> = { admin: '#1d63f0', ops_lead: '#7a5af8', oncall: '#0e9f8f', viewer: '#8c8c8c' }
const roleColor = (code: string) => ROLE_COLOR[code] ?? '#f08a24'
const isAll = (r: Role) => r.permissions.includes('*')
const has = (r: Role, code: string) => isAll(r) || r.permissions.includes(code)

/** 矩阵行：权限点平铺，模块列合并单元格 */
const flat = computed(() => perms.value.flatMap((g) => g.items.map((i, idx) => ({ ...i, group: g.group, first: idx === 0, size: g.items.length }))))
function spanMethod({ row, columnIndex }: { row: { first: boolean; size: number }; columnIndex: number }) {
  if (columnIndex !== 0) return [1, 1]
  return row.first ? [row.size, 1] : [0, 0]
}

// ---- 新增 / 编辑 ----
const formRef = ref<FormInstance>()
const blank = () => ({ code: '', name: '', description: '', permissions: [] as string[] })
const dlg = reactive({ visible: false, saving: false, role: null as Role | null, form: blank() })
const rules: FormRules = {
  code: [{ required: true, message: '请输入编码' }, { pattern: /^[a-z][a-z0-9_]{1,31}$/, message: '小写字母开头，可含数字和下划线' }],
  name: [{ required: true, message: '请输入名称' }],
}
function open(r?: Role) {
  if (!canManage.value) return
  dlg.role = r ?? null
  dlg.form = r ? { code: r.code, name: r.name, description: r.description, permissions: [...r.permissions] } : blank()
  dlg.visible = true
  setTimeout(() => formRef.value?.clearValidate())
}
const groupState = (g: PermissionGroup) => {
  const n = g.items.filter((i) => dlg.form.permissions.includes(i.code)).length
  return n === 0 ? 'none' : n === g.items.length ? 'all' : 'some'
}
function toggleGroup(g: PermissionGroup, on: boolean) {
  const codes = g.items.map((i) => i.code)
  const rest = dlg.form.permissions.filter((c) => !codes.includes(c))
  dlg.form.permissions = on ? [...rest, ...codes] : rest
}
function preset(kind: 'view' | 'oncall' | 'none') {
  if (kind === 'none') dlg.form.permissions = []
  else if (kind === 'view') dlg.form.permissions = flat.value.filter((p) => p.code.endsWith(':view')).map((p) => p.code)
  else dlg.form.permissions = [...(roles.value.find((r) => r.code === 'oncall')?.permissions ?? [])]
}
/** 常见的“有操作权限但没有查看权限”组合提示 */
const warn = computed(() => {
  const p = new Set(dlg.form.permissions)
  const miss = flat.value.filter((x) => !x.code.endsWith(':view') && p.has(x.code)).map((x) => x.code.split(':')[0])
    .filter((m) => flat.value.some((x) => x.code === `${m}:view`) && !p.has(`${m}:view`))
  const uniq = [...new Set(miss)]
  return uniq.length ? `已选操作权限但未勾选对应的查看权限：${uniq.map((m) => `${m}:view`).join('、')}（页面将不可见）` : ''
})
async function save() {
  if (!canManage.value || dlg.saving) return
  if (!(await formRef.value?.validate().catch(() => false))) return
  if (!canManage.value || dlg.saving) return
  dlg.saving = true
  try {
    const f = dlg.form
    if (dlg.role) await patchRole(dlg.role.id, { name: f.name, description: f.description, permissions: f.permissions })
    else await createRole({ code: f.code, name: f.name, description: f.description, permissions: f.permissions })
    ElMessage.success('已保存，用户下次刷新页面后生效')
    dlg.visible = false
    await load()
    if (dlg.role && dlg.role.id === userStore.user?.role_id) await userStore.fetchMe()
  } finally { dlg.saving = false }
}
async function remove(r: Role) {
  if (!canManage.value) return
  await ElMessageBox.confirm(`删除角色「${r.name}」？`, '删除角色', { type: 'warning', confirmButtonText: '删除' })
  await deleteRole(r.id)
  ElMessage.success('已删除')
  await load()
}
</script>

<style scoped lang="scss">
.table-toolbar { display: flex; align-items: flex-start; gap: 16px; margin-bottom: 14px; .intro { flex: 1; font-size: 12px; line-height: 1.8; b { color: var(--hi-text-2); } } }
.role-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 12px; margin-bottom: 18px; }
.rc { padding: 12px 14px; border-radius: 8px; border: 1px solid var(--hi-border); border-top: 3px solid var(--c); background: #fff;
  .rc-head { display: flex; align-items: center; gap: 6px; .rc-name { font-weight: 600; font-size: 15px; color: var(--c); } .rc-code { margin-left: auto; font-size: 12px; } }
  .rc-desc { margin: 6px 0 10px; font-size: 12px; line-height: 1.6; min-height: 38px; }
  .rc-foot { display: flex; align-items: center; gap: 12px; font-size: 12px; color: var(--hi-text-3); b { color: var(--hi-text-1); font-size: 14px; } }
  .lock { color: var(--hi-text-3); } }
.flex-1 { flex: 1; }
.sub-title { margin: 0 0 8px; font-weight: 600; font-size: 13px; color: var(--hi-text-2); .text-3 { font-weight: 400; font-size: 12px; } }
.matrix { .code { margin-left: 8px; font-size: 11px; } .yes { font-size: 16px; font-weight: 700; } .no { color: var(--hi-border); }
  .col-head { font-weight: 600; } }
.mono-input :deep(input) { font-family: var(--hi-font-mono); }
.presets { display: flex; align-items: center; gap: 6px; width: 100%; margin-bottom: 8px; }
.perm-groups { width: 100%; border: 1px solid var(--hi-border); border-radius: 6px; max-height: 46vh; overflow-y: auto;
  .pg { display: flex; padding: 6px 12px; border-bottom: 1px solid var(--hi-border); &:last-child { border-bottom: none; }
    .pg-head { width: 110px; flex: none; font-weight: 600; }
    .pg-items { display: flex; flex-wrap: wrap; column-gap: 18px; .code { margin-left: 6px; font-size: 11px; } } } }
.warn { display: flex; align-items: center; gap: 6px; margin-top: 8px; font-size: 12px; color: var(--hi-p3, #f57c00); }
</style>
