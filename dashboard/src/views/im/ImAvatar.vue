<script setup lang="ts">
import { computed } from "vue";
import { getPlatformIcon } from "@/utils/platformUtils";

const props = withDefaults(
  defineProps<{ name: string; platformType?: string; size?: number }>(),
  { platformType: "", size: 40 },
);

const platformIcon = computed(() => getPlatformIcon(props.platformType) as string | undefined);
// Array.from keeps a surrogate pair together.
const initial = computed(() => (Array.from(props.name.trim())[0] ?? "?").toUpperCase());
</script>

<template>
  <span
    class="im-avatar"
    :style="{ width: `${size}px`, height: `${size}px`, fontSize: `${Math.round(size * 0.42)}px` }"
    aria-hidden="true"
  >
    <span class="im-avatar__initial">{{ initial }}</span>
    <img v-if="platformIcon" :src="platformIcon" alt="" class="im-avatar__platform" />
  </span>
</template>

<style scoped>
.im-avatar {
  align-items: center;
  background: rgba(var(--v-theme-primary), 0.12);
  border-radius: 50%;
  color: rgb(var(--v-theme-primary));
  display: inline-flex;
  flex: 0 0 auto;
  font-weight: 600;
  justify-content: center;
  line-height: 1;
  position: relative;
  user-select: none;
}

.im-avatar__platform {
  background: rgb(var(--v-theme-surface));
  border: 1.5px solid rgb(var(--v-theme-surface));
  border-radius: 5px;
  bottom: -2px;
  height: 40%;
  max-height: 18px;
  max-width: 18px;
  min-height: 12px;
  min-width: 12px;
  object-fit: contain;
  position: absolute;
  right: -2px;
  width: 40%;
}
</style>
