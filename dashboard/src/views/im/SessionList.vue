<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { Archive, EllipsisVertical, Pin, Search } from "@lucide/vue";
import { useI18n, useModuleI18n } from "@/i18n/composables";
import { copyToClipboard } from "@/utils/clipboard";
import { askForConfirmation, useConfirmDialog } from "@/utils/confirmDialog";
import { useToast } from "@/utils/toast";
import ImAvatar from "./ImAvatar.vue";
import { errorMessage, type ImMessageType, type ImSession } from "./api";
import { formatListTime, localizeMarkers } from "./format";
import { useImStore } from "./store";

const emit = defineEmits<{ (event: "open", umo: string): void }>();

const { locale } = useI18n();
const { tm, getRaw } = useModuleI18n("features/im");
const store = useImStore();
const toast = useToast();
const confirmDialog = useConfirmDialog();

const scroller = ref<HTMLElement | null>(null);
const query = ref(store.filters.q);
let queryTimer: ReturnType<typeof setTimeout> | null = null;

const typeOptions = computed<{ value: ImMessageType | ""; label: string }[]>(() => [
  { value: "", label: tm("types.all") },
  { value: "GroupMessage", label: tm("types.GroupMessage") },
  { value: "FriendMessage", label: tm("types.FriendMessage") },
]);

const platformItems = computed(() => [
  { title: tm("list.allPlatforms"), value: "" },
  ...store.platforms.map((platform) => ({ title: platform.id, value: platform.id })),
]);

const platformId = computed({
  get: () => store.filters.platformId,
  set: (value: string | null) => store.setFilters({ platformId: value ?? "" }),
});

const hasFilters = computed(
  () => Boolean(store.filters.type || store.filters.platformId || store.filters.q.trim()),
);

watch(query, (value) => {
  if (queryTimer) clearTimeout(queryTimer);
  queryTimer = setTimeout(() => {
    queryTimer = null;
    const q = (value ?? "").trim();
    if (q !== store.filters.q) store.setFilters({ q });
  }, 300);
});

onBeforeUnmount(() => {
  if (queryTimer) clearTimeout(queryTimer);
});

const markerLabels = computed(() => getRaw("segment.other"));

function preview(session: ImSession): string {
  const text = localizeMarkers(session.last_preview, markerLabels.value);
  if (session.message_type === "GroupMessage" && session.last_sender_name && text) {
    return `${session.last_sender_name}: ${text}`;
  }
  return text;
}

function unreadBadge(count: number): string {
  return count > 99 ? "99+" : String(count);
}

function loadMoreIfNeeded() {
  const el = scroller.value;
  if (!el || !store.nextCursor) return;
  if (el.scrollHeight - el.scrollTop - el.clientHeight < 160) void store.loadMoreSessions();
}

// A first page shorter than the viewport gives no scroll event to load the next one.
watch(
  () => store.sessionList.length,
  () => nextTick(loadMoreIfNeeded),
);

function openOnKey(event: KeyboardEvent, session: ImSession) {
  if (event.target !== event.currentTarget) return;
  event.preventDefault();
  emit("open", session.umo);
}

async function runAction(action: () => Promise<unknown>) {
  try {
    await action();
  } catch (error) {
    toast.error(tm("session.actionFailed", { error: errorMessage(error) }));
  }
}

async function copyUmo(session: ImSession) {
  if (await copyToClipboard(session.umo)) toast.success(tm("session.copied"));
  else toast.error(tm("session.copyFailed"));
}

async function removeSession(session: ImSession) {
  const confirmed = await askForConfirmation(
    tm("session.deleteConfirm", { name: session.display_name }),
    confirmDialog,
  );
  if (!confirmed) return;
  await runAction(async () => {
    await store.deleteSession(session.umo);
    toast.success(tm("session.deleted"));
  });
}
</script>

<template>
  <aside class="im-card im-list">
    <div class="im-list__filters">
      <v-text-field
        v-model="query"
        :placeholder="tm('list.search')"
        :aria-label="tm('list.search')"
        density="compact"
        variant="solo-filled"
        flat
        clearable
        hide-details
      >
        <template #prepend-inner>
          <Search :size="16" aria-hidden="true" />
        </template>
      </v-text-field>
      <div class="im-list__types">
        <button
          v-for="option in typeOptions"
          :key="option.value"
          type="button"
          class="im-type"
          :class="{ 'im-type--active': store.filters.type === option.value }"
          :aria-pressed="store.filters.type === option.value"
          @click="store.setFilters({ type: option.value })"
        >
          {{ option.label }}
        </button>
      </div>
      <v-select
        v-if="store.platforms.length > 1"
        v-model="platformId"
        :items="platformItems"
        :aria-label="tm('list.platformFilter')"
        density="compact"
        variant="solo-filled"
        flat
        hide-details
      />
    </div>

    <div ref="scroller" class="im-list__scroll" @scroll.passive="loadMoreIfNeeded">
      <div v-if="store.listLoading && !store.sessionList.length" class="im-list__state">
        <v-progress-circular indeterminate size="24" width="2" />
      </div>
      <div v-else-if="store.listError && !store.sessionList.length" class="im-list__state">
        <span>{{ tm("list.loadFailed") }}</span>
        <v-btn size="small" variant="tonal" @click="store.loadSessions()">
          {{ tm("actions.retry") }}
        </v-btn>
      </div>
      <div v-else-if="!store.sessionList.length" class="im-list__state">
        <template v-if="hasFilters">{{ tm("list.filteredEmpty") }}</template>
        <template v-else>
          <span class="im-list__state-title">{{ tm("list.empty") }}</span>
          <span>{{ tm("list.emptyHint") }}</span>
        </template>
      </div>
      <template v-else>
        <div
          v-for="session in store.sessionList"
          :key="session.umo"
          class="im-session"
          :class="{
            'im-session--active': session.umo === store.currentUmo,
            'im-session--pinned': session.pinned,
          }"
          role="button"
          tabindex="0"
          :aria-current="session.umo === store.currentUmo ? 'true' : undefined"
          @click="emit('open', session.umo)"
          @keydown.enter="openOnKey($event, session)"
          @keydown.space="openOnKey($event, session)"
        >
          <ImAvatar :name="session.display_name" :platform-type="session.platform_type" :size="40" />
          <div class="im-session__main">
            <div class="im-session__line">
              <span class="im-session__name" :title="session.display_name">{{ session.display_name }}</span>
              <Pin
                v-if="session.pinned"
                class="im-session__flag"
                :size="13"
                :aria-label="tm('list.pinned')"
              />
              <Archive
                v-if="session.keep_forever"
                class="im-session__flag"
                :size="13"
                :aria-label="tm('list.keepForever')"
              />
              <span class="im-session__time">
                {{ formatListTime(session.last_time, locale, tm("time.yesterday")) }}
              </span>
            </div>
            <div class="im-session__line">
              <span class="im-session__preview">{{ preview(session) }}</span>
              <span v-if="session.unread_count > 0" class="im-session__badge">
                {{ unreadBadge(session.unread_count) }}
              </span>
            </div>
          </div>
          <v-menu location="bottom end">
            <template #activator="{ props: menuProps }">
              <v-btn
                v-bind="menuProps"
                class="im-session__more"
                icon
                size="x-small"
                variant="text"
                :aria-label="tm('session.menu')"
                @click.stop
                @keydown.stop
              >
                <EllipsisVertical :size="16" aria-hidden="true" />
              </v-btn>
            </template>
            <v-list density="compact" min-width="168">
              <v-list-item
                :title="session.pinned ? tm('session.unpin') : tm('session.pin')"
                @click="runAction(() => store.togglePinned(session))"
              />
              <v-list-item
                :title="session.keep_forever ? tm('session.unkeep') : tm('session.keep')"
                @click="runAction(() => store.toggleKeepForever(session))"
              />
              <v-list-item :title="tm('session.copyUmo')" @click="copyUmo(session)" />
              <v-list-item
                class="text-error"
                :title="tm('session.delete')"
                @click="removeSession(session)"
              />
            </v-list>
          </v-menu>
        </div>
        <div v-if="store.listLoadingMore" class="im-list__state im-list__state--inline">
          <v-progress-circular indeterminate size="20" width="2" />
        </div>
      </template>
    </div>
  </aside>
</template>

<style scoped>
.im-list {
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.im-list__filters {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 12px 8px;
}

.im-list__filters :deep(.v-field) {
  background: var(--im-surface);
  border-radius: var(--astrbot-radius-control);
  box-shadow: none;
  font-size: 0.8rem;
}

.im-list__filters :deep(.v-field__overlay) {
  opacity: 0;
}

.im-list__types {
  display: grid;
  gap: 6px;
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

.im-type {
  background: var(--im-surface);
  border: 0;
  border-radius: 9px;
  color: rgba(var(--v-theme-on-surface), 0.7);
  cursor: pointer;
  font-size: 0.76rem;
  height: 30px;
  overflow: hidden;
  padding: 0 4px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.im-type:hover {
  color: rgb(var(--v-theme-on-surface));
}

.im-type--active {
  background: rgba(var(--v-theme-primary), 0.11);
  color: rgb(var(--v-theme-primary));
  font-weight: 600;
}

.im-list__scroll {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: 0 6px 6px;
}

.im-list__state {
  align-items: center;
  color: var(--im-muted);
  display: flex;
  flex-direction: column;
  font-size: 0.78rem;
  gap: 8px;
  justify-content: center;
  line-height: 1.5;
  padding: 32px 16px;
  text-align: center;
}

.im-list__state--inline {
  padding: 10px;
}

.im-list__state-title {
  color: rgba(var(--v-theme-on-surface), 0.76);
  font-weight: 600;
}

.im-session {
  align-items: center;
  border-radius: 12px;
  cursor: pointer;
  display: grid;
  gap: 10px;
  grid-template-columns: 40px minmax(0, 1fr) 24px;
  min-height: 62px;
  outline: none;
  padding: 8px 4px 8px 8px;
  transition: background-color 0.15s ease;
}

.im-session:hover,
.im-session:focus-visible {
  background: rgba(var(--v-theme-on-surface), 0.045);
}

.im-session--pinned {
  background: rgba(var(--v-theme-on-surface), 0.025);
}

.im-session--active,
.im-session--active:hover {
  background: rgba(var(--v-theme-primary), 0.1);
}

.im-session__main {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}

.im-session__line {
  align-items: center;
  display: flex;
  gap: 4px;
  min-width: 0;
}

.im-session__name {
  flex: 0 1 auto;
  font-size: 0.84rem;
  font-weight: 600;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.im-session__flag {
  color: var(--im-muted);
  flex: 0 0 auto;
}

.im-session__time {
  color: var(--im-muted);
  flex: 0 0 auto;
  font-size: 0.7rem;
  margin-left: auto;
  padding-left: 6px;
}

.im-session__preview {
  color: var(--im-muted);
  flex: 1 1 auto;
  font-size: 0.76rem;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.im-session__badge {
  background: rgb(var(--v-theme-error));
  border-radius: 999px;
  color: rgb(var(--v-theme-on-error));
  flex: 0 0 auto;
  font-size: 0.66rem;
  font-weight: 600;
  line-height: 18px;
  min-width: 18px;
  padding: 0 5px;
  text-align: center;
}

.im-session__more {
  color: var(--im-muted);
  opacity: 0;
  transition: opacity 0.15s ease;
}

.im-session:hover .im-session__more,
.im-session:focus-within .im-session__more,
.im-session__more[aria-expanded="true"] {
  opacity: 1;
}

@media (hover: none) {
  .im-session__more {
    opacity: 1;
  }
}

@media (max-width: 700px) {
  .im-list__filters {
    padding: 10px 8px 6px;
  }

  .im-list__scroll {
    padding-inline: 4px;
  }

  .im-session {
    gap: 8px;
    padding-left: 6px;
  }
}
</style>
