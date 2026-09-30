<template>
  <div>
    <div v-if="!comic">
      <v-row class="mb-2" align="center">
        <v-col cols="12" sm="8">
          <v-text-field v-model="query" label="搜索 CopyManga 漫画" prepend-inner-icon="mdi-magnify"
                        hide-details clearable variant="outlined" @keyup.enter="loadList(0)" />
        </v-col>
        <v-col cols="12" sm="4"><v-btn color="primary" block :loading="listLoading" @click="loadList(0)">搜索</v-btn></v-col>
      </v-row>
      <v-btn-toggle v-if="!query" v-model="rank" color="primary" mandatory class="mb-5" @update:model-value="loadList(0)">
        <v-btn value="day">日榜</v-btn><v-btn value="week">周榜</v-btn>
        <v-btn value="month">月榜</v-btn><v-btn value="total">总榜</v-btn>
      </v-btn-toggle>
      <v-alert v-if="listError" type="error" class="mb-4">{{ listError }} <v-btn variant="text" @click="loadList(offset)">重试</v-btn></v-alert>
      <v-progress-linear v-if="listLoading" indeterminate color="primary" class="mb-4" />
      <v-row v-if="!listLoading && items.length">
        <v-col v-for="item in items" :key="item.path_word" cols="6" sm="4" md="3" lg="2">
          <v-card class="fill-height" border @click="openComic(item.path_word)">
            <v-img :src="item.cover" aspect-ratio="0.75" cover><template #error><div class="pa-4">暂无封面</div></template></v-img>
            <v-card-title class="text-body-1 text-truncate">{{ item.name }}</v-card-title>
            <v-card-subtitle class="pb-3 text-truncate">{{ item.last_chapter || item.authors.join('、') || '查看详情' }}</v-card-subtitle>
          </v-card>
        </v-col>
      </v-row>
      <v-alert v-else-if="!listLoading && !listError" type="info" variant="tonal">暂无结果</v-alert>
      <div v-if="query && !listLoading && !listError" class="d-flex align-center justify-center ga-3 mt-5">
        <v-btn :disabled="offset === 0" @click="loadList(Math.max(0, offset - 20))">上一页</v-btn>
        <span>第 {{ Math.floor(offset / 20) + 1 }} 页</span>
        <v-btn :disabled="items.length < 20 || (total !== null && offset + 20 >= total)" @click="loadList(offset + 20)">下一页</v-btn>
      </div>
    </div>

    <div v-else>
      <v-btn variant="text" prepend-icon="mdi-arrow-left" class="mb-4" @click="comic = null">返回漫画列表</v-btn>
      <v-alert v-if="detailError" type="error" class="mb-4">{{ detailError }} <v-btn variant="text" @click="openComic(comic.path_word)">重试</v-btn></v-alert>
      <v-row>
        <v-col cols="5" sm="3" md="2"><v-img :src="comic.cover" aspect-ratio="0.75" cover rounded="lg" /></v-col>
        <v-col cols="7" sm="9" md="10">
          <h2 class="text-h5 mb-2">{{ comic.name }}</h2>
          <p v-if="comic.authors.length">作者：{{ comic.authors.join('、') }}</p>
          <p v-if="comic.status">状态：{{ comic.status }}</p>
          <p v-if="comic.themes.length">分类：{{ comic.themes.join('、') }}</p>
          <p v-if="comic.last_chapter">最近章节：{{ comic.last_chapter }}</p>
          <p class="mt-3" style="white-space: pre-wrap">{{ comic.brief }}</p>
        </v-col>
      </v-row>

      <v-divider class="my-5" />
      <v-select v-model="group" :items="comic.groups" item-title="name" item-value="path_word"
                label="章节分组" variant="outlined" @update:model-value="changeGroup" />
      <v-alert v-if="chapterError" type="error" class="mb-4">{{ chapterError }} <v-btn variant="text" @click="loadChapters(chapterOffset)">重试</v-btn></v-alert>
      <v-progress-linear v-if="chapterLoading" indeterminate color="primary" />
      <template v-else-if="!chapterError">
        <h3 class="text-h6 mb-2">章节目录</h3>
        <v-list v-if="chapterItems.length" border rounded="lg" class="mb-3" max-height="350" style="overflow-y: auto">
          <v-list-item v-for="chapter in chapterItems" :key="chapter.uuid" :title="chapter.name"
                       :subtitle="chapter.datetime_created || undefined"
                       :active="startChapter?.uuid === chapter.uuid" @click="startChapter = chapter" />
        </v-list>
        <v-alert v-else type="info" variant="tonal">暂无章节</v-alert>
        <div class="d-flex align-center ga-3 my-4">
          <v-btn :disabled="chapterOffset === 0" @click="loadChapters(Math.max(0, chapterOffset - 100))">上一页</v-btn>
          <span>第 {{ Math.floor(chapterOffset / 100) + 1 }} 页</span>
          <v-btn :disabled="chapterItems.length < 100 || (chapterTotal !== null && chapterOffset + 100 >= chapterTotal)"
                 @click="loadChapters(chapterOffset + 100)">下一页</v-btn>
        </div>
      </template>

      <v-divider class="my-5" />
      <h3 class="text-h6 mb-3">加入订阅</h3>
      <v-alert v-if="subscribed" type="info" variant="tonal">此分组已在「我的订阅」中。</v-alert>
      <template v-else>
        <v-text-field v-model="saveName" label="保存名称" variant="outlined" />
        <v-select v-model="mode" label="下载范围" variant="outlined" :items="modes" />
        <p v-if="mode === 'from'" class="mb-3">请在上方目录中点击起始章节（可翻页选择）。当前选择：{{ startChapter?.name || '未选择' }}</p>
        <p v-if="mode === 'future'" class="mb-3">跳过现有章节，只下载以后更新的章节。</p>
        <p v-if="mode === 'all'" class="mb-3">下次运行时从第一章开始下载全部章节。</p>
        <v-alert v-if="saveError" type="error" class="mb-3">{{ saveError }}</v-alert>
        <v-btn color="primary" :loading="saving" :disabled="chapterLoading || !!chapterError || !chapterItems.length || (mode === 'from' && !startChapter)"
               @click="addSubscription">确认加入订阅</v-btn>
      </template>
    </div>
  </div>
</template>

<script setup>
import {computed, inject, onMounted, ref} from 'vue'
import axios from 'axios'

const showMsg = inject('showMsg')
const query = ref('')
const rank = ref('day')
const items = ref([])
const offset = ref(0)
const total = ref(null)
const listLoading = ref(false)
const listError = ref('')
const comic = ref(null)
const detailError = ref('')
const group = ref('default')
const chapterItems = ref([])
const chapterOffset = ref(0)
const chapterTotal = ref(null)
const chapterLoading = ref(false)
const chapterError = ref('')
const subscriptions = ref([])
const saveName = ref('')
const mode = ref('future')
const startChapter = ref(null)
const saving = ref(false)
const saveError = ref('')
const modes = [
  {title: '只下载以后更新', value: 'future'},
  {title: '从头下载全部章节', value: 'all'},
  {title: '从指定章节开始', value: 'from'},
]

const errorText = (error) => error.response?.data?.detail || error.message || '请求失败'
const subscribed = computed(() => subscriptions.value.some(item =>
  item.path_word === comic.value?.path_word && (item.group_word || 'default') === group.value))

async function loadList(nextOffset = 0) {
  listLoading.value = true
  listError.value = ''
  try {
    const response = await axios.get('/api/copymanga/browse', {params: {q: query.value || '', rank: rank.value, offset: nextOffset, limit: 20}})
    items.value = response.data.items
    total.value = response.data.total
    offset.value = nextOffset
  } catch (error) {
    listError.value = errorText(error)
  } finally {
    listLoading.value = false
  }
}

async function loadSubscriptions() {
  try {
    const response = await axios.get('/api/config')
    subscriptions.value = response.data.copymanga || []
  } catch (error) {
    saveError.value = errorText(error)
  }
}

async function openComic(pathWord) {
  detailError.value = ''
  try {
    const response = await axios.get(`/api/copymanga/comics/${encodeURIComponent(pathWord)}`)
    comic.value = response.data
    saveName.value = comic.value.name
    group.value = comic.value.groups[0]?.path_word || 'default'
    mode.value = 'future'
    startChapter.value = null
    await Promise.all([loadChapters(0), loadSubscriptions()])
  } catch (error) {
    detailError.value = errorText(error)
    showMsg(detailError.value, 'error')
  }
}

async function loadChapters(nextOffset = 0) {
  if (!comic.value) return
  chapterLoading.value = true
  chapterError.value = ''
  chapterItems.value = []
  chapterOffset.value = nextOffset
  try {
    const response = await axios.get(`/api/copymanga/comics/${encodeURIComponent(comic.value.path_word)}/groups/${encodeURIComponent(group.value)}/chapters`,
      {params: {offset: nextOffset, limit: 100}})
    chapterItems.value = response.data.items
    chapterTotal.value = response.data.total
  } catch (error) {
    chapterError.value = errorText(error)
  } finally {
    chapterLoading.value = false
  }
}

function changeGroup() {
  startChapter.value = null
  loadChapters(0)
}

async function addSubscription() {
  saving.value = true
  saveError.value = ''
  try {
    const response = await axios.post('/api/copymanga/subscriptions', {
      path_word: comic.value.path_word, group_word: group.value, name: saveName.value,
      mode: mode.value, chapter_uuid: startChapter.value?.uuid || ''
    })
    await loadSubscriptions()
    showMsg(response.data.first_download_chapter
      ? `已加入订阅，下次从「${response.data.first_download_chapter}」开始下载`
      : '已加入订阅，只追踪后续更新')
  } catch (error) {
    saveError.value = errorText(error)
  } finally {
    saving.value = false
  }
}

onMounted(() => loadList(0))
</script>
