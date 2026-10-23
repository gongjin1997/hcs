<template>
  <el-dialog :model-value="visible" title="台账字典" width="900px" top="5vh" append-to-body @close="emit('update:visible', false)">
    <div v-loading="loading" class="dict">
      <el-alert type="info" :closable="false" show-icon class="mb-12"
                title="统一“业务系统 / 负责人”的取值，避免同一个系统写成多种名字导致统计和告警指派不准；删除字典项不会修改资源上已填写的值。" />
      <div class="cols">
        <div v-for="col in COLS" :key="col.key" class="col">
          <div class="col-head">
            <b>{{ col.label }}</b><span class="text-3">{{ lists[col.key].length }} 项</span>
          </div>
          <div class="items">
            <div v-for="(it, i) in lists[col.key]" :key="i" class="item">
              <el-input v-model="it.name" size="small" :disabled="!editable" maxlength="64" class="nm" :placeholder="col.label" />
              <el-input v-model="it.remark" size="small" :disabled="!editable" maxlength="128" placeholder="备注（可选）" class="rm" />
              <el-tooltip :content="it.used ? `${it.used} 个资源在用` : '没有资源使用'" placement="top">
                <span class="used num" :class="{ zero: !it.used }">{{ it.used ?? 0 }}</span>
              </el-tooltip>
              <el-button v-if="editable" link type="danger" :icon="Delete" @click="lists[col.key].splice(i, 1)" />
            </div>
            <div v-if="!lists[col.key].length" class="text-3 empty">字典为空（不限制取值）</div>
          </div>
          <div v-if="editable" class="add">
            <el-input v-model="adding[col.key]" size="small" :placeholder="`新增${col.label}，回车添加`" @keyup.enter="add(col.key)" />
            <el-button size="small" :icon="Plus" @click="add(col.key)">添加</el-button>
          </div>
          <div v-if="outside[col.key].length" class="outside">
            <div class="text-3">台账中已使用但不在字典里的取值（{{ outside[col.key].length }}）：</div>
            <div class="tags">
              <el-tag v-for="o in outside[col.key]" :key="o.name" size="small" type="warning" effect="plain" :closable="false"
                      :class="{ clickable: editable }" @click="editable && addName(col.key, o.name, o.used)">
                {{ o.name }} · {{ o.used }}<template v-if="editable"> ＋</template>
              </el-tag>
            </div>
          </div>
        </div>
      </div>
      <div class="enforce">
        <el-switch v-model="enforce" :disabled="!editable" />
        <div>
          <b>只能从字典选择</b>
          <div class="text-3">开启后：资源详情编辑时只能从字典下拉选择；Excel 导入时字典外的取值整行报错。已有的字典外取值不受影响，建议先清理再开启。</div>
        </div>
      </div>
      <div v-if="meta" class="text-3 meta">{{ meta }}</div>
    </div>
    <template #footer>
      <span v-if="!editable" class="text-3 foot-tip">只读：修改需要“编辑台账”权限和全部租户的数据权限</span>
      <el-button @click="emit('update:visible', false)">{{ editable ? '取消' : '关闭' }}</el-button>
      <el-button v-if="editable" type="primary" :loading="saving" @click="save">保存</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { Delete, Plus } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'
import { getLedgerDict, saveLedgerDict } from '@/api'
import { useUserStore } from '@/stores/user'
import type { DictItem } from '@/types'
import { fmtTime } from '@/utils/format'

type Key = 'business' | 'owners'
const COLS: { key: Key; label: string }[] = [{ key: 'business', label: '业务系统' }, { key: 'owners', label: '负责人' }]
const props = defineProps<{ visible: boolean }>()
const emit = defineEmits<{ 'update:visible': [v: boolean]; saved: [] }>()
const user = useUserStore()
const editable = computed(() => user.can('ledger:edit') && user.user?.tenant_ids == null)
const loading = ref(false)
const saving = ref(false)
const lists = reactive<Record<Key, DictItem[]>>({ business: [], owners: [] })
const outside = reactive<Record<Key, { name: string; used: number }[]>>({ business: [], owners: [] })
const adding = reactive<Record<Key, string>>({ business: '', owners: '' })
const enforce = ref(false)
const meta = ref('')

watch(() => props.visible, async (v) => {
  if (!v) return
  loading.value = true
  try {
    const d = await getLedgerDict()
    lists.business = d.business.map((x) => ({ ...x, remark: x.remark ?? '' }))
    lists.owners = d.owners.map((x) => ({ ...x, remark: x.remark ?? '' }))
    outside.business = d.business_outside
    outside.owners = d.owners_outside
    enforce.value = d.enforce
    meta.value = d.customized && d.updated_by ? `${d.updated_by} 于 ${fmtTime(d.updated_at, 'YYYY-MM-DD HH:mm')} 修改` : ''
  } finally { loading.value = false }
})

function addName(k: Key, name: string, used = 0) {
  const n = name.trim()
  if (!n) return
  if (lists[k].some((x) => x.name === n)) return ElMessage.info(`“${n}”已在字典中`)
  lists[k].push({ name: n, remark: '', used })
  outside[k] = outside[k].filter((o) => o.name !== n)
}
function add(k: Key) {
  addName(k, adding[k])
  adding[k] = ''
}
async function save() {
  const clean = (l: DictItem[]) => l.filter((x) => x.name.trim()).map((x) => ({ name: x.name.trim(), remark: x.remark?.trim() || null }))
  for (const c of COLS) {
    const names = clean(lists[c.key]).map((x) => x.name)
    const dup = names.find((n, i) => names.indexOf(n) !== i)
    if (dup) return ElMessage.warning(`${c.label}重复：${dup}`)
  }
  saving.value = true
  try {
    await saveLedgerDict({ business: clean(lists.business), owners: clean(lists.owners), enforce: enforce.value })
    ElMessage.success('台账字典已保存')
    emit('saved')
    emit('update:visible', false)
  } finally { saving.value = false }
}
</script>

<style scoped lang="scss">
.cols { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.col { padding: 12px; border: 1px solid var(--hi-border); border-radius: var(--hi-radius);
  .col-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
  .items { max-height: 340px; overflow: auto; display: flex; flex-direction: column; gap: 6px; padding-right: 4px; }
  .item { display: flex; align-items: center; gap: 6px; .nm { width: 130px; flex: none; } .rm { flex: 1; }
    .used { flex: none; min-width: 32px; text-align: center; padding: 0 6px; border-radius: 10px; font-size: 12px; color: var(--el-color-primary); background: var(--el-color-primary-light-9);
      &.zero { color: var(--hi-text-3); background: var(--hi-bg-soft); } } }
  .empty { padding: 12px 0; font-size: 12px; }
  .add { display: flex; gap: 6px; margin-top: 10px; }
  .outside { margin-top: 10px; font-size: 12px; .tags { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; } .clickable { cursor: pointer; } } }
.enforce { display: flex; gap: 12px; align-items: flex-start; margin-top: 14px; padding: 12px 14px; border-radius: 8px; background: var(--hi-bg-soft);
  .text-3 { margin-top: 2px; font-size: 12px; line-height: 1.6; } }
.meta { margin-top: 8px; font-size: 12px; text-align: right; }
.foot-tip { float: left; line-height: 32px; font-size: 12px; }
</style>
