<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { Archive, ArrowDown, ArrowLeft, MessagesSquare, Send } from "@lucide/vue";
import { useI18n, useModuleI18n } from "@/i18n/composables";
import { isComposingEnter } from "@/utils/imeInput.mjs";
import ImAvatar from "./ImAvatar.vue";
import MessageItem from "./MessageItem.vue";
import { formatSeparator } from "./format";
import { useImStore, type ChatMessage } from "./store";

const emit = defineEmits<{ (event: "back"): void }>();

const { locale } = useI18n();
const { tm } = useModuleI18n("features/im");
const store = useImStore();

const scroller = ref<HTMLElement | null>(null);
const content = ref<HTMLElement | null>(null);
const composer = ref<HTMLElement | null>(null);
const atBottom = ref(true);
const newCount = ref(0);
const draft = ref("");
const previewUrl = ref("");
const previewOpen = ref(false);
// Message whose full time is shown; revealed by a tap so touch screens get it too.
const timeKey = ref<string | null>(null);
const drafts = new Map<string, string>();
// Scroll handling waits until the first page has been placed at the bottom.
let positioned = false;
let seenMaxId = 0;
let resizeObserver: ResizeObserver | null = null;
let lastScrollHeight = 0;
let lastClientHeight = 0;
let composing = false;
let lastCompositionEndAt: number | null = null;

const session = computed(() => store.currentSession);
const isGroupLike = computed(() => session.value?.message_type !== "FriendMessage");

type Row =
  | { kind: "time"; key: string; label: string }
  | { kind: "message"; key: string; message: ChatMessage; showSender: boolean };

const rows = computed<Row[]>(() => {
  const result: Row[] = [];
  const yesterday = tm("time.yesterday");
  let previous: ChatMessage | null = null;
  for (const message of store.messages) {
    const gap = previous
      ? Date.parse(message.created_at) - Date.parse(previous.created_at)
      : Number.POSITIVE_INFINITY;
    const separated = gap > 5 * 60 * 1000;
    if (separated) {
      result.push({
        kind: "time",
        key: `t:${message.key}`,
        label: formatSeparator(message.created_at, locale.value, yesterday),
      });
    }
    const showSender =
      isGroupLike.value &&
      message.direction === "in" &&
      (separated || previous?.direction !== "in" || previous.sender_id !== message.sender_id);
    result.push({ kind: "message", key: message.key, message, showSender });
    previous = message;
  }
  return result;
});

function isNearBottom(el: HTMLElement): boolean {
  return el.scrollHeight - el.scrollTop - el.clientHeight <= 48;
}

function pinToBottom(el: HTMLElement) {
  el.scrollTop = el.scrollHeight;
  lastScrollHeight = el.scrollHeight;
  lastClientHeight = el.clientHeight;
}

function scrollToBottom() {
  const el = scroller.value;
  if (el) pinToBottom(el);
  atBottom.value = true;
  newCount.value = 0;
}

async function loadOlder() {
  const el = scroller.value;
  const inner = content.value;
  if (!el || !inner) return;
  let anchorKey: string | undefined;
  let anchorTop = 0;
  let scrollTop = 0;
  const loaded = await store.loadOlder(() => {
    const first = inner.querySelector<HTMLElement>("[data-message-key]");
    anchorKey = first?.dataset.messageKey;
    anchorTop = first?.offsetTop ?? 0;
    scrollTop = el.scrollTop;
  });
  if (!loaded || anchorKey === undefined) return;
  await nextTick();
  // Keep the previously first message where it was on screen.
  const anchor = Array.from(inner.querySelectorAll<HTMLElement>("[data-message-key]")).find(
    (item) => item.dataset.messageKey === anchorKey,
  );
  if (anchor) el.scrollTop = scrollTop + anchor.offsetTop - anchorTop;
  maybeLoadOlder();
}

function maybeLoadOlder() {
  const el = scroller.value;
  if (!el || !positioned || !store.hasMoreBefore || store.olderError) return;
  if (el.scrollTop < 160) void loadOlder();
}

function onScroll() {
  const el = scroller.value;
  if (!el || !positioned) return;
  // A reflow (e.g. a narrower window) can fire scroll anchoring's scroll event
  // before the ResizeObserver runs; that is not the user leaving the bottom.
  const reflowed = el.scrollHeight !== lastScrollHeight || el.clientHeight !== lastClientHeight;
  if (reflowed && atBottom.value) {
    pinToBottom(el);
    return;
  }
  lastScrollHeight = el.scrollHeight;
  lastClientHeight = el.clientHeight;
  atBottom.value = isNearBottom(el);
  if (atBottom.value) newCount.value = 0;
  maybeLoadOlder();
}

watch(
  () => store.currentUmo,
  (umo, previous) => {
    if (previous) drafts.set(previous, draft.value);
    draft.value = umo ? drafts.get(umo) ?? "" : "";
    positioned = false;
    seenMaxId = 0;
    atBottom.value = true;
    newCount.value = 0;
    previewOpen.value = false;
    timeKey.value = null;
  },
);

watch(
  () => store.loadingInitial,
  async (loading) => {
    if (loading || !store.currentUmo) return;
    await nextTick();
    scrollToBottom();
    positioned = true;
    seenMaxId = store.maxMessageId;
    maybeLoadOlder();
  },
);

watch(
  () => store.maxMessageId,
  async (maxId) => {
    if (!positioned || maxId <= seenMaxId) return;
    let added = 0;
    for (let index = store.messages.length - 1; index >= 0; index--) {
      const id = store.messages[index].id;
      if (id === null) continue;
      if (id <= seenMaxId) break;
      added++;
    }
    seenMaxId = maxId;
    if (!atBottom.value) {
      newCount.value += added;
      return;
    }
    store.trimOldest();
    await nextTick();
    scrollToBottom();
  },
);

// Stay pinned to the bottom while images load or the composer grows.
watch(
  [scroller, content],
  ([el, inner]) => {
    resizeObserver?.disconnect();
    resizeObserver = null;
    if (!el || !inner) return;
    resizeObserver = new ResizeObserver(() => {
      if (positioned && atBottom.value) pinToBottom(el);
    });
    resizeObserver.observe(el);
    resizeObserver.observe(inner);
  },
  { flush: "post" },
);

onBeforeUnmount(() => {
  resizeObserver?.disconnect();
});

function submit() {
  const text = draft.value.trim();
  if (!text || !store.currentUmo) return;
  draft.value = "";
  void store.send(text);
  nextTick(() => {
    scrollToBottom();
    composer.value?.querySelector("textarea")?.focus();
  });
}

function onKeydown(event: KeyboardEvent) {
  if (event.key !== "Enter" || event.shiftKey) return;
  if (isComposingEnter(event, composing, lastCompositionEndAt)) return;
  event.preventDefault();
  submit();
}

function onCompositionStart() {
  composing = true;
}

function onCompositionEnd(event: CompositionEvent) {
  composing = false;
  lastCompositionEndAt = event.timeStamp;
}

function retry(message: ChatMessage) {
  store.retry(message);
  nextTick(scrollToBottom);
}

function openPreview(url: string) {
  previewUrl.value = url;
  previewOpen.value = true;
}

// A click on a message toggles its time; one elsewhere in the stream hides it.
function onStreamClick(event: MouseEvent) {
  const target = event.target as Element;
  if (target.closest("a, button")) return;
  // Ends a text selection rather than picking a message.
  if (window.getSelection()?.isCollapsed === false) return;
  const item = target.closest("[data-time-toggle]")?.closest<HTMLElement>("[data-message-key]");
  const key = item?.dataset.messageKey ?? null;
  timeKey.value = key !== timeKey.value ? key : null;
}

const typeLabel = computed(() => {
  const type = session.value?.message_type;
  return type === "GroupMessage" || type === "FriendMessage" || type === "OtherMessage"
    ? tm(`types.${type}`)
    : "";
});
</script>

<template>
  <section class="im-card im-pane">
    <template v-if="store.currentUmo">
      <header class="im-pane__header">
        <v-btn
          class="im-pane__back"
          icon
          size="small"
          variant="text"
          :aria-label="tm('stream.back')"
          @click="emit('back')"
        >
          <ArrowLeft :size="18" aria-hidden="true" />
        </v-btn>
        <ImAvatar
          :name="session?.display_name || store.currentUmo"
          :platform-type="session?.platform_type"
          :size="34"
        />
        <div class="im-pane__title">
          <div class="im-pane__name" :title="session?.display_name">
            {{ session?.display_name || store.currentUmo }}
          </div>
          <div class="im-pane__meta">
            <span>{{ session?.platform_id }}</span>
            <span v-if="typeLabel">· {{ typeLabel }}</span>
            <span v-if="session?.keep_forever" class="im-pane__keep">
              <Archive :size="12" aria-hidden="true" />
              {{ tm("list.keepForever") }}
            </span>
          </div>
        </div>
      </header>

      <div class="im-pane__stream">
        <div ref="scroller" class="im-stream" @scroll.passive="onScroll">
          <div ref="content" class="im-stream__content" @click="onStreamClick">
            <div v-if="store.loadingInitial && !store.messages.length" class="im-stream__state">
              <v-progress-circular indeterminate size="26" width="2" />
            </div>
            <div v-else-if="store.historyError && !store.messages.length" class="im-stream__state">
              <span>{{ tm("stream.loadFailed") }}</span>
              <v-btn size="small" variant="tonal" @click="store.loadLatest()">
                {{ tm("actions.retry") }}
              </v-btn>
            </div>
            <template v-else>
              <div class="im-stream__top">
                <v-progress-circular v-if="store.loadingOlder" indeterminate size="18" width="2" />
                <template v-else-if="store.olderError">
                  <span>{{ tm("stream.olderFailed") }}</span>
                  <v-btn size="x-small" variant="text" color="primary" @click="loadOlder">
                    {{ tm("actions.retry") }}
                  </v-btn>
                </template>
                <span v-else-if="store.reachedPurge">{{ tm("stream.purged") }}</span>
                <span v-else-if="!store.messages.length">{{ tm("stream.empty") }}</span>
              </div>
              <template v-for="row in rows" :key="row.key">
                <div v-if="row.kind === 'time'" class="im-stream__time">{{ row.label }}</div>
                <MessageItem
                  v-else
                  :message="row.message"
                  :show-sender="row.showSender"
                  :selected="row.message.key === timeKey"
                  @preview="openPreview"
                  @retry="retry"
                />
              </template>
            </template>
          </div>
        </div>
        <v-btn
          v-if="newCount > 0"
          class="im-pane__jump"
          color="primary"
          size="small"
          rounded="pill"
          @click="scrollToBottom"
        >
          <ArrowDown :size="15" aria-hidden="true" />
          <span>{{ tm("stream.newMessages", { count: newCount }) }}</span>
        </v-btn>
      </div>

      <footer ref="composer" class="im-pane__composer">
        <v-textarea
          v-model="draft"
          :placeholder="tm('composer.placeholder')"
          :aria-label="tm('composer.placeholder')"
          :maxlength="10000"
          rows="1"
          max-rows="6"
          auto-grow
          density="compact"
          variant="solo-filled"
          flat
          hide-details
          @keydown="onKeydown"
          @compositionstart="onCompositionStart"
          @compositionend="onCompositionEnd"
        />
        <v-btn
          icon
          color="primary"
          size="small"
          :disabled="!draft.trim()"
          :aria-label="tm('composer.send')"
          @click="submit"
        >
          <Send :size="16" aria-hidden="true" />
        </v-btn>
      </footer>
    </template>

    <div v-else class="im-pane__placeholder">
      <MessagesSquare :size="32" :stroke-width="1.5" aria-hidden="true" />
      <span>{{ tm("stream.selectSession") }}</span>
    </div>

    <v-dialog v-model="previewOpen" max-width="92vw" content-class="im-preview">
      <img
        v-if="previewUrl"
        :src="previewUrl"
        alt=""
        class="im-preview__image"
        @click="previewOpen = false"
      />
    </v-dialog>
  </section>
</template>

<style scoped>
.im-pane {
  display: flex;
  flex-direction: column;
  min-height: 0;
  min-width: 0;
}

.im-pane__header {
  align-items: center;
  border-bottom: 1px solid var(--im-subtle);
  display: flex;
  gap: 10px;
  min-height: 56px;
  padding: 8px 14px;
}

.im-pane__back {
  display: none;
  margin-left: -6px;
}

.im-pane__title {
  min-width: 0;
}

.im-pane__name {
  font-size: 0.9rem;
  font-weight: 650;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.im-pane__meta {
  align-items: center;
  color: var(--im-muted);
  display: flex;
  font-size: 0.72rem;
  gap: 4px;
  overflow: hidden;
  white-space: nowrap;
}

.im-pane__meta > span:first-child {
  overflow: hidden;
  text-overflow: ellipsis;
}

.im-pane__keep {
  align-items: center;
  display: inline-flex;
  gap: 3px;
  margin-left: 6px;
}

.im-pane__stream {
  flex: 1 1 auto;
  min-height: 0;
  position: relative;
}

.im-stream {
  height: 100%;
  overflow-y: auto;
  overscroll-behavior: contain;
}

.im-stream__content {
  display: flex;
  flex-direction: column;
  min-height: 100%;
  padding: 4px 16px 12px;
  position: relative;
}

.im-stream__state {
  align-items: center;
  color: var(--im-muted);
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  font-size: 0.8rem;
  gap: 8px;
  justify-content: center;
}

.im-stream__top {
  align-items: center;
  color: var(--im-muted);
  display: flex;
  font-size: 0.74rem;
  gap: 6px;
  justify-content: center;
  min-height: 32px;
  padding: 6px 0;
}

.im-stream__time {
  align-self: center;
  color: var(--im-muted);
  font-size: 0.7rem;
  margin: 10px 0 4px;
}

.im-pane__jump {
  bottom: 12px;
  left: 50%;
  position: absolute;
  transform: translateX(-50%);
}

.im-pane__jump :deep(.v-btn__content) {
  gap: 5px;
}

.im-pane__composer {
  align-items: flex-end;
  border-top: 1px solid var(--im-subtle);
  display: flex;
  gap: 8px;
  padding: 10px 12px;
}

.im-pane__composer :deep(.v-field) {
  background: var(--im-surface);
  border-radius: var(--astrbot-radius-control);
  box-shadow: none;
  font-size: 0.86rem;
}

.im-pane__composer :deep(.v-field__overlay) {
  opacity: 0;
}

.im-pane__composer > .v-btn {
  margin-bottom: 2px;
}

.im-pane__placeholder {
  align-items: center;
  color: var(--im-muted);
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  font-size: 0.82rem;
  gap: 10px;
  justify-content: center;
}

:global(.im-preview) {
  align-items: center;
  box-shadow: none !important;
  display: flex;
  justify-content: center;
}

.im-preview__image {
  border-radius: 8px;
  cursor: zoom-out;
  display: block;
  max-height: 88vh;
  max-width: 100%;
  object-fit: contain;
}

@media (max-width: 500px) {
  .im-pane__back {
    display: inline-flex;
  }

  .im-pane__header {
    padding-inline: 10px;
  }

  .im-stream__content {
    padding-inline: 10px;
  }

  .im-pane__composer {
    padding: 8px;
  }
}
</style>
