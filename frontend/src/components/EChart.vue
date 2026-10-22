<template><div ref="el" class="echart" :style="{ height }" /></template>

<script setup lang="ts">
import { BarChart, GaugeChart, LineChart, PieChart } from 'echarts/charts'
import { GridComponent, LegendComponent, MarkLineComponent, TooltipComponent } from 'echarts/components'
import { type ECharts, type EChartsCoreOption, init, use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

// 按需注册，控制打包体积
use([BarChart, LineChart, GaugeChart, PieChart, GridComponent, TooltipComponent, LegendComponent, MarkLineComponent, CanvasRenderer])

const props = withDefaults(defineProps<{ option: EChartsCoreOption; height?: string; theme?: string }>(), { height: '280px', theme: undefined })
const el = ref<HTMLDivElement>()
let chart: ECharts | null = null
let ro: ResizeObserver | null = null

onMounted(() => {
  chart = init(el.value!, props.theme, { renderer: 'canvas' })
  chart.setOption(props.option)
  ro = new ResizeObserver(() => chart?.resize())
  ro.observe(el.value!)
})
watch(() => props.option, (opt) => chart?.setOption(opt, true), { deep: true })
onBeforeUnmount(() => { ro?.disconnect(); chart?.dispose() })
defineExpose({ getInstance: () => chart })
</script>

<style scoped>.echart { width: 100%; }</style>
