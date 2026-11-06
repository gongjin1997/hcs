<template>
  <el-dialog :model-value="visible" title="用户 Excel 导入" width="min(980px, calc(100vw - 24px))" top="5vh" append-to-body
             :close-on-click-modal="!applying" :close-on-press-escape="!applying" :show-close="!applying" @close="close">
    <el-steps :active="result ? 2 : 1" finish-status="success" simple class="mb-16">
      <el-step title="下载模板" /><el-step title="上传并预览" /><el-step title="确认导入" />
    </el-steps>
    <div class="intro">
      <div>按不可变账号匹配用户；新账号新增，已有账号更新。单次最多 5000 行 / 10 MB。
        <el-button link type="primary" :icon="Download" :loading="downloading" :disabled="!canManage || applying" @click="downloadTpl">下载模板</el-button>
      </div>
      <div class="text-3">支持仅登记人员；新开通登录须提供角色和初始密码。导出不含密码，导入不能重置现有密码。</div>
      <div class="text-3">预览不写入；确认时重新校验，全部通过后一次性导入。有错误请修改文件并重新预览。</div>
    </div>
    <el-upload :disabled="!canManage || applying || parsing" drag :auto-upload="false" :show-file-list="false" accept=".xlsx" :on-change="onFile">
      <el-icon class="up-icon"><UploadFilled /></el-icon>
      <div><b>{{ file ? '重新选择 Excel 文件（.xlsx）' : '拖入或选择 Excel 文件（.xlsx）' }}</b></div>
      <div class="text-3">上传后预览，确认后导入</div>
    </el-upload>
    <div v-loading="parsing" element-loading-text="正在校验…">
      <div v-if="file" class="file-line"><el-icon><Document /></el-icon><b>{{ file.name }}</b></div>
      <template v-if="result">
        <div class="stats">
          <span>数据行 <b class="num">{{ result.total }}</b></span>
          <span>新增 <b class="num">{{ result.created }}</b></span>
          <span>更新 <b class="num">{{ result.updated }}</b></span>
          <span>无变化 <b class="num">{{ result.unchanged }}</b></span>
          <span :class="{ 'text-danger': result.error_count }">错误行 <b class="num">{{ result.error_count }}</b></span>
        </div>
        <el-alert v-if="result.error_count" type="error" :closable="false" show-icon title="存在错误，整批不会导入。请修正文件后重新预览。" class="mb-12" />
        <el-tabs v-model="tab">
          <el-tab-pane label="变更预览" name="items">
            <el-table :data="result.items" size="small" max-height="340" empty-text="没有变更">
              <el-table-column label="行" prop="row" width="60" />
              <el-table-column label="账号" prop="username" min-width="160" />
              <el-table-column label="操作" width="100"><template #default="{ row }">{{ actionLabel(row.action) }}</template></el-table-column>
              <el-table-column label="变更内容" min-width="420"><template #default="{ row }">
                <div v-for="(change, label) in row.changes" :key="label" class="change"><b>{{ label }}</b><del>{{ valueText(change[0]) }}</del><span> → </span><ins>{{ valueText(change[1]) }}</ins></div>
              </template></el-table-column>
            </el-table>
          </el-tab-pane>
          <el-tab-pane :label="`错误（${result.error_count}）`" name="errors" :disabled="!result.error_count">
            <el-table :data="result.errors" size="small" max-height="340">
              <el-table-column label="行" prop="row" width="60" />
              <el-table-column label="账号" min-width="160"><template #default="{ row }">{{ row.username || '-' }}</template></el-table-column>
              <el-table-column label="问题" prop="message" min-width="420" />
            </el-table>
          </el-tab-pane>
        </el-tabs>
      </template>
      <el-alert v-if="parseError" type="error" :closable="false" show-icon :title="parseError" class="mt-12" />
    </div>
    <template #footer>
      <el-button :disabled="applying" @click="close">取消</el-button>
      <el-button type="primary" :disabled="!canApply" :loading="applying" @click="apply">确认导入<template v-if="result">（{{ result.created + result.updated }} 位人员）</template></el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { Document, Download, UploadFilled } from '@element-plus/icons-vue'
import { ElMessage, type UploadFile } from 'element-plus'
import { ref, watch } from 'vue'
import { downloadUserImportTemplate, importUsers } from '@/api'
import { useUserImport } from './useUserImport'

const props = defineProps<{ visible: boolean; canManage: boolean }>()
const emit = defineEmits<{ 'update:visible': [value: boolean]; imported: [] }>()
const importer = useUserImport({ allowed: () => props.visible && props.canManage, importer: importUsers })
const { file, result, parseError, parsing, applying, canApply } = importer
const downloading = ref(false)
const tab = ref('items')
watch(() => props.visible, () => { importer.reset(); tab.value = 'items' })
const close = () => { if (!applying.value) { importer.reset(); emit('update:visible', false) } }
async function downloadTpl() {
  if (!props.canManage || downloading.value || applying.value) return
  downloading.value = true
  try { await downloadUserImportTemplate() }
  catch { /* 请求层提示下载错误。 */ }
  finally { downloading.value = false }
}
async function onFile(selected: UploadFile) {
  if (!selected.raw) return
  await importer.preview(selected.raw)
  tab.value = result.value?.error_count ? 'errors' : 'items'
}
async function apply() {
  const value = await importer.apply()
  if (!value) { if (result.value?.error_count) tab.value = 'errors'; return }
  ElMessage.success(`已导入 ${value.applied} 位人员（新增 ${value.created}，更新 ${value.updated}）`)
  emit('imported')
  close()
}
const actionLabel = (action: string) => ({ create: '新增', update: '更新', unchanged: '无变化' }[action] ?? action)
const valueText = (value: unknown): string => value == null || value === '' ? '（空）'
  : typeof value === 'boolean' ? (value ? '是' : '否') : Array.isArray(value) ? value.map(valueText).join('、') : String(value)
</script>

<style scoped lang="scss">
.intro { padding: 10px 14px; margin-bottom: 12px; border-radius: 8px; background: var(--hi-bg-soft); font-size: 13px; line-height: 1.8; .text-3 { font-size: 12px; } }
.up-icon { font-size: 36px; color: var(--el-color-primary); margin-bottom: 6px; }
.file-line { display: flex; align-items: center; gap: 8px; margin-top: 12px; }
.stats { display: flex; flex-wrap: wrap; gap: 24px; padding: 14px 0; b { margin-left: 6px; } }
.change { overflow-wrap: anywhere; b { margin-right: 8px; font-weight: 500; } del { color: var(--hi-text-3); } ins { text-decoration: none; } }
</style>
