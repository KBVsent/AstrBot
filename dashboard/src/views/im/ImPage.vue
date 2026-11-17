<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, watch } from "vue";
import { isNavigationFailure, NavigationFailureType, useRoute, useRouter } from "vue-router";
import { EventSourcePolyfill } from "event-source-polyfill";
import { useCustomizerStore } from "@/stores/customizer";
import MessagePane from "./MessagePane.vue";
import SessionList from "./SessionList.vue";
import { snsApi } from "./api";
import { useImStore } from "./store";

const store = useImStore();
const customizer = useCustomizerStore();
const isDark = computed(() => customizer.isDark);
const route = useRoute();
const router = useRouter();

// The open session lives in the query, so browser back closes it before leaving the page.
const routeUmo = computed(() => {
  const umo = route.name === "Messages" ? route.query.umo : null;
  return typeof umo === "string" && umo ? umo : null;
});
// Set while router.back() is pending, so a second tap cannot step back past the list.
let leaving = false;

function openSession(umo: string) {
  if (umo === routeUmo.value) return;
  const location = { query: { ...route.query, umo } };
  // Switching replaces the entry, so back returns to the list, not the previous session.
  void (routeUmo.value ? router.replace(location) : router.push(location));
}

function dropUmo() {
  const query = { ...route.query };
  delete query.umo;
  void router.replace({ query });
}

function closeSession() {
  if (!routeUmo.value || leaving) return;
  const back = window.history.state?.back;
  const previous = typeof back === "string" ? router.resolve(back) : null;
  if (previous?.name === "Messages" && !previous.query.umo) {
    leaving = true;
    router.back();
  } else {
    // Opened by a direct link or a reload: there is no list entry to return to.
    dropUmo();
  }
}

// An open Vuetify overlay, the global toast included, vetoes a back navigation and the router
// steps forward again; drop the query only once that step has landed.
const stopAfterEach = router.afterEach((_to, _from, failure) => {
  if (!leaving) return;
  if (!isNavigationFailure(failure, NavigationFailureType.aborted)) {
    leaving = false;
    return;
  }
  const onReturn = () => {
    leaving = false;
    if (routeUmo.value) dropUmo();
  };
  window.addEventListener("popstate", onReturn, { once: true });
});

watch(routeUmo, (umo) => {
  if (umo) store.openSession(umo);
  else store.closeSession();
});

// The store closes the session itself when it is deleted.
watch(
  () => store.currentUmo,
  (umo) => {
    if (!umo) closeSession();
  },
);

let source: EventSourcePolyfill | null = null;
let retryTimer: ReturnType<typeof setTimeout> | null = null;
let retryAttempts = 0;
let disposed = false;
let baseTitle = "";

function connect() {
  if (disposed) return;
  source?.close();
  const token = localStorage.getItem("token");
  const eventSource = new EventSourcePolyfill(snsApi.eventsUrl(), {
    headers: { Authorization: token ? `Bearer ${token}` : "" },
    // The server sends a heartbeat every 15 seconds.
    heartbeatTimeout: 45000,
    withCredentials: true,
  });
  source = eventSource;

  // Every open, the first one included, refetches: events before the subscription are not replayed.
  eventSource.onopen = () => {
    retryAttempts = 0;
    store.resync();
  };

  for (const type of ["message.created", "message.updated", "session.updated", "session.deleted", "resync"]) {
    eventSource.addEventListener(type, (event) => {
      if (source !== eventSource) return;
      let data: Record<string, any>;
      try {
        data = JSON.parse(event.data || "{}");
      } catch (error) {
        console.error("Failed to parse SNS event:", error);
        return;
      }
      store.applyEvent(type, data);
    });
  }

  eventSource.onerror = (event) => {
    if (source !== eventSource) return;
    eventSource.close();
    source = null;
    if (disposed) return;
    if (event.status === 401) {
      // A plain request lets the shared 401 handler send the user to the login page.
      void store.loadSessions();
      return;
    }
    const delay = Math.min(1000 * 2 ** retryAttempts, 30000);
    retryAttempts++;
    if (retryTimer) clearTimeout(retryTimer);
    retryTimer = setTimeout(() => {
      retryTimer = null;
      connect();
    }, delay);
  };
}

function onVisibilityChange() {
  store.setPageVisible(document.visibilityState === "visible");
}

watch(
  () => store.unreadSessions,
  (count) => {
    if (disposed) return;
    document.title = count > 0 ? `(${count}) ${baseTitle}` : baseTitle;
  },
);

onMounted(() => {
  baseTitle = document.title;
  store.start();
  document.addEventListener("visibilitychange", onVisibilityChange);
  void store.loadSessions();
  if (routeUmo.value) store.openSession(routeUmo.value);
  connect();
});

onBeforeUnmount(() => {
  stopAfterEach();
  disposed = true;
  source?.close();
  source = null;
  if (retryTimer) clearTimeout(retryTimer);
  retryTimer = null;
  document.removeEventListener("visibilitychange", onVisibilityChange);
  document.title = baseTitle;
  store.stop();
});
</script>

<template>
  <div
    class="im-page"
    :class="{ 'im-page--open': Boolean(store.currentUmo), 'is-dark': isDark }"
  >
    <SessionList class="im-page__list" @open="openSession" />
    <MessagePane class="im-page__pane" @back="closeSession" />
  </div>
</template>

<style scoped>
.im-page {
  --im-card: #f5f6f7;
  --im-surface: rgb(var(--v-theme-surface));
  --im-muted: rgba(var(--v-theme-on-surface), 0.58);
  --im-subtle: rgba(var(--v-theme-on-surface), 0.08);
  --im-bubble-in: rgb(var(--v-theme-surface));
  --im-bubble-out: rgba(var(--v-theme-primary), 0.14);
  display: grid;
  flex: 1 1 0;
  gap: 12px;
  grid-template-columns: minmax(260px, 320px) minmax(0, 1fr);
  min-height: 0;
  padding: 0 12px 8px;
  position: relative;
}

.im-page.is-dark {
  --im-card: rgba(var(--v-theme-on-surface), 0.06);
  --im-surface: rgba(var(--v-theme-on-surface), 0.05);
  --im-subtle: rgba(var(--v-theme-on-surface), 0.1);
  --im-bubble-in: rgba(var(--v-theme-on-surface), 0.08);
  --im-bubble-out: rgba(var(--v-theme-primary), 0.28);
}

.im-card {
  background: var(--im-card);
  border-radius: var(--astrbot-radius-card);
  min-height: 0;
  min-width: 0;
  overflow: hidden;
}

@media (max-width: 700px) {
  .im-page {
    gap: 8px;
    grid-template-columns: 220px minmax(0, 1fr);
    padding: 0 4px 4px;
  }
}

/* The stream covers the list; the list stays mounted so its scroll position survives. */
@media (max-width: 500px) {
  .im-page {
    grid-template-columns: minmax(0, 1fr);
  }

  /* Two classes so this outranks the pane's own display rule. */
  /* The dark card color is translucent, so the covering pane needs the page color underneath. */
  .im-page > .im-page__pane {
    background:
      linear-gradient(var(--im-card), var(--im-card)),
      rgb(var(--v-theme-containerBg));
    display: none;
    inset: 0 4px 4px;
    position: absolute;
    z-index: 2;
  }

  .im-page--open > .im-page__pane {
    display: flex;
  }
}
</style>
