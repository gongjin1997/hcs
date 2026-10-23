<template>
  <el-dialog :model-value="visible" title="Excel 批量导入管理字段" width="980px" top="5vh" append-to-body @close="close">
    <el-steps :active="step" finish-status="success" simple class="mb-16">
      <el-step title="下载模板" /><el-step title="上传并预览" /><el-step title="确认导入" />
    </el-steps>

    <div class="intro">
      <div>
        <b>1. 下载模板</b>：按当前筛选（{{ scopeText }}）导出在用资源，已带当前取值。
        <el-button size="small" type="primary" link :icon="Download" :loading="downloading" @click="downloadTpl">下载模板</el-button>
      </div>
      <div class="text-3 rules">
        只导入管理字段：<b>负责人、业务系统、环境、核心资源、允许关机、备注</b>；按“资源 ID”匹配；单元格留空 = 不修改，填“-” = 清空；
        台账同步不会覆盖这些字段。每个变更写入台账变更记录（来源“Excel 导入”）。单次最多 5000 行。
      </div>
    </div>

    <el-upload v-if="!result" drag :auto-upload="false" :show-file-list="false" accept=".xlsx" :on-change="onFile" class="upload">
      <el-icon class="up-icon"><UploadFilled /></el-icon>
      <div><b>2. 把修改好的 Excel 拖到这里</b>，或点击选择文件（.xlsx）</div>
      <div class="text-3">上传后先预览，确认无误再写入</div>
    </el-upload>

    <div v-loading="parsing" element-loading-text="正在校验…">
      <template v-if="result">
        <div class="file-line">
          <el-icon><Document /></el-icon><b>{{ file?.name }}</b>
          <el-button link type="primary" @click="reset">重新选择</el-button>
        </div>
        <div class="stats">
          <div><span>数据行</span><b class="num">{{ result.total }}</b></div>
          <div><span>匹配到资源</span><b class="num">{{ result.matched }}</b></div>
          <div class="chg"><span>将修改</span><b class="num">{{ result.changed }}</b></div>
          <div><span>无变化</span><b class="num">{{ result.unchanged }}</b></div>
          <div :class="{ err: result.error_count }"><span>错误行</span><b class="num">{{ result.error_count }}</b></div>
        </div>

        <el-tabs v-model="tab">
          <el-tab-pane :label="`变更预览（${result.changed}）`" name="items">
            <el-table :data="result.items" size="small" max-height="340" empty-text="没有需要修改的资源">
              <el-table-column label="行" prop="row" width="60" />
              <el-table-column label="资源" min-width="200">
                <template #default="{ row }"><b>{{ row.name }}</b><div class="text-3 mono ellipsis">{{ row.resource_id }}</div></template>
              </el-table-column>
              <el-table-column label="类型 / 租户" width="150"><template #default="{ row }">{{ row.type_name }}<div class="text-3">{{ row.tenant }}</div></template></el-table-column>
              <el-table-column label="修改内容" min-width="380">
                <template #default="{ row }">
                  <div v-for="c in row.changes" :key="c.field" class="change">
                    <span class="lbl">{{ c.label }}</span><del>{{ c.old || '（空）' }}</del><el-icon><Right /></el-icon><ins>{{ c.new || '（清空）' }}</ins>
                  </div>
                </template>
              </el-table-column>
            </el-table>
          </el-tab-pane>
          <el-tab-pane :label="`错误（${result.error_count}）`" name="errors" :disabled="!result.error_count">
            <el-table :data="result.errors" size="small" max-height="340">
              <el-table-column label="行" prop="row" width="60" />
              <el-table-column label="资源 ID" min-width="200"><template #default="{ row }"><span class="mono">{{ row.resource_id ?? '-' }}</span></template></el-table-column>
              <el-table-column label="名称" prop="name" min-width="140" />
              <el-table-column label="问题" min-width="320"><template #default="{ row }"><span class="text-danger">{{ row.message }}</span></template></el-table-column>
            </el-table>
          </el-tab-pane>
        </el-tabs>
      </template>
      <el-alert v-if="parseError" type="error" :closable="false" show-icon :title="parseError" class="mt-12" />
    </div>

    <template #footer>
      <el-checkbox v-if="result?.error_count" v-model="skipErrors" class="skip">跳过 {{ result.error_count }} 个错误行，只导入正确的行</el-checkbox>
      <el-button @click="close">取消</el-button>
      <el-button type="primary" :disabled="!canApply" :loading="applying" @click="apply">
        3. 确认导入<template v-if="result?.changed">（{{ result.changed }} 个资源）</template>
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { Document, Download, Right, UploadFilled } from '@element-plus/icons-vue'
import { ElMessage, type UploadFile } from 'element-plus'
import { computed, ref, watch } from 'vue'
import { downloadImportTemplate, importLedger } from '@/api'
import type { ImportResult } from '@/types'

const props = defineProps<{ visible: boolean; resourceType?: string; category?: string; scopeText: string }>()
const emit = defineEmits<{ 'update:visible': [v: boolean]; imported: [n: number] }>()
const file = ref<File | null>(null)
const result = ref<ImportResult | null>(null)
const parseError = ref('')
const parsing = ref(false)
const applying = ref(false)
const downloading = ref(false)
const skipErrors = ref(false)
const tab = ref('items')
const step = computed(() => (result.value ? 2 : file.value ? 1 : 1))
const canApply = computed(() => !!result.value && result.value.changed > 0 && (!result.value.error_count || skipErrors.value))

function reset() {
  file.value = null
  result.value = null
  parseError.value = ''
  skipErrors.value = false
  tab.value = 'items'
}
watch(() => props.visible, (v) => { if (v) reset() })
const close = () => emit('update:visible', false)

async function downloadTpl() {
  downloading.value = true
  try { await downloadImportTemplate({ resource_type: props.resourceType, category: props.category }) } finally { downloading.value = false }
}
async function onFile(f: UploadFile) {
  if (!f.raw) return
  if (!f.name.toLowerCase().endsWith('.xlsx')) return ElMessage.warning('请上传 .xlsx 文件（可先下载模板）')
  file.value = f.raw
  parseError.value = ''
  parsing.value = true
  try {
    result.value = await importLedger(f.raw, true)
    tab.value = result.value.changed ? 'items' : result.value.error_count ? 'errors' : 'items'
  } catch (e) {
    parseError.value = (e as Error).message
    file.value = null
  } finally { parsing.value = false }
}
async function apply() {
  if (!file.value) return
  applying.value = true
  try {
    const r = await importLedger(file.value, false, skipErrors.value)
    ElMessage.success(`已导入：修改 ${r.applied ?? r.changed} 个资源${r.error_count ? `，跳过 ${r.error_count} 个错误行` : ''}`)
    emit('imported', r.applied ?? r.changed)
    close()
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally { applying.value = false }
}
</script>

<style scoped lang="scss">
.intro { padding: 10px 14px; margin-bottom: 12px; border-radius: 8px; background: var(--hi-bg-soft); font-size: 13px; line-height: 1.8;
  .rules { font-size: 12px; } }
.upload { :deep(.el-upload-dragger) { padding: 26px 0; } .up-icon { font-size: 40px; color: var(--el-color-primary); margin-bottom: 6px; } }
.file-line { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
.stats { display: grid; grid-template-columns: repeat(5, 1fr); gap: 8px; margin-bottom: 6px;
  > div { padding: 8px 12px; border-radius: 8px; background: var(--hi-bg-soft); display: flex; flex-direction: column; gap: 2px;
    span { font-size: 12px; color: var(--hi-text-3); } b { font-size: 20px; } }
  .chg b { color: var(--el-color-primary); } .err b { color: var(--hi-p1); } }
.change { display: flex; align-items: center; gap: 6px; font-size: 12px; line-height: 1.8;
  .lbl { min-width: 56px; color: var(--hi-text-3); } del { color: var(--hi-text-3); } ins { text-decoration: none; color: var(--hi-ok); font-weight: 600; } }
.skip { float: left; margin-top: 6px; }
</style>
