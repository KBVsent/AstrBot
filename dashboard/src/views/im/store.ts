import { defineStore } from 'pinia';
import {
  errorMessage,
  errorStatus,
  snsApi,
  type ImMessage,
  type ImMessageStatus,
  type ImMessageType,
  type ImPlatform,
  type ImSession,
} from './api';

const MESSAGE_PAGE_SIZE = 30;

/**
 * A message in the open session; id is null for a local echo the server has not stored yet.
 *
 * unknown is local only: the send request got no answer, so the server may or may not have it.
 */
export interface ChatMessage extends Omit<ImMessage, 'id' | 'status'> {
  id: number | null;
  key: string;
  status: ImMessageStatus | 'unknown';
}

export interface SessionFilters {
  type: ImMessageType | '';
  platformId: string;
  q: string;
}

type SessionChange = 'created-in' | 'created-out' | 'updated';

// Generations only grow, so a response from before stop() can never match a later request.
let listGeneration = 0;
let historyGeneration = 0;
let readTimer: ReturnType<typeof setTimeout> | null = null;
let readInFlight = false;
let readAgain = false;
let unreadRefreshTimer: ReturnType<typeof setTimeout> | null = null;
// Last known "has unread" flag per session, used to keep unreadSessions current between list loads.
const unreadKnown = new Map<string, boolean>();
// Session events seen while a list load is in flight; null marks a deletion.
const eventsDuringListLoad = new Map<string, ImSession | null>();
// Keys of failed messages removed by a resend, so a later merge does not bring them back.
const dismissed = new Set<string>();
// client_ids with a send request in flight, so repeated confirmations post once.
const posting = new Set<string>();

function messageKey(message: { id: number | null; client_id: string | null }): string {
  return message.client_id ? `c:${message.client_id}` : `m:${message.id}`;
}

// crypto.randomUUID needs a secure context; the dashboard is often served over plain HTTP.
function newClientId(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
}

function insertSorted(list: ChatMessage[], message: ChatMessage) {
  if (message.id === null) {
    list.push(message);
    return;
  }
  let index = list.length;
  while (index > 0) {
    const previous = list[index - 1];
    if (previous.id !== null && previous.id < message.id) break;
    index--;
  }
  list.splice(index, 0, message);
}

// A final status from the server never goes back to "sending"; a local echo takes the server's status.
function mergeMessage(previous: ChatMessage, next: ImMessage): ChatMessage {
  const keepStatus = previous.id !== null && previous.status !== 'sending' && next.status === 'sending';
  return {
    ...next,
    key: previous.key,
    status: keepStatus ? previous.status : next.status,
    error: keepStatus ? previous.error : next.error,
  };
}

function compareSessions(a: ImSession, b: ImSession): number {
  if (a.pinned !== b.pinned) return a.pinned ? -1 : 1;
  const timeDiff = Date.parse(b.last_time) - Date.parse(a.last_time);
  if (timeDiff) return timeDiff;
  return a.umo < b.umo ? 1 : a.umo > b.umo ? -1 : 0;
}

function matchesFilters(session: ImSession, filters: SessionFilters): boolean {
  if (filters.type && session.message_type !== filters.type) return false;
  if (filters.platformId && session.platform_id !== filters.platformId) return false;
  const query = filters.q.trim().toLowerCase();
  if (!query) return true;
  return (
    session.display_name.toLowerCase().includes(query) ||
    session.session_id.toLowerCase().includes(query)
  );
}

export const useImStore = defineStore('im', {
  state: () => ({
    sessions: new Map<string, ImSession>(),
    filters: { type: '', platformId: '', q: '' } as SessionFilters,
    platforms: [] as ImPlatform[],
    nextCursor: null as string | null,
    unreadSessions: 0,
    listLoading: false,
    listLoadingMore: false,
    listError: '',

    currentUmo: null as string | null,
    currentSession: null as ImSession | null,
    messages: [] as ChatMessage[],
    hasMoreBefore: false,
    purgedUntil: null as string | null,
    loadingInitial: false,
    loadingOlder: false,
    historyError: '',
    olderError: '',
    // Highest id up to which the message list is known to be complete; the after_id for catch-up.
    syncedId: 0,

    active: false,
    pageVisible: true,
  }),

  getters: {
    sessionList(state): ImSession[] {
      return [...state.sessions.values()].sort(compareSessions);
    },
    maxMessageId(state): number {
      for (let index = state.messages.length - 1; index >= 0; index--) {
        const id = state.messages[index].id;
        if (id !== null) return id;
      }
      return 0;
    },
    oldestMessageId(state): number | null {
      for (const message of state.messages) {
        if (message.id !== null) return message.id;
      }
      return null;
    },
    reachedPurge(state): boolean {
      return !state.loadingInitial && !state.hasMoreBefore && Boolean(state.purgedUntil);
    },
  },

  actions: {
    start() {
      this.active = true;
      this.pageVisible = document.visibilityState === 'visible';
    },

    stop() {
      if (readTimer) clearTimeout(readTimer);
      if (unreadRefreshTimer) clearTimeout(unreadRefreshTimer);
      readTimer = null;
      unreadRefreshTimer = null;
      readInFlight = false;
      readAgain = false;
      listGeneration++;
      historyGeneration++;
      unreadKnown.clear();
      eventsDuringListLoad.clear();
      dismissed.clear();
      this.$reset();
    },

    sessionQuery() {
      return {
        type: this.filters.type,
        platform_id: this.filters.platformId,
        q: this.filters.q.trim(),
        limit: 50,
      };
    },

    async loadSessions() {
      const generation = ++listGeneration;
      eventsDuringListLoad.clear();
      this.listLoading = true;
      this.listLoadingMore = false;
      this.listError = '';
      try {
        const page = await snsApi.listSessions(this.sessionQuery());
        if (generation !== listGeneration) return;
        const sessions = new Map(page.sessions.map((session) => [session.umo, session]));
        // The page may predate events that arrived while it loaded.
        for (const [umo, session] of eventsDuringListLoad) {
          const listed = sessions.get(umo);
          if (session === null) sessions.delete(umo);
          else if (!listed || (session.last_message_id ?? 0) > (listed.last_message_id ?? 0)) {
            if (matchesFilters(session, this.filters)) sessions.set(umo, session);
          }
        }
        this.sessions = sessions;
        this.nextCursor = page.next_cursor;
        this.platforms = page.platforms;
        this.unreadSessions = page.unread_sessions;
        unreadKnown.clear();
        for (const session of sessions.values()) {
          unreadKnown.set(session.umo, session.unread_count > 0);
          if (session.umo === this.currentUmo) this.currentSession = session;
        }
        this.maybeMarkRead();
      } catch (error) {
        if (generation === listGeneration) this.listError = errorMessage(error);
      } finally {
        if (generation === listGeneration) {
          this.listLoading = false;
          eventsDuringListLoad.clear();
        }
      }
    },

    async loadMoreSessions() {
      if (!this.nextCursor || this.listLoading || this.listLoadingMore) return;
      const generation = listGeneration;
      this.listLoadingMore = true;
      try {
        const page = await snsApi.listSessions({ ...this.sessionQuery(), cursor: this.nextCursor });
        if (generation !== listGeneration) return;
        for (const session of page.sessions) {
          // An event may already have delivered a newer state while this page loaded.
          const known = this.sessions.get(session.umo);
          if (known && (known.last_message_id ?? 0) >= (session.last_message_id ?? 0)) continue;
          this.sessions.set(session.umo, session);
          unreadKnown.set(session.umo, session.unread_count > 0);
        }
        this.nextCursor = page.next_cursor;
        this.unreadSessions = page.unread_sessions;
      } catch (error) {
        if (generation === listGeneration) this.listError = errorMessage(error);
      } finally {
        if (generation === listGeneration) this.listLoadingMore = false;
      }
    },

    setFilters(patch: Partial<SessionFilters>) {
      Object.assign(this.filters, patch);
      void this.loadSessions();
    },

    scheduleUnreadRefresh() {
      if (unreadRefreshTimer) return;
      unreadRefreshTimer = setTimeout(async () => {
        unreadRefreshTimer = null;
        try {
          const page = await snsApi.listSessions({ limit: 1 });
          if (this.active) this.unreadSessions = page.unread_sessions;
        } catch {
          // The next list load corrects the count.
        }
      }, 1000);
    },

    trackUnread(session: ImSession, change: SessionChange) {
      const now = session.unread_count > 0;
      const before = unreadKnown.get(session.umo);
      unreadKnown.set(session.umo, now);
      if (before !== undefined) {
        if (before !== now) this.unreadSessions = Math.max(0, this.unreadSessions + (now ? 1 : -1));
        return;
      }
      // Unseen session: an inbound message leaving exactly one unread means it just became unread.
      if (change === 'created-in') {
        if (session.unread_count === 1) this.unreadSessions += 1;
      } else if (change === 'updated' && !now) {
        this.scheduleUnreadRefresh();
      }
    },

    applySession(session: ImSession, change: SessionChange) {
      this.trackUnread(session, change);
      if (this.currentUmo === session.umo) this.currentSession = session;
      if (this.listLoading) eventsDuringListLoad.set(session.umo, session);
      if (matchesFilters(session, this.filters)) this.sessions.set(session.umo, session);
      else this.sessions.delete(session.umo);
      this.maybeMarkRead();
    },

    removeSession(umo: string) {
      const before = unreadKnown.get(umo);
      // Kept as false so a repeated deletion event does not count again.
      unreadKnown.set(umo, false);
      if (before === true) this.unreadSessions = Math.max(0, this.unreadSessions - 1);
      else if (before === undefined) this.scheduleUnreadRefresh();
      if (this.listLoading) eventsDuringListLoad.set(umo, null);
      this.sessions.delete(umo);
      if (this.currentUmo === umo) this.closeSession();
    },

    applyEvent(type: string, data: Record<string, any>) {
      switch (type) {
        case 'message.created': {
          const message = data.message as ImMessage;
          this.applySession(data.session as ImSession, message.direction === 'in' ? 'created-in' : 'created-out');
          this.upsertMessage(message, 'live');
          break;
        }
        case 'message.updated':
          this.upsertMessage(data.message as ImMessage, 'live');
          break;
        case 'session.updated':
          this.applySession(data.session as ImSession, 'updated');
          break;
        case 'session.deleted':
          this.removeSession(String(data.umo));
          break;
        case 'resync':
          this.resync();
          break;
      }
    },

    /** Refetch after a reconnect or a resync event. */
    resync() {
      void this.loadSessions();
      void this.catchUp();
      for (const message of this.messages) {
        if (message.status === 'unknown') this.retry(message);
      }
    },

    /**
     * Merge a server message into the open session.
     *
     * source "live" is an SSE event, "page" a history response, "post" the send response.
     * Only live and page results advance syncedId: a send response can run ahead of
     * events that are still missing.
     */
    upsertMessage(message: ImMessage, source: 'live' | 'page' | 'post'): void {
      if (message.umo !== this.currentUmo) return;
      if (dismissed.has(`m:${message.id}`) || dismissed.has(messageKey(message))) return;
      if (source !== 'post') this.syncedId = Math.max(this.syncedId, message.id);
      const list = this.messages;
      let index = list.findIndex((item) => item.id === message.id);
      if (index < 0 && message.client_id) {
        index = list.findIndex((item) => item.id === null && item.client_id === message.client_id);
      }
      if (index >= 0) {
        const merged = mergeMessage(list[index], message);
        if (list[index].id === message.id) {
          list[index] = merged;
        } else {
          list.splice(index, 1);
          insertSorted(list, merged);
        }
        return;
      }
      // A live update for a message older than the loaded window belongs to unloaded history.
      const oldest = this.oldestMessageId;
      if (source === 'live' && this.hasMoreBefore && oldest !== null && message.id < oldest) return;
      insertSorted(list, { ...message, key: messageKey(message) });
    },

    resetHistory() {
      historyGeneration++;
      if (readTimer) clearTimeout(readTimer);
      readTimer = null;
      dismissed.clear();
      this.messages = [];
      this.hasMoreBefore = false;
      this.purgedUntil = null;
      this.loadingInitial = false;
      this.loadingOlder = false;
      this.historyError = '';
      this.olderError = '';
      this.syncedId = 0;
    },

    openSession(umo: string) {
      if (this.currentUmo === umo) return;
      this.resetHistory();
      this.currentUmo = umo;
      this.currentSession = this.sessions.get(umo) ?? null;
      if (!this.currentSession) void this.findCurrentSession();
      void this.loadLatest();
    },

    /** A session opened from the URL may be outside the loaded list; look it up by its session id. */
    async findCurrentSession() {
      const umo = this.currentUmo;
      const [platformId, , ...rest] = umo?.split(':') ?? [];
      const sessionId = rest.join(':');
      if (!umo || !sessionId) return;
      try {
        const page = await snsApi.listSessions({ platform_id: platformId, q: sessionId, limit: 50 });
        const session = page.sessions.find((item) => item.umo === umo);
        if (!session || this.currentUmo !== umo || this.currentSession) return;
        this.currentSession = session;
        this.maybeMarkRead();
      } catch {
        // The header falls back to the UMO.
      }
    },

    closeSession() {
      this.resetHistory();
      this.currentUmo = null;
      this.currentSession = null;
    },

    /** Load the newest page of the open session and merge it into the list. */
    async loadLatest() {
      const umo = this.currentUmo;
      if (!umo) return;
      const generation = ++historyGeneration;
      // The new generation orphans any older-page request, whose finally no longer resets this.
      this.loadingOlder = false;
      this.loadingInitial = true;
      this.historyError = '';
      try {
        const page = await snsApi.listMessages({ umo, limit: MESSAGE_PAGE_SIZE });
        if (generation !== historyGeneration) return;
        for (const message of page.messages) this.upsertMessage(message, 'page');
        this.hasMoreBefore = page.has_more;
        this.purgedUntil = page.purged_until;
      } catch (error) {
        if (generation !== historyGeneration) return;
        if (errorStatus(error) === 404) {
          this.removeSession(umo);
          return;
        }
        this.historyError = errorMessage(error);
      } finally {
        if (generation === historyGeneration) this.loadingInitial = false;
      }
      this.maybeMarkRead();
    },

    /**
     * Load the page before the oldest loaded message.
     *
     * beforeApply runs right before the page is merged, so the caller can measure
     * the scroll position against the DOM that is still unchanged.
     */
    async loadOlder(beforeApply?: () => void): Promise<boolean> {
      const umo = this.currentUmo;
      const before = this.oldestMessageId;
      if (!umo || before === null || !this.hasMoreBefore || this.loadingOlder || this.loadingInitial) {
        return false;
      }
      const generation = historyGeneration;
      this.loadingOlder = true;
      this.olderError = '';
      try {
        const page = await snsApi.listMessages({ umo, before_id: before, limit: MESSAGE_PAGE_SIZE });
        // A trim in the meantime would leave a gap between this page and the list.
        if (generation !== historyGeneration || this.oldestMessageId !== before) return false;
        beforeApply?.();
        for (const message of page.messages) this.upsertMessage(message, 'page');
        this.hasMoreBefore = page.has_more;
        this.purgedUntil = page.purged_until;
        return true;
      } catch (error) {
        if (generation !== historyGeneration) return false;
        if (errorStatus(error) === 404) this.removeSession(umo);
        else this.olderError = errorMessage(error);
        return false;
      } finally {
        if (generation === historyGeneration) this.loadingOlder = false;
      }
    },

    /**
     * Fetch everything after syncedId, page by page, until the server has no more.
     *
     * Starts earlier when a loaded message is still sending, since its outcome may
     * have been missed while the stream was down.
     */
    async catchUp() {
      const umo = this.currentUmo;
      if (!umo) return;
      // Without a known position (still loading, or nothing loaded) the latest page is the catch-up.
      if (this.loadingInitial || this.syncedId === 0) {
        await this.loadLatest();
        return;
      }
      const generation = historyGeneration;
      let after = this.syncedId;
      for (const message of this.messages) {
        if (message.status === 'sending' && message.id !== null) after = Math.min(after, message.id - 1);
      }
      try {
        for (;;) {
          const page = await snsApi.listMessages({ umo, after_id: after, limit: 100 });
          if (generation !== historyGeneration) return;
          for (const message of page.messages) this.upsertMessage(message, 'page');
          this.purgedUntil = page.purged_until;
          if (!page.has_more || !page.messages.length) break;
          after = page.messages[page.messages.length - 1].id;
        }
      } catch (error) {
        if (generation === historyGeneration && errorStatus(error) === 404) this.removeSession(umo);
      }
    },

    /** Called only while the view sits at the bottom, so the dropped messages are off screen. */
    trimOldest() {
      const excess = this.messages.length - 300;
      if (excess <= 0) return;
      this.messages.splice(0, excess);
      this.hasMoreBefore = true;
    },

    async send(text: string) {
      const umo = this.currentUmo;
      if (!umo) return;
      const clientId = newClientId();
      this.messages.push({
        id: null,
        key: `c:${clientId}`,
        umo,
        direction: 'out',
        platform_message_id: null,
        sender_id: '',
        sender_name: '',
        content: [{ type: 'Plain', text }],
        source: 'webui',
        client_id: clientId,
        status: 'sending',
        error: null,
        created_at: new Date().toISOString(),
      });
      await this.post(umo, text, clientId);
    },

    async post(umo: string, text: string, clientId: string) {
      if (posting.has(clientId)) return;
      posting.add(clientId);
      const key = `c:${clientId}`;
      try {
        this.upsertMessage(await snsApi.sendMessage({ umo, text, client_id: clientId }), 'post');
      } catch (error) {
        if (this.currentUmo !== umo) return;
        // Once an event has given the echo an id, the server owns its status.
        const index = this.messages.findIndex((item) => item.key === key);
        if (index >= 0 && this.messages[index].id === null) {
          // A 4xx answer means nothing was stored; without an answer the server may have it.
          const status = errorStatus(error);
          const rejected = status !== undefined && status >= 400 && status < 500;
          this.messages[index] = {
            ...this.messages[index],
            status: rejected ? 'failed' : 'unknown',
            error: errorMessage(error),
          };
        }
      } finally {
        posting.delete(clientId);
      }
    },

    /**
     * Confirm an unknown send, or resend a failed one.
     *
     * An unknown send is posted again with its own client_id, which the server never
     * delivers twice. A failed one goes out under a new client_id and replaces it.
     */
    retry(message: ChatMessage) {
      const text = message.content
        .map((segment) => (segment.type === 'Plain' ? segment.text : ''))
        .join('');
      if (message.status === 'unknown' && message.client_id) {
        const index = this.messages.findIndex((item) => item.key === message.key);
        if (index < 0) return;
        this.messages[index] = { ...this.messages[index], status: 'sending', error: null };
        void this.post(message.umo, text, message.client_id);
        return;
      }
      if (message.status !== 'failed') return;
      dismissed.add(message.key);
      if (message.id !== null) dismissed.add(`m:${message.id}`);
      this.messages = this.messages.filter((item) => item.key !== message.key);
      if (text) void this.send(text);
    },

    setPageVisible(visible: boolean) {
      this.pageVisible = visible;
      this.maybeMarkRead();
    },

    maybeMarkRead() {
      const session = this.currentSession;
      if (!session || session.unread_count <= 0 || !this.active || !this.pageVisible) return;
      if (readTimer) return;
      readTimer = setTimeout(async () => {
        readTimer = null;
        const umo = this.currentUmo;
        if (!umo || !this.currentSession?.unread_count || !this.active || !this.pageVisible) return;
        if (readInFlight) {
          readAgain = true;
          return;
        }
        readInFlight = true;
        try {
          const updated = await snsApi.markRead(umo);
          if (this.active) this.applySession(updated, 'updated');
        } catch {
          // A later message or visibility change retries.
        } finally {
          readInFlight = false;
          if (readAgain) {
            readAgain = false;
            this.maybeMarkRead();
          }
        }
      }, 300);
    },

    async togglePinned(session: ImSession) {
      this.applySession(await snsApi.patchSession({ umo: session.umo, pinned: !session.pinned }), 'updated');
    },

    async toggleKeepForever(session: ImSession) {
      this.applySession(
        await snsApi.patchSession({ umo: session.umo, keep_forever: !session.keep_forever }),
        'updated',
      );
    },

    async deleteSession(umo: string) {
      await snsApi.deleteSession(umo);
      this.removeSession(umo);
    },
  },
});
