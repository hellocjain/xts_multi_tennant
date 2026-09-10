import React, { useState } from 'react';
import { api } from '../../services/api';
import { Lock, User, KeyRound, Loader2, ShieldCheck } from 'lucide-react';

interface LoginViewProps {
  onLoginSuccess: () => void;
}

export const LoginView: React.FC<LoginViewProps> = ({ onLoginSuccess }) => {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setIsLoading(true);

    try {
      await api.login(username, password);
      onLoginSuccess();
    } catch (err: any) {
      setError(err.message || 'Invalid credentials or login failed');
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="min-h-screen w-full bg-[#05070A] flex flex-col items-center justify-center p-4 relative overflow-hidden">
      {/* Background Decorative Glow */}
      <div className="absolute w-[500px] h-[500px] bg-brand-600/10 rounded-full blur-[120px] pointer-events-none -top-40 -left-40" />
      <div className="absolute w-[400px] h-[400px] bg-emerald-600/5 rounded-full blur-[100px] pointer-events-none -bottom-20 -right-20" />

      <div className="w-full max-w-md bg-cardbg/90 backdrop-blur-xl border border-bordercolor rounded-3xl p-8 shadow-2xl space-y-6 relative z-10">
        {/* Brand Title */}
        <div className="text-center space-y-2">
          <div className="inline-flex items-center justify-center w-12 h-12 rounded-2xl bg-brand-600/20 border border-brand-500/40 text-brand-400 font-mono font-black text-xl shadow-inner mb-1">
            X
          </div>
          <h1 className="text-lg font-bold tracking-wider text-slate-100 uppercase">
            XTS TERMINAL
          </h1>
          <p className="text-xs text-slate-400 font-mono">
            Multi-Tenant Institutional Execution Portal
          </p>
        </div>

        {error && (
          <div className="p-3 bg-rose-500/10 border border-rose-500/30 rounded-xl text-rose-400 text-xs font-mono">
            {error}
          </div>
        )}

        {/* Login Form */}
        <form onSubmit={handleSubmit} className="space-y-4 text-xs font-mono">
          <div className="space-y-1.5">
            <label htmlFor="login-username" className="text-slate-400 font-medium">Username</label>
            <div className="relative">
              <User className="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-500" />
              <input
                id="login-username"
                type="text"
                required
                autoFocus
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="Admin username"
                className="w-full pl-10 pr-4 py-2.5 rounded-xl bg-obsidian border border-bordercolor text-slate-100 placeholder-slate-600 focus:outline-none focus:border-brand-500 transition"
              />
            </div>
          </div>

          <div className="space-y-1.5">
            <label htmlFor="login-password" className="text-slate-400 font-medium">Password</label>
            <div className="relative">
              <Lock className="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-500" />
              <input
                id="login-password"
                type="password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="••••••••••••"
                className="w-full pl-10 pr-4 py-2.5 rounded-xl bg-obsidian border border-bordercolor text-slate-100 placeholder-slate-600 focus:outline-none focus:border-brand-500 transition"
              />
            </div>
          </div>

          <button
            type="submit"
            disabled={isLoading}
            className="w-full py-3 bg-brand-600 hover:bg-brand-500 active:bg-brand-700 text-white font-bold text-xs uppercase tracking-wider rounded-xl transition shadow-lg shadow-brand-600/25 flex items-center justify-center space-x-2 mt-2"
          >
            {isLoading ? (
              <>
                <Loader2 className="w-4 h-4 animate-spin" />
                <span>Authenticating...</span>
              </>
            ) : (
              <>
                <KeyRound className="w-4 h-4" />
                <span>Sign In to Terminal</span>
              </>
            )}
          </button>
        </form>

        <div className="pt-2 border-t border-bordercolor/60 flex items-center justify-center space-x-1 text-[11px] font-mono text-slate-500">
          <ShieldCheck className="w-3.5 h-3.5 text-slate-400" />
          <span>AES-256 GCM • Multi-Tenant Protected</span>
        </div>
      </div>
    </div>
  );
};
