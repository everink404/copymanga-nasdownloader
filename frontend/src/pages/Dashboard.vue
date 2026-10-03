<template>
  <div>
    <div class="d-flex ga-3 mb-4 flex-wrap">
      <v-text-field v-model="query" label="搜索订阅漫画" prepend-inner-icon="mdi-magnify" hide-details style="min-width:220px"/>
      <v-select v-model="filter" :items="filters" label="状态" hide-details style="max-width:220px"/>
      <v-btn color="primary" @click="openBrowse">从漫画源添加</v-btn>
    </div>
    <v-alert v-if="error" type="error" class="mb-4">{{ error }}<v-btn @click="load">重试</v-btn></v-alert>
    <v-alert v-if="taskError" type="warning" class="mb-4">{{ taskError }}</v-alert>
    <v-progress-linear v-if="loading" indeterminate/>
    <v-row>
      <v-col v-for="item in visible" :key="key(item)" cols="6" sm="4" md="3" lg="2">
        <v-card border rounded="lg" class="h-100">
          <div role="button" tabindex="0" @click="open(item)" @keydown.enter="open(item)" style="cursor:pointer">
            <v-img :src="item.cover || undefined" :aspect-ratio="0.7" cover class="bg-grey-lighten-3"><template #placeholder><div class="d-flex align-center justify-center h-100"><v-icon size="48">mdi-book-outline</v-icon></div></template><template #error><div class="d-flex align-center justify-center h-100">暂无封面</div></template></v-img>
            <v-card-title class="text-body-1 font-weight-bold">{{ item.name }}</v-card-title>
          </div>
          <v-card-text class="pt-0">
            <v-chip size="small" :color="state(item) === 'failed' ? 'error' : 'primary'">{{ item.paused ? '暂停订阅' : labels[state(item)] || '等待检查' }}</v-chip>
            <div class="mt-2 text-caption">{{ item.downloaded_count == null ? '历史下载数量未统计' : `已记录下载 ${item.downloaded_count} 章` }}</div>
            <div v-if="latest(item)?.chapters_total != null" class="text-caption">本次剩余 {{ Math.max(0, latest(item).chapters_total - latest(item).chapters_done) }} 章</div>
            <div v-if="latest(item)?.status === 'running'" class="text-caption">{{ latest(item).phase }} · {{ latest(item).chapter }}</div>
            <div v-else class="text-caption">进度：{{ item.latest_chapter || '从头开始' }}</div>
          </v-card-text>
          <v-card-actions class="flex-wrap"><v-btn size="small" :disabled="item.paused || busy || ['running','waiting'].includes(state(item))" @click="run(item)">立即检查下载</v-btn><v-btn size="small" @click="openTasks">查看任务</v-btn></v-card-actions>
        </v-card>
      </v-col>
    </v-row>
    <div v-if="!loading && !visible.length" class="text-center pa-12">{{ items.length ? '没有符合条件的订阅' : '暂无订阅，请从漫画源添加漫画' }}</div>
    <v-dialog v-model="dialog" max-width="850" scrollable>
      <v-card v-if="selected" rounded="xl">
        <v-card-title class="d-flex">{{ selected.name }}<v-spacer/><v-btn icon="mdi-close" variant="text" @click="close"/></v-card-title>
        <v-card-text>
          <v-progress-linear v-if="detailLoading" indeterminate class="mb-3"/>
          <v-alert v-if="detailError" type="warning" class="mb-3">{{ detailError }}<v-btn @click="open(selected)">重新获取</v-btn></v-alert>
          <div class="d-flex ga-4 mb-4"><v-img v-if="comic?.cover" :src="comic.cover" width="110" max-width="110" cover/><div><p>{{ comic?.brief || '暂无简介' }}</p><p class="mt-3">订阅分组：{{ selected.group_word || 'default' }}</p><p>上次处理到：{{ selected.latest_chapter || '尚未推进' }}</p></div></div>
          <v-alert v-if="comic?.directory_error" type="warning">{{ comic.directory_error }}</v-alert>
          <v-select v-model="mode" :items="modes" label="修改下载范围" class="mt-4"/>
          <v-alert v-if="mode" type="info" variant="tonal" class="mb-3">保存会重设下载起点；之前的文件仍保留。上次处理位置以前的章节可能是跳过的，不能当作已下载。</v-alert>
          <v-select v-if="mode === 'from'" v-model="start" :items="chapters" item-title="name" item-value="uuid" label="从本页章节开始（可翻页选择）"/>
          <v-btn v-if="mode" :disabled="busy || detailLoading || !chapters.length || (mode === 'from' && !start)" color="primary" @click="saveRange">保存下载范围</v-btn>
          <h3 class="mt-5 mb-2">章节目录（{{ total }}）</h3>
          <div v-for="(chapter, index) in chapters" :key="chapter.uuid" class="d-flex py-2 border-b"><span>{{ chapter.name }}</span><v-spacer/><span class="text-caption">{{ chapterState(index) }}</span></div>
          <div class="d-flex mt-3"><v-btn :disabled="detailLoading || offset === 0" @click="loadChapters(offset - 100)">上一页</v-btn><v-spacer/><v-btn :disabled="detailLoading || offset + chapters.length >= total" @click="loadChapters(offset + 100)">下一页</v-btn></div>
        </v-card-text>
        <v-card-actions class="flex-wrap"><v-btn :disabled="busy" @click="pause">{{ selected.paused ? '恢复自动检查' : '暂停自动检查' }}</v-btn><v-btn :disabled="busy || selected.paused" @click="run(selected)">立即检查下载</v-btn><v-spacer/><v-btn color="error" :disabled="busy" @click="remove">取消订阅</v-btn></v-card-actions>
      </v-card>
    </v-dialog>
  </div>
</template>
<script setup>
import {ref, computed, inject, onMounted, onBeforeUnmount} from 'vue'
import axios from 'axios'
import {useTasks, labels} from '../taskState'
const showMsg = inject('showMsg'), openConfirm = inject('openConfirm'), openBrowse = inject('openBrowse'), openTasks = inject('openTasks')
const {jobs, error: taskError} = useTasks()
const items = ref([]), loading = ref(false), error = ref(''), query = ref(''), filter = ref('all'), busy = ref(false)
const dialog = ref(false), selected = ref(null), comic = ref(null), chapters = ref([]), total = ref(0), offset = ref(0), detailLoading = ref(false), detailError = ref(''), mode = ref(null), start = ref(null)
let generation = 0, disposed = false, timer
const filters = [{title:'全部',value:'all'},{title:'下载中',value:'running'},{title:'等待中',value:'waiting'},{title:'失败或中断',value:'failed'},{title:'暂停订阅',value:'paused'}]
const modes = [{title:'保持现有范围',value:null},{title:'从头下载全部章节',value:'all'},{title:'只下载以后更新',value:'future'},{title:'从指定章节开始',value:'from'}]
const key = item => `${item.path_word}/${item.group_word || 'default'}`
const url = item => `/api/copymanga/subscriptions/${encodeURIComponent(item.path_word)}/${encodeURIComponent(item.group_word || 'default')}`
const latest = item => jobs.value.filter(j => key(j) === key(item)).at(-1)
const state = item => latest(item)?.status
const visible = computed(() => items.value.filter(item => item.name.toLowerCase().includes(query.value.toLowerCase()) && (filter.value === 'all' || (filter.value === 'paused' ? item.paused : filter.value === 'failed' ? ['failed','interrupted'].includes(state(item)) : state(item) === filter.value))))
const err = e => e.response?.data?.detail || '请求失败，请检查网络或运行日志'
async function load() { loading.value = true; try { items.value = (await axios.get('/api/config')).data.copymanga || []; error.value = ''; fillCovers() } catch(e) {error.value=err(e)} finally {loading.value=false} }
async function run(item) { busy.value=true; try {await axios.post(url(item)+'/run'); showMsg('已加入下载队列'); openTasks()} catch(e){showMsg(err(e),'error')} finally{busy.value=false} }
async function open(item) {
  const g = ++generation
  selected.value = item; dialog.value=true; comic.value=null; chapters.value=[]; total.value=0; mode.value=null; start.value=null; detailError.value=''; detailLoading.value=true
  try { const {data} = await axios.get(`/api/copymanga/comics/${encodeURIComponent(item.path_word)}`); if(g!==generation)return; comic.value=data
    if (data.cover && data.cover !== item.cover) { await axios.patch(url(item), {cover:data.cover}); item.cover=data.cover }
    if(g!==generation)return
    await loadChapters(0)
  } catch(e){if(g===generation)detailError.value=err(e)} finally{if(g===generation)detailLoading.value=false}
}
async function loadChapters(page) {
  const g=generation, item=selected.value; detailLoading.value=true; detailError.value=''
  try {const {data}=await axios.get(`/api/copymanga/comics/${encodeURIComponent(item.path_word)}/groups/${encodeURIComponent(item.group_word || 'default')}/chapters`,{params:{offset:page,limit:100}}); if(g!==generation)return; chapters.value=data.items; total.value=data.total; offset.value=page}
  catch(e){if(g===generation){chapters.value=[]; detailError.value=err(e)}} finally{if(g===generation)detailLoading.value=false}
}
function chapterState(index) {if ((selected.value.downloaded_chapters || []).includes(chapters.value[index].name)) return '已下载'; const cursor=chapters.value.findIndex(c=>c.name===selected.value.latest_chapter); return cursor<0 ? '下载情况未核实' : index<=cursor ? '之前已处理（可能跳过）' : '待检查下载'}
async function pause(){busy.value=true; try{await axios.patch(url(selected.value),{paused:!selected.value.paused});selected.value.paused=!selected.value.paused;showMsg(selected.value.paused?'已暂停后续自动检查，当前任务继续':'已恢复自动检查')}catch(e){showMsg(err(e),'error')}finally{busy.value=false}}
async function saveRange(){busy.value=true;try{const {data}=await axios.put(url(selected.value)+'/range',{path_word:selected.value.path_word,group_word:selected.value.group_word || 'default',name:selected.value.name,mode:mode.value,chapter_uuid:start.value || ''});Object.assign(selected.value,data.record);mode.value=null;showMsg('下载范围已更新')}catch(e){showMsg(err(e),'error')}finally{busy.value=false}}
async function remove(){if(!await openConfirm('取消订阅','取消后保留已经下载的文件。确定取消这部漫画的订阅？'))return;busy.value=true;try{await axios.delete(url(selected.value));close();await load()}catch(e){showMsg(err(e),'error')}finally{busy.value=false}}
function close(){dialog.value=false;++generation}
async function fillCovers() {
  for (const item of items.value.filter(i => !i.cover)) {
    if(disposed)return
    try { const {data}=await axios.get(`/api/copymanga/comics/${encodeURIComponent(item.path_word)}`,{timeout:30000}); if(disposed)return
      if(data.cover) {await axios.patch(url(item),{cover:data.cover}); item.cover=data.cover}
    } catch(e) { /* A missing cover must not block subscription management. */ }
  }
}
async function refreshRecords() {
  try { const {data}=await axios.get('/api/config', {timeout:10000}); if(!disposed) {items.value=data.copymanga || []; if(selected.value) {const item=items.value.find(i=>key(i)===key(selected.value)); if(item) Object.assign(selected.value,item)}} } catch(e) {if(!disposed)error.value=err(e)}
  if(!disposed)timer=setTimeout(refreshRecords,5000)
}
onMounted(()=>{load(); timer=setTimeout(refreshRecords,5000)})
onBeforeUnmount(()=>{disposed=true;clearTimeout(timer);++generation})
</script>
