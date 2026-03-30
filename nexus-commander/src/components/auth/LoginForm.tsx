// ═══════════════════════════════════════════════════════════════
//  NEXUS Commander — Login Form
// ═══════════════════════════════════════════════════════════════

import { useState, type FormEvent } from 'react';
import { useAuthStore } from '../../store/auth-store';
import { Shield, Eye, EyeOff, Zap } from 'lucide-react';

export default function LoginForm() {
  const { login, isLoading, error } = useAuthStore();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (username.trim() && password.trim()) {
      login(username.trim(), password.trim());
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-nexus-bg relative overflow-hidden">
      {/* Animated background grid */}
      <div className="absolute inset-0 opacity-5">
        <div
          className="w-full h-full"
          style={{
            backgroundImage: `linear-gradient(rgba(6,182,212,0.3) 1px, transparent 1px),
                              linear-gradient(90deg, rgba(6,182,212,0.3) 1px, transparent 1px)`,
            backgroundSize: '50px 50px',
          }}
        />
      </div>

      {/* Scan line effect */}
      <div
        className="absolute inset-0 pointer-events-none opacity-[0.03]"
        style={{ animation: 'scan-line 8s linear infinite' }}
      >
        <div className="w-full h-[2px] bg-nexus-cyan" />
      </div>

      <div className="glass-card p-10 w-full max-w-md animate-fade-in relative z-10">
        {/* Header */}
        <div className="flex flex-col items-center mb-8">
          <div className="w-16 h-16 rounded-2xl bg-gradient-to-br from-nexus-cyan to-nexus-purple flex items-center justify-center mb-4 animate-pulse-glow">
            <Shield className="w-8 h-8 text-white" />
          </div>
          <h1 className="text-2xl font-bold text-nexus-text tracking-tight">
            NEXUS <span className="text-nexus-cyan">COMMANDER</span>
          </h1>
          <p className="text-nexus-text-muted text-sm mt-1">
            Operational Control Panel v4.0
          </p>
        </div>

        {/* Form */}
        <form onSubmit={handleSubmit} className="space-y-5">
          <div>
            <label htmlFor="login-user" className="block text-xs font-medium text-nexus-text-dim mb-1.5 uppercase tracking-wider">
              Operator ID
            </label>
            <input
              id="login-user"
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className="w-full bg-nexus-bg border border-nexus-border rounded-lg px-4 py-3 text-nexus-text
                         placeholder:text-nexus-text-muted focus:outline-none focus:border-nexus-cyan
                         focus:ring-1 focus:ring-nexus-cyan/30 transition-all duration-200
                         font-[family-name:var(--font-mono)] text-sm"
              placeholder="Enter username"
              autoComplete="username"
              autoFocus
            />
          </div>

          <div>
            <label htmlFor="login-pass" className="block text-xs font-medium text-nexus-text-dim mb-1.5 uppercase tracking-wider">
              Security Key
            </label>
            <div className="relative">
              <input
                id="login-pass"
                type={showPassword ? 'text' : 'password'}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="w-full bg-nexus-bg border border-nexus-border rounded-lg px-4 py-3 pr-12 text-nexus-text
                           placeholder:text-nexus-text-muted focus:outline-none focus:border-nexus-cyan
                           focus:ring-1 focus:ring-nexus-cyan/30 transition-all duration-200
                           font-[family-name:var(--font-mono)] text-sm"
                placeholder="Enter password"
                autoComplete="current-password"
              />
              <button
                type="button"
                onClick={() => setShowPassword(!showPassword)}
                className="absolute right-3 top-1/2 -translate-y-1/2 text-nexus-text-muted hover:text-nexus-text transition-colors"
              >
                {showPassword ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
              </button>
            </div>
          </div>

          {error && (
            <div className="bg-nexus-red/10 border border-nexus-red/30 rounded-lg px-4 py-2.5 text-nexus-red text-sm animate-slide-in">
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={isLoading || !username.trim() || !password.trim()}
            className="w-full bg-gradient-to-r from-nexus-cyan to-nexus-purple text-white font-semibold
                       py-3 rounded-lg transition-all duration-300 hover:opacity-90 hover:shadow-lg
                       hover:shadow-nexus-cyan/20 disabled:opacity-40 disabled:cursor-not-allowed
                       flex items-center justify-center gap-2 text-sm uppercase tracking-wider"
          >
            {isLoading ? (
              <div className="w-5 h-5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
            ) : (
              <>
                <Zap className="w-4 h-4" />
                Authenticate
              </>
            )}
          </button>
        </form>

        {/* Footer */}
        <div className="mt-6 pt-4 border-t border-nexus-border text-center">
          <p className="text-nexus-text-muted text-xs">
            Encrypted Connection • HS256 JWT • 24h Session
          </p>
        </div>
      </div>
    </div>
  );
}
