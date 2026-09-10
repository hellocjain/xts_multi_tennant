import React, { useState, useMemo } from 'react';
import { 
  ClientSummary, 
  DashboardTelemetry 
} from '../../types/telemetry';
import { 
  Search, 
  Grid, 
  List, 
  TrendingUp, 
  TrendingDown, 
  Pause, 
  Play, 
  Flame, 
  ExternalLink, 
  ShieldAlert, 
  Wallet, 
  Activity, 
  Layers, 
  RotateCw,
  Plus
} from 'lucide-react';
import { toast } from 'sonner';

interface DashboardViewProps {
  telemetry?: DashboardTelemetry;
  isLoading: boolean;
  onRefresh: () => void;
  onOpenClientTab: (clientId: string, clientName: string) => void;
  onToggleTrading: (clientId: string, pause: boolean) => Promise<void>;
  onPanicClient: (clientId: string) => Promise<void>;
  onOpenAddClient: () => void;
}

export const DashboardView: React.FC<DashboardViewProps> = ({
  telemetry,
  isLoading,
  onRefresh,
  onOpenClientTab,
  onToggleTrading,
  onPanicClient,
  onOpenAddClient,
}) => {
  const [searchQuery, setSearchQuery] = useState('');
  const [activeFilter, setActiveFilter] = useState<'all' | 'live' | 'paper' | 'profit' | 'loss' | 'paused'>('all');
  const [viewMode, setViewMode] = useState<'cards' | 'table'>('cards');
  const [actionLoadingId, setActionLoadingId] = useState<string | null>(null);

  const clients = telemetry?.clients || [];

  const filteredClients = useMemo(() => {
    return clients.filter((c) => {
      // Search match
      const q = searchQuery.toLowerCase().trim();
      const matchQuery = 
        !q || 
        c.id.toLowerCase().includes(q) || 
        c.name.toLowerCase().includes(q) || 
        c.broker_client_id.toLowerCase().includes(q);

      if (!matchQuery) return false;

      // Category filter
      if (activeFilter === 'live') return c.execution_mode === 'LIVE';
      if (activeFilter === 'paper') return c.execution_mode === 'PAPER';
      if (activeFilter === 'profit') return c.net_mtm > 0;
      if (activeFilter === 'loss') return c.net_mtm < 0;
      if (activeFilter === 'paused') return c.trading_paused || c.status === 'PAUSED';

      return true;
    });
  }, [clients, searchQuery, activeFilter]);

  const handleTogglePause = async (client: ClientSummary) => {
    const newPause = !client.trading_paused && client.status !== 'PAUSED';
    setActionLoadingId(client.id);
    try {
      await onToggleTrading(client.id, newPause);
      toast.success(`${client.name || client.id} trading is now ${newPause ? 'PAUSED' : 'ACTIVE'}`);
    } catch (err: any) {
      toast.error(`Failed to update trading status: ${err.message}`);
    } finally {
      setActionLoadingId(null);
    }
  };

  const handlePanic = async (client: ClientSummary) => {
    if (!confirm(`🚨 PANIC CONFIRMATION: Square off all positions and cancel orders for ${client.name || client.id}?`)) {
      return;
    }
    setActionLoadingId(client.id);
    try {
      await onPanicClient(client.id);
      toast.success(`Panic square-off completed for ${client.name || client.id}`);
    } catch (err: any) {
      toast.error(`Panic failed: ${err.message}`);
    } finally {
      setActionLoadingId(null);
    }
  };

  const formatINR = (val: number = 0) => {
    return `₹${val.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  };

  const aggregate = telemetry || {
    aggregate_net_mtm: 0,
    aggregate_realized_pnl: 0,
    aggregate_unrealized_mtm: 0,
    total_clients_count: clients.length,
    active_clients_count: clients.filter((c) => c.status === 'ACTIVE').length,
    paper_clients_count: clients.filter((c) => c.execution_mode === 'PAPER').length,
    live_clients_count: clients.filter((c) => c.execution_mode === 'LIVE').length,
    open_positions_total: clients.reduce((acc, c) => acc + (c.open_positions_count || 0), 0),
  };

  const isNetProfit = aggregate.aggregate_net_mtm >= 0;

  return (
    <div className="flex-1 overflow-y-auto p-4 md:p-6 space-y-6">
      {/* Top Aggregate KPI Metrics Row */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        {/* Net MTM Card */}
        <div className="bg-cardbg border border-bordercolor rounded-2xl p-4 shadow-sm relative overflow-hidden">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">PORTFOLIO NET MTM</span>
            {isNetProfit ? (
              <TrendingUp className="w-4 h-4 text-emerald-400" />
            ) : (
              <TrendingDown className="w-4 h-4 text-rose-400" />
            )}
          </div>
          <div className={`mt-2 font-mono text-2xl md:text-3xl font-bold tracking-tight ${isNetProfit ? 'text-emerald-400' : 'text-rose-400'}`}>
            {isNetProfit ? '+' : ''}{formatINR(aggregate.aggregate_net_mtm)}
          </div>
          <div className="mt-2 flex items-center space-x-3 text-[11px] font-mono text-slate-400">
            <span>Realized: <strong className={aggregate.aggregate_realized_pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}>{formatINR(aggregate.aggregate_realized_pnl)}</strong></span>
            <span>•</span>
            <span>Unrealized: <strong className={aggregate.aggregate_unrealized_mtm >= 0 ? 'text-emerald-400' : 'text-rose-400'}>{formatINR(aggregate.aggregate_unrealized_mtm)}</strong></span>
          </div>
        </div>

        {/* Client Accounts Card */}
        <div className="bg-cardbg border border-bordercolor rounded-2xl p-4 shadow-sm">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">CLIENT ACCOUNTS</span>
            <Activity className="w-4 h-4 text-brand-400" />
          </div>
          <div className="mt-2 font-mono text-2xl md:text-3xl font-bold text-slate-100">
            {aggregate.total_clients_count}
          </div>
          <div className="mt-2 flex items-center space-x-3 text-[11px] font-mono text-slate-400">
            <span>Live: <strong className="text-emerald-400">{aggregate.live_clients_count}</strong></span>
            <span>•</span>
            <span>Paper: <strong className="text-amber-400">{aggregate.paper_clients_count}</strong></span>
            <span>•</span>
            <span>Active: <strong className="text-slate-200">{aggregate.active_clients_count}</strong></span>
          </div>
        </div>

        {/* Active Open Positions Card */}
        <div className="bg-cardbg border border-bordercolor rounded-2xl p-4 shadow-sm">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">ACTIVE POSITIONS</span>
            <Layers className="w-4 h-4 text-sky-400" />
          </div>
          <div className="mt-2 font-mono text-2xl md:text-3xl font-bold text-slate-100">
            {aggregate.open_positions_total}
          </div>
          <div className="mt-2 text-[11px] text-slate-400">
            Open market contracts across all clients
          </div>
        </div>

        {/* Quick Add Client & Shortcuts */}
        <div className="bg-cardbg border border-bordercolor rounded-2xl p-4 shadow-sm flex flex-col justify-between">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">MANAGEMENT</span>
            <Wallet className="w-4 h-4 text-slate-400" />
          </div>
          <div className="flex items-center space-x-2 pt-2">
            <button
              type="button"
              onClick={onOpenAddClient}
              className="flex-1 py-2 px-3 bg-brand-600 hover:bg-brand-500 active:bg-brand-700 text-white rounded-xl text-xs font-semibold flex items-center justify-center space-x-1.5 transition shadow-sm"
            >
              <Plus className="w-4 h-4" />
              <span>Add Client</span>
            </button>
            <button
              type="button"
              onClick={onRefresh}
              disabled={isLoading}
              title="Refresh Telemetry"
              className="p-2 text-slate-400 hover:text-slate-200 hover:bg-slate-800 rounded-xl border border-bordercolor transition"
            >
              <RotateCw className={`w-4 h-4 ${isLoading ? 'animate-spin text-brand-400' : ''}`} />
            </button>
          </div>
        </div>
      </div>

      {/* Filter and Search Bar */}
      <div className="flex flex-col sm:flex-row items-stretch sm:items-center justify-between gap-3 bg-cardbg/70 border border-bordercolor p-3 rounded-2xl">
        {/* Search */}
        <div className="relative flex-1 max-w-md">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search clients by name, tenant ID, or broker ID..."
            className="w-full pl-9 pr-4 py-2 rounded-xl bg-obsidian border border-bordercolor text-xs text-slate-100 placeholder-slate-500 focus:outline-none focus:border-brand-500 transition"
          />
        </div>

        {/* Filter Pills */}
        <div className="flex items-center space-x-1 overflow-x-auto no-scrollbar py-0.5 text-xs">
          {(['all', 'live', 'paper', 'profit', 'loss', 'paused'] as const).map((filter) => (
            <button
              key={filter}
              type="button"
              onClick={() => setActiveFilter(filter)}
              className={`px-3 py-1.5 rounded-xl font-medium uppercase tracking-wider text-[11px] transition ${
                activeFilter === filter
                  ? 'bg-brand-600 text-white shadow-sm'
                  : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/80'
              }`}
            >
              {filter}
            </button>
          ))}
        </div>

        {/* View Toggle */}
        <div className="flex items-center space-x-1 border-l border-bordercolor pl-2 shrink-0">
          <button
            type="button"
            onClick={() => setViewMode('cards')}
            title="Card Matrix View"
            className={`p-1.5 rounded-lg transition ${
              viewMode === 'cards' ? 'bg-slate-800 text-brand-400' : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            <Grid className="w-4 h-4" />
          </button>
          <button
            type="button"
            onClick={() => setViewMode('table')}
            title="Dense Table View"
            className={`p-1.5 rounded-lg transition ${
              viewMode === 'table' ? 'bg-slate-800 text-brand-400' : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            <List className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* Clients View */}
      {viewMode === 'cards' ? (
        /* Cards Matrix */
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {filteredClients.length === 0 ? (
            <div className="col-span-full py-12 text-center text-slate-500 text-xs italic">
              No trading clients match your criteria.
            </div>
          ) : (
            filteredClients.map((client) => {
              const isProfit = client.net_mtm >= 0;
              const isPaused = client.trading_paused || client.status === 'PAUSED';
              const isLoadingThis = actionLoadingId === client.id;

              return (
                <div
                  key={client.id}
                  className="bg-cardbg border border-bordercolor hover:border-slate-700 rounded-2xl p-4 transition-all duration-150 flex flex-col justify-between space-y-4 hover:shadow-lg"
                >
                  {/* Card Header */}
                  <div className="flex items-start justify-between">
                    <div>
                      <div className="flex items-center space-x-2">
                        <span className="font-bold text-sm text-slate-100">{client.name || client.id}</span>
                        <span className="text-[10px] font-mono px-1.5 py-0.2 rounded bg-slate-800 text-slate-400 border border-slate-700">
                          {client.id}
                        </span>
                      </div>
                      <div className="text-[11px] font-mono text-slate-400 mt-0.5">
                        Broker ID: <span className="text-slate-300">{client.broker_client_id || 'N/A'}</span>
                      </div>
                    </div>

                    <div className="flex items-center space-x-1.5">
                      {/* Mode Badge */}
                      <span
                        className={`text-[10px] font-mono font-bold px-2 py-0.5 rounded-full uppercase ${
                          client.execution_mode === 'LIVE'
                            ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20'
                            : 'bg-amber-500/10 text-amber-400 border border-amber-500/20'
                        }`}
                      >
                        {client.execution_mode || 'LIVE'}
                      </span>

                      {/* Status Dot */}
                      <span
                        title={`Status: ${client.status}`}
                        className={`w-2.5 h-2.5 rounded-full ${
                          client.status === 'ACTIVE'
                            ? 'bg-emerald-400 animate-pulse'
                            : isPaused
                            ? 'bg-amber-400'
                            : 'bg-rose-500'
                        }`}
                      />
                    </div>
                  </div>

                  {/* Card MTM & Positions */}
                  <div className="bg-obsidian/70 rounded-xl p-3 border border-bordercolor/60 flex items-center justify-between">
                    <div>
                      <div className="text-[10px] font-semibold text-slate-400 uppercase tracking-wider">NET MTM</div>
                      <div className={`font-mono text-base font-bold ${isProfit ? 'text-emerald-400' : 'text-rose-400'}`}>
                        {isProfit ? '+' : ''}{formatINR(client.net_mtm)}
                      </div>
                    </div>
                    <div className="text-right">
                      <div className="text-[10px] font-semibold text-slate-400 uppercase tracking-wider">POSITIONS</div>
                      <div className="font-mono text-sm font-semibold text-slate-200">
                        {client.open_positions_count || 0} Open
                      </div>
                    </div>
                  </div>

                  {/* Margin & Strategies Meta */}
                  <div className="space-y-1.5 text-[11px] font-mono text-slate-400">
                    <div className="flex justify-between">
                      <span>Available Margin:</span>
                      <span className="text-slate-200 font-semibold">{formatINR(client.available_margin)}</span>
                    </div>
                    <div className="flex justify-between">
                      <span>Margin Used:</span>
                      <span className="text-slate-300">{formatINR(client.margin_used)}</span>
                    </div>
                    <div className="flex justify-between">
                      <span>Active Strategies:</span>
                      <span className="text-brand-400 font-bold">{client.active_strategies_count || 0}</span>
                    </div>
                  </div>

                  {/* Card Actions Footer */}
                  <div className="pt-2 border-t border-bordercolor flex items-center justify-between gap-2">
                    <button
                      type="button"
                      onClick={() => onOpenClientTab(client.id, client.name || client.id)}
                      className="flex-1 py-1.5 px-3 bg-slate-800 hover:bg-slate-750 text-slate-200 rounded-xl text-xs font-semibold flex items-center justify-center space-x-1.5 transition"
                    >
                      <ExternalLink className="w-3.5 h-3.5" />
                      <span>Workspace</span>
                    </button>

                    <button
                      type="button"
                      onClick={() => handleTogglePause(client)}
                      disabled={isLoadingThis}
                      title={isPaused ? 'Resume Trading' : 'Pause Trading'}
                      className={`p-1.5 rounded-xl border transition ${
                        isPaused
                          ? 'bg-amber-500/20 text-amber-300 border-amber-500/40 hover:bg-amber-500/30'
                          : 'bg-slate-800 text-slate-300 border-slate-700 hover:bg-slate-700'
                      }`}
                    >
                      {isPaused ? <Play className="w-4 h-4 fill-current" /> : <Pause className="w-4 h-4" />}
                    </button>

                    <button
                      type="button"
                      onClick={() => handlePanic(client)}
                      disabled={isLoadingThis}
                      title="Panic Square Off Client"
                      className="p-1.5 rounded-xl bg-rose-600/10 hover:bg-rose-600 text-rose-400 hover:text-white border border-rose-500/30 transition"
                    >
                      <Flame className="w-4 h-4" />
                    </button>
                  </div>
                </div>
              );
            })
          )}
        </div>
      ) : (
        /* Dense Table View */
        <div className="bg-cardbg border border-bordercolor rounded-2xl overflow-hidden shadow-sm">
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs font-mono">
              <thead className="bg-slate-950/80 text-slate-400 text-[11px] uppercase tracking-wider border-b border-bordercolor select-none">
                <tr>
                  <th className="p-3">Client</th>
                  <th className="p-3">Status</th>
                  <th className="p-3">Mode</th>
                  <th className="p-3 text-right">Net MTM</th>
                  <th className="p-3 text-right">Avail Margin</th>
                  <th className="p-3 text-center">Positions</th>
                  <th className="p-3 text-center">Strategies</th>
                  <th className="p-3 text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-bordercolor/50 text-slate-200">
                {filteredClients.map((client) => {
                  const isProfit = client.net_mtm >= 0;
                  const isPaused = client.trading_paused || client.status === 'PAUSED';

                  return (
                    <tr key={client.id} className="hover:bg-slate-800/40 transition">
                      <td className="p-3 font-semibold">
                        <div className="text-slate-100">{client.name || client.id}</div>
                        <div className="text-[10px] text-slate-500">{client.id}</div>
                      </td>
                      <td className="p-3">
                        <span
                          className={`inline-flex items-center space-x-1 px-2 py-0.5 rounded text-[10px] font-bold uppercase ${
                            client.status === 'ACTIVE'
                              ? 'bg-emerald-500/10 text-emerald-400'
                              : isPaused
                              ? 'bg-amber-500/10 text-amber-400'
                              : 'bg-rose-500/10 text-rose-400'
                          }`}
                        >
                          <span className={`w-1.5 h-1.5 rounded-full ${client.status === 'ACTIVE' ? 'bg-emerald-400' : 'bg-slate-400'}`} />
                          <span>{client.status}</span>
                        </span>
                      </td>
                      <td className="p-3">
                        <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-800 text-slate-300">
                          {client.execution_mode || 'LIVE'}
                        </span>
                      </td>
                      <td className={`p-3 text-right font-bold ${isProfit ? 'text-emerald-400' : 'text-rose-400'}`}>
                        {isProfit ? '+' : ''}{formatINR(client.net_mtm)}
                      </td>
                      <td className="p-3 text-right text-slate-300">{formatINR(client.available_margin)}</td>
                      <td className="p-3 text-center">{client.open_positions_count || 0}</td>
                      <td className="p-3 text-center text-brand-400">{client.active_strategies_count || 0}</td>
                      <td className="p-3 text-right space-x-2">
                        <button
                          type="button"
                          onClick={() => onOpenClientTab(client.id, client.name || client.id)}
                          className="px-2.5 py-1 bg-slate-800 hover:bg-slate-700 text-slate-200 rounded-lg text-[11px] transition"
                        >
                          Open
                        </button>
                        <button
                          type="button"
                          onClick={() => handlePanic(client)}
                          className="p-1 text-rose-400 hover:text-rose-300 hover:bg-rose-500/10 rounded-lg transition"
                          title="Panic Square Off"
                        >
                          <Flame className="w-3.5 h-3.5 inline" />
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
};
