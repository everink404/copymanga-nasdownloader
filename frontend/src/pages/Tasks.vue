<template>
  <div>
    <v-alert v-if="error" type="warning" class="mb-4">{{ error }}</v-alert>
    <v-tabs v-model="tab" class="mb-4"><v-tab value="active">当前队列 ({{ active.length }})</v-tab><v-tab value="history">任务历史</v-tab><v-tab value="logs">运行日志</v-tab></v-tabs>
    <Logs v-if="tab === 'logs'"/>
    <template v-else>
    <v-alert v-if="!visible.length" type="info" variant="tonal">{{ tab === 'active' ? '当前没有下载任务，可以从订阅页运行一部漫画。' : '暂无历史记录。' }}</v-alert>
    <v-card v-for="job in visible" :key="job.id" class="mb-4" border rounded="xl">
      <v-card-title class="d-flex align-center">{{ job.name }}<v-spacer/><v-chip :color="job.status === 'failed' ? 'error' : 'primary'">{{ labels[job.status] }}</v-chip></v-card-title>
      <v-card-text>
        <div class="mb-2">{{ job.phase }}<span v-if="job.chapter"> · {{ job.chapter }}</span></div>
        <div v-if="job.chapters_total != null">本次完成 {{ job.chapters_done }} / {{ job.chapters_total }} 章</div>
        <div v-if="job.images_total != null">当前章节图片 {{ job.images_done }} / {{ job.images_total }}</div>
        <v-progress-linear v-if="job.status === 'running'" class="my-3" color="primary" height="8" rounded :indeterminate="job.phase !== '下载图片' || !job.images_total" :model-value="job.images_total ? job.images_done / job.images_total * 100 : 0"/>
        <div v-if="job.bytes_downloaded">本次传输 {{ (job.bytes_downloaded / 1048576).toFixed(1) }} MB<span v-if="job.speed && job.phase === '下载图片'"> · 最近一张 {{ (job.speed / 1024).toFixed(0) }} KB/s</span></div>
        <v-alert v-if="job.error" type="error" variant="tonal" class="my-3">{{ job.error }}</v-alert>
        <div v-if="job.stop_requested" class="text-warning">将在当前章节完成后停止</div>
        <div class="text-caption mt-2">开始：{{ date(job.started_at) }} · 结束：{{ date(job.finished_at) }}</div>
        <div v-if="job.cbz_path" class="text-break mt-2">最近保存：{{ job.cbz_path }}</div>
        <v-expansion-panels v-if="job.events?.length" class="mt-3"><v-expansion-panel title="查看任务记录"><v-expansion-panel-text><div v-for="(event, i) in job.events" :key="i">{{ date(event.time) }} · {{ event.text }}</div></v-expansion-panel-text></v-expansion-panel></v-expansion-panels>
      </v-card-text>
      <v-card-actions><v-btn v-if="['running', 'waiting'].includes(job.status)" :disabled="job.stop_requested || busy" @click="action(job, 'stop')">{{ job.status === 'waiting' ? '取消排队' : '完成当前章节后停止' }}</v-btn><v-btn v-if="['failed', 'interrupted', 'stopped'].includes(job.status)" :disabled="busy" color="primary" @click="action(job, 'retry')">重新检查并补下载</v-btn></v-card-actions>
    </v-card>
    </template>
  </div>
</template>
<script setup>
import {computed, ref, inject} from 'vue'
import axios from 'axios'
import Logs from './Logs.vue'
import {useTasks, labels} from '../taskState'
const {jobs, error, refresh} = useTasks(), tab = ref('active'), busy = ref(false), showMsg = inject('showMsg')
const active = computed(() => jobs.value.filter(j => ['running', 'waiting'].includes(j.status)))
const visible = computed(() => tab.value === 'active' ? active.value : jobs.value.filter(j => !['running', 'waiting'].includes(j.status)).slice().reverse())
const date = value => value ? new Date(value).toLocaleString() : '—'
async function action(job, operation) {
  busy.value = true
  try { await axios.post(`/api/tasks/${job.id}/${operation}`); await refresh() }
  catch (e) { showMsg(e.response?.data?.detail || '操作失败', 'error') }
  finally { busy.value = false }
}
</script>
