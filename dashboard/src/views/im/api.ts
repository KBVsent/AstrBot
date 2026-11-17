import { apiV1Client } from '@/api/http';

export type ImMessageType = 'GroupMessage' | 'FriendMessage' | 'OtherMessage';

export interface ImSession {
  umo: string;
  platform_id: string;
  platform_type: string;
  message_type: ImMessageType | string;
  session_id: string;
  display_name: string;
  last_message_id: number | null;
  last_preview: string;
  last_sender_name: string;
  last_time: string;
  unread_count: number;
  pinned: boolean;
  keep_forever: boolean;
  purged_until: string | null;
}

export type ImSegment =
  | { type: 'Plain'; text: string }
  | { type: 'At'; target: string; name: string }
  | { type: 'Reply'; id: string; sender_name: string; text: string }
  | { type: 'Image'; available: boolean }
  | { type: 'Other'; name: string; text: string };

export type ImMessageStatus = 'ok' | 'sending' | 'failed';

export interface ImMessage {
  id: number;
  umo: string;
  direction: 'in' | 'out';
  platform_message_id: string | null;
  sender_id: string;
  sender_name: string;
  content: ImSegment[];
  source: string | null;
  client_id: string | null;
  status: ImMessageStatus;
  error: string | null;
  created_at: string;
}

export interface ImPlatform {
  id: string;
  type: string;
}

export interface ImSessionPage {
  sessions: ImSession[];
  next_cursor: string | null;
  unread_sessions: number;
  platforms: ImPlatform[];
}

export interface ImMessagePage {
  messages: ImMessage[];
  has_more: boolean;
  purged_until: string | null;
}

export interface ImSessionQuery {
  type?: string;
  platform_id?: string;
  q?: string;
  limit?: number;
  cursor?: string | null;
}

interface Envelope<T> {
  status: string;
  message?: string | null;
  data: T;
}

async function unwrap<T>(request: Promise<{ data: Envelope<T> }>): Promise<T> {
  const response = await request;
  if (response.data?.status !== 'ok') {
    throw new Error(response.data?.message || 'Request failed');
  }
  return response.data.data;
}

// An empty type or platform must be left out: the backend rejects empty enum values.
function compact<T extends object>(params: T): Partial<T> {
  return Object.fromEntries(
    Object.entries(params).filter(([, value]) => value !== undefined && value !== null && value !== ''),
  ) as Partial<T>;
}

export function errorStatus(error: unknown): number | undefined {
  return (error as { response?: { status?: number } } | null)?.response?.status;
}

export function errorMessage(error: unknown): string {
  if (typeof error === 'string') return error;
  const data = (error as { response?: { data?: { message?: unknown } } } | null)?.response?.data;
  if (typeof data?.message === 'string' && data.message) return data.message;
  if (error instanceof Error && error.message) return error.message;
  return String(error);
}

export const snsApi = {
  listSessions(query: ImSessionQuery) {
    return unwrap<ImSessionPage>(apiV1Client.get('/sns/sessions', { params: compact(query) }));
  },
  patchSession(body: { umo: string; pinned?: boolean; keep_forever?: boolean }) {
    return unwrap<ImSession>(apiV1Client.patch('/sns/session', body));
  },
  markRead(umo: string) {
    return unwrap<ImSession>(apiV1Client.post('/sns/session/read', { umo }));
  },
  deleteSession(umo: string) {
    return unwrap<{ umo: string }>(apiV1Client.delete('/sns/session', { params: { umo } }));
  },
  listMessages(query: { umo: string; before_id?: number; after_id?: number; limit?: number }) {
    return unwrap<ImMessagePage>(apiV1Client.get('/sns/messages', { params: compact(query) }));
  },
  sendMessage(body: { umo: string; text: string; client_id: string }) {
    return unwrap<ImMessage>(apiV1Client.post('/sns/messages', body));
  },
  // Fetched with the token header: the Secure login cookie is not sent over plain HTTP.
  async fetchImage(messageId: number, index: number) {
    const response = await apiV1Client.get<Blob>('/sns/image', {
      params: { message_id: messageId, index },
      responseType: 'blob',
    });
    return response.data;
  },
  eventsUrl() {
    return '/api/v1/sns/events';
  },
};
