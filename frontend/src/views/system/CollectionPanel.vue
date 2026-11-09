<template>
  <section class="collection-panel">
    <h3>{{ setting.label }}</h3>
    <p class="text-3">QPS：每秒请求数。0.1 QPS 约为每 10 秒 1 次。</p>
    <el-collapse class="qps-help">
      <el-collapse-item title="限流与并发说明" name="qps">
        <ul>
          <li>并发数是同时进行的请求数；QPS 控制发送速度，增加并发不会绕过限流。</li>
          <li>按云平台批准的配额设置，并为重试和其它应用预留余量。页面默认值是本平台初始配置，不是云平台配额。</li>
          <li>当前限流按单个采集客户端、单个云服务生效；不同任务、租户和进程的总请求量会叠加。</li>
          <li>例如 300 次请求、平均 5 QPS，发送时间可估算为约 60 秒，还要计算接口耗时和重试；采集周期应留足时间。</li>
          <li>出现 429 时先降低 QPS，并检查限流等待、任务耗时和失败记录；按实际接口配额逐步调整。</li>
        </ul>
      </el-collapse-item>
    </el-collapse>
    <el-form label-width="180px" :disabled="!setting.editable || disabled || saving" @submit.prevent>
      <el-form-item label="HCS 明细并发数">
        <el-input-number v-model="form.hcs_concurrency" :min="1" :max="16" :disabled="!setting.editable" />
      </el-form-item>
      <div class="qps-grid">
        <el-form-item v-for="service in Object.keys(form.hcs_qps)" :key="service" :label="`${service} QPS`">
          <el-input-number v-model="form.hcs_qps[service]" :min="0.1" :max="1000" :precision="1"
                           :disabled="!setting.editable" controls-position="right" />
        </el-form-item>
      </div>
    </el-form>
    <el-button v-if="setting.editable" type="primary" :loading="saving" :disabled="disabled" @click="save">保存采集配置</el-button>
  </section>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { saveSetting } from '@/api'
import type { SettingItem } from '@/types'

interface Collection { hcs_concurrency: number; hcs_qps: Record<string, number> }
const props = defineProps<{ setting: SettingItem<Collection>; disabled?: boolean }>()
const emit = defineEmits<{ saved: [] }>()
const form = reactive<Collection>({ hcs_concurrency: 4, hcs_qps: {} })
const saving = ref(false)
const dirty = computed(() => form.hcs_concurrency !== props.setting.value.hcs_concurrency
  || Object.keys(form.hcs_qps).some(service => form.hcs_qps[service] !== props.setting.value.hcs_qps[service]))
defineExpose({ busy: saving, dirty })
watch(() => props.setting.value, value => {
  form.hcs_concurrency = value.hcs_concurrency
  form.hcs_qps = { ...value.hcs_qps }
}, { immediate: true })
async function save() {
  if (saving.value || props.disabled || !props.setting.editable) return
  saving.value = true
  try {
    await saveSetting<Collection>('collect', { hcs_concurrency: form.hcs_concurrency, hcs_qps: { ...form.hcs_qps } })
    ElMessage.success('采集配置已保存，下一次任务生效')
    emit('saved')
  } catch { /* 请求层提示错误，保留草稿。 */ }
  finally { saving.value = false }
}
</script>

<style scoped lang="scss">
.collection-panel { padding: 16px; border: 1px solid var(--hi-border); border-radius: var(--hi-radius); background: #fff; }
h3 { margin: 0 0 12px; font-size: 15px; }
.qps-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 0 16px; }
.qps-help { margin: 12px 0 20px; }
.qps-help ul { margin: 0; padding-left: 18px; line-height: 1.9; }
@media (max-width: 767px) { .qps-grid { grid-template-columns: minmax(0, 1fr); } }
</style>
