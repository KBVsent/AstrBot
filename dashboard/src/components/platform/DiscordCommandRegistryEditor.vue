<template>
  <!-- 触发按钮（内联在配置表单中） -->
  <v-btn variant="tonal" color="primary" size="small" @click="open">
    <v-icon start size="small">mdi-format-list-checks</v-icon>
    {{ tm('buttonText') }}
    <v-chip v-if="summaryCount" size="x-small" label color="primary" class="ml-2">{{ summaryCount }}</v-chip>
  </v-btn>

  <!-- 主编辑器：居中大弹窗 -->
  <v-dialog v-model="dialog" max-width="1160">
    <v-card class="dcr-root d-flex flex-column" rounded="lg" style="overflow: hidden">
      <!-- 顶部栏 -->
      <div class="dcr-header d-flex align-center px-5 py-3">
        <v-icon color="primary" class="mr-2">mdi-discord</v-icon>
        <span class="text-h4 font-weight-medium">{{ tm('dialogTitle') }}</span>
        <v-chip size="small" label color="primary" variant="tonal" class="ml-3">
          {{ tm('counts', { enabled: enabledCount, total: editModel.length }) }}
        </v-chip>
        <v-spacer></v-spacer>
        <v-btn variant="text" class="mr-2" @click="cancel">{{ tm('cancel') }}</v-btn>
        <v-btn color="primary" variant="flat" :disabled="errors.length > 0" @click="apply">
          <v-icon start>mdi-check</v-icon>{{ tm('apply') }}
        </v-btn>
      </div>
      <v-divider></v-divider>

      <!-- 横幅 -->
      <div class="px-5">
        <v-alert v-if="parseError" type="error" variant="tonal" density="compact" class="mt-3" closable
          @click:close="parseError = ''">{{ tm('parseError') }}: {{ parseError }}</v-alert>
        <v-alert v-if="errors.length" type="warning" variant="tonal" density="compact" class="mt-3">
          <div v-for="(e, i) in errors" :key="i" class="text-caption">• {{ e }}</div>
        </v-alert>
      </div>

      <!-- 主体：左列表 右详情 -->
      <div class="dcr-body d-flex flex-grow-1">
        <!-- 左侧 -->
        <div class="dcr-sidebar d-flex flex-column">
          <div class="pa-3">
            <v-text-field v-model="search" :placeholder="tm('search')" prepend-inner-icon="mdi-magnify"
              density="compact" variant="solo-filled" flat hide-details clearable rounded="lg"></v-text-field>
            <div class="d-flex flex-wrap ga-2 mt-3">
              <v-btn size="small" variant="tonal" color="primary" @click="addCommand">
                <v-icon start size="small">mdi-plus</v-icon>{{ tm('addCommand') }}
              </v-btn>
              <v-btn size="small" variant="tonal" @click="openAvailable">
                <v-icon start size="small">mdi-playlist-plus</v-icon>{{ tm('addFromAvailable') }}
              </v-btn>
            </div>
          </div>
          <v-divider></v-divider>
          <div class="dcr-list flex-grow-1">
            <v-list density="compact" bg-color="transparent" class="py-1">
              <v-list-item v-for="(cmd, idx) in filteredCommands" :key="cmd._id" :active="cmd._id === selectedId"
                color="primary" rounded="lg" class="mx-2 my-1" @click="selectedId = cmd._id">
                <template v-slot:prepend>
                  <span class="dcr-dot mr-3" :class="cmd.enabled ? 'dcr-dot--on' : 'dcr-dot--off'"></span>
                </template>
                <v-list-item-title class="font-weight-medium">/{{ cmd.slash_name || cmd.key || '?' }}</v-list-item-title>
                <v-list-item-subtitle v-if="cmd.slash_name !== cmd.key" class="text-caption">{{ cmd.key }}</v-list-item-subtitle>
                <template v-slot:append>
                  <v-btn icon="mdi-delete-outline" size="x-small" variant="text" color="error"
                    @click.stop="deleteCommand(idx)"></v-btn>
                </template>
              </v-list-item>
              <div v-if="!filteredCommands.length" class="text-center text-disabled text-caption py-6">
                {{ tm('noCommands') }}
              </div>
            </v-list>
          </div>
        </div>

        <v-divider vertical></v-divider>

        <!-- 右侧 -->
        <div class="dcr-detail flex-grow-1">
          <div v-if="!selected" class="d-flex flex-column align-center justify-center text-disabled" style="height: 100%">
            <v-icon size="48" class="mb-2">mdi-gesture-tap</v-icon>
            <div>{{ tm('selectHint') }}</div>
          </div>

          <div v-else class="dcr-form pa-6">
            <div class="d-flex align-center mb-4">
              <div class="text-h5 font-weight-medium">/{{ selected.slash_name || selected.key }}</div>
              <v-spacer></v-spacer>
              <v-switch v-model="selected.enabled" color="success" :label="tm('enabled')" hide-details inset
                density="compact"></v-switch>
            </div>

            <div class="d-flex ga-4 flex-wrap">
              <v-text-field v-model="selected.key" :label="tm('commandKey')" :hint="tm('commandKeyHint')"
                persistent-hint density="comfortable" variant="outlined" class="dcr-field"
                :error-messages="selected.key ? [] : [tm('keyRequired')]"></v-text-field>
              <v-text-field v-model="selected.slash_name" :label="tm('slashName')" density="comfortable"
                variant="outlined" class="dcr-field" :error-messages="slashNameErrors(selected)"></v-text-field>
            </div>

            <v-text-field v-model="selected.description" :label="tm('description')" density="comfortable"
              variant="outlined" counter="100" maxlength="100" class="mt-1"></v-text-field>

            <!-- 多语言注释 -->
            <v-card variant="flat" class="dcr-section mt-2">
              <div class="d-flex align-center pa-3 pb-2">
                <v-icon size="small" class="mr-2">mdi-translate</v-icon>
                <span class="text-subtitle-2 font-weight-medium">{{ tm('localizations') }}</span>
                <v-spacer></v-spacer>
                <v-btn size="x-small" variant="tonal" color="primary" @click="addLoc(selected.locs)">
                  <v-icon start size="x-small">mdi-plus</v-icon>{{ tm('addLocalization') }}
                </v-btn>
              </div>
              <div class="px-3 pb-3">
                <div v-if="!selected.locs.length" class="text-caption text-disabled">{{ tm('noLocalizations') }}</div>
                <div v-for="(loc, li) in selected.locs" :key="li" class="d-flex ga-2 mb-2 align-center">
                  <v-select v-model="loc.locale" :items="VALID_LOCALES" :label="tm('locale')" density="compact"
                    variant="outlined" hide-details style="max-width: 150px"></v-select>
                  <v-text-field v-model="loc.text" :label="tm('text')" density="compact" variant="outlined"
                    hide-details maxlength="100"></v-text-field>
                  <v-btn icon="mdi-close" size="x-small" variant="text" @click="selected.locs.splice(li, 1)"></v-btn>
                </div>
              </div>
            </v-card>

            <!-- 参数 -->
            <v-card variant="flat" class="dcr-section mt-3">
              <div class="d-flex align-center pa-3 pb-2">
                <v-icon size="small" class="mr-2">mdi-tune-variant</v-icon>
                <span class="text-subtitle-2 font-weight-medium">{{ tm('options') }}</span>
                <span class="text-caption text-disabled ml-2">{{ tm('optionsHint') }}</span>
                <v-spacer></v-spacer>
                <v-btn size="x-small" variant="tonal" color="primary" @click="addOption(selected)">
                  <v-icon start size="x-small">mdi-plus</v-icon>{{ tm('addOption') }}
                </v-btn>
              </div>
              <div class="px-3 pb-3">
                <div v-if="!selected.options.length" class="text-caption text-disabled">{{ tm('noOptions') }}</div>
                <v-card v-for="(opt, oi) in selected.options" :key="oi" variant="outlined" class="pa-3 mb-2" rounded="lg">
                  <div class="d-flex ga-2 align-center">
                    <v-chip size="x-small" label color="primary" variant="tonal">#{{ oi + 1 }}</v-chip>
                    <v-text-field v-model="opt.name" :label="tm('optionName')" density="compact" variant="outlined"
                      hide-details style="max-width: 200px" :error-messages="optionNameErrors(selected, oi)"></v-text-field>
                    <v-text-field v-model="opt.description" :label="tm('description')" density="compact"
                      variant="outlined" hide-details maxlength="100"></v-text-field>
                    <v-switch v-model="opt.required" color="primary" :label="tm('required')" hide-details inset
                      density="compact" class="flex-grow-0"></v-switch>
                    <v-btn icon="mdi-arrow-up" size="x-small" variant="text" :disabled="oi === 0"
                      @click="moveOption(selected, oi, -1)"></v-btn>
                    <v-btn icon="mdi-arrow-down" size="x-small" variant="text"
                      :disabled="oi === selected.options.length - 1" @click="moveOption(selected, oi, 1)"></v-btn>
                    <v-btn icon="mdi-delete-outline" size="x-small" variant="text" color="error"
                      @click="selected.options.splice(oi, 1)"></v-btn>
                  </div>
                  <div class="d-flex align-center mt-2 mb-1">
                    <span class="text-caption text-disabled">{{ tm('optionLocalizations') }}</span>
                    <v-btn size="x-small" variant="text" color="primary" @click="addLoc(opt.locs)">
                      <v-icon start size="x-small">mdi-plus</v-icon>{{ tm('addLocalization') }}
                    </v-btn>
                  </div>
                  <div v-for="(loc, li) in opt.locs" :key="li" class="d-flex ga-2 mb-1 align-center">
                    <v-select v-model="loc.locale" :items="VALID_LOCALES" :label="tm('locale')" density="compact"
                      variant="outlined" hide-details style="max-width: 140px"></v-select>
                    <v-text-field v-model="loc.text" :label="tm('text')" density="compact" variant="outlined"
                      hide-details maxlength="100"></v-text-field>
                    <v-btn icon="mdi-close" size="x-small" variant="text" @click="opt.locs.splice(li, 1)"></v-btn>
                  </div>
                </v-card>
              </div>
            </v-card>
          </div>
        </div>
      </div>
    </v-card>
  </v-dialog>

  <!-- 从可用指令添加 -->
  <v-dialog v-model="availableDialog" max-width="600">
    <v-card rounded="lg">
      <div class="d-flex align-center py-4 px-5">
        <span class="text-h5 font-weight-medium">{{ tm('availableTitle') }}</span>
        <v-spacer></v-spacer>
        <v-btn size="small" variant="tonal" color="primary" :disabled="!missingCount || discovering"
          @click="addAllMissing">
          <v-icon start size="small">mdi-playlist-check</v-icon>{{ tm('addAllMissing', { count: missingCount }) }}
        </v-btn>
      </div>
      <v-divider></v-divider>
      <div class="pa-4">
        <v-text-field v-model="availableSearch" :placeholder="tm('search')" prepend-inner-icon="mdi-magnify"
          density="compact" variant="solo-filled" flat hide-details clearable rounded="lg" class="mb-3"></v-text-field>
        <div v-if="discovering" class="text-center py-8">
          <v-progress-circular indeterminate color="primary"></v-progress-circular>
        </div>
        <template v-else>
          <div class="text-caption text-disabled mb-2">{{ tm('availableHint') }}</div>
          <v-list density="compact" class="dcr-available-list" bg-color="transparent">
            <v-list-item v-for="item in availableList" :key="item.key" rounded="lg" class="mb-1"
              :disabled="item.present" @click="!item.present && addFromAvailable(item.key)">
              <v-list-item-title class="font-weight-medium">/{{ item.key }}</v-list-item-title>
              <v-list-item-subtitle class="text-caption">{{ item.schema.description }}</v-list-item-subtitle>
              <template v-slot:append>
                <v-chip v-if="item.present" size="x-small" label color="grey">{{ tm('alreadyAdded') }}</v-chip>
                <v-icon v-else color="primary">mdi-plus-circle</v-icon>
              </template>
            </v-list-item>
            <div v-if="!availableList.length" class="text-center text-disabled text-caption py-6">
              {{ tm('discoverEmpty') }}
            </div>
          </v-list>
        </template>
      </div>
      <v-divider></v-divider>
      <v-card-actions class="px-4 py-3">
        <v-spacer></v-spacer>
        <v-btn variant="text" @click="availableDialog = false">{{ tm('close') }}</v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>

  <v-snackbar v-model="snackbar" :timeout="3000" color="primary" location="top">{{ snackbarText }}</v-snackbar>
</template>

<script setup>
import { ref, computed, watch } from 'vue'
import axios from 'axios'
import { useModuleI18n } from '@/i18n/composables'

const { tm } = useModuleI18n('features/discord-registry')

// Discord 合法 locale 码（与适配器 _DISCORD_VALID_LOCALE_SET / pycord valid_locales 对齐）
const VALID_LOCALES = [
  'id', 'da', 'de', 'en-GB', 'en-US', 'es-ES', 'es-419', 'fr', 'hr', 'it', 'lt', 'hu',
  'nl', 'no', 'pl', 'pt-BR', 'ro', 'fi', 'sv-SE', 'vi', 'tr', 'cs', 'el', 'bg', 'ru',
  'uk', 'hi', 'th', 'zh-CN', 'ja', 'zh-TW', 'ko',
]
const SLASH_RE = /^[a-z0-9_-]{1,32}$/

const props = defineProps({
  modelValue: { type: String, default: '' },
  platformId: { type: String, default: '' },
})
const emit = defineEmits(['update:modelValue'])

const dialog = ref(false)
const parseError = ref('')
const editModel = ref([])
const selectedId = ref(null)
const search = ref('')
const snackbar = ref(false)
const snackbarText = ref('')

let _uid = 0
const nextId = () => `c${_uid++}`

function locsObjToArr(obj) {
  if (!obj || typeof obj !== 'object') return []
  return Object.entries(obj).map(([locale, text]) => ({ locale, text: String(text ?? '') }))
}
function locsArrToObj(arr) {
  const o = {}
  for (const { locale, text } of arr || []) {
    if (locale && String(text).trim()) o[locale] = String(text)
  }
  return o
}

function entryToModel(key, e) {
  e = e && typeof e === 'object' ? e : {}
  return {
    _id: nextId(),
    key,
    enabled: e.enabled !== false,
    slash_name: e.slash_name || key,
    description: e.description || '',
    locs: locsObjToArr(e.description_localizations),
    options: Array.isArray(e.options)
      ? e.options.filter((o) => o && typeof o === 'object').map((o) => ({
          name: o.name || '',
          description: o.description || '',
          required: !!o.required,
          locs: locsObjToArr(o.description_localizations),
        }))
      : [],
  }
}

function modelToSchema() {
  const out = {}
  for (const c of editModel.value) {
    const key = String(c.key || '').trim()
    if (!key) continue
    out[key] = {
      enabled: c.enabled !== false,
      slash_name: String(c.slash_name || key).trim(),
      description: c.description || '',
      description_localizations: locsArrToObj(c.locs),
      options: c.options.map((o) => ({
        name: String(o.name || '').trim(),
        description: o.description || '',
        description_localizations: locsArrToObj(o.locs),
        required: !!o.required,
      })),
    }
  }
  return out
}

const summaryCount = computed(() => {
  const raw = (props.modelValue || '').trim()
  if (!raw) return 0
  try {
    const o = JSON.parse(raw)
    return o && typeof o === 'object' ? Object.keys(o).length : 0
  } catch {
    return 0
  }
})

function load() {
  parseError.value = ''
  let obj = {}
  const raw = (props.modelValue || '').trim()
  if (raw) {
    try {
      const parsed = JSON.parse(raw)
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) obj = parsed
      else parseError.value = 'not a JSON object'
    } catch (e) {
      parseError.value = String(e.message || e)
    }
  }
  editModel.value = Object.entries(obj).map(([k, v]) => entryToModel(k, v))
  selectedId.value = editModel.value.length ? editModel.value[0]._id : null
}

function open() {
  load()
  dialog.value = true
}

const selected = computed(() => editModel.value.find((c) => c._id === selectedId.value) || null)
const enabledCount = computed(() => editModel.value.filter((c) => c.enabled !== false).length)

const filteredCommands = computed(() => {
  const q = (search.value || '').toLowerCase().trim()
  if (!q) return editModel.value
  return editModel.value.filter(
    (c) => (c.key || '').toLowerCase().includes(q) || (c.slash_name || '').toLowerCase().includes(q),
  )
})

function slashNameErrors(c) {
  const sn = String(c.slash_name || '').trim()
  if (!sn) return [tm('slashRequired')]
  if (!SLASH_RE.test(sn)) return [tm('invalidSlashName')]
  return []
}
function optionNameErrors(c, oi) {
  const name = String(c.options[oi].name || '').trim()
  if (!name) return [tm('optionNameRequired')]
  if (!SLASH_RE.test(name)) return [tm('invalidOptionName')]
  if (c.options.findIndex((o) => String(o.name).trim() === name) !== oi) return [tm('duplicateOption')]
  return []
}

const errors = computed(() => {
  const errs = []
  const slashSeen = new Map()
  const keySeen = new Set()
  for (const c of editModel.value) {
    const key = String(c.key || '').trim()
    if (!key) {
      errs.push(tm('keyRequired'))
      continue
    }
    if (keySeen.has(key)) errs.push(tm('cmdDuplicateKey', { cmd: key }))
    keySeen.add(key)
    const sn = String(c.slash_name || '').trim()
    if (!sn || !SLASH_RE.test(sn)) {
      errs.push(tm('cmdInvalidSlash', { cmd: key }))
    } else {
      if (slashSeen.has(sn)) errs.push(tm('cmdDuplicateSlash', { cmd: key, slash: sn }))
      slashSeen.set(sn, true)
    }
    const onames = new Set()
    let sawOptional = false
    for (const o of c.options) {
      const on = String(o.name || '').trim()
      if (!on || !SLASH_RE.test(on)) errs.push(tm('cmdInvalidOption', { cmd: key }))
      else if (onames.has(on)) errs.push(tm('cmdDuplicateOption', { cmd: key, opt: on }))
      onames.add(on)
      if (o.required && sawOptional) errs.push(tm('cmdRequiredAfterOptional', { cmd: key, opt: on }))
      if (!o.required) sawOptional = true
    }
  }
  return [...new Set(errs)]
})

function addCommand() {
  const keys = new Set(editModel.value.map((c) => c.key))
  let key = 'new_command'
  let n = 2
  while (keys.has(key)) key = `new_command_${n++}`
  const m = entryToModel(key, { description: '' })
  m.slash_name = key
  editModel.value.push(m)
  selectedId.value = m._id
}
function deleteCommand(idx) {
  const removed = editModel.value.splice(idx, 1)[0]
  if (removed && removed._id === selectedId.value) {
    selectedId.value = editModel.value.length ? editModel.value[0]._id : null
  }
}
function addLoc(arr) {
  const used = new Set(arr.map((l) => l.locale))
  const next = VALID_LOCALES.find((l) => !used.has(l)) || 'en-US'
  arr.push({ locale: next, text: '' })
}
function addOption(c) {
  c.options.push({ name: `arg${c.options.length}`, description: '', required: false, locs: [] })
}
function moveOption(c, idx, dir) {
  const j = idx + dir
  if (j < 0 || j >= c.options.length) return
  const [it] = c.options.splice(idx, 1)
  c.options.splice(j, 0, it)
}

// discover / available
const availableSchemas = ref({})
const discovering = ref(false)
const availableDialog = ref(false)
const availableSearch = ref('')

async function fetchAvailable() {
  discovering.value = true
  try {
    const res = await axios.get('/api/config/platform/discover-commands', {
      params: props.platformId ? { platform_id: props.platformId } : {},
    })
    availableSchemas.value = res.data?.data?.schemas || {}
    return availableSchemas.value
  } catch (e) {
    notify(tm('discoverFailed') + ': ' + (e.message || e))
    return {}
  } finally {
    discovering.value = false
  }
}

async function openAvailable() {
  availableDialog.value = true
  await fetchAvailable()
}

// 列出全部可用指令，已在表中的标记为 present（不隐藏，避免“点了啥也没有”）
const availableList = computed(() => {
  const present = new Set(editModel.value.map((c) => c.key))
  const q = (availableSearch.value || '').toLowerCase().trim()
  return Object.entries(availableSchemas.value)
    .filter(([k]) => !q || k.toLowerCase().includes(q))
    .map(([key, schema]) => ({ key, schema, present: present.has(key) }))
})

const missingCount = computed(() => availableList.value.filter((i) => !i.present).length)

function addFromAvailable(key) {
  if (editModel.value.some((c) => c.key === key)) return
  const m = entryToModel(key, availableSchemas.value[key])
  editModel.value.push(m)
  selectedId.value = m._id
  notify(tm('addedOne', { cmd: key }))
}

// 把当前可用列表里"表中还没有的"一次性全部加入（操作已拉取的最新列表）
function addAllMissing() {
  const present = new Set(editModel.value.map((c) => c.key))
  let added = 0
  let last = null
  for (const [key, schema] of Object.entries(availableSchemas.value)) {
    if (!present.has(key)) {
      last = entryToModel(key, schema)
      editModel.value.push(last)
      added++
    }
  }
  if (last) selectedId.value = last._id
  notify(added ? tm('addedAll', { count: added }) : tm('syncedNone'))
}

function notify(msg) {
  snackbarText.value = msg
  snackbar.value = true
}

function apply() {
  if (errors.value.length) return
  emit('update:modelValue', JSON.stringify(modelToSchema(), null, 2))
  dialog.value = false
}
function cancel() {
  dialog.value = false
}

watch(() => props.modelValue, () => {
  if (!dialog.value) parseError.value = ''
})
</script>

<style scoped>
.dcr-root {
  height: 86vh;
  max-height: 860px;
  background: rgb(var(--v-theme-containerBg));
}
.dcr-header {
  background: rgb(var(--v-theme-surface));
  flex: 0 0 auto;
}
.dcr-body {
  min-height: 0;
  overflow: hidden;
}
.dcr-sidebar {
  width: 320px;
  flex: 0 0 320px;
  background: rgb(var(--v-theme-surface));
  min-height: 0;
}
.dcr-list {
  overflow-y: auto;
  min-height: 0;
}
.dcr-detail {
  min-height: 0;
  overflow-y: auto;
  background: rgb(var(--v-theme-containerBg));
}
.dcr-form {
  max-width: 880px;
}
.dcr-field {
  flex: 1 1 280px;
}
.dcr-section {
  background: rgb(var(--v-theme-surface));
  border: 1px solid rgba(var(--v-theme-borderLight), 0.6);
  border-radius: 12px;
}
.dcr-available-list {
  max-height: 360px;
  overflow-y: auto;
}
.dcr-dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  display: inline-block;
}
.dcr-dot--on {
  background: rgb(var(--v-theme-success));
}
.dcr-dot--off {
  background: rgba(var(--v-theme-on-surface-variant), 0.35);
  border: 1px solid rgba(var(--v-theme-on-surface-variant), 0.4);
}
</style>
