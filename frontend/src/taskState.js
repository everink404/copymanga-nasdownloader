import {ref, onMounted, onBeforeUnmount} from 'vue'
import axios from 'axios'

export const labels = {waiting: '等待中', running: '下载中', completed: '已完成', failed: '失败', stopped: '已停止', interrupted: '已中断'}
export function useTasks() {
  const jobs = ref([]), error = ref('')
  let timer, disposed = false
  async function refresh() {
    try {
      const {data} = await axios.get('/api/tasks', {timeout: 10000})
      if (!disposed) { jobs.value = data.items; error.value = '' }
    } catch (e) { if (!disposed) error.value = '下载状态暂时无法刷新，以下是上次取得的数据' }
  }
  async function poll() { await refresh(); if (!disposed) timer = setTimeout(poll, 2000) }
  onMounted(poll)
  onBeforeUnmount(() => { disposed = true; clearTimeout(timer) })
  return {jobs, error, refresh}
}
