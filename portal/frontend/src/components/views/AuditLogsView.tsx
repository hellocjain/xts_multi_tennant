import React, { useState, useEffect } from 'react';
import { api } from '../../services/api';
import { ShieldCheck, Search, RotateCw, ChevronRight, ChevronDown, X } from 'lucide-react';
import { toast } from 'sonner';

export const AuditLogsView: React.FC = () => {
  const [logs, setLogs] = useState<any[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [expandedLogId, setExpandedLogId] = useState<string | null>(null);

  const fetchLogs = async () => {
    setIsLoading(true);
    try {
      const res = await api.getAuditLogs(150);
      setLogs(res.logs || []);
    } catch (err: any) {
      toast.error(`Failed to load audit logs: ${err.message}`);
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    fetchLogs();
  }, []);

  const filteredLogs = logs.filter((log) => {
    const q = search.toLowerCase().trim();
    if (!q) return true;
    return (
      (log.actor && log.actor.toLowerCase().includes(q)) ||
      (log.action && log.action.toLowerCase().includes(q)) ||
      (log.target_tenant_id && log.target_tenant_id.toLowerCase().includes(q)) ||
      (log.details_json && log.details_json.toLowerCase().includes(q))
    );
  });

  return (
    <div className="flex-1 flex flex-col overflow-hidden p-4 md:p-6 space-y-4">
      {/* Title */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-base font-bold text-slate-100 flex items-center space-x-2">
            <ShieldCheck className="w-5 h-5 text-amber-400" />
            <span>Security & Audit Log</span>
          </h2>
          <p className="text-xs text-slate-400">Immutable trace of administrative actions and trading overrides</p>
        </div>
        <button
          type="button"
          onClick={fetchLogs}
          disabled={isLoading}
          className="p-2 bg-slate-800 hover:bg-slate-750 text-slate-200 rounded-xl border border-bordercolor transition cursor-pointer"
          title="Refresh Logs"
        >
          <RotateCw className={`w-4 h-4 ${isLoading ? 'animate-spin text-brand-400' : ''}`} />
        </button>
      </div>

      {/* Filter */}
      <div className="bg-cardbg/80 border border-bordercolor p-3 rounded-2xl">
        <div className="relative max-w-md">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
          <input
            type="text"
            data-search="true"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search by action, actor, or tenant... (Cmd+K)"
            className="w-full pl-9 pr-9 py-2 rounded-xl bg-obsidian border border-bordercolor text-xs text-slate-100 placeholder-slate-500 focus:outline-none focus:border-brand-500 transition"
          />
          {search && (
            <button
              type="button"
              onClick={() => setSearch('')}
              className="absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-200 p-0.5 rounded-full hover:bg-slate-800 transition cursor-pointer"
              title="Clear search"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          )}
        </div>
      </div>

      {/* Logs Table */}
      <div className="flex-1 bg-cardbg border border-bordercolor rounded-2xl overflow-hidden shadow-sm flex flex-col">
        <div className="flex-1 overflow-y-auto">
          <table className="w-full text-left text-xs font-mono">
            <thead className="bg-slate-950/80 text-slate-400 text-[11px] uppercase tracking-wider border-b border-bordercolor sticky top-0 z-10 select-none">
              <tr>
                <th className="p-3 w-10"></th>
                <th className="p-3">Time</th>
                <th className="p-3">Actor</th>
                <th className="p-3">Action</th>
                <th className="p-3">Target Client</th>
                <th className="p-3">Details Summary</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-bordercolor/50 text-slate-200">
              {filteredLogs.length === 0 ? (
                <tr>
                  <td colSpan={6} className="p-12 text-center text-slate-500 italic">
                    No audit records found.
                  </td>
                </tr>
              ) : (
                filteredLogs.map((l, idx) => {
                  const isExpanded = expandedLogId === (l.id || String(idx));
                  return (
                    <React.Fragment key={l.id || idx}>
                      <tr
                        onClick={() => setExpandedLogId(isExpanded ? null : (l.id || String(idx)))}
                        className="hover:bg-slate-800/40 cursor-pointer transition"
                      >
                        <td className="p-3 text-slate-500">
                          {isExpanded ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
                        </td>
                        <td className="p-3 text-slate-400 whitespace-nowrap">{l.formatted_time || l.timestamp}</td>
                        <td className="p-3 font-semibold text-brand-300">{l.actor}</td>
                        <td className="p-3">
                          <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-slate-800 text-slate-200 border border-slate-700">
                            {l.action}
                          </span>
                        </td>
                        <td className="p-3 text-slate-400">{l.target_tenant_id || '-'}</td>
                        <td className="p-3 text-slate-400 truncate max-w-md">{l.details_json}</td>
                      </tr>
                      {isExpanded && (
                        <tr className="bg-obsidian/90">
                          <td colSpan={6} className="p-4 border-t border-bordercolor/40">
                            <div className="text-[10px] text-slate-400 uppercase tracking-wider mb-1">Raw Payload & Diagnostics</div>
                            <pre className="p-3 bg-slate-950 border border-bordercolor rounded-xl text-[11px] font-mono text-emerald-400 overflow-x-auto whitespace-pre-wrap">
                              {(() => {
                                try {
                                  return JSON.stringify(JSON.parse(l.details_json), null, 2);
                                } catch {
                                  return l.details_json;
                                }
                              })()}
                            </pre>
                          </td>
                        </tr>
                      )}
                    </React.Fragment>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
};
