// ═══════════════════════════════════════════════════════════════
//  NEXUS Commander — WebSocket Engine
//  Exponential Backoff | Auto-Reconnect | Heartbeat | Latency
// ═══════════════════════════════════════════════════════════════

import type { WSConnectionStatus, WSFrame } from '../types/ws';

type FrameHandler = (frame: WSFrame) => void;
type StatusHandler = (status: WSConnectionStatus) => void;

interface WSEngineConfig {
  url: string;
  onFrame: FrameHandler;
  onStatus: StatusHandler;
  /** Base reconnect delay in ms (default: 1000) */
  baseDelay?: number;
  /** Max reconnect delay in ms (default: 30000) */
  maxDelay?: number;
  /** Heartbeat interval in ms (default: 25000) */
  heartbeatInterval?: number;
}

/**
 * Institutional-grade WebSocket manager with:
 * - Exponential backoff with jitter
 * - Automatic reconnection
 * - Heartbeat keepalive
 * - Latency measurement
 * - Clean teardown
 */
export class WSEngine {
  private ws: WebSocket | null = null;
  private readonly url: string;
  private readonly onFrame: FrameHandler;
  private readonly onStatus: StatusHandler;
  private readonly baseDelay: number;
  private readonly maxDelay: number;
  private readonly heartbeatInterval: number;

  private reconnectAttempt = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  private intentionalClose = false;
  private _status: WSConnectionStatus = 'DISCONNECTED';
  private _latencyMs = 0;

  constructor(config: WSEngineConfig) {
    this.url = config.url;
    this.onFrame = config.onFrame;
    this.onStatus = config.onStatus;
    this.baseDelay = config.baseDelay ?? 1000;
    this.maxDelay = config.maxDelay ?? 30000;
    this.heartbeatInterval = config.heartbeatInterval ?? 25000;
  }

  /** Current connection status */
  get status(): WSConnectionStatus {
    return this._status;
  }

  /** Last measured latency in ms */
  get latencyMs(): number {
    return this._latencyMs;
  }

  /** Connect to the WebSocket server */
  connect(): void {
    if (this.ws?.readyState === WebSocket.OPEN || this.ws?.readyState === WebSocket.CONNECTING) {
      return;
    }

    this.intentionalClose = false;
    this.setStatus('CONNECTING');

    try {
      this.ws = new WebSocket(this.url);
    } catch {
      this.scheduleReconnect();
      return;
    }

    this.ws.onopen = () => {
      this.reconnectAttempt = 0;
      this.setStatus('CONNECTED');
      this.startHeartbeat();
    };

    this.ws.onmessage = (event: MessageEvent) => {
      try {
        const frame = JSON.parse(event.data as string) as WSFrame;

        // Measure latency from server timestamp
        if ('timestamp' in frame && frame.timestamp) {
          const serverTime = new Date(frame.timestamp).getTime();
          this._latencyMs = Math.max(0, Date.now() - serverTime);
        }

        this.onFrame(frame);
      } catch {
        // Silently drop malformed frames
      }
    };

    this.ws.onclose = () => {
      this.stopHeartbeat();
      if (!this.intentionalClose) {
        this.setStatus('RECONNECTING');
        this.scheduleReconnect();
      } else {
        this.setStatus('DISCONNECTED');
      }
    };

    this.ws.onerror = () => {
      // onclose will fire after this — reconnection handled there
    };
  }

  /** Disconnect cleanly */
  disconnect(): void {
    this.intentionalClose = true;
    this.stopHeartbeat();
    this.clearReconnectTimer();
    if (this.ws) {
      if (this.ws.readyState === WebSocket.CONNECTING) {
        // If it's still connecting, wait for exactly open to close, suppressing browser error
        this.ws.onopen = () => {
          this.ws?.close();
        };
      } else {
        this.ws.close();
      }
      this.ws = null;
    }
    this.setStatus('DISCONNECTED');
  }

  /** Schedule reconnection with exponential backoff + jitter */
  private scheduleReconnect(): void {
    this.clearReconnectTimer();

    // Exponential backoff: base * 2^attempt + random jitter
    const delay = Math.min(
      this.baseDelay * Math.pow(2, this.reconnectAttempt) + Math.random() * 1000,
      this.maxDelay
    );

    this.reconnectAttempt++;

    this.reconnectTimer = setTimeout(() => {
      this.connect();
    }, delay);
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  /** Keepalive heartbeat to detect stale connections */
  private startHeartbeat(): void {
    this.stopHeartbeat();
    this.heartbeatTimer = setInterval(() => {
      if (this.ws?.readyState === WebSocket.OPEN) {
        // Send a ping frame (text-based, server can ignore)
        try {
          this.ws.send(JSON.stringify({ type: 'ping', timestamp: new Date().toISOString() }));
        } catch {
          // Connection likely dead — will reconnect on close
        }
      }
    }, this.heartbeatInterval);
  }

  private stopHeartbeat(): void {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
  }

  private setStatus(s: WSConnectionStatus): void {
    this._status = s;
    this.onStatus(s);
  }
}
