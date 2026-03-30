// ═══════════════════════════════════════════════════════════════
//  NEXUS Commander — Auth Store (Zustand)
// ═══════════════════════════════════════════════════════════════

import { create } from 'zustand';
import { login as apiLogin, logout as apiLogout } from '../lib/api';

interface AuthState {
  token: string | null;
  user: string | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  error: string | null;
  login: (username: string, password: string) => Promise<void>;
  logout: () => void;
  hydrate: () => void;
}

export const useAuthStore = create<AuthState>((set) => ({
  token: null,
  user: null,
  isAuthenticated: false,
  isLoading: false,
  error: null,

  login: async (username: string, password: string) => {
    set({ isLoading: true, error: null });
    try {
      const res = await apiLogin(username, password);
      set({
        token: res.access_token,
        user: username,
        isAuthenticated: true,
        isLoading: false,
        error: null,
      });
    } catch (err) {
      set({
        isLoading: false,
        error: err instanceof Error ? err.message : 'Login failed',
      });
    }
  },

  logout: () => {
    apiLogout();
    set({
      token: null,
      user: null,
      isAuthenticated: false,
      error: null,
    });
  },

  hydrate: () => {
    const token = localStorage.getItem('nexus_token');
    if (token) {
      try {
        // Decode JWT payload to check expiry
        const payload = JSON.parse(atob(token.split('.')[1]));
        const now = Math.floor(Date.now() / 1000);
        if (payload.exp && payload.exp > now) {
          set({
            token,
            user: payload.sub ?? 'nexus',
            isAuthenticated: true,
          });
        } else {
          localStorage.removeItem('nexus_token');
        }
      } catch {
        localStorage.removeItem('nexus_token');
      }
    }
  },
}));
