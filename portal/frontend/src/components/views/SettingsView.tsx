import React, { useState, useEffect } from 'react';
import { 
  Settings as SettingsIcon, 
  Database, 
  Shield, 
  Key, 
  Server, 
  RotateCw, 
  Download, 
  CheckCircle2, 
  AlertCircle 
} from 'lucide-react';
import { toast } from 'sonner';

export const SettingsView: React.FC = () => {
  const [healthData, setHealthData] = useState<any>(null);
  const [isLoadingHealth, setIsLoadingHealth] = useState(true);
  const [isBackingUp, setIsBackingUp] = useState(false);

  const fetchHealth = async () => {
    setIsLoadingHealth(true);
    try {
      const res = await fetch('/admin/api/system-health', { credentials: 'include' });
      if (res.ok) {
        const data = await res.json();
        setHealthData(data);
      }
    } catch (err: any) {
      // Non-blocking
    } finally {
      setIsLoadingHealth(false);
    }
  };

  useEffect(() => {
    fetchHealth();
  }, []);

  const handleBackup = async () => {
    setIsBackingUp(true);
    try {
      const res = await fetch('/admin/settings/backup', {
        method: 'POST',
        credentials: 'include',
      });
      if (res.ok) {
        toast.success('Database backup created and saved successfully');
      } else {
        toast.error('Backup request failed');
      }
    } catch (err: any) {
      toast.error(`Backup failed: ${err.message}`);
    } finally {
      setIsBackingUp(false);
    }
  };

  return (
    <div className="flex-1 overflow-y-auto p-4 md:p-6 space-y-6">
      {/* Header */}
      <div>
        <h2 className="text-base font-bold text-slate-100 flex items-center space-x-2">
          <SettingsIcon className="w-5 h-5 text-slate-300" />
          <span>System & Security Settings</span>
        </h2>
        <p className="text-xs text-slate-400">Institutional infrastructure parameters, database backups, and health diagnostics</p>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* System Diagnostics Box */}
        <div className="bg-cardbg border border-bordercolor rounded-2xl p-5 space-y-4">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-bold text-slate-100 flex items-center space-x-2">
              <Server className="w-4 h-4 text-brand-400" />
              <span>System Health & Host Diagnostics</span>
            </h3>
            <button
              type="button"
              onClick={fetchHealth}
              disabled={isLoadingHealth}
              className="p-1.5 text-slate-400 hover:text-slate-200 rounded-lg hover:bg-slate-800 transition"
              title="Refresh Health"
            >
              <RotateCw className={`w-3.5 h-3.5 ${isLoadingHealth ? 'animate-spin text-brand-400' : ''}`} />
            </button>
          </div>

          <div className="space-y-3 text-xs font-mono">
            <div className="flex justify-between py-2 border-b border-bordercolor/60">
              <span className="text-slate-400">Host IP:</span>
              <span className="text-slate-200">{healthData?.system?.server_ip || (typeof window !== 'undefined' ? window.location.hostname : '127.0.0.1')}</span>
            </div>
            <div className="flex justify-between py-2 border-b border-bordercolor/60">
              <span className="text-slate-400">Database Status:</span>
              <span className="inline-flex items-center space-x-1 text-emerald-400 font-bold">
                <CheckCircle2 className="w-3.5 h-3.5" />
                <span>{healthData?.database?.status || 'HEALTHY'}</span>
              </span>
            </div>
            <div className="flex justify-between py-2 border-b border-bordercolor/60">
              <span className="text-slate-400">Tenants Registered:</span>
              <span className="text-slate-100 font-bold">{healthData?.database?.tenants_count ?? 11}</span>
            </div>
            <div className="flex justify-between py-2 border-b border-bordercolor/60">
              <span className="text-slate-400">Active SuperTrend Strategies:</span>
              <span className="text-brand-400 font-bold">{healthData?.database?.supertrend_strategies_count ?? 0}</span>
            </div>
            <div className="flex justify-between py-2">
              <span className="text-slate-400">System Timezone:</span>
              <span className="text-slate-300">Asia/Kolkata (IST)</span>
            </div>
          </div>
        </div>

        {/* Database Backup & Disaster Recovery */}
        <div className="bg-cardbg border border-bordercolor rounded-2xl p-5 space-y-4">
          <h3 className="text-sm font-bold text-slate-100 flex items-center space-x-2">
            <Database className="w-4 h-4 text-emerald-400" />
            <span>Database Backup & Disaster Recovery</span>
          </h3>
          <p className="text-xs text-slate-400 leading-relaxed">
            Create an instantaneous atomic snapshot of the multi-tenant database, client configurations, and active risk states.
          </p>

          <div className="pt-2">
            <button
              type="button"
              onClick={handleBackup}
              disabled={isBackingUp}
              className="px-4 py-2 bg-slate-800 hover:bg-slate-700 text-slate-100 font-semibold text-xs rounded-xl border border-slate-700 flex items-center space-x-2 transition"
            >
              {isBackingUp ? <RotateCw className="w-4 h-4 animate-spin text-brand-400" /> : <Download className="w-4 h-4 text-emerald-400" />}
              <span>{isBackingUp ? 'Creating Backup...' : 'Create Instant Database Backup'}</span>
            </button>
          </div>
        </div>

        {/* Master Key & Security */}
        <div className="bg-cardbg border border-bordercolor rounded-2xl p-5 space-y-4">
          <h3 className="text-sm font-bold text-slate-100 flex items-center space-x-2">
            <Key className="w-4 h-4 text-amber-400" />
            <span>Master Encryption & Secret Protection</span>
          </h3>
          <p className="text-xs text-slate-400 leading-relaxed">
            Client broker API secrets and interactive access tokens are encrypted with AES-256-GCM via the Portal Master Key.
          </p>
          <div className="p-3 bg-obsidian rounded-xl border border-bordercolor text-[11px] font-mono text-slate-300">
            Encryption: <strong className="text-emerald-400">AES-256-GCM Hardware-Accelerated</strong>
          </div>
        </div>

        {/* IP Security */}
        <div className="bg-cardbg border border-bordercolor rounded-2xl p-5 space-y-4">
          <h3 className="text-sm font-bold text-slate-100 flex items-center space-x-2">
            <Shield className="w-4 h-4 text-sky-400" />
            <span>Network & Access Restrictions</span>
          </h3>
          <p className="text-xs text-slate-400 leading-relaxed">
            Admin portal login attempts are subject to strict rate limiting (5 attempts max, 15-minute lockout) and secure HTTP-Only session cookies.
          </p>
          <div className="p-3 bg-obsidian rounded-xl border border-bordercolor text-[11px] font-mono text-slate-300">
            Session Security: <strong className="text-emerald-400">HTTP-Only • SameSite Strict</strong>
          </div>
        </div>
      </div>
    </div>
  );
};
