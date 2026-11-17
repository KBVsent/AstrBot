declare module 'event-source-polyfill' {
  export interface EventSourcePolyfillInit {
    headers?: Record<string, string>;
    heartbeatTimeout?: number;
    withCredentials?: boolean;
    lastEventIdQueryParameterName?: string;
  }

  /** status is set when the server answered with a non-200 response. */
  export interface EventSourcePolyfillErrorEvent extends Event {
    status?: number;
    statusText?: string;
    error?: Error;
  }

  export class EventSourcePolyfill {
    constructor(url: string, init?: EventSourcePolyfillInit);
    readonly url: string;
    readonly readyState: number;
    withCredentials: boolean;
    onopen: ((event: Event) => void) | null;
    onmessage: ((event: MessageEvent<string>) => void) | null;
    onerror: ((event: EventSourcePolyfillErrorEvent) => void) | null;
    addEventListener(type: string, listener: (event: MessageEvent<string>) => void): void;
    removeEventListener(type: string, listener: (event: MessageEvent<string>) => void): void;
    close(): void;
  }
}
