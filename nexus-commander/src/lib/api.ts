// ═══════════════════════════════════════════════════════════════
//  NEXUS Commander — API Client (JWT Interceptor)
// ═══════════════════════════════════════════════════════════════

import { ENDPOINTS } from './constants';
import type {
  TokenResponse,
  TradesResponse,
  RiskSettingsRequest,
  AIModeRequest,
  VenueRequest,
  AccountTypeRequest,
} from '../types/api';

/** Get the stored JWT token */
function getToken(): string | null {
  return localStorage.getItem('nexus_token');
}

/** Authenticated fetch wrapper */
async function authFetch(url: string, options: RequestInit = {}): Promise<Response> {
  const token = getToken();
  if (!token) {
    throw new Error('Not authenticated');
  }

  const headers = new Headers(options.headers);
  headers.set('Authorization', `Bearer ${token}`);
  if (!headers.has('Content-Type') && options.body) {
    headers.set('Content-Type', 'application/json');
  }

  const response = await fetch(url, { ...options, headers });

  if (response.status === 401) {
    localStorage.removeItem('nexus_token');
    window.location.reload();
    throw new Error('Token expired');
  }

  if (!response.ok) {
    const detail = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(detail.detail ?? `HTTP ${response.status}`);
  }

  return response;
}

// ── Auth ────────────────────────────────────────────────────────

export async function login(username: string, password: string): Promise<TokenResponse> {
  const body = new URLSearchParams({ username, password });
  const res = await fetch(ENDPOINTS.LOGIN, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body,
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Login failed' }));
    throw new Error(err.detail);
  }

  const data: TokenResponse = await res.json();
  localStorage.setItem('nexus_token', data.access_token);
  return data;
}

export function logout(): void {
  localStorage.removeItem('nexus_token');
  window.location.reload();
}

// ── Telemetry ───────────────────────────────────────────────────

export async function fetchTrades(limit = 20): Promise<TradesResponse> {
  const res = await authFetch(`${ENDPOINTS.TRADES}?limit=${limit}`);
  return res.json();
}

// ── Control Panel ───────────────────────────────────────────────

export async function triggerPanic(): Promise<void> {
  await authFetch(ENDPOINTS.PANIC, { method: 'POST' });
}

export async function resetPanic(): Promise<void> {
  await authFetch(ENDPOINTS.PANIC_RESET, { method: 'POST' });
}

export async function updateRiskSettings(settings: RiskSettingsRequest): Promise<void> {
  await authFetch(ENDPOINTS.RISK_SETTINGS, {
    method: 'POST',
    body: JSON.stringify(settings),
  });
}

export async function toggleAIMode(req: AIModeRequest): Promise<void> {
  await authFetch(ENDPOINTS.AI_MODE, {
    method: 'POST',
    body: JSON.stringify(req),
  });
}

export async function switchVenue(req: VenueRequest): Promise<void> {
  await authFetch(ENDPOINTS.VENUE, {
    method: 'POST',
    body: JSON.stringify(req),
  });
}

export async function switchAccountType(req: AccountTypeRequest): Promise<void> {
  await authFetch(ENDPOINTS.ACCOUNT_TYPE, {
    method: 'POST',
    body: JSON.stringify(req),
  });
}
