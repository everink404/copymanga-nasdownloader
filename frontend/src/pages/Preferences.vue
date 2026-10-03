<template><v-tabs v-model="tab" class="mb-4"><v-tab value="system">系统设置</v-tab><v-tab value="schedule">自动检查时间</v-tab></v-tabs><Settings v-if="tab === 'system'"/><Schedule v-else/></template>
<script setup>
import {ref} from 'vue'
import Settings from './Settings.vue'
import Schedule from './Schedule.vue'
const tab = ref('system')
</script>
