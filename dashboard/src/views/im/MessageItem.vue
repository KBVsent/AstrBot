<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, watch } from "vue";
import { CircleAlert, CircleHelp, ImageOff } from "@lucide/vue";
import { useI18n, useModuleI18n } from "@/i18n/composables";
import { snsApi, type ImSegment } from "./api";
import { formatFullTime, localizeMarkers, markerLabel } from "./format";
import type { ChatMessage } from "./store";

const props = defineProps<{ message: ChatMessage; showSender: boolean; selected: boolean }>();
const emit = defineEmits<{
  (event: "preview", url: string): void;
  (event: "retry", message: ChatMessage): void;
}>();

const { locale } = useI18n();
const { tm, getRaw } = useModuleI18n("features/im");
const brokenImages = reactive(new Set<number>());
const imageUrls = reactive(new Map<number, string>());
let unmounted = false;

watch(
  () => props.message.id,
  (id) => {
    if (id === null) return;
    props.message.content.forEach(async (segment, index) => {
      if (segment.type !== "Image" || !segment.available || imageUrls.has(index)) return;
      try {
        const url = URL.createObjectURL(await snsApi.fetchImage(id, index));
        if (unmounted) URL.revokeObjectURL(url);
        else imageUrls.set(index, url);
      } catch {
        brokenImages.add(index);
      }
    });
  },
  { immediate: true },
);

onBeforeUnmount(() => {
  unmounted = true;
  imageUrls.forEach((url) => URL.revokeObjectURL(url));
});

type TextPart = { text: string; href?: string };

/** Split text into plain runs and http(s) links; anything else stays text. */
function linkify(text: string): TextPart[] {
  const pattern = /https?:\/\/[^\s<>"'`，。！？；：、「」『』（）【】《》“”‘’]+/gi;
  const closers: Record<string, string> = { ")": "(", "]": "[", "}": "{" };
  const trimLink = (link: string) => {
    while (link) {
      const last = link[link.length - 1];
      const opener = closers[last];
      // A closing bracket stays only when the link itself opened it.
      if (opener) {
        if (link.split(opener).length >= link.split(last).length) break;
      } else if (!".,;:!?'\"*_~".includes(last)) {
        break;
      }
      link = link.slice(0, -1);
    }
    return link;
  };
  const isHttp = (link: string) => {
    try {
      const url = new URL(link);
      return (url.protocol === "http:" || url.protocol === "https:") && Boolean(url.hostname);
    } catch {
      return false;
    }
  };

  const parts: TextPart[] = [];
  let cursor = 0;
  for (const match of text.matchAll(pattern)) {
    const start = match.index ?? 0;
    const link = trimLink(match[0]);
    if (!isHttp(link)) continue;
    if (start > cursor) parts.push({ text: text.slice(cursor, start) });
    parts.push({ text: link, href: link });
    cursor = start + link.length;
  }
  if (cursor < text.length) parts.push({ text: text.slice(cursor) });
  return parts;
}

const textParts = computed(() =>
  props.message.content.map((segment) => (segment.type === "Plain" ? linkify(segment.text) : [])),
);

const senderName = computed(() => {
  const { sender_name: name, sender_id: id } = props.message;
  if (name.trim()) return name;
  // QQ official often sends an empty nickname.
  if (id) return id.length > 8 ? `${id.slice(0, 8)}…` : id;
  return tm("stream.unknownSender");
});

const fullTime = computed(() => formatFullTime(props.message.created_at, locale.value));
const markerLabels = computed(() => getRaw("segment.other"));

function imageUrl(index: number): string {
  return brokenImages.has(index) ? "" : (imageUrls.get(index) ?? "");
}

function imageLoading(index: number): boolean {
  const segment = props.message.content[index];
  return (
    segment?.type === "Image" &&
    segment.available &&
    props.message.id !== null &&
    !brokenImages.has(index) &&
    !imageUrls.has(index)
  );
}

function otherLabel(segment: ImSegment): string {
  const name = "name" in segment ? segment.name : "";
  return markerLabel(markerLabels.value, name) ?? tm("segment.unknown");
}
</script>

<template>
  <div
    class="im-msg"
    :class="`im-msg--${message.direction}`"
    :data-message-key="message.key"
  >
    <div class="im-msg__column" data-time-toggle>
      <div v-if="showSender || selected" class="im-msg__meta">
        <span v-if="showSender" class="im-msg__sender">{{ senderName }}</span>
        <time v-if="selected" class="im-msg__time" :datetime="message.created_at">{{ fullTime }}</time>
      </div>
      <div class="im-msg__row">
        <div class="im-msg__bubble" :title="fullTime">
          <template v-for="(segment, index) in message.content" :key="index">
            <div v-if="segment.type === 'Reply'" class="im-reply">
              <span v-if="segment.sender_name" class="im-reply__sender">{{ segment.sender_name }}</span>
              <span class="im-reply__text">{{ segment.text ? localizeMarkers(segment.text, markerLabels) : tm("stream.quotedMessage") }}</span>
            </div>
            <span v-else-if="segment.type === 'At'" class="im-at">@{{ segment.name || segment.target }}</span>
            <template v-else-if="segment.type === 'Plain'">
              <template v-for="(part, partIndex) in textParts[index]" :key="partIndex">
                <a
                  v-if="part.href"
                  :href="part.href"
                  target="_blank"
                  rel="noopener noreferrer"
                  class="im-link"
                >{{ part.text }}</a>
                <template v-else>{{ part.text }}</template>
              </template>
            </template>
            <button
              v-else-if="segment.type === 'Image' && imageUrl(index)"
              type="button"
              class="im-image"
              :aria-label="tm('segment.image')"
              @click="emit('preview', imageUrl(index))"
            >
              <img :src="imageUrl(index)" alt="" @error="brokenImages.add(index)" />
            </button>
            <span v-else-if="imageLoading(index)" class="im-image im-image--loading" aria-hidden="true" />
            <span v-else-if="segment.type === 'Image'" class="im-image im-image--expired">
              <ImageOff :size="16" aria-hidden="true" />
              <span>{{ tm("segment.imageExpired") }}</span>
            </span>
            <span v-else class="im-other">{{ otherLabel(segment) }}</span>
          </template>
          <span v-if="!message.content.length" class="im-other">{{ tm("segment.empty") }}</span>
        </div>
        <span
          v-if="message.status === 'sending'"
          class="im-msg__status"
          :title="tm('status.sending')"
        >
          <v-progress-circular indeterminate size="14" width="2" />
        </span>
        <span
          v-else-if="message.status === 'failed'"
          class="im-msg__status im-msg__status--failed"
          :title="tm('status.failed')"
        >
          <CircleAlert :size="16" aria-hidden="true" />
        </span>
        <span v-else-if="message.status === 'unknown'" class="im-msg__status" :title="tm('status.unknown')">
          <CircleHelp :size="16" aria-hidden="true" />
        </span>
      </div>
      <div v-if="message.status === 'failed' || message.status === 'unknown'" class="im-msg__error">
        <span class="im-msg__error-text">
          {{ tm(`status.${message.status}`) }}<template v-if="message.error">: {{ message.error }}</template>
        </span>
        <button type="button" class="im-msg__retry" @click="emit('retry', message)">
          {{ tm(message.status === "unknown" ? "actions.confirm" : "actions.resend") }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.im-msg {
  display: flex;
  padding: 2px 0;
}

.im-msg--out {
  justify-content: flex-end;
}

.im-msg__column {
  display: flex;
  flex-direction: column;
  max-width: min(78%, 640px);
  min-width: 0;
}

.im-msg--out .im-msg__column {
  align-items: flex-end;
}

.im-msg__meta {
  color: var(--im-muted);
  display: flex;
  font-size: 0.72rem;
  gap: 8px;
  margin: 6px 4px 3px;
  max-width: 100%;
  min-width: 0;
  white-space: nowrap;
}

.im-msg__sender {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}

.im-msg__time {
  flex: 0 0 auto;
}

.im-msg__row {
  align-items: flex-end;
  display: flex;
  gap: 6px;
  max-width: 100%;
  min-width: 0;
}

.im-msg--out .im-msg__row {
  flex-direction: row-reverse;
}

.im-msg__bubble {
  background: var(--im-bubble-in);
  border-radius: 14px;
  font-size: 0.86rem;
  line-height: 1.5;
  min-width: 0;
  overflow-wrap: anywhere;
  padding: 7px 11px;
  white-space: pre-wrap;
}

.im-msg--out .im-msg__bubble {
  background: var(--im-bubble-out);
}

.im-msg__status {
  align-items: center;
  color: var(--im-muted);
  display: inline-flex;
  flex: 0 0 auto;
  margin-bottom: 6px;
}

.im-msg__status--failed {
  color: rgb(var(--v-theme-error));
}

.im-msg__error {
  align-items: baseline;
  color: rgb(var(--v-theme-error));
  display: flex;
  flex-wrap: wrap;
  font-size: 0.72rem;
  gap: 4px 8px;
  justify-content: flex-end;
  margin-top: 3px;
  max-width: 100%;
}

.im-msg__error-text {
  min-width: 0;
  overflow-wrap: anywhere;
}

.im-msg__retry {
  background: none;
  border: 0;
  color: rgb(var(--v-theme-primary));
  cursor: pointer;
  font-size: inherit;
  font-weight: 600;
  padding: 0;
}

.im-msg__retry:hover {
  text-decoration: underline;
}

.im-reply {
  border-left: 3px solid rgba(var(--v-theme-primary), 0.45);
  display: flex;
  flex-direction: column;
  font-size: 0.76rem;
  line-height: 1.4;
  margin: 1px 0 5px;
  padding: 1px 0 1px 8px;
  white-space: normal;
}

.im-reply__sender {
  color: rgba(var(--v-theme-on-surface), 0.72);
  font-weight: 600;
}

.im-reply__text {
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  color: var(--im-muted);
  display: -webkit-box;
  overflow: hidden;
}

.im-at {
  color: rgb(var(--v-theme-primary));
}

.im-link {
  color: rgb(var(--v-theme-primary));
  text-decoration: underline;
  text-underline-offset: 2px;
}

.im-other {
  color: var(--im-muted);
}

.im-image {
  background: transparent;
  border: 0;
  border-radius: 8px;
  cursor: zoom-in;
  display: block;
  margin: 3px 0;
  max-width: 100%;
  padding: 0;
}

/* A percentage max-width would not cap the bubble's intrinsic width. */
.im-image img {
  background: rgba(var(--v-theme-on-surface), 0.05);
  border-radius: 8px;
  display: block;
  max-height: 240px;
  max-width: 240px;
  min-height: 40px;
  min-width: 40px;
  object-fit: contain;
}

.im-image--loading {
  display: inline-block;
  width: 120px;
  height: 90px;
  border-radius: 8px;
  background: rgba(var(--v-theme-on-surface), 0.06);
}

.im-image--expired {
  align-items: center;
  background: rgba(var(--v-theme-on-surface), 0.05);
  color: var(--im-muted);
  cursor: default;
  display: inline-flex;
  font-size: 0.76rem;
  gap: 6px;
  padding: 8px 12px;
  white-space: nowrap;
}
</style>
